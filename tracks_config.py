"""Configuration for WebTracks.

The web player owns its own library.  By default the SQLite database and the
artwork cache are created inside the project so that nothing is shared with the
Tracks macOS app:

    <project>/data/library.sqlite
    <project>/data/Artwork/

Edit ``config.json`` (created on first run) to point ``database_dir`` at a
different location or to list the music folders scanned by default.
"""

import hashlib
import json
import os

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_DIR, "config.json")
DATABASE_FILENAME = "library.sqlite"
ARTWORK_DIRNAME = "Artwork"
ALBUM_SORT_KEYS = ("recentlyAdded", "album", "artist", "year")
DEFAULT_ADMIN_PASSWORD_SHA1 = hashlib.sha1(b"1").hexdigest()

DEFAULT_SETTINGS = {
    "database_dir": "data",
    "music_dirs": [],
    "host": "127.0.0.1",
    "port": 8765,
    "read_only": False,
    "jpeg_quality": 80,
    "transcode_cache_mb": 512,
    "album_sort": "recentlyAdded",
    "album_sort_descending": True,
    "admin_password_sha1": DEFAULT_ADMIN_PASSWORD_SHA1,
}

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off", ""}


def _expand(path, base):
    path = os.path.expanduser(str(path))
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    return os.path.abspath(path)


def _as_bool(value, fallback=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return fallback


def _as_port(value, fallback):
    try:
        port = int(value)
    except (TypeError, ValueError):
        return fallback
    return port if 0 < port < 65536 else fallback


def _as_quality(value, fallback):
    try:
        quality = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(15, min(100, quality))


def _as_megabytes(value, fallback):
    try:
        size = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(0, size)


def _read_config(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Could not parse %s: %s" % (path, exc)
        ) from exc
    if not isinstance(data, dict):
        raise ValueError("%s must contain a JSON object" % path)
    return data


def ensure_config_file(path=None):
    """Create the configuration file with defaults if it does not exist."""

    path = os.path.abspath(os.path.expanduser(path or DEFAULT_CONFIG_PATH))
    if os.path.exists(path):
        return path
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(DEFAULT_SETTINGS, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path


def load_settings(config_path=None):
    """Load ``config.json`` plus environment overrides.

    Paths in the file are relative to the configuration file's directory.
    ``WEBTRACKS_*`` environment variables take precedence over the file; the
    caller can still override the returned paths with command line flags.
    """

    config_path = config_path or os.environ.get(
        "WEBTRACKS_CONFIG", DEFAULT_CONFIG_PATH
    )
    config_path = os.path.abspath(os.path.expanduser(config_path))
    values = dict(DEFAULT_SETTINGS)
    values.update(_read_config(config_path))
    base = os.path.dirname(config_path) or PROJECT_DIR

    database_dir = _expand(
        values.get("database_dir") or DEFAULT_SETTINGS["database_dir"], base
    )

    raw_music_dirs = values.get("music_dirs") or []
    if isinstance(raw_music_dirs, str):
        raw_music_dirs = [raw_music_dirs]
    if not isinstance(raw_music_dirs, list):
        raise ValueError("%s: music_dirs must be a list" % config_path)
    music_dirs = [
        _expand(item, base) for item in raw_music_dirs if str(item).strip()
    ]

    database_dir = os.environ.get("WEBTRACKS_DATABASE_DIR", database_dir)
    database_dir = os.path.abspath(os.path.expanduser(database_dir))
    database = os.environ.get(
        "WEBTRACKS_DB", os.path.join(database_dir, DATABASE_FILENAME)
    )
    artwork = os.environ.get(
        "WEBTRACKS_ARTWORK", os.path.join(database_dir, ARTWORK_DIRNAME)
    )
    if "WEBTRACKS_READ_ONLY" in os.environ:
        read_only = _as_bool(os.environ["WEBTRACKS_READ_ONLY"])
    else:
        read_only = _as_bool(values.get("read_only"))
    host = os.environ.get("WEBTRACKS_HOST", values.get("host") or "127.0.0.1")
    port = _as_port(
        os.environ.get("WEBTRACKS_PORT", values.get("port")),
        DEFAULT_SETTINGS["port"],
    )
    jpeg_quality = _as_quality(
        os.environ.get("WEBTRACKS_JPEG_QUALITY", values.get("jpeg_quality")),
        DEFAULT_SETTINGS["jpeg_quality"],
    )
    cache_env = os.environ.get("WEBTRACKS_TRANSCODE_CACHE_MB")
    if cache_env is None:
        # Accept the former AAC-only name for existing local configs.
        cache_env = os.environ.get("WEBTRACKS_AAC_CACHE_MB")
    cache_value = values.get("transcode_cache_mb")
    if cache_value is None:
        cache_value = values.get("aac_cache_mb")
    transcode_cache_mb = _as_megabytes(
        cache_env if cache_env is not None else cache_value,
        DEFAULT_SETTINGS["transcode_cache_mb"],
    )
    album_sort = str(
        os.environ.get(
            "WEBTRACKS_ALBUM_SORT",
            values.get("album_sort") or DEFAULT_SETTINGS["album_sort"],
        )
    ).strip()
    if album_sort not in ALBUM_SORT_KEYS:
        album_sort = DEFAULT_SETTINGS["album_sort"]
    sort_direction = os.environ.get("WEBTRACKS_ALBUM_SORT_DESCENDING")
    if sort_direction is None:
        sort_direction = values.get("album_sort_descending")
    album_sort_descending = _as_bool(
        sort_direction,
        DEFAULT_SETTINGS["album_sort_descending"],
    )
    admin_password_sha1 = str(
        os.environ.get(
            "WEBTRACKS_ADMIN_PASSWORD_SHA1",
            values.get("admin_password_sha1")
            or DEFAULT_SETTINGS["admin_password_sha1"],
        )
    ).strip().lower()
    if len(admin_password_sha1) != 40 or any(
        char not in "0123456789abcdef" for char in admin_password_sha1
    ):
        raise ValueError(
            "admin_password_sha1 must be a 40-character SHA-1 hex digest"
        )

    return {
        "config_path": config_path,
        "base_dir": base,
        "database_dir": os.path.abspath(os.path.expanduser(database_dir)),
        "database": os.path.abspath(os.path.expanduser(database)),
        "artwork": os.path.abspath(os.path.expanduser(artwork)),
        "music_dirs": music_dirs,
        "host": str(host),
        "port": port,
        "read_only": read_only,
        "jpeg_quality": jpeg_quality,
        "transcode_cache_mb": transcode_cache_mb,
        "album_sort": album_sort,
        "album_sort_descending": album_sort_descending,
        "admin_password_sha1": admin_password_sha1,
    }
