"""In-memory cache for audio transcode streams.

``QUALITIES`` declares the streaming formats.  AAC 128 kbps is the first one;
additional formats (MP3 and others) can be added with their ffmpeg codec and
container settings.  The LRU cache is keyed by song, quality, source path,
mtime, and size, so entries for different qualities share one byte budget.
Set ``transcode_cache_mb`` in ``config.json`` (or
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

QUALITIES = (
    {
        "id": "aac128",
        "label": "AAC",
        "detail": "128 kbps",
        "bitrate": 128,
        "mime": "audio/mp4",
        "extension": ".m4a",
        "codec": "aac",
        "profile": "aac_low",
        "sample_rate": 44100,
        "channels": 2,
        "container": "mp4",
        "movflags": "+faststart",
    },
)

DEFAULT_QUALITY = QUALITIES[0]["id"]
QUALITY_BY_ID = {quality["id"]: quality for quality in QUALITIES}


class TranscodeError(RuntimeError):
    pass


class TranscodeCache:
    """Thread-safe, size-limited LRU cache of encoded audio bytes."""

    def __init__(self, max_bytes=DEFAULT_CACHE_MB * 1024 * 1024,
                 ffmpeg=None, encode_workers=2):
        self.max_bytes = max(0, int(max_bytes))
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg") or "ffmpeg"
        self._cache = OrderedDict()
        self._keys = {}
        self._lock = threading.RLock()
        self._used_bytes = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
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
                for quality in QUALITIES
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
                "qualities": list(by_quality.values()),
            }

    def get_or_encode(self, song_id, source_path, quality=DEFAULT_QUALITY):
        if quality not in QUALITY_BY_ID:
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
                self._cache.move_to_end(key)
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
                    self._cache.move_to_end(key)
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
                    self._cache[key] = entry
                    self._used_bytes += len(data)
                    self._cache.move_to_end(key)
                    self._evict_locked()
                return entry

    # ------------------------------------------------------------------
    # Internals

    def _evict_locked(self):
        while self._used_bytes > self.max_bytes and self._cache:
            _key, (data, _etag) = self._cache.popitem(last=False)
            self._used_bytes -= len(data)
            self._evictions += 1

    def _encode(self, source_path, quality):
        spec = QUALITY_BY_ID[quality]
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
                "-map", "0:a:0",
                "-vn", "-sn", "-dn",
                "-map_metadata", "-1",
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
