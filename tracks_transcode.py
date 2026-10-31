"""In-memory cache for audio transcode streams.

``build_quality_specs`` turns the ``audio_formats`` configuration entries into
streaming formats.  The default is AAC 128 kbps; additional AAC bitrates and a
``raw`` entry for the original file can be configured.  The cache is keyed by
song, quality, source path, mtime, and size, so entries for different qualities
share one byte budget.  Entries are kept for the lifetime of the process until
a new item would exceed the budget; at that point the cache is cleared and the
new item is retained.  Set ``transcode_cache_mb`` in ``config.yaml`` (or
``WEBTRACKS_TRANSCODE_CACHE_MB``) to change the budget; 0 disables retention
while still allowing on-demand encoding.
"""

from collections import OrderedDict
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading

DEFAULT_CACHE_MB = 512

DEFAULT_AUDIO_FORMATS = (
    {"index": 0, "format": "aac", "bitrate": 128},
)

AUDIO_FORMAT_LABELS = {
    "aac": "AAC",
    "raw": "Raw",
}


def build_quality_specs(formats=None):
    """Build streaming quality dictionaries from configuration entries.

    Each entry has an ``index``, a ``format``, and (for AAC) a ``bitrate``.
    ``raw`` means the original file and is never transcoded.
    """

    entries = list(DEFAULT_AUDIO_FORMATS if formats is None else formats)
    if not entries:
        raise ValueError("audio_formats must contain at least one entry")
    specs = []
    seen_indexes = set()
    seen_ids = set()
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(
                "audio_formats[%d] must be an object" % position
            )
        fmt = str(entry.get("format") or "").strip().lower()
        if fmt not in AUDIO_FORMAT_LABELS:
            raise ValueError(
                "audio_formats[%d].format must be aac or raw" % position
            )
        try:
            index = int(entry.get("index", position))
        except (TypeError, ValueError):
            raise ValueError(
                "audio_formats[%d].index must be an integer" % position
            )
        if index < 0:
            raise ValueError(
                "audio_formats[%d].index must be non-negative" % position
            )
        if index in seen_indexes:
            raise ValueError("duplicate audio format index %d" % index)
        seen_indexes.add(index)
        if fmt == "raw":
            spec = {
                "id": "raw",
                "index": index,
                "format": "raw",
                "label": AUDIO_FORMAT_LABELS["raw"],
                "detail": "Original",
                "bitrate": None,
                "mime": None,
                "raw": True,
            }
        else:
            try:
                bitrate = int(entry.get("bitrate", 128))
            except (TypeError, ValueError):
                raise ValueError(
                    "audio_formats[%d].bitrate must be an integer"
                    % position
                )
            if not 8 <= bitrate <= 512:
                raise ValueError(
                    "audio_formats[%d].bitrate must be between 8 and 512"
                    % position
                )
            spec = {
                "id": "%s%d" % (fmt, bitrate),
                "index": index,
                "format": fmt,
                "label": AUDIO_FORMAT_LABELS[fmt],
                "detail": "%d kbps" % bitrate,
                "bitrate": bitrate,
                "mime": "audio/mp4",
                "extension": ".m4a",
                "codec": "aac",
                "profile": "aac_low",
                "sample_rate": 44100,
                "channels": 2,
                "container": "mp4",
                "movflags": "+faststart",
                "raw": False,
            }
        if spec["id"] in seen_ids:
            raise ValueError("duplicate audio quality %s" % spec["id"])
        seen_ids.add(spec["id"])
        specs.append(spec)
    return tuple(specs)


QUALITIES = build_quality_specs(DEFAULT_AUDIO_FORMATS)
DEFAULT_QUALITY = QUALITIES[0]["id"]
QUALITY_BY_ID = {quality["id"]: quality for quality in QUALITIES}


class TranscodeError(RuntimeError):
    pass


