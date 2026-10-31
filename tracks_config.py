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

from tracks_identity import normalize_identifier

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_DIR, "config.json")
DATABASE_FILENAME = "library.sqlite"
ARTWORK_DIRNAME = "Artwork"
ALBUM_SORT_KEYS = ("recentlyAdded", "album", "artist", "year")
DEFAULT_ADMIN_PASSWORD_SHA1 = hashlib.sha1(b"1").hexdigest()

DEFAULT_SETTINGS = {
    "database_dir": "data",
    "libraries": [],
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


def save_libraries_to_config(config_path, libraries):
    """Write canonical library entries back to config.json atomically."""

    config_path = os.path.abspath(os.path.expanduser(config_path))
    values = _read_config(config_path)
    serialized = []
    for library in libraries or []:
        identifier = str(library.get("identifier") or "").strip()
        namespace = normalize_identifier(library.get("namespace") or "")
        path = os.path.realpath(os.path.expanduser(library.get("path") or ""))
        if not identifier or not namespace or not path:
            raise ValueError("Library entries need identifier, namespace and path")
        serialized.append({
            "identifier": identifier,
            "namespace": namespace,
            "path": path,
        })
    values["libraries"] = serialized
    values.pop("music_dirs", None)
    parent = os.path.dirname(config_path) or PROJECT_DIR
    os.makedirs(parent, exist_ok=True)
    temporary = config_path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(values, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary, config_path)
    return config_path


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
    seen_identifiers = set()
    seen_namespaces = set()
    for index, item in enumerate(raw_libraries):
        if not isinstance(item, dict):
            raise ValueError(
                "%s: libraries[%d] must be an object" % (config_path, index)
            )
        raw_path = item.get("path") or item.get("root")
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

        identifier = item.get("identifier")
        if identifier is None:
            identifier = item.get("id")
        identifier = str(identifier).strip() if identifier is not None else None
        if identifier == "":
            identifier = None
        if identifier is not None:
            if any(ord(char) < 32 for char in identifier):
                raise ValueError(
                    "%s: library identifier contains control characters"
                    % config_path
                )
            key = normalize_identifier(identifier)
            if key in seen_identifiers:
                raise ValueError(
                    "%s: duplicate library identifier %s"
                    % (config_path, identifier)
                )
            seen_identifiers.add(key)

        namespace = item.get("namespace")
        namespace = (
            normalize_identifier(namespace) if namespace is not None else None
        )
        if namespace == "":
            namespace = None
        if namespace is not None:
            if any(ord(char) < 32 for char in namespace):
                raise ValueError(
                    "%s: library namespace contains control characters"
                    % config_path
                )
            if namespace in seen_namespaces:
                raise ValueError(
                    "%s: duplicate library namespace %s"
                    % (config_path, namespace)
                )
            seen_namespaces.add(namespace)

        libraries.append({
            "identifier": identifier,
            "namespace": namespace,
            "path": path,
        })

    seen_keys = {}
    for library in libraries:
        for key in (library["identifier"], library["namespace"]):
            if key is None:
                continue
            normalized = normalize_identifier(key)
            owner = seen_keys.get(normalized)
            if owner is not None and owner != library["path"]:
                raise ValueError(
                    "%s: library identifier/namespace %s is already in use"
                    % (config_path, key)
                )
            seen_keys[normalized] = library["path"]

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
        "libraries": libraries,
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
