"""Library scanner and artwork cache generator for Tracks-compatible databases.

For FLAC files this is a direct port of the metadata reader used by the macOS
app (STREAMINFO duration, Vorbis comments, PICTURE block).  Other formats are
read with ``ffprobe`` and their embedded cover is extracted with ``ffmpeg``;
the resulting database rows use exactly the same stable IDs, path layout and
artwork cache naming as Tracks.

Artwork cache files are ``<ALBUM-UUID>.jpg``, at most 500x500 pixels, JPEG
quality 80 by default.  Album cover extraction is attempted once per album,
using the first track in scan order that carries a picture.
"""

from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from tracks_identity import (
    album_uuid,
    artist_uuid,
    canonical_path,
    is_under,
    swift_lower,
)

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover - surfaced as a scan warning
    Image = None
    ImageOps = None


SUPPORTED_EXTENSIONS = {
    "flac", "mp3", "m4a", "wav", "aiff", "aif", "aifc", "ogg", "oga", "opus",
}

ARTWORK_MAX_PIXEL = 500
DEFAULT_JPEG_QUALITY = 80
READ_WORKERS = 8
ARTWORK_WORKERS = 6


@dataclass
class ScannedTrack:
    path: str
    title: str
    artist: str
    album_artist: str
    album: str
    genre: str
    year: str
    track: int
    track_total: int
    disc: int
    disc_total: int
    comments: str
    duration: float
    has_artwork: bool
    mtime: float
    file_size: int