class TranscodeCache:
    """Thread-safe, size-limited process-lifetime cache of audio bytes."""

    def __init__(self, max_bytes=DEFAULT_CACHE_MB * 1024 * 1024,
                 ffmpeg=None, encode_workers=2, qualities=None):
        self.max_bytes = max(0, int(max_bytes))
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg") or "ffmpeg"
        specs = QUALITIES if qualities is None else tuple(qualities)
        self.qualities = tuple(
            spec for spec in specs if not spec.get("raw")
        )
        self.quality_by_id = {
            spec["id"]: spec for spec in self.qualities
        }
        self._cache = OrderedDict()
        self._keys = {}
        self._lock = threading.RLock()
        self._used_bytes = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._clears = 0
        self._encode_gate = threading.Semaphore(max(1, int(encode_workers)))

    # ------------------------------------------------------------------
    # Public API

    def stats(self):
        with self._lock:
            by_quality = {
                quality["id"]: {
                    "id": quality["id"],
                    "label": quality["label"],
                    "detail": quality["detail"],
                    "bitrate": quality["bitrate"],
                    "entries": 0,
                    "usedBytes": 0,
                }
                for quality in self.qualities
            }
            for key, (data, _etag) in self._cache.items():
                bucket = by_quality.get(key[1])
                if bucket is not None:
                    bucket["entries"] += 1
                    bucket["usedBytes"] += len(data)
            return {
                "mode": "memory",
                "maxBytes": self.max_bytes,
                "usedBytes": self._used_bytes,
                "entries": len(self._cache),
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "clears": self._clears,
                "qualities": list(by_quality.values()),
            }

    def get_or_encode(self, song_id, source_path, quality=None):
        if quality is None:
            if not self.qualities:
                raise ValueError("No transcoding formats are configured")
            quality = self.qualities[0]["id"]
        if quality not in self.quality_by_id:
            raise ValueError("Unsupported quality: %s" % quality)
        try:
            stat = os.stat(source_path)
        except OSError as exc:
            raise FileNotFoundError(source_path) from exc
        key = (
            song_id,
            quality,
            os.path.abspath(source_path),
            stat.st_mtime_ns,
            stat.st_size,
        )
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                self._hits += 1
                return entry
            key_lock = self._keys.get(key)
            if key_lock is None:
                key_lock = threading.Lock()
                self._keys[key] = key_lock

        # One encoder per track at a time.  Other requests wait and then read
        # the finished cache entry instead of spawning duplicate ffmpeg jobs.
        with key_lock:
            with self._lock:
                entry = self._cache.get(key)
                if entry is not None:
                    self._hits += 1
                    return entry
                self._misses += 1
            with self._encode_gate:
                data = self._encode(source_path, quality)
            etag = '"%s"' % hashlib.sha1(
                (
                    "%s|%s|%s|%s|%s"
                    % key
                ).encode("utf-8")
            ).hexdigest()
            entry = (data, etag)
            with self._lock:
                if self.max_bytes > 0 and len(data) <= self.max_bytes:
                    if self._used_bytes + len(data) > self.max_bytes:
                        self._clear_locked()
                    self._cache[key] = entry
                    self._used_bytes += len(data)
                return entry

    # ------------------------------------------------------------------
    # Internals

    def _clear_locked(self):
        if not self._cache:
            return
        self._evictions += len(self._cache)
        self._clears += 1
        self._cache.clear()
        self._used_bytes = 0

    def _encode(self, source_path, quality):
        spec = self.quality_by_id[quality]
        if not self.ffmpeg or not (
            os.path.isabs(self.ffmpeg) and os.path.exists(self.ffmpeg)
            or shutil.which(self.ffmpeg)
        ):
            raise TranscodeError(
                "ffmpeg is required for transcoded streaming"
            )
        file_descriptor, temporary = tempfile.mkstemp(
            prefix="tracks-transcode-",
            suffix=spec.get("extension") or ".audio",
        )
        os.close(file_descriptor)
        try:
            command = [
                self.ffmpeg,
                "-nostdin", "-v", "error", "-y",
                "-i", source_path,
                # Audio only: drop artwork/video, subtitles, data streams,
                # chapters, and all source tags.  Bitexact flags also stop the
                # muxer from writing an encoder/version tag.
                "-map", "0:a:0",
                "-vn", "-sn", "-dn",
                "-map_metadata", "-1",
                "-map_chapters", "-1",
                "-fflags", "+bitexact",
                "-flags:a", "+bitexact",
                "-c:a", spec["codec"],
            ]
            if spec.get("profile"):
                command.extend(["-profile:a", spec["profile"]])
            command.extend(["-b:a", "%dk" % spec["bitrate"]])
            if spec.get("sample_rate"):
                command.extend(["-ar", str(spec["sample_rate"])])
            if spec.get("channels"):
                command.extend(["-ac", str(spec["channels"])])
            if spec.get("movflags"):
                command.extend(["-movflags", spec["movflags"]])
            command.extend(["-f", spec["container"], temporary])
            try:
                process = subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=600,
                )
            except subprocess.TimeoutExpired as exc:
                raise TranscodeError(
                    "ffmpeg timed out while encoding the track"
                ) from exc
            if process.returncode != 0:
                message = process.stderr.decode(
                    "utf-8", "replace"
                ).strip().splitlines()
                raise TranscodeError(
                    message[-1] if message else "ffmpeg could not encode the track"
                )
            with open(temporary, "rb") as handle:
                data = handle.read()
            if not data:
                raise TranscodeError("ffmpeg produced an empty file")
            return data
        finally:
            try:
                os.remove(temporary)
            except OSError:
                pass
