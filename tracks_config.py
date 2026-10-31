"""Configuration for WebTracks.

The web player owns its own library.  By default the SQLite database and the
artwork cache are created inside the project so that nothing is shared with the
Tracks macOS app:

    <project>/data/library.sqlite
    <project>/data/Artwork/

Edit ``config.yaml`` (created on first run) to point ``database_dir`` at a
different location or to change server, logging, and Setup options.  Music
folders and their library IDs/namespaces live in ``library.sqlite``.
"""

import hashlib
import json
import os

import yaml

from tracks_transcode import DEFAULT_AUDIO_FORMATS, build_quality_specs

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_DIR, "config.yaml")
DATABASE_FILENAME = "library.sqlite"
ARTWORK_DIRNAME = "Artwork"
ALBUM_SORT_KEYS = ("recentlyAdded", "album", "artist", "year")
DEFAULT_ADMIN_PASSWORD_SHA1 = hashlib.sha1(b"1").hexdigest()

DEFAULT_SETTINGS = {
    "database_dir": "data",
    "host": "127.0.0.1",
    "port": 8765,
    "server_header": "",
    "read_only": False,
    "jpeg_quality": 80,
    "cover_size": 500,
    "transcode_cache_mb": 512,
    "audio_formats": [dict(item) for item in DEFAULT_AUDIO_FORMATS],
    "default_audio_format": 0,
    "album_sort": "recentlyAdded",
    "album_sort_descending": True,
    "admin_password_sha1": DEFAULT_ADMIN_PASSWORD_SHA1,
    "admin_bruteforce_limit": 5,
    "admin_bruteforce_window_seconds": 60,
    "admin_bruteforce_lock_seconds": 86400,
    "logs_dir": "logs",
    "log_level": "INFO",
    "log_format": "text",
    "rate_limit": 28,
    "url_id_format": "uuid",
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


def _as_cover_size(value, fallback):
    try:
        size = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(16, min(2000, size))


def _as_megabytes(value, fallback):
    try:
        size = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(0, size)


def _as_nonnegative_int(value, fallback):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(0, number)


def _as_audio_formats(value):
    if value is None:
        value = DEFAULT_AUDIO_FORMATS
    if not isinstance(value, (list, tuple)):
        raise ValueError("audio_formats must be a list")
    specs = build_quality_specs(value)
    return [
        {
            "index": spec["index"],
            "format": spec["format"],
            **(
                {"bitrate": spec["bitrate"]}
                if spec["format"] == "aac" else {}
            ),
        }
        for spec in specs
    ]


def _as_audio_format_index(value, formats, fallback):
    try:
        index = int(value)
    except (TypeError, ValueError):
        index = None
    available = {entry["index"] for entry in formats}
    if index is not None and index in available:
        return index
    if not formats:
        return 0
    if fallback in available:
        return fallback
    return formats[0]["index"]


def _read_config(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            if path.lower().endswith(".json"):
                data = json.load(handle)
            else:
                data = yaml.safe_load(handle)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ValueError(
            "Could not parse %s: %s" % (path, exc)
        ) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("%s must contain a mapping/object" % path)
    return data


def _write_config(path, values):
    values = dict(values)
    if isinstance(values.get("libraries"), list):
        # Library IDs and namespaces are database state, never config state.
        # Strip legacy fields whenever the configuration is written.
        serialized = []
        for item in values["libraries"]:
            if isinstance(item, str):
                raw_path = item
            elif isinstance(item, dict):
                raw_path = item.get("path") or item.get("root")
            else:
                raw_path = None
            if raw_path is not None:
                serialized.append({"path": raw_path})
        values["libraries"] = serialized
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        if path.lower().endswith(".json"):
            json.dump(values, handle, indent=2, ensure_ascii=False)
        else:
            yaml.safe_dump(
                values, handle, sort_keys=False, allow_unicode=True,
                default_flow_style=False,
            )
        handle.write("\n")
    os.replace(temporary, path)


def ensure_config_file(path=None):
    """Create the configuration file with defaults if it does not exist."""

    path = os.path.abspath(os.path.expanduser(path or DEFAULT_CONFIG_PATH))
    if os.path.exists(path):
        return path
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if path == os.path.abspath(DEFAULT_CONFIG_PATH):
        legacy = os.path.join(parent, "config.json")
        if os.path.exists(legacy):
            _write_config(path, _read_config(legacy))
            return path
    _write_config(path, DEFAULT_SETTINGS)
    return path


def update_config(config_path, **updates):
    """Atomically update top-level configuration values."""

    config_path = os.path.abspath(os.path.expanduser(config_path))
    values = _read_config(config_path)
    values.update(updates)
    _write_config(config_path, values)
    return config_path


def load_settings(config_path=None):
    """Load the YAML config plus environment overrides.

    Paths in the file are relative to the configuration file's directory.
    ``WEBTRACKS_*`` environment variables take precedence over the file; the
    caller can still override the returned paths with command line flags.
    """

    config_path = config_path or os.environ.get(
        "WEBTRACKS_CONFIG", DEFAULT_CONFIG_PATH
    )
    config_path = os.path.abspath(os.path.expanduser(config_path))
    raw_values = _read_config(config_path)
    values = dict(DEFAULT_SETTINGS)
    values.update(raw_values)
    base = os.path.dirname(config_path) or PROJECT_DIR

    database_dir = _expand(
        values.get("database_dir") or DEFAULT_SETTINGS["database_dir"], base
    )

    if "libraries" in raw_values:
        raw_libraries = raw_values.get("libraries") or []
    else:
        # Backward compatibility for configs that only had music_dirs.
        raw_music_dirs = raw_values.get("music_dirs") or []
        if isinstance(raw_music_dirs, str):
            raw_music_dirs = [raw_music_dirs]
        if not isinstance(raw_music_dirs, list):
            raise ValueError("%s: music_dirs must be a list" % config_path)
        raw_libraries = [
            {"path": path} for path in raw_music_dirs
            if str(path).strip()
        ]
    if not isinstance(raw_libraries, list):
        raise ValueError("%s: libraries must be a list" % config_path)

    libraries = []
    seen_paths = set()
    for index, item in enumerate(raw_libraries):
        if isinstance(item, str):
            raw_path = item
        elif isinstance(item, dict):
            # Library IDs and namespaces live in the database, as in Tracks.
            # Only the folder path belongs in the configuration file.
            raw_path = item.get("path") or item.get("root")
        else:
            raise ValueError(
                "%s: libraries[%d] must be a path or an object with a path"
                % (config_path, index)
            )
        if not str(raw_path or "").strip():
            raise ValueError(
                "%s: libraries[%d].path is required" % (config_path, index)
            )
        path = os.path.realpath(_expand(raw_path, base))
        if path in seen_paths:
            raise ValueError("%s: duplicate library path %s" % (
                config_path, path
            ))
        seen_paths.add(path)
        libraries.append({"path": path})

    music_dirs = [library["path"] for library in libraries]

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
    server_header = str(
        os.environ.get(
            "WEBTRACKS_SERVER_HEADER",
            values.get("server_header", DEFAULT_SETTINGS["server_header"]),
        ) or ""
    ).strip()
    if len(server_header) > 200 or any(
        ord(char) < 32 or ord(char) == 127 for char in server_header
    ):
        raise ValueError(
            "server_header must be at most 200 characters without "
            "control characters"
        )
    jpeg_quality = _as_quality(
        os.environ.get("WEBTRACKS_JPEG_QUALITY", values.get("jpeg_quality")),
        DEFAULT_SETTINGS["jpeg_quality"],
    )
    cover_size = _as_cover_size(
        os.environ.get("WEBTRACKS_COVER_SIZE", values.get("cover_size")),
        DEFAULT_SETTINGS["cover_size"],
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
    audio_formats = _as_audio_formats(
        values.get("audio_formats", DEFAULT_SETTINGS["audio_formats"])
    )
    default_audio_format = _as_audio_format_index(
        os.environ.get(
            "WEBTRACKS_DEFAULT_AUDIO_FORMAT",
            values.get(
                "default_audio_format",
                DEFAULT_SETTINGS["default_audio_format"],
            ),
        ),
        audio_formats,
        DEFAULT_SETTINGS["default_audio_format"],
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
    admin_bruteforce_limit = _as_nonnegative_int(
        os.environ.get(
            "WEBTRACKS_ADMIN_BRUTEFORCE_LIMIT",
            values.get(
                "admin_bruteforce_limit",
                DEFAULT_SETTINGS["admin_bruteforce_limit"],
            ),
        ),
        DEFAULT_SETTINGS["admin_bruteforce_limit"],
    )
    admin_bruteforce_window_seconds = max(1, _as_nonnegative_int(
        os.environ.get(
            "WEBTRACKS_ADMIN_BRUTEFORCE_WINDOW_SECONDS",
            values.get(
                "admin_bruteforce_window_seconds",
                DEFAULT_SETTINGS["admin_bruteforce_window_seconds"],
            ),
        ),
        DEFAULT_SETTINGS["admin_bruteforce_window_seconds"],
    ))
    admin_bruteforce_lock_seconds = max(1, _as_nonnegative_int(
        os.environ.get(
            "WEBTRACKS_ADMIN_BRUTEFORCE_LOCK_SECONDS",
            values.get(
                "admin_bruteforce_lock_seconds",
                DEFAULT_SETTINGS["admin_bruteforce_lock_seconds"],
            ),
        ),
        DEFAULT_SETTINGS["admin_bruteforce_lock_seconds"],
    ))
    logs_dir = os.path.realpath(_expand(
        os.environ.get(
            "WEBTRACKS_LOG_DIR",
            values.get("logs_dir") or DEFAULT_SETTINGS["logs_dir"],
        ),
        base,
    ))
    log_level = str(
        os.environ.get(
            "WEBTRACKS_LOG_LEVEL",
            values.get("log_level") or DEFAULT_SETTINGS["log_level"],
        )
    ).strip().upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        log_level = DEFAULT_SETTINGS["log_level"]
    log_format = str(
        os.environ.get(
            "WEBTRACKS_LOG_FORMAT",
            values.get("log_format") or DEFAULT_SETTINGS["log_format"],
        )
    ).strip().lower()
    if log_format not in ("text", "json"):
        log_format = DEFAULT_SETTINGS["log_format"]
    try:
        rate_limit = int(
            os.environ.get(
                "WEBTRACKS_RATE_LIMIT",
                values.get("rate_limit", DEFAULT_SETTINGS["rate_limit"]),
            )
        )
    except (TypeError, ValueError):
        rate_limit = DEFAULT_SETTINGS["rate_limit"]
    rate_limit = max(0, rate_limit)
    url_id_format = str(
        os.environ.get(
            "WEBTRACKS_URL_ID_FORMAT",
            values.get("url_id_format")
            or DEFAULT_SETTINGS["url_id_format"],
        )
    ).strip().lower()
    if url_id_format in ("base64", "base64-url", "base64_url"):
        url_id_format = "base64url"
    if url_id_format not in ("uuid", "base64url"):
        url_id_format = DEFAULT_SETTINGS["url_id_format"]

    return {
        "config_path": config_path,
        "base_dir": base,
        "database_dir": os.path.abspath(os.path.expanduser(database_dir)),
        "database": os.path.abspath(os.path.expanduser(database)),
        "artwork": os.path.abspath(os.path.expanduser(artwork)),
        "libraries": libraries,
        "music_dirs": music_dirs,
        "host": str(host),
        "port": port,
        "server_header": server_header,
        "read_only": read_only,
        "jpeg_quality": jpeg_quality,
        "cover_size": cover_size,
        "transcode_cache_mb": transcode_cache_mb,
        "audio_formats": audio_formats,
        "default_audio_format": default_audio_format,
        "album_sort": album_sort,
        "album_sort_descending": album_sort_descending,
        "admin_password_sha1": admin_password_sha1,
        "admin_bruteforce_limit": admin_bruteforce_limit,
        "admin_bruteforce_window_seconds":
            admin_bruteforce_window_seconds,
        "admin_bruteforce_lock_seconds": admin_bruteforce_lock_seconds,
        "logs_dir": logs_dir,
        "log_level": log_level,
        "log_format": log_format,
        "rate_limit": rate_limit,
        "url_id_format": url_id_format,
    }