def _clean(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _digits(value):
    return int("".join(ch for ch in str(value) if ch.isdigit()) or "0")


def _parse_pair(value):
    """Parse ``3/12`` into (3, 12) the way Tracks parses tag pairs."""
    text = _clean(value)
    if not text:
        return 0, 0
    parts = text.split("/")
    number = _digits(parts[0])
    total = _digits(parts[1]) if len(parts) > 1 and parts[1] else 0
    return number, total


def _stat(path):
    try:
        stat = os.stat(path)
        return float(stat.st_mtime), int(stat.st_size)
    except OSError:
        return 0.0, 0


# ---------------------------------------------------------------------------
# FLAC (exact algorithm used by FLACMetadataReader)


def _flac_blocks(path):
    with open(path, "rb") as handle:
        if handle.read(4) != b"fLaC":
            return
        while True:
            header = handle.read(4)
            if len(header) != 4:
                break
            is_last = bool(header[0] & 0x80)
            block_type = header[0] & 0x7F
            length = (header[1] << 16) | (header[2] << 8) | header[3]
            if length < 0 or length >= 64 * 1024 * 1024:
                break
            yield block_type, length, handle
            if is_last:
                break


def _flac_comments(data):
    tags = {}
    offset = 0

    def read_u32():
        nonlocal offset
        if offset + 4 > len(data):
            return None
        value = (
            data[offset]
            | (data[offset + 1] << 8)
            | (data[offset + 2] << 16)
            | (data[offset + 3] << 24)
        )
        offset += 4
        return value

    def read_string(length):
        nonlocal offset
        if length < 0 or offset + length > len(data):
            return None
        value = data[offset:offset + length].decode("utf-8", "replace")
        offset += length
        return value

    vendor_length = read_u32()
    if vendor_length is None or read_string(vendor_length) is None:
        return tags
    count = read_u32()
    if count is None:
        return tags
    for _ in range(count):
        length = read_u32()
        if length is None:
            break
        entry = read_string(length)
        if entry is None:
            break
        if "=" in entry:
            key, value = entry.split("=", 1)
            tags[key.upper()] = value
    return tags


def _flac_picture(path):
    for block_type, length, handle in _flac_blocks(path):
        if block_type != 6:
            handle.seek(length, os.SEEK_CUR)
            continue
        data = handle.read(length)
        if len(data) != length:
            return None
        try:
            offset = 0

            def read_u32():
                nonlocal offset
                if offset + 4 > len(data):
                    raise ValueError
                value = (
                    (data[offset] << 24)
                    | (data[offset + 1] << 16)
                    | (data[offset + 2] << 8)
                    | data[offset + 3]
                )
                offset += 4
                return value

            read_u32()  # picture type
            mime_length = read_u32()
            offset += mime_length
            description_length = read_u32()
            offset += description_length
            offset += 16  # width, height, depth, colors
            data_length = read_u32()
            if data_length <= 0 or offset + data_length > len(data):
                return None
            return data[offset:offset + data_length]
        except (ValueError, IndexError):
            return None
    return None


def _read_flac(path):
    tags = {}
    sample_rate = 44100.0
    total_samples = 0
    has_artwork = False
    for block_type, length, handle in _flac_blocks(path):
        if block_type == 0:
            block = handle.read(length)
            if len(block) >= 18:
                rate = ((block[10] << 12) | (block[11] << 4)
                        | (block[12] >> 4))
                if rate > 0:
                    sample_rate = float(rate)
                total_samples = (
                    ((block[13] & 0x0F) << 32)
                    | (block[14] << 24)
                    | (block[15] << 16)
                    | (block[16] << 8)
                    | block[17]
                )
        elif block_type == 4:
            block = handle.read(length)
            if len(block) == length:
                tags.update(_flac_comments(block))
        elif block_type == 6:
            has_artwork = True
            handle.seek(length, os.SEEK_CUR)
        else:
            handle.seek(length, os.SEEK_CUR)

    def tag(key):
        value = _clean(tags.get(key))
        return value

    def int_tag(key):
        value = tag(key)
        if value is None:
            return None
        digits = "".join(
            ch for ch in value.split("/")[0] if ch.isdigit()
        )
        # Swift's Int(...) returns nil for an empty digit string, while an
        # explicit tag such as "0" is a real zero.
        return int(digits) if digits else None

    def parse_pair(value):
        text = _clean(value)
        if not text:
            return 0, 0
        parts = text.split("/", 1)
        number = _digits(parts[0])
        total = _digits(parts[1]) if len(parts) > 1 else 0
        return number, total

    title = tags.get("TITLE", Path(path).stem)
    artist = tag("ARTIST") or tag("ALBUMARTIST") or "Unknown Artist"
    album_artist = (
        tag("ALBUMARTIST")
        or tag("ALBUM ARTIST")
        or tag("ALBUM_ARTIST")
        or tag("ENSEMBLE")
        or tag("BAND")
        or artist
    )
    album = tags.get("ALBUM", "Unknown Album")
    genre = tags.get("GENRE", "Unknown")
    raw_year = tag("DATE") or tag("YEAR") or ""
    year = "" if raw_year == "0" else raw_year
    track, pair_total = parse_pair(tags.get("TRACKNUMBER"))
    track_total = int_tag("TRACKTOTAL")
    if track_total is None:
        track_total = int_tag("TOTALTRACKS")
    if track_total is None:
        track_total = pair_total
    disc, disc_pair_total = parse_pair(
        tags.get("DISCNUMBER") or tags.get("DISC")
    )
    disc_total = int_tag("DISCTOTAL")
    if disc_total is None:
        disc_total = int_tag("TOTALDISCS")
    if disc_total is None:
        disc_total = disc_pair_total
    comments = tag("COMMENT") or tag("COMMENTS") or tag("DESCRIPTION") or ""
    duration = (
        float(total_samples) / sample_rate
        if sample_rate > 0 and total_samples > 0 else 0.0
    )
    mtime, file_size = _stat(path)
    return ScannedTrack(
        path=path,
        title=title,
        artist=artist,
        album_artist=album_artist,
        album=album,
        genre=genre,
        year=year,
        track=track,
        track_total=track_total,
        disc=disc,
        disc_total=disc_total,
        comments=comments,
        duration=duration,
        has_artwork=has_artwork,
        mtime=mtime,
        file_size=file_size,
    )


# ---------------------------------------------------------------------------
# ffprobe based reader for MP3/M4A/WAV/AIFF/OGG/Opus


def _compact_tag_key(key):
    key = str(key or "").strip().lower()
    return "".join(ch for ch in key if ch not in " _-")


def _ffprobe_tags(payload):
    tags = {}

    def add(values):
        if not isinstance(values, dict):
            return
        for key, value in values.items():
            value = _clean(value)
            if not value:
                continue
            compact = _compact_tag_key(key)
            tags.setdefault(compact, value)

    add(payload.get("format", {}).get("tags"))
    for stream in payload.get("streams", []):
        add(stream.get("tags"))
    return tags


def _read_ffprobe(path):
    if not shutil.which("ffprobe"):
        raise RuntimeError(
            "ffprobe is required to read non-FLAC metadata "
            "(install ffmpeg)"
        )
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    if proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(message or "ffprobe could not read the file")
    import json
    payload = json.loads(proc.stdout.decode("utf-8", "replace"))
    tags = _ffprobe_tags(payload)

    def tag(*names):
        for name in names:
            value = tags.get(_compact_tag_key(name))
            if value:
                return value
        return None

    title = tag("title", "TIT2", "TT2", "©NAM", "INAM", "NAME")
    artist = tag("artist", "TPE1", "TP1", "©ART", "IART", "AUTH")
    album_artist = tag(
        "album_artist", "albumartist", "album artist", "TPE2", "TP2",
        "AART", "IAAR", "ENSEMBLE", "BAND",
    )
    album = tag("album", "TALB", "TAL", "©ALB", "IPRD")
    genre = tag("genre", "TCON", "TCO", "©GEN", "GNRE", "IGNR")
    raw_year = tag(
        "date", "TDRC", "TYER", "TYE", "©DAY", "ICRD", "year", "originaldate",
    ) or ""
    year = "" if raw_year == "0" else raw_year
    track, pair_total = _parse_pair(
        tag("track", "TRCK", "TRK", "TRKN", "ITRK")
    )
    raw_track_total = tag("tracktotal", "TOTALTRACKS", "TRACKTOTAL")
    track_total = _digits(raw_track_total) if raw_track_total else pair_total
    disc, disc_pair_total = _parse_pair(
        tag("disc", "DISCNUMBER", "DISKNUMBER", "TPOS", "DISK")
    )
    raw_disc_total = tag("disctotal", "TOTALDISCS", "DISCTOTAL")
    disc_total = _digits(raw_disc_total) if raw_disc_total else disc_pair_total
    comments = (
        tag("comment", "comments", "COMM", "ICMT", "©CMT", "DESC",
            "description")
        or ""
    )

    duration = 0.0
    try:
        duration = float(payload.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        for stream in payload.get("streams", []):
            try:
                duration = max(duration, float(stream.get("duration") or 0))
            except (TypeError, ValueError):
                pass
    if duration <= 0:
        raise RuntimeError("could not determine track duration")

    has_artwork = False
    for stream in payload.get("streams", []):
        if (stream.get("disposition") or {}).get("attached_pic") == 1:
            has_artwork = True
            break
        if stream.get("codec_type") == "video":
            has_artwork = True
            break
    if not has_artwork:
        for key in tags:
            if "picture" in key or "artwork" in key or "coverart" in key:
                has_artwork = True
                break

    artist = artist or album_artist or "Unknown Artist"
    album_artist = album_artist or artist
    title = title or Path(path).stem
    album = album or "Unknown Album"
    genre = genre or "Unknown"
    mtime, file_size = _stat(path)
    return ScannedTrack(
        path=path,
        title=title,
        artist=artist,
        album_artist=album_artist,
        album=album,
        genre=genre,
        year=year,
        track=track,
        track_total=track_total,
        disc=disc,
        disc_total=disc_total,
        comments=comments,
        duration=duration,
        has_artwork=has_artwork,
        mtime=mtime,
        file_size=file_size,
    )


def read_metadata(path):
    path = canonical_path(path)
    if Path(path).suffix.lower() == ".flac":
        return _read_flac(path)
    return _read_ffprobe(path)


# ---------------------------------------------------------------------------
# Artwork extraction and cache writing


def extract_artwork(track):
    if not track.has_artwork:
        return None
    if Path(track.path).suffix.lower() == ".flac":
        return _flac_picture(track.path)
    if not shutil.which("ffmpeg"):
        return None
    base = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", track.path,
        "-map", "0:v:0", "-frames:v", "1",
    ]
    for codec in ("copy", "mjpeg"):
        try:
            proc = subprocess.run(
                base + ["-c:v", codec, "-f", "image2pipe", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    return None


def write_artwork_cache(data, album_id, cache_dir, jpeg_quality=DEFAULT_JPEG_QUALITY):
    if not data or Image is None:
        return False
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with Image.open(BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            ):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                image = background
            else:
                image = image.convert("RGB")
            image.thumbnail(
                (ARTWORK_MAX_PIXEL, ARTWORK_MAX_PIXEL), Image.LANCZOS
            )
            target = os.path.join(cache_dir, "%s.jpg" % album_id.upper())
            temporary = target + ".tmp"
            image.save(
                temporary, "JPEG",
                quality=max(15, min(100, int(jpeg_quality))),
                optimize=False,
            )
            os.replace(temporary, target)
        return True
    except Exception:
        return False


def album_id_for_track(track):
    return album_uuid(artist_uuid(track.album_artist), track.album)


# ---------------------------------------------------------------------------
# Scan pipeline


def enumerate_files(root, progress=None):
    root = canonical_path(root)
    if not os.path.isdir(root):
        raise ValueError("Media directory is missing: %s" % root)
    if not os.access(root, os.R_OK):
        raise ValueError("Media directory is unreadable: %s" % root)

    discovered = {}
    errors = []
    complete = True

    def on_error(error):
        nonlocal complete
        complete = False
        errors.append("Could not enumerate %s: %s" % (root, error))

    for dirpath, dirnames, filenames in os.walk(
        root, topdown=True, onerror=on_error, followlinks=False
    ):
        dirnames[:] = sorted(
            name for name in dirnames if not name.startswith(".")
        )
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if Path(name).suffix.lower().lstrip(".") not in SUPPORTED_EXTENSIONS:
                continue
            raw_path = os.path.join(dirpath, name)
            try:
                if not os.path.isfile(raw_path):
                    continue
                path = canonical_path(raw_path)
                if not os.path.isfile(path):
                    continue
                stat = os.stat(path)
            except OSError as exc:
                complete = False
                errors.append("Could not inspect %s: %s" % (raw_path, exc))
                continue
            discovered[path] = (float(stat.st_mtime), int(stat.st_size))
            if progress and len(discovered) % 250 == 0:
                progress(
                    "Finding Files", len(discovered), None,
                    "{:,} files".format(len(discovered)),
                )
    return discovered, errors, complete


def _pending_paths(discovered, known, mode, force):
    pending = []
    for path in sorted(discovered):
        mtime, size = discovered[path]
        existing = known.get(path)
        if existing is None or force:
            pending.append(path)
            continue
        if mode == "quick":
            continue
        old_mtime, old_size = existing
        if abs(old_mtime - mtime) >= 1 or old_size != size:
            pending.append(path)
    return pending


def _read_tracks(paths, progress=None):
    tracks = []
    errors = []
    if not paths:
        return tracks, errors
    if any(Path(path).suffix.lower() != ".flac" for path in paths):
        if not shutil.which("ffprobe"):
            raise RuntimeError(
                "ffprobe is required to read non-FLAC files; install ffmpeg"
            )
    results = [None] * len(paths)
    done = 0
    with ThreadPoolExecutor(max_workers=READ_WORKERS) as executor:
        futures = {
            executor.submit(read_metadata, path): index
            for index, path in enumerate(paths)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                results[index] = exc
            done += 1
            if progress and (done % 10 == 0 or done == len(paths)):
                progress(
                    "Reading Metadata", done, len(paths),
                    paths[index].rsplit("/", 1)[-1],
                )
    for path, result in zip(paths, results):
        if isinstance(result, Exception):
            errors.append("Could not read metadata: %s (%s)" % (path, result))
        elif result is None:
            errors.append("Could not read metadata: %s" % path)
        else:
            tracks.append(result)
    return tracks, errors


def _write_artwork(tracks, cache_dir, jpeg_quality, progress=None,
                   album_ids_by_key=None):
    groups = {}
    for track in tracks:
        if not track.has_artwork:
            continue
        key = (track.album_artist, swift_lower(track.album))
        groups.setdefault(key, []).append(track)
    if not groups:
        return 0, []

    written = 0
    errors = []
    done = 0
    total = len(groups)
    album_ids_by_key = album_ids_by_key or {}

    def process(item):
        key, group = item
        album_id = album_ids_by_key.get(key)
        for track in group:
            data = extract_artwork(track)
            target_id = album_id or album_id_for_track(track)
            if data and write_artwork_cache(
                data, target_id, cache_dir, jpeg_quality
            ):
                return True
        return False

    with ThreadPoolExecutor(max_workers=ARTWORK_WORKERS) as executor:
        futures = {
            executor.submit(process, item): item[0]
            for item in groups.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                ok = bool(future.result())
            except Exception as exc:
                ok = False
                errors.append(
                    "Artwork failed for %s / %s: %s" % (key[0], key[1], exc)
                )
            if ok:
                written += 1
            else:
                errors.append(
                    "Could not extract artwork for %s / %s" % (key[0], key[1])
                )
            done += 1
            if progress:
                progress(
                    "Writing Artwork", done, total, "%d covers" % written
                )
    return written, errors


def _remove_cached_artwork(album_ids, cache_dir):
    for album_id in album_ids:
        try:
            os.remove(os.path.join(cache_dir, "%s.jpg" % album_id.upper()))
        except OSError:
            pass


def scan_one(db, root, identifier=None, mode="quick", prune=False,
             artwork_dir=None, jpeg_quality=DEFAULT_JPEG_QUALITY,
             force=False, progress=None):
    """Create/update one library and its artwork cache.

    Returns a summary dictionary.  ``progress`` is called as
    ``(phase, completed, total, message)``.
    """

    def report(phase, completed=0, total=None, message=""):
        if progress:
            progress(phase, completed, total, message)

    mode = mode if mode in ("quick", "full") else "quick"
    db.ensure_schema()
    library = db.ensure_library(root, identifier)
    libraries = db.load_libraries()
    report("Finding Files", 0, None, canonical_path(root))

    discovered, errors, complete = enumerate_files(root, progress=report)
    known = db.known_files()
    pending = _pending_paths(discovered, known, mode, force)
    report(
        "Reading Metadata", 0, len(pending),
        "%d new or changed files" % len(pending),
    )
    tracks, read_errors = _read_tracks(pending, progress=report)
    errors.extend(read_errors)

    report("Updating Database", 0, len(tracks), "%d tracks" % len(tracks))
    album_ids_by_key = db.sync_tracks(tracks, libraries)
    report("Updating Database", len(tracks), len(tracks), "Database updated")

    cache_written = 0
    if artwork_dir:
        report("Writing Artwork", 0, None, "Preparing cover cache")
        cache_written, artwork_errors = _write_artwork(
            tracks, artwork_dir, jpeg_quality, progress=report,
            album_ids_by_key=album_ids_by_key,
        )
        errors.extend(artwork_errors)

    missing = 0
    if prune:
        if not complete:
            errors.append(
                "Skipped missing-file removal because the folder could not "
                "be enumerated completely."
            )
        else:
            report("Removing Missing Tracks", 0, None, "")
            # Only consider rows owned by the library being scanned.
            # Overlapping libraries must not delete each other's indexes.
            library_known = db.known_files(library_id=library["id"])
            missing_paths = [
                path for path in library_known
                if is_under(path, canonical_path(root))
            ]
            missing_paths = [
                path for path in missing_paths if path not in discovered
            ]
            removed, orphan_albums = db.prune_missing(missing_paths)
            missing = removed
            if artwork_dir and orphan_albums:
                _remove_cached_artwork(orphan_albums, artwork_dir)
            report(
                "Removing Missing Tracks", removed, removed,
                "%d tracks removed" % removed,
            )

    report("Done", len(tracks), len(tracks), "Scan complete")
    return {
        "root": canonical_path(root),
        "library": library,
        "discovered": len(discovered),
        "pending": len(pending),
        "tracks": len(tracks),
        "artwork": cache_written,
        "missing": missing,
        "errors": errors,
    }
