#!/usr/bin/env python3
"""WebTracks: a local-first web music player for a Tracks-style library.

The SQLite schema and artwork cache layout match the macOS Tracks app, but the
database and cache belong to this project.  They are created under ``data/``
next to ``server.py`` (see ``config.yaml``) and are never shared with the
native app.  All web assets are served locally; the page makes no external
requests.
"""

import argparse
import collections
import datetime
import gzip
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import sys
import threading
import time
from urllib.parse import urlsplit

from flask import (
    Flask, Response, current_app, request, send_file, send_from_directory,
)
from werkzeug.serving import WSGIRequestHandler

from tracks_admin_guard import AdminLoginGuard
from tracks_analytics import Analytics
from tracks_transcode import (
    DEFAULT_AUDIO_FORMATS,
    DEFAULT_CACHE_MB,
    build_quality_specs,
    TranscodeCache,
    TranscodeError,
)
from tracks_config import (
    ALBUM_SORT_KEYS,
    DEFAULT_ADMIN_PASSWORD_SHA1,
    DEFAULT_CONFIG_PATH,
    ensure_config_file,
    load_settings,
    update_config,
)
from tracks_db import (
    LibraryCatalog,
    LibraryDB,
    ReadOnlyError,
)
from tracks_identity import canonical_path, normalize_identifier
from tracks_logging import configure_logging
from tracks_rate_limit import DownloadRateLimiter
from tracks_scanner import (
    DEFAULT_JPEG_QUALITY,
    rebuild_artwork_cache,
    scan_one,
)

runtime_log = logging.getLogger("webtracks.runtime")
operation_log = logging.getLogger("webtracks.operations")

UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)

AUDIO_TYPES = {
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
    ".aifc": "audio/aiff",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
}


class Config:
    def __init__(self, database, artwork, read_only=False, jpeg_quality=None,
                 cover_size=None, music_dirs=None, config_path=None,
                 transcode_cache_mb=None,
                 audio_formats=None, default_audio_format=None,
                 server_header=None, trusted_hosts=None,
                 album_sort=None, album_sort_descending=None,
                 admin_password_sha1=None, libraries=None,
                 admin_bruteforce_limit=None,
                 admin_bruteforce_window_seconds=None,
                 admin_bruteforce_lock_seconds=None,
                 logs_dir=None, log_level=None, log_format=None,
                 rate_limit=None, url_id_format=None):
        self.database = os.path.abspath(os.path.expanduser(database))
        self.artwork = os.path.abspath(os.path.expanduser(artwork))
        self.read_only = bool(read_only)
        self.jpeg_quality = int(
            jpeg_quality or DEFAULT_JPEG_QUALITY
        )
        self.jpeg_quality = max(15, min(100, self.jpeg_quality))
        try:
            self.cover_size = int(
                cover_size if cover_size is not None else 500
            )
        except (TypeError, ValueError):
            self.cover_size = 500
        self.cover_size = max(16, min(2000, self.cover_size))
        if libraries is None:
            libraries = [
                {"path": path} for path in (music_dirs or [])
            ]
        self.libraries = []
        for library in libraries:
            path = library if isinstance(library, str) else library.get("path")
            self.libraries.append({
                "path": os.path.realpath(os.path.expanduser(path)),
            })
        self.music_dirs = [library["path"] for library in self.libraries]
        self.config_path = config_path
        try:
            self.transcode_cache_mb = max(0, int(
                transcode_cache_mb
                if transcode_cache_mb is not None else DEFAULT_CACHE_MB
            ))
        except (TypeError, ValueError):
            self.transcode_cache_mb = DEFAULT_CACHE_MB
        try:
            self.audio_quality_specs = build_quality_specs(
                audio_formats if audio_formats is not None
                else DEFAULT_AUDIO_FORMATS
            )
        except ValueError:
            self.audio_quality_specs = build_quality_specs(
                DEFAULT_AUDIO_FORMATS
            )
        self.audio_quality_by_id = {
            spec["id"]: spec for spec in self.audio_quality_specs
        }
        self.transcodable_qualities = tuple(
            spec for spec in self.audio_quality_specs
            if spec["format"] != "raw"
        )
        try:
            default_index = int(
                default_audio_format
                if default_audio_format is not None
                else self.audio_quality_specs[0]["index"]
            )
        except (TypeError, ValueError):
            default_index = self.audio_quality_specs[0]["index"]
        if default_index not in {
            spec["index"] for spec in self.audio_quality_specs
        }:
            default_index = self.audio_quality_specs[0]["index"]
        self.default_audio_format = default_index
        self.default_audio_quality = next(
            spec["id"] for spec in self.audio_quality_specs
            if spec["index"] == default_index
        )
        server_header = str(server_header or "").strip()
        if len(server_header) > 200 or any(
            ord(char) < 32 or ord(char) == 127 for char in server_header
        ):
            server_header = ""
        self.server_header = server_header
        hosts = trusted_hosts if trusted_hosts is not None else ["*"]
        if isinstance(hosts, str):
            hosts = [hosts]
        self.trusted_hosts = [
            str(host).strip() for host in hosts if str(host).strip()
        ] or ["*"]
        self.album_sort = album_sort or "recentlyAdded"
        self.album_sort_descending = (
            True if album_sort_descending is None
            else bool(album_sort_descending)
        )
        self.admin_password_sha1 = str(
            admin_password_sha1 or DEFAULT_ADMIN_PASSWORD_SHA1
        ).strip().lower()
        try:
            self.admin_bruteforce_limit = max(
                0, int(admin_bruteforce_limit
                       if admin_bruteforce_limit is not None else 5)
            )
        except (TypeError, ValueError):
            self.admin_bruteforce_limit = 5
        try:
            self.admin_bruteforce_window_seconds = max(
                1, int(admin_bruteforce_window_seconds
                       if admin_bruteforce_window_seconds is not None else 60)
            )
        except (TypeError, ValueError):
            self.admin_bruteforce_window_seconds = 60
        try:
            self.admin_bruteforce_lock_seconds = max(
                1, int(admin_bruteforce_lock_seconds
                       if admin_bruteforce_lock_seconds is not None else 86400)
            )
        except (TypeError, ValueError):
            self.admin_bruteforce_lock_seconds = 86400
        self.logs_dir = (
            os.path.abspath(os.path.expanduser(logs_dir))
            if logs_dir else None
        )
        self.log_level = str(log_level or "INFO").upper()
        self.log_format = str(log_format or "text").strip().lower()
        if self.log_format not in ("text", "json"):
            self.log_format = "text"
        try:
            self.rate_limit = max(0, int(
                rate_limit if rate_limit is not None else 28
            ))
        except (TypeError, ValueError):
            self.rate_limit = 28
        self.url_id_format = (
            str(url_id_format or "uuid").strip().lower()
        )
        if self.url_id_format not in ("uuid", "base64url"):
            self.url_id_format = "uuid"

    @classmethod
    def from_settings(cls, settings):
        return cls(
            settings["database"],
            settings["artwork"],
            read_only=settings.get("read_only", False),
            jpeg_quality=settings.get("jpeg_quality"),
            cover_size=settings.get("cover_size"),
            music_dirs=settings.get("music_dirs") or [],
            config_path=settings.get("config_path"),
            transcode_cache_mb=settings.get("transcode_cache_mb"),
            audio_formats=settings.get("audio_formats"),
            default_audio_format=settings.get("default_audio_format"),
            server_header=settings.get("server_header"),
            trusted_hosts=settings.get("trusted_hosts"),
            libraries=settings.get("libraries") or [],
            album_sort=settings.get("album_sort"),
            album_sort_descending=settings.get("album_sort_descending"),
            admin_password_sha1=settings.get("admin_password_sha1"),
            admin_bruteforce_limit=settings.get("admin_bruteforce_limit"),
            admin_bruteforce_window_seconds=settings.get(
                "admin_bruteforce_window_seconds"
            ),
            admin_bruteforce_lock_seconds=settings.get(
                "admin_bruteforce_lock_seconds"
            ),
            logs_dir=settings.get("logs_dir"),
            log_level=settings.get("log_level"),
            log_format=settings.get("log_format"),
            rate_limit=settings.get("rate_limit"),
            url_id_format=settings.get("url_id_format"),
        )


class PresenceTracker:
    """Approximate count of open web clients using a heartbeat TTL.

    Presence is keyed by source IP. Multiple tabs on one machine count once,
    which keeps the map bounded by the number of active addresses.
    """

    def __init__(self, ttl=30, max_entries=10000):
        self.ttl = max(5, int(ttl))
        self.max_entries = max(1, int(max_entries))
        self._clients = collections.OrderedDict()
        self._lock = threading.Lock()

    def _prune(self, now):
        while self._clients:
            address, client = next(iter(self._clients.items()))
            if now - client["seen"] <= self.ttl:
                break
            self._clients.popitem(last=False)

    def touch(self, address=""):
        now = time.time()
        address = str(address or "")
        with self._lock:
            self._prune(now)
            if address:
                self._clients[address] = {"seen": now}
                self._clients.move_to_end(address)
                while len(self._clients) > self.max_entries:
                    self._clients.popitem(last=False)
            return len(self._clients)

    def count(self):
        now = time.time()
        with self._lock:
            self._prune(now)
            return len(self._clients)

    def counts_by_ip(self):
        now = time.time()
        with self._lock:
            self._prune(now)
            return {address: 1 for address in self._clients}


PLAY_COUNT_COOLDOWN_SECONDS = 30


class PlayTracker:
    """Count one play per client playback session.

    The web player adds a random ``play`` query parameter to audio requests.
    Range and quality requests reuse it, so a single playback only increments
    the count once. Clients that do not send a token fall back to a per-IP
    window. A separate cooldown key prevents different tokens from inflating
    the count for the same song.
    """

    def __init__(self, ttl=6 * 60 * 60, fallback_ttl=5 * 60,
                 max_entries=20000):
        self.ttl = max(1, int(ttl))
        self.fallback_ttl = max(1, int(fallback_ttl))
        self.max_entries = max(1, int(max_entries))
        self._entries = {}
        self._lock = threading.Lock()

    def claim(self, key, ttl=None):
        now = time.monotonic()
        with self._lock:
            expires = self._entries.get(key)
            if expires is not None and expires > now:
                return False
            self._entries[key] = now + (
                self.ttl if ttl is None else max(1, int(ttl))
            )
            while len(self._entries) > self.max_entries:
                self._entries.pop(next(iter(self._entries)))
            return True


class AdminAuth:
    """In-memory bearer tokens for the local Setup window."""

    def __init__(self, password_sha1, ttl=12 * 60 * 60):
        self.password_sha1 = str(password_sha1 or "").strip().lower()
        self.ttl = max(60, int(ttl))
        self._tokens = {}
        self._lock = threading.Lock()

    def authenticate(self, password):
        digest = hashlib.sha1(
            str(password or "").encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(digest, self.password_sha1):
            return None
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._tokens = {
                value: expiry for value, expiry in self._tokens.items()
                if expiry > now
            }
            self._tokens[token] = now + self.ttl
        return token

    def is_authenticated(self, token):
        if not token:
            return False
        now = time.time()
        with self._lock:
            expiry = self._tokens.get(token)
            if expiry is None:
                return False
            if expiry <= now:
                self._tokens.pop(token, None)
                return False
            return True

    def revoke(self, token):
        if not token:
            return
        with self._lock:
            self._tokens.pop(token, None)

    def set_password_sha1(self, password_sha1):
        self.password_sha1 = str(password_sha1 or "").strip().lower()
        with self._lock:
            self._tokens.clear()


def _seed_library_paths(config, db):
    """Import legacy config paths into the database.

    Library folders, identifiers, and namespaces live in ``library.sqlite``.
    Old configs may still contain ``libraries`` or ``music_dirs``; those paths
    are imported once so existing installations keep working, but the
    configuration file is not the runtime source of truth.
    """

    if not config.read_only and config.libraries:
        db.sync_libraries(config.libraries)


def _database_scan_roots(config, db=None):
    """Return every enabled library stored in the database."""

    if db is None:
        db = LibraryDB(config.database, read_only=True)
    roots = []
    for library in db.load_libraries():
        if not library.get("enabled", True):
            continue
        root = canonical_path(library["path"])
        roots.append({
            "id": library.get("id"),
            "root": root,
            "path": root,
            "name": library.get("name") or os.path.basename(root),
            "identifier": library.get("identifier"),
            "namespace": library.get("id_namespace"),
        })
    return roots


def _remap_artwork_cache(cache_dir, album_id_map):
    changed = 0
    for old_id, new_id in (album_id_map or {}).items():
        if not old_id or not new_id or old_id == new_id:
            continue
        source = os.path.join(cache_dir, "%s.jpg" % old_id.upper())
        target = os.path.join(cache_dir, "%s.jpg" % new_id.upper())
        try:
            if not os.path.isfile(source):
                continue
            if os.path.exists(target):
                os.remove(source)
            else:
                os.replace(source, target)
            changed += 1
        except OSError:
            continue
    return changed


class ScanManager:
    """Runs at most one scan job in a background thread."""

    def __init__(self, config, catalog):
        self.config = config
        self.catalog = catalog
        self.lock = threading.Lock()
        self.job = {
            "running": False,
            "phase": "Idle",
            "completed": 0,
            "total": None,
            "message": "",
            "errors": [],
            "startedAt": None,
            "finishedAt": None,
            "summary": None,
        }

    def snapshot(self):
        with self.lock:
            return {
                **self.job,
                "errors": list(self.job.get("errors") or []),
            }

    def _update(self, **values):
        with self.lock:
            self.job.update(values)

    def _progress(self, phase, completed, total, message):
        with self.lock:
            self.job["phase"] = phase
            self.job["completed"] = int(completed or 0)
            self.job["total"] = None if total is None else int(total)
            self.job["message"] = message or ""

    def start(self, payload):
        if self.config.read_only:
            raise ReadOnlyError("The server is running in read-only mode")
        with self.lock:
            if self.job.get("running"):
                raise RuntimeError("A scan is already running")
            self.job = {
                "running": True,
                "phase": "Starting",
                "completed": 0,
                "total": None,
                "message": "",
                "errors": [],
                "startedAt": time.time(),
                "finishedAt": None,
                "summary": None,
            }

        db = LibraryDB(self.config.database, read_only=False)
        _seed_library_paths(self.config, db)
        mode = payload.get("mode") or "quick"
        if mode not in ("quick", "full"):
            mode = "quick"
        prune = bool(payload.get("prune"))
        force = bool(payload.get("force"))
        roots = _database_scan_roots(self.config, db)
        if not roots:
            with self.lock:
                self.job["running"] = False
                self.job["phase"] = "Error"
                self.job["message"] = "No libraries in the database"
            raise ValueError(
                "No libraries are in the database. Add one with "
                "`python3 server.py scan --root PATH`."
            )

        runtime_log.info(
            "Scan starting; mode=%s force=%s prune=%s libraries=%s",
            mode, force, prune,
            [entry["root"] for entry in roots],
        )

        def run():
            summaries = []
            errors = []
            try:
                for entry in roots:
                    result = scan_one(
                        db,
                        entry["root"],
                        identifier=entry.get("identifier"),
                        mode=mode,
                        prune=prune,
                        artwork_dir=self.config.artwork,
                        jpeg_quality=self.config.jpeg_quality,
                        cover_size=self.config.cover_size,
                        force=force,
                        progress=self._progress,
                    )
                    summaries.append(result)
                    errors.extend(result.get("errors") or [])
                summary = {
                    "libraries": [entry["root"] for entry in roots],
                    "tracks": sum(result["tracks"] for result in summaries),
                    "discovered": sum(
                        result["discovered"] for result in summaries
                    ),
                    "artwork": sum(result["artwork"] for result in summaries),
                    "missing": sum(result["missing"] for result in summaries),
                    "errors": errors,
                }
                self._update(
                    phase="Done",
                    completed=summary["tracks"],
                    total=summary["tracks"],
                    message="Scan complete",
                    summary=summary,
                    errors=errors,
                )
                self.catalog.reload()
                runtime_log.info(
                    "Scan complete; tracks=%s artwork=%s discovered=%s "
                    "missing=%s errors=%s",
                    summary["tracks"], summary["artwork"],
                    summary["discovered"], summary["missing"],
                    len(errors),
                )
            except Exception as exc:
                errors.append(str(exc))
                runtime_log.exception("Scan failed")
                self._update(
                    phase="Error",
                    message=str(exc),
                    errors=errors,
                    summary={"errors": errors},
                )
            finally:
                with self.lock:
                    self.job["running"] = False
                    self.job["finishedAt"] = time.time()

        thread = threading.Thread(
            target=run, name="tracks-web-scan", daemon=True
        )
        thread.start()
        return self.snapshot()


class RekeyManager:
    """Runs one database ReKey in the background."""

    def __init__(self, config, catalog):
        self.config = config
        self.catalog = catalog
        self.lock = threading.Lock()
        self.job = {
            "running": False,
            "phase": "Idle",
            "message": "",
            "errors": [],
            "startedAt": None,
            "finishedAt": None,
            "summary": None,
        }

    def snapshot(self):
        with self.lock:
            return {
                **self.job,
                "errors": list(self.job.get("errors") or []),
            }

    def _update(self, **values):
        with self.lock:
            self.job.update(values)

    def start(self):
        if self.config.read_only:
            raise ReadOnlyError("The server is running in read-only mode")
        with self.lock:
            if self.job.get("running"):
                raise RuntimeError("A ReKey is already running")
            self.job = {
                "running": True,
                "phase": "Starting",
                "message": "Preparing database ReKey",
                "errors": [],
                "startedAt": time.time(),
                "finishedAt": None,
                "summary": None,
            }
        runtime_log.info("ReKey starting")
        thread = threading.Thread(
            target=self._run, name="webtracks-rekey", daemon=True
        )
        thread.start()
        return self.snapshot()

    def _run(self):
        errors = []
        summary = None
        try:
            db = LibraryDB(self.config.database, read_only=False)
            _seed_library_paths(self.config, db)
            self._update(
                phase="Rekeying database",
                message="Recalculating song, album, and artist IDs",
            )
            # Library identifiers and namespaces are database state. ReKey
            # reads the identifier from each row and writes the new namespace
            # back to the same row; config.yaml only stores folder paths.
            result = db.rekey_libraries()
            self._update(
                phase="Updating artwork cache",
                message="Moving cached artwork to the new album IDs",
            )
            artwork = _remap_artwork_cache(
                self.config.artwork, result.get("albumIDMap")
            )
            self._update(
                phase="Reloading catalog",
                message="Refreshing the in-memory library",
            )
            self.catalog.reload()
            summary = {
                "songs": result.get("songCount", 0),
                "albums": result.get("albumCount", 0),
                "artists": result.get("artistCount", 0),
                "artwork": artwork,
                "backup": result.get("backup"),
            }
            self._update(
                phase="Done",
                message="ReKey complete",
                summary=summary,
            )
            runtime_log.info(
                "ReKey complete; songs=%s albums=%s artists=%s artwork=%s "
                "backup=%s",
                summary["songs"], summary["albums"], summary["artists"],
                summary["artwork"], summary["backup"],
            )
        except Exception as exc:
            errors.append(str(exc))
            runtime_log.exception("ReKey failed")
            self._update(
                phase="Error",
                message=str(exc),
                errors=errors,
                summary=summary,
            )
        finally:
            with self.lock:
                self.job["running"] = False
                self.job["finishedAt"] = time.time()


class ArtworkManager:
    """Rebuild cached album covers in a background thread."""

    def __init__(self, config, catalog):
        self.config = config
        self.catalog = catalog
        self.lock = threading.Lock()
        self.job = {
            "running": False,
            "phase": "Idle",
            "completed": 0,
            "total": None,
            "message": "",
            "errors": [],
            "startedAt": None,
            "finishedAt": None,
            "summary": None,
        }

    def snapshot(self):
        with self.lock:
            return {
                **self.job,
                "errors": list(self.job.get("errors") or []),
            }

    def _update(self, **values):
        with self.lock:
            self.job.update(values)

    def _progress(self, phase, completed, total, message):
        with self.lock:
            self.job["phase"] = phase
            self.job["completed"] = int(completed or 0)
            self.job["total"] = None if total is None else int(total)
            self.job["message"] = message or ""

    def start(self):
        if self.config.read_only:
            raise ReadOnlyError("The server is running in read-only mode")
        with self.lock:
            if self.job.get("running"):
                raise RuntimeError("A Rebuild Artwork job is already running")
            self.job = {
                "running": True,
                "phase": "Starting",
                "completed": 0,
                "total": None,
                "message": "Preparing artwork rebuild",
                "errors": [],
                "startedAt": time.time(),
                "finishedAt": None,
                "summary": None,
            }
        runtime_log.info("Artwork rebuild starting")
        thread = threading.Thread(
            target=self._run, name="webtracks-artwork", daemon=True
        )
        thread.start()
        return self.snapshot()

    def _run(self):
        errors = []
        summary = None
        try:
            self.catalog.maybe_refresh()
            with self.catalog.lock:
                albums = list(self.catalog.albums)
            self._update(
                phase="Rebuilding Artwork",
                message="Extracting covers from audio files",
            )
            result = rebuild_artwork_cache(
                albums,
                self.config.artwork,
                jpeg_quality=self.config.jpeg_quality,
                cover_size=self.config.cover_size,
                progress=self._progress,
            )
            errors.extend(result.get("errors") or [])
            summary = {
                "albums": result.get("albums", 0),
                "artwork": result.get("artwork", 0),
                "missing": result.get("missing", 0),
                "orphansRemoved": result.get("orphansRemoved", 0),
                "errors": errors,
            }
            self._update(
                phase="Done",
                completed=result.get("albums", 0),
                total=result.get("albums", 0),
                message="Artwork cache rebuilt",
                summary=summary,
                errors=errors,
            )
            runtime_log.info(
                "Artwork rebuild complete; albums=%s artwork=%s missing=%s "
                "errors=%s",
                summary["albums"], summary["artwork"], summary["missing"],
                len(errors),
            )
        except Exception as exc:
            errors.append(str(exc))
            runtime_log.exception("Artwork rebuild failed")
            self._update(
                phase="Error",
                message=str(exc),
                errors=errors,
                summary=summary,
            )
        finally:
            with self.lock:
                self.job["running"] = False
                self.job["finishedAt"] = time.time()


def _json_response(payload, status=200):
    raw = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    headers = {
        "Cache-Control": "no-store",
        "Vary": "Accept-Encoding",
        "X-Content-Type-Options": "nosniff",
    }
    if (
        "gzip" in (request.headers.get("Accept-Encoding") or "").lower()
        and len(raw) > 1024
    ):
        raw = gzip.compress(raw, compresslevel=5)
        headers["Content-Encoding"] = "gzip"
    return Response(raw, status=status, mimetype="application/json",
                    headers=headers)


def _error(message, status=400):
    return _json_response({"error": message}, status=status)


def _public_job(job, authenticated):
    """Return a background job snapshot without filesystem details.

    Scan, ReKey, and artwork jobs keep absolute paths in ``message``,
    ``errors``, and ``summary``.  Only admins need those details; public
    clients get progress fields so the status bar and setup sheet still work.
    """

    if authenticated:
        return job or {}
    job = job or {}
    return {
        "running": bool(job.get("running")),
        "phase": job.get("phase") or "",
        "completed": int(job.get("completed") or 0),
        "total": job.get("total"),
        "message": "",
        "errors": [],
        "summary": None,
    }


def _valid_ip(value):
    """Return a normalized IP address, or an empty string."""

    value = (value or "").strip().strip('"')
    if not value:
        return ""
    if value.startswith("["):
        closing = value.find("]")
        if closing != -1:
            value = value[1:closing]
    elif value.count(":") == 1 and "." in value:
        # IPv4 with an optional port, e.g. "203.0.113.7:54321".
        host, port = value.rsplit(":", 1)
        if port.isdigit():
            value = host
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return ""


def _client_ip():
    """Resolve the client IP, preferring proxy headers over the socket peer.

    ``CF-Connecting-IP`` (set by Cloudflare Tunnel) is checked first, then
    ``X-Forwarded-For`` (leftmost entry first), then ``X-Real-IP``, and finally
    ``request.remote_addr``. Invalid or empty values are ignored so a malformed
    proxy header cannot hide the real source address.
    """

    address = _valid_ip(request.headers.get("CF-Connecting-IP"))
    if address:
        return address
    forwarded = request.headers.get("X-Forwarded-For") or ""
    for part in forwarded.split(","):
        address = _valid_ip(part)
        if address:
            return address
    address = _valid_ip(request.headers.get("X-Real-IP"))
    if address:
        return address
    return request.remote_addr or ""


def _log_operation(action, result="ok", **details):
    client_ip = _client_ip()
    analytics = current_app.extensions.get("tracks_analytics")
    if analytics is not None:
        analytics.record_operation(client_ip, action, result)
    if not operation_log.handlers:
        return
    forwarded = (request.headers.get("X-Forwarded-For") or "").strip()
    payload = {
        "time": datetime.datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "action": action,
        "result": result,
        # remoteIp is resolved from CF-Connecting-IP/X-Forwarded-For when
        # present, otherwise from the direct socket peer.
        "remoteIp": client_ip,
        # proxyIp is the direct socket peer (usually the reverse proxy).
        "proxyIp": request.remote_addr or "",
        "forwardedFor": forwarded,
        "cfConnectingIp": (
            request.headers.get("CF-Connecting-IP") or ""
        ).strip(),
        "userAgent": request.headers.get("User-Agent") or "",
        "method": request.method,
        "path": request.path,
    }
    payload.update(details)
    operation_log.info("operation", extra={"event": payload})


def _byte_range_response(data, mimetype, etag):
    """Serve an in-memory encoded track with HTTP byte-range support."""

    total = len(data)
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=3600",
        "Content-Type": mimetype,
        "ETag": etag,
    }
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers=headers)

    range_header = request.headers.get("Range", "")
    if range_header.startswith("bytes=") and total > 0:
        specification = range_header[6:].split(",", 1)[0].strip()
        if "-" in specification:
            start_text, end_text = specification.split("-", 1)
            try:
                if start_text:
                    start = int(start_text)
                    end = int(end_text) if end_text else total - 1
                else:
                    suffix = int(end_text)
                    if suffix <= 0:
                        raise ValueError
                    start = max(0, total - suffix)
                    end = total - 1
            except ValueError:
                start = total
                end = total - 1
            if start < 0 or start >= total or end < start:
                headers["Content-Range"] = "bytes */%d" % total
                headers["Content-Length"] = "0"
                return Response(status=416, headers=headers)
            end = min(end, total - 1)
            body = data[start:end + 1]
            headers["Content-Range"] = "bytes %d-%d/%d" % (
                start, end, total
            )
            headers["Content-Length"] = str(len(body))
            return Response(body, status=206, headers=headers)

    headers["Content-Length"] = str(total)
    return Response(data, status=200, headers=headers)


def _fallback_artwork_svg(album_id, album_title=""):
    digest = hashlib.md5(album_id.encode("utf-8")).digest()
    hue = int.from_bytes(digest[:2], "big") % 360
    hue2 = (hue + 34 + digest[2] % 48) % 360
    light = 30 + digest[3] % 15
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="500" height="500" viewBox="0 0 500 500">
<defs>
  <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="hsl({hue} 32% {light + 8}%)"/>
    <stop offset="1" stop-color="hsl({hue2} 38% {max(10, light - 10)}%)"/>
  </linearGradient>
  <radialGradient id="v" cx="0.3" cy="0.25" r="0.9">
    <stop offset="0" stop-color="#ffffff" stop-opacity="0.14"/>
    <stop offset="1" stop-color="#000000" stop-opacity="0.30"/>
  </radialGradient>
</defs>
<rect width="500" height="500" fill="url(#g)"/>
<rect width="500" height="500" fill="url(#v)"/>
<g fill="none" stroke="#ffffff" stroke-opacity="0.62">
  <circle cx="250" cy="250" r="104" stroke-width="3"/>
  <circle cx="250" cy="250" r="74" stroke-width="2" stroke-opacity="0.42"/>
  <circle cx="250" cy="250" r="17" fill="#ffffff" fill-opacity="0.75" stroke="none"/>
  <path d="M250 146a104 104 0 0 1 79 35" stroke-width="6" stroke-linecap="round"/>
  <path d="M250 354a104 104 0 0 1-79-35" stroke-width="6" stroke-linecap="round"/>
</g>
</svg>"""


def create_app(config):
    app = Flask(
        __name__,
        static_folder=os.path.join(os.path.dirname(__file__), "static"),
        static_url_path="/static",
    )
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
    if "*" in config.trusted_hosts:
        app.config["TRUSTED_HOSTS"] = None
    else:
        app.config["TRUSTED_HOSTS"] = list(config.trusted_hosts)
    catalog = LibraryCatalog(config.database, read_only=config.read_only)
    if not config.read_only and config.libraries:
        _seed_library_paths(config, catalog.db)
        catalog.reload()
    scans = ScanManager(config, catalog)
    rekeys = RekeyManager(config, catalog)
    artworks = ArtworkManager(config, catalog)
    analytics = Analytics()
    admin_auth = AdminAuth(config.admin_password_sha1)
    admin_guard = AdminLoginGuard(
        config.admin_bruteforce_limit,
        config.admin_bruteforce_window_seconds,
        config.admin_bruteforce_lock_seconds,
    )
    presence = PresenceTracker()
    play_tracker = PlayTracker()
    transcode_cache = TranscodeCache(
        config.transcode_cache_mb * 1024 * 1024,
        qualities=config.transcodable_qualities,
    )
    rate_limiter = DownloadRateLimiter(config.rate_limit)
    app.extensions["tracks_catalog"] = catalog
    app.extensions["tracks_config"] = config
    app.extensions["tracks_scans"] = scans
    app.extensions["tracks_rekeys"] = rekeys
    app.extensions["tracks_artworks"] = artworks
    app.extensions["tracks_analytics"] = analytics
    app.extensions["tracks_admin_auth"] = admin_auth
    app.extensions["tracks_admin_guard"] = admin_guard
    app.extensions["tracks_presence"] = presence
    app.extensions["tracks_play_tracker"] = play_tracker
    app.extensions["tracks_transcode_cache"] = transcode_cache
    app.extensions["tracks_rate_limit"] = rate_limiter

    def _admin_token():
        header = (request.headers.get("Authorization") or "").strip()
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return (request.headers.get("X-Admin-Token") or "").strip()

    def _require_admin():
        if admin_auth.is_authenticated(_admin_token()):
            return None
        return _error("Admin login required", 401)

    @app.before_request
    def validate_origin():
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        fetch_site = (
            request.headers.get("Sec-Fetch-Site") or ""
        ).strip().lower()
        if fetch_site and fetch_site not in ("same-origin", "none"):
            return _error("Cross-origin request rejected", 403)
        origin = (request.headers.get("Origin") or "").strip()
        if not origin:
            return None
        parsed = urlsplit(origin)
        if not parsed.scheme or not parsed.netloc:
            return _error("Cross-origin request rejected", 403)
        if parsed.netloc.lower() != (request.host or "").lower():
            return _error("Cross-origin request rejected", 403)
        return None

    @app.after_request
    def security_headers(response):
        if config.server_header:
            response.headers["Server"] = config.server_header
        elif "Server" in response.headers:
            del response.headers["Server"]
        if request.path.startswith(("/api/audio/", "/api/artwork/")):
            response.headers.setdefault(
                "Cross-Origin-Resource-Policy", "same-origin"
            )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; media-src 'self'; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; font-src 'self'; object-src 'none'; "
            "base-uri 'self'; form-action 'self'",
        )
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "X-Robots-Tag",
            "noindex, nofollow, noarchive, nosnippet, noimageindex",
        )
        if response.status_code >= 500:
            runtime_log.error(
                "HTTP %s %s -> %s", request.method, request.path,
                response.status_code,
            )
        size = response.content_length
        if size is None and not response.direct_passthrough:
            try:
                size = response.calculate_content_length()
            except Exception:
                size = None
        analytics.record_request(
            _client_ip(),
            request.path,
            response.status_code,
            size or 0,
            request.headers.get("User-Agent") or "",
        )
        return response

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html", max_age=0)

    @app.get("/robots.txt")
    def robots_txt():
        return Response(
            "User-agent: *\nDisallow: /\n",
            mimetype="text/plain",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.get("/api/bootstrap")
    def api_bootstrap():
        try:
            payload = catalog.bootstrap_payload()
            # Library roots and the database location are filesystem details.
            # Admins can read them from /api/setup after logging in; the public
            # player only needs identifiers and display names.
            payload["libraries"] = [
                {
                    key: value
                    for key, value in library.items()
                    if key != "path"
                }
                for library in payload.get("libraries") or []
            ]
            payload["albumSort"] = config.album_sort
            payload["albumSortDescending"] = config.album_sort_descending
            payload["urlIdFormat"] = config.url_id_format
            return _json_response(payload)
        except Exception:
            runtime_log.exception("Could not read the library")
            return _error("Could not read the library", 500)

    @app.get("/api/songs")
    def api_songs():
        scope = request.args.get("scope", "all")
        try:
            return _json_response(catalog.songs_payload(scope))
        except Exception:
            runtime_log.exception("Could not read songs")
            return _error("Could not read songs", 500)

    @app.get("/api/songs/<song_id>")
    def api_song(song_id):
        if not UUID_RE.match(song_id):
            return _error("Song not found", 404)
        song = catalog.song_internal(song_id.upper())
        if song is None:
            return _error("Song not found", 404)
        return _json_response({"song": catalog.public_song(song)})

    @app.get("/api/albums/<album_id>")
    def api_album(album_id):
        if not UUID_RE.match(album_id):
            return _error("Invalid album id", 404)
        payload = catalog.album_payload(album_id.upper())
        if payload is None:
            return _error("Album not found", 404)
        return _json_response(payload)

    @app.get("/api/playlists/<playlist_id>")
    def api_playlist(playlist_id):
        payload = catalog.playlist_payload(playlist_id)
        if payload is None:
            return _error("Playlist not found", 404)
        return _json_response(payload)

    @app.post("/api/playlists")
    def api_create_playlist():
        unauthorized = _require_admin()
        if unauthorized is not None:
            return unauthorized
        body = request.get_json(silent=True) or {}
        try:
            playlist = catalog.create_playlist(body.get("name", ""))
            _log_operation(
                "playlist_created", "success", actor="admin",
                playlistId=playlist["id"], playlistName=playlist["name"],
            )
            return _json_response({"playlist": playlist}, status=201)
        except ReadOnlyError as exc:
            return _error(str(exc), 403)
        except ValueError as exc:
            return _error(str(exc), 400)

    @app.patch("/api/playlists/<playlist_id>")
    def api_rename_playlist(playlist_id):
        unauthorized = _require_admin()
        if unauthorized is not None:
            return unauthorized
        body = request.get_json(silent=True) or {}
        before = catalog.playlist_by_id.get(playlist_id) or {}
        try:
            playlist = catalog.rename_playlist(
                playlist_id, body.get("name", "")
            )
            _log_operation(
                "playlist_renamed", "success", actor="admin",
                playlistId=playlist_id,
                oldName=before.get("name"),
                newName=playlist["name"],
            )
            return _json_response({"playlist": playlist})
        except ReadOnlyError as exc:
            return _error(str(exc), 403)
        except KeyError as exc:
            return _error(str(exc.args[0]), 404)
        except ValueError as exc:
            return _error(str(exc), 400)

    @app.delete("/api/playlists/<playlist_id>")
    def api_delete_playlist(playlist_id):
        unauthorized = _require_admin()
        if unauthorized is not None:
            return unauthorized
        before = catalog.playlist_by_id.get(playlist_id) or {}
        try:
            catalog.delete_playlist(playlist_id)
            _log_operation(
                "playlist_deleted", "success", actor="admin",
                playlistId=playlist_id,
                playlistName=before.get("name"),
            )
            return _json_response({"deleted": True})
        except ReadOnlyError as exc:
            return _error(str(exc), 403)
        except KeyError as exc:
            return _error(str(exc.args[0]), 404)

    @app.post("/api/playlists/<playlist_id>/songs")
    def api_add_playlist_songs(playlist_id):
        body = request.get_json(silent=True) or {}
        song_ids = body.get("songIds")
        if song_ids is None and body.get("songId"):
            song_ids = [body["songId"]]
        if not isinstance(song_ids, list):
            return _error("songIds must be a list", 400)
        try:
            added = catalog.add_to_playlist(playlist_id, song_ids)
            _log_operation(
                "playlist_songs_added", "success",
                actor=("admin" if admin_auth.is_authenticated(_admin_token())
                       else "anonymous"),
                playlistId=playlist_id,
                requested=len(song_ids),
                added=added,
            )
            return _json_response({
                "added": added,
                "playlist": catalog.playlist_payload(playlist_id),
            })
        except ReadOnlyError as exc:
            return _error(str(exc), 403)
        except KeyError as exc:
            return _error(str(exc.args[0]), 404)

    @app.delete("/api/playlists/<playlist_id>/songs")
    def api_remove_playlist_songs(playlist_id):
        body = request.get_json(silent=True) or {}
        song_ids = body.get("songIds") or []
        if not isinstance(song_ids, list):
            return _error("songIds must be a list", 400)
        try:
            removed = catalog.remove_from_playlist(playlist_id, song_ids)
            _log_operation(
                "playlist_songs_removed", "success",
                actor=("admin" if admin_auth.is_authenticated(_admin_token())
                       else "anonymous"),
                playlistId=playlist_id,
                requested=len(song_ids),
                removed=removed,
            )
            return _json_response({
                "removed": removed,
                "playlist": catalog.playlist_payload(playlist_id),
            })
        except ReadOnlyError as exc:
            return _error(str(exc), 403)
        except KeyError as exc:
            return _error(str(exc.args[0]), 404)

    @app.post("/api/songs/<song_id>/star")
    def api_star(song_id):
        body = request.get_json(silent=True) or {}
        try:
            song = catalog.song_internal(song_id)
            if song is None:
                return _error("Song not found", 404)
            if "loved" in body:
                loved = bool(body["loved"])
            else:
                loved = not bool(song["loved"])
            loved = catalog.update_loved(song_id, loved)
            return _json_response({"id": song_id, "loved": loved})
        except ReadOnlyError as exc:
            return _error(str(exc), 403)
        except KeyError:
            return _error("Song not found", 404)

    @app.get("/api/artwork/<album_id>")
    def api_artwork(album_id):
        album_id = album_id.upper()
        if not UUID_RE.match(album_id):
            return _error("Invalid artwork id", 404)
        if catalog.album_by_id.get(album_id) is None:
            return _error("Album not found", 404)
        target = os.path.join(config.artwork, album_id + ".jpg")
        if os.path.isfile(target):
            return send_file(
                target,
                mimetype="image/jpeg",
                conditional=True,
                last_modified=os.path.getmtime(target),
                max_age=60,
            )
        album = catalog.album_by_id.get(album_id) or {}
        svg = _fallback_artwork_svg(album_id, album.get("title", ""))
        etag = hashlib.md5(svg.encode("utf-8")).hexdigest()
        response = Response(svg, mimetype="image/svg+xml")
        response.set_etag(etag)
        response.headers["Cache-Control"] = "public, max-age=60"
        return response.make_conditional(request.environ)

    @app.get("/api/quality")
    def api_quality():
        return _json_response({
            "default": config.default_audio_quality,
            "qualities": [
                {
                    "id": spec["id"],
                    "index": spec["index"],
                    "format": spec["format"],
                    "label": spec["label"],
                    "detail": spec["detail"],
                    "bitrate": spec["bitrate"],
                    "mime": spec["mime"],
                    "raw": bool(spec.get("raw")),
                }
                for spec in config.audio_quality_specs
            ],
        })

    @app.get("/api/rate-limit")
    def api_rate_limit():
        limited, retry_after = rate_limiter.is_limited(_client_ip())
        return _json_response({
            "limited": limited,
            "retryAfter": retry_after,
            "limit": rate_limiter.limit,
        })

    def _record_playback(song_id, quality):
        client_ip = _client_ip()
        cooldown_key = ("cooldown", song_id, client_ip)
        if not play_tracker.claim(
            cooldown_key, PLAY_COUNT_COOLDOWN_SECONDS
        ):
            return
        play_id = (request.args.get("play") or "").strip()[:128]
        if play_id:
            key = (song_id, play_id)
            ttl = play_tracker.ttl
        else:
            key = (song_id, quality, client_ip)
            ttl = play_tracker.fallback_ttl
        if not play_tracker.claim(key, ttl):
            return
        try:
            plays = catalog.record_play(song_id)
        except ReadOnlyError:
            return
        except Exception:
            runtime_log.exception(
                "Could not record play for song %s", song_id
            )
            return
        if plays is None:
            return
        analytics.record_play(client_ip, song_id)

    @app.get("/api/audio/<song_id>")
    def api_audio(song_id):
        song = catalog.song_internal(song_id)
        if song is None:
            return _error("Song not found", 404)
        path = song.get("_path") or ""
        if not os.path.isfile(path) or not os.access(path, os.R_OK):
            return _error("Audio file is missing", 404)

        allowed, retry_after = rate_limiter.check(_client_ip(), song_id)
        if not allowed:
            _log_operation(
                "download_rate_limited",
                "blocked",
                songId=song_id,
                retryAfter=retry_after,
            )
            response = _error("Download rate limit exceeded", 429)
            response.headers["Retry-After"] = str(retry_after)
            return response

        quality = (request.args.get("quality") or "").strip()
        if not quality:
            quality = config.default_audio_quality
        if quality == "original":
            quality = (
                "raw" if "raw" in config.audio_quality_by_id else ""
            )
        spec = config.audio_quality_by_id.get(quality)
        if spec is None:
            return _error("Unsupported audio quality", 400)

        if not spec.get("raw"):
            try:
                data, etag = transcode_cache.get_or_encode(
                    song_id, path, quality
                )
            except FileNotFoundError:
                return _error("Audio file disappeared while encoding", 404)
            except (TranscodeError, OSError):
                runtime_log.exception(
                    "Could not transcode audio for song %s", song_id
                )
                return _error("Could not transcode audio", 500)
            response = _byte_range_response(data, spec["mime"], etag)
            if (request.method != "HEAD"
                    and response.status_code in (200, 206)):
                _record_playback(song_id, quality)
            return response

        mime = AUDIO_TYPES.get(
            os.path.splitext(path)[1].lower(),
            mimetypes.guess_type(path)[0] or "application/octet-stream",
        )
        try:
            response = send_file(
                path,
                mimetype=mime,
                conditional=True,
                as_attachment=False,
                download_name=os.path.basename(path),
                max_age=0,
            )
        except OSError:
            return _error("Audio file could not be read", 404)
        if (request.method != "HEAD"
                and response.status_code in (200, 206)):
            _record_playback(song_id, quality)
        return response

    @app.get("/api/setup")
    def api_setup():
        configured = _database_scan_roots(config)
        authenticated = admin_auth.is_authenticated(_admin_token())
        libraries = []
        for item in configured:
            entry = {
                "id": item.get("id"),
                "name": item["name"],
                "identifier": item["identifier"],
                "namespace": item["namespace"],
            }
            if authenticated:
                entry["path"] = item["path"]
            libraries.append(entry)
        return _json_response({
            "name": "Setup",
            "authenticated": authenticated,
            "libraries": libraries,
            "readOnly": config.read_only,
            "urlIdFormat": config.url_id_format,
            "albumSort": config.album_sort,
            "albumSortDescending": config.album_sort_descending,
            "coverSize": config.cover_size,
            "jpegQuality": config.jpeg_quality,
            "scan": _public_job(scans.snapshot(), authenticated),
            "rekey": _public_job(rekeys.snapshot(), authenticated),
            "artworkJob": _public_job(artworks.snapshot(), authenticated),
        })

    @app.get("/api/setup/stats")
    def api_setup_stats():
        authenticated = admin_auth.is_authenticated(_admin_token())
        snapshot = analytics.snapshot()
        online_by_ip = presence.counts_by_ip()
        for client in snapshot["clients"]:
            client["online"] = online_by_ip.get(client["ip"], 0)
        snapshot["totals"]["online"] = sum(online_by_ip.values())
        if not authenticated:
            totals = snapshot["totals"]
            return _json_response({
                "since": snapshot["since"],
                "uptimeSeconds": snapshot["uptimeSeconds"],
                "totals": {
                    key: totals[key]
                    for key in (
                        "visitors",
                        "requests",
                        "bytesSent",
                        "songsPlayed",
                        "uniqueSongs",
                        "playlistActions",
                    )
                },
            })
        return _json_response(snapshot)

    @app.patch("/api/setup/library")
    def api_setup_library():
        unauthorized = _require_admin()
        if unauthorized is not None:
            return unauthorized
        if config.read_only:
            return _error("The server is running in read-only mode", 403)
        if scans.snapshot().get("running"):
            return _error("A scan is already running", 409)
        if rekeys.snapshot().get("running"):
            return _error("A ReKey is already running", 409)
        body = request.get_json(silent=True) or {}
        root = canonical_path(body.get("path") or "")
        identifier = str(body.get("identifier") or "").strip()
        if not identifier:
            return _error("Library identifier must not be empty", 400)
        if any(ord(char) < 32 or ord(char) == 127 for char in identifier):
            return _error(
                "Library identifier contains control characters", 400
            )
        key = normalize_identifier(identifier)
        db = LibraryDB(config.database, read_only=False)
        libraries = db.load_libraries()
        target = next(
            (
                library for library in libraries
                if canonical_path(library["path"]) == root
            ),
            None,
        )
        if target is None:
            return _error("Library not found in the database", 404)
        for library in libraries:
            if canonical_path(library["path"]) == root:
                continue
            for value in (library.get("identifier"),
                          library.get("id_namespace")):
                if value and normalize_identifier(value) == key:
                    return _error(
                        "Library identifier/namespace is already in use", 409
                    )
        try:
            updated = db.set_library_identifier(root, identifier)
        except ValueError as exc:
            return _error(str(exc), 409)
        catalog.reload()
        namespace = normalize_identifier(updated.get("id_namespace") or "")
        _log_operation(
            "library_identifier_changed",
            "success",
            path=target["path"],
            oldIdentifier=target.get("identifier"),
            newIdentifier=identifier,
            rekeyRequired=key != namespace,
        )
        return _json_response({
            "library": {
                "identifier": updated["identifier"],
                "namespace": updated["id_namespace"],
                "path": updated["path"],
            },
            "rekeyRequired": key != namespace,
        })

    @app.post("/api/setup/login")
    def api_setup_login():
        body = request.get_json(silent=True) or {}
        client_ip = _client_ip()
        locked, _retry_after = admin_guard.is_locked(client_ip)
        if locked:
            analytics.record_admin_blocked(client_ip)
            _log_operation("admin_login", "failure", reason="locked")
            return _error("Incorrect admin password", 401)
        token = admin_auth.authenticate(body.get("password"))
        if not token:
            became_locked = admin_guard.record_failure(client_ip)
            if became_locked:
                analytics.record_admin_blocked(client_ip)
            _log_operation(
                "admin_login",
                "failure",
                reason="locked" if became_locked else "invalid_password",
            )
            return _error("Incorrect admin password", 401)
        admin_guard.record_success(client_ip)
        _log_operation("admin_login", "success")
        return _json_response({
            "authenticated": True,
            "token": token,
            "expiresIn": admin_auth.ttl,
        })

    @app.post("/api/setup/logout")
    def api_setup_logout():
        _log_operation("admin_logout", "success")
        admin_auth.revoke(_admin_token())
        return _json_response({"authenticated": False})

    @app.patch("/api/setup/settings")
    def api_setup_settings():
        unauthorized = _require_admin()
        if unauthorized is not None:
            return unauthorized
        body = request.get_json(silent=True) or {}
        updates = {}
        url_format = None
        if "urlIdFormat" in body:
            url_format = str(body.get("urlIdFormat") or "").strip().lower()
            if url_format not in ("uuid", "base64url"):
                return _error("urlIdFormat must be uuid or base64url", 400)
            updates["url_id_format"] = url_format
        album_sort = None
        if "albumSort" in body:
            album_sort = str(body.get("albumSort") or "").strip()
            if album_sort not in ALBUM_SORT_KEYS:
                return _error("albumSort is not supported", 400)
            updates["album_sort"] = album_sort
        album_sort_descending = None
        if "albumSortDescending" in body:
            album_sort_descending = body.get("albumSortDescending")
            if not isinstance(album_sort_descending, bool):
                return _error("albumSortDescending must be a boolean", 400)
            updates["album_sort_descending"] = album_sort_descending
        cover_size = None
        if "coverSize" in body:
            try:
                cover_size = int(body.get("coverSize"))
            except (TypeError, ValueError):
                return _error("coverSize must be an integer", 400)
            if not 16 <= cover_size <= 2000:
                return _error("coverSize must be between 16 and 2000", 400)
            updates["cover_size"] = cover_size
        jpeg_quality = None
        if "jpegQuality" in body:
            try:
                jpeg_quality = int(body.get("jpegQuality"))
            except (TypeError, ValueError):
                return _error("jpegQuality must be an integer", 400)
            if not 15 <= jpeg_quality <= 100:
                return _error("jpegQuality must be between 15 and 100", 400)
            updates["jpeg_quality"] = jpeg_quality
        if not updates:
            return _error("No settings were provided", 400)
        if not config.config_path:
            return _error("The configuration file is not available", 400)

        previous_url_format = config.url_id_format
        previous_album_sort = config.album_sort
        previous_album_sort_descending = config.album_sort_descending
        previous_cover_size = config.cover_size
        previous_jpeg_quality = config.jpeg_quality
        try:
            update_config(config.config_path, **updates)
        except Exception as exc:
            return _error("Could not update config.yaml: %s" % exc, 500)
        if url_format is not None:
            config.url_id_format = url_format
        if album_sort is not None:
            config.album_sort = album_sort
        if album_sort_descending is not None:
            config.album_sort_descending = album_sort_descending
        if cover_size is not None:
            config.cover_size = cover_size
        if jpeg_quality is not None:
            config.jpeg_quality = jpeg_quality
        if url_format is not None:
            _log_operation(
                "url_id_format_changed", "success", actor="admin",
                oldValue=previous_url_format, newValue=url_format,
            )
        if album_sort is not None or album_sort_descending is not None:
            _log_operation(
                "album_sort_changed", "success", actor="admin",
                oldSort=previous_album_sort,
                newSort=config.album_sort,
                oldDescending=previous_album_sort_descending,
                newDescending=config.album_sort_descending,
            )
        if cover_size is not None or jpeg_quality is not None:
            _log_operation(
                "artwork_cache_changed", "success", actor="admin",
                oldCoverSize=previous_cover_size,
                newCoverSize=config.cover_size,
                oldJpegQuality=previous_jpeg_quality,
                newJpegQuality=config.jpeg_quality,
            )
        return _json_response({
            "urlIdFormat": config.url_id_format,
            "albumSort": config.album_sort,
            "albumSortDescending": config.album_sort_descending,
            "coverSize": config.cover_size,
            "jpegQuality": config.jpeg_quality,
        })

    @app.post("/api/setup/password")
    def api_setup_password():
        unauthorized = _require_admin()
        if unauthorized is not None:
            return unauthorized
        body = request.get_json(silent=True) or {}
        password = str(body.get("newPassword") or "")
        if not password:
            return _error("New password must not be empty", 400)
        if len(password) > 1024:
            return _error("New password is too long", 400)
        if not config.config_path:
            return _error("The configuration file is not available", 400)
        digest = hashlib.sha1(password.encode("utf-8")).hexdigest()
        try:
            update_config(
                config.config_path, admin_password_sha1=digest
            )
        except Exception as exc:
            return _error("Could not update config.yaml: %s" % exc, 500)
        config.admin_password_sha1 = digest
        admin_auth.set_password_sha1(digest)
        _log_operation("admin_password_changed", "success", actor="admin")
        return _json_response({
            "changed": True,
            "reauthenticate": True,
        })

    @app.post("/api/setup/scan")
    def api_setup_scan():
        unauthorized = _require_admin()
        if unauthorized is not None:
            return unauthorized
        if rekeys.snapshot().get("running"):
            return _error("A ReKey is already running", 409)
        if artworks.snapshot().get("running"):
            return _error("An artwork rebuild is already running", 409)
        body = request.get_json(silent=True) or {}
        mode = "full" if str(body.get("mode") or "").lower() == "full" else "quick"
        force = (
            bool(body.get("force")) if "force" in body else mode == "full"
        )
        try:
            job = scans.start({
                "mode": mode,
                "force": force,
                "prune": False,
            })
            _log_operation(
                "scan_started", "success", actor="admin",
                mode=mode, force=force
            )
            return _json_response({"job": job}, status=202)
        except ReadOnlyError as exc:
            return _error(str(exc), 403)
        except ValueError as exc:
            return _error(str(exc), 400)
        except RuntimeError as exc:
            return _error(str(exc), 409)

    @app.post("/api/setup/rekey")
    def api_setup_rekey():
        unauthorized = _require_admin()
        if unauthorized is not None:
            return unauthorized
        if scans.snapshot().get("running"):
            return _error("A scan is already running", 409)
        if artworks.snapshot().get("running"):
            return _error("An artwork rebuild is already running", 409)
        try:
            job = rekeys.start()
            _log_operation("rekey_started", "success", actor="admin")
            return _json_response({"job": job}, status=202)
        except ReadOnlyError as exc:
            return _error(str(exc), 403)
        except RuntimeError as exc:
            return _error(str(exc), 409)

    @app.get("/api/setup/rekey")
    def api_setup_rekey_status():
        authenticated = admin_auth.is_authenticated(_admin_token())
        return _json_response(
            _public_job(rekeys.snapshot(), authenticated)
        )

    @app.post("/api/setup/artwork")
    def api_setup_artwork():
        unauthorized = _require_admin()
        if unauthorized is not None:
            return unauthorized
        if scans.snapshot().get("running"):
            return _error("A scan is already running", 409)
        if rekeys.snapshot().get("running"):
            return _error("A ReKey is already running", 409)
        if artworks.snapshot().get("running"):
            return _error("An artwork rebuild is already running", 409)
        try:
            job = artworks.start()
            _log_operation("artwork_rebuild_started", "success", actor="admin")
            return _json_response({"job": job}, status=202)
        except ReadOnlyError as exc:
            return _error(str(exc), 403)
        except RuntimeError as exc:
            return _error(str(exc), 409)

    @app.get("/api/setup/artwork")
    def api_setup_artwork_status():
        authenticated = admin_auth.is_authenticated(_admin_token())
        return _json_response(
            _public_job(artworks.snapshot(), authenticated)
        )

    @app.get("/api/scan")
    @app.get("/api/scan/status")
    def api_scan_status():
        authenticated = admin_auth.is_authenticated(_admin_token())
        return _json_response(_public_job(scans.snapshot(), authenticated))

    @app.post("/api/scan")
    def api_scan_start():
        unauthorized = _require_admin()
        if unauthorized is not None:
            return unauthorized
        if rekeys.snapshot().get("running"):
            return _error("A ReKey is already running", 409)
        if artworks.snapshot().get("running"):
            return _error("An artwork rebuild is already running", 409)
        body = request.get_json(silent=True) or {}
        try:
            job = scans.start({
                "mode": body.get("mode"),
                "force": bool(body.get("force")),
                "prune": bool(body.get("prune")),
            })
            _log_operation(
                "scan_started",
                "success",
                actor="admin",
                mode=body.get("mode") or "quick",
                force=bool(body.get("force")),
            )
            return _json_response({"job": job}, status=202)
        except ReadOnlyError as exc:
            return _error(str(exc), 403)
        except (ValueError, KeyError) as exc:
            return _error(str(exc), 400)
        except RuntimeError as exc:
            return _error(str(exc), 409)

    @app.post("/api/status")
    def api_status():
        online = presence.touch(_client_ip())
        scan = scans.snapshot()
        rekey = rekeys.snapshot()
        artwork = artworks.snapshot()
        if rekey.get("running"):
            state = "rekeying"
            job = rekey
        elif scan.get("running"):
            state = "scanning"
            job = scan
        elif artwork.get("running"):
            state = "artwork"
            job = artwork
        else:
            state = "ready"
            job = None
        authenticated = admin_auth.is_authenticated(_admin_token())
        public_job = _public_job(job, authenticated)
        return _json_response({
            "online": online,
            "state": state,
            "phase": public_job.get("phase") or "",
            "completed": int(public_job.get("completed") or 0),
            "total": public_job.get("total"),
            "message": public_job.get("message") or "",
            "cache": transcode_cache.stats(),
        })

    @app.get("/api/health")
    def api_health():
        return _json_response({
            "ok": True,
            "available": catalog.available,
            "songs": len(catalog.all_songs),
            "readOnly": config.read_only,
        })

    @app.errorhandler(404)
    def not_found(error):
        if request.path.startswith("/api/"):
            return _error("Not found", 404)
        if request.path.startswith("/static/"):
            return _error("Static file not found", 404)
        return send_from_directory(app.static_folder, "index.html", max_age=0)

    @app.errorhandler(413)
    def too_large(error):
        return _error("Request is too large", 413)

    return app


# ---------------------------------------------------------------------------
# Command line


def _cli_progress():
    last = {"phase": None, "time": 0.0}

    def progress(phase, completed, total, message):
        now = time.time()
        if (
            phase != last["phase"]
            or now - last["time"] > 0.5
            or (total is not None and completed >= total)
        ):
            last["phase"] = phase
            last["time"] = now
            total_text = "/%d" % total if total else ""
            suffix = "  %s" % message if message else ""
            sys.stderr.write(
                "\r%-22s %s%s%s" % (
                    phase, completed, total_text, suffix
                )
            )
            sys.stderr.flush()

    return progress


def _request_handler(server_header):
    """Build a Werkzeug request handler with a configurable Server header."""

    class Handler(WSGIRequestHandler):
        def version_string(self):
            return server_header or ""

        def send_response(self, code, message=None):
            self.log_request(code)
            self.send_response_only(code, message)
            if server_header:
                self.send_header("Server", server_header)
            self.send_header("Date", self.date_time_string())

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="tracks-web",
        description="Local-first web player with its own Tracks-style library",
    )
    parser.add_argument(
        "command", nargs="?", default="serve",
        choices=["serve", "scan", "init"],
        help="serve the web player (default), generate/update a library, "
             "or create an empty local database",
    )
    parser.add_argument(
        "--config", default=os.environ.get("WEBTRACKS_CONFIG",
                                          DEFAULT_CONFIG_PATH),
        help="path to config.yaml (default: next to server.py)",
    )
    parser.add_argument(
        "--database", default=None,
        help="override the database path (default: <database_dir>/library.sqlite)",
    )
    parser.add_argument(
        "--artwork", default=None,
        help="override the artwork cache directory "
             "(default: <database_dir>/Artwork)",
    )
    parser.add_argument(
        "--read-only", action="store_true", default=None,
        help="serve without writing stars, play counts or playlists",
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--root", help="music folder to scan (scan command)")
    parser.add_argument(
        "--identifier", help="library identifier for a new folder",
    )
    parser.add_argument(
        "--mode", choices=["quick", "full"], default="quick",
        help="quick discovers new paths; full refreshes changed files",
    )
    parser.add_argument(
        "--prune", action="store_true",
        help="remove indexed tracks whose files no longer exist",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="re-read every file, including unchanged ones",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="scan every enabled library (default when --root is omitted)",
    )
    parser.add_argument(
        "--jpeg-quality", type=int, default=None,
        help="artwork cache JPEG quality (15-100)",
    )
    parser.add_argument(
        "--cover-size", type=int, default=None,
        help="maximum artwork cache width/height in pixels (16-2000)",
    )
    parser.add_argument(
        "--transcode-cache-mb", dest="transcode_cache_mb", type=int,
        default=None,
        help="in-memory transcode cache size in MiB (default 512)",
    )
    parser.add_argument(
        "--aac-cache-mb", dest="transcode_cache_mb", type=int,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    config_path = ensure_config_file(args.config)
    try:
        settings = load_settings(config_path)
    except ValueError as exc:
        parser.error(str(exc))

    database = args.database or settings["database"]
    artwork = args.artwork or settings["artwork"]
    config = Config(
        database,
        artwork,
        read_only=(
            settings["read_only"] if args.read_only is None
            else args.read_only
        ),
        jpeg_quality=(
            settings["jpeg_quality"] if args.jpeg_quality is None
            else args.jpeg_quality
        ),
        cover_size=(
            settings["cover_size"] if args.cover_size is None
            else args.cover_size
        ),
        music_dirs=settings["music_dirs"],
        libraries=settings["libraries"],
        config_path=config_path,
        transcode_cache_mb=(
            settings["transcode_cache_mb"]
            if args.transcode_cache_mb is None
            else args.transcode_cache_mb
        ),
        audio_formats=settings["audio_formats"],
        default_audio_format=settings["default_audio_format"],
        server_header=settings["server_header"],
        trusted_hosts=settings["trusted_hosts"],
        album_sort=settings["album_sort"],
        album_sort_descending=settings["album_sort_descending"],
        admin_password_sha1=settings["admin_password_sha1"],
        admin_bruteforce_limit=settings["admin_bruteforce_limit"],
        admin_bruteforce_window_seconds=settings[
            "admin_bruteforce_window_seconds"
        ],
        admin_bruteforce_lock_seconds=settings[
            "admin_bruteforce_lock_seconds"
        ],
        logs_dir=settings["logs_dir"],
        log_level=settings["log_level"],
        log_format=settings["log_format"],
        rate_limit=settings["rate_limit"],
        url_id_format=settings["url_id_format"],
    )
    host = args.host or settings["host"]
    port = args.port or settings["port"]

    if args.command == "init":
        if config.read_only:
            parser.error("--read-only cannot be combined with the init command")
        LibraryDB(config.database).ensure_schema()
        os.makedirs(config.artwork, exist_ok=True)
        print("WebTracks")
        print("  config:   %s" % config.config_path)
        print("  database: %s" % config.database)
        print("  artwork:  %s (%dpx, JPEG %d)" % (
            config.artwork, config.cover_size, config.jpeg_quality
        ))
        print("Created an empty library. Add a music folder with:")
        print('  python3 server.py scan --root "/path/to/Music"')
        return 0

    if args.command == "scan":
        if config.read_only:
            parser.error("--read-only cannot be combined with the scan command")
        db = LibraryDB(config.database, read_only=False)
        progress = _cli_progress()
        roots = []
        if args.root:
            roots.append({
                "root": args.root,
                "identifier": args.identifier,
            })
        else:
            _seed_library_paths(config, db)
            roots = _database_scan_roots(config, db)
            if not roots:
                parser.error(
                    "no libraries are in the database; add one with "
                    "`python3 server.py scan --root PATH`"
                )
        summaries = []
        try:
            for entry in roots:
                result = scan_one(
                    db,
                    entry["root"],
                    identifier=entry.get("identifier"),
                    mode=args.mode,
                    prune=args.prune,
                    artwork_dir=config.artwork,
                    jpeg_quality=config.jpeg_quality,
                    cover_size=config.cover_size,
                    force=args.force,
                    progress=progress,
                )
                summaries.append(result)
                sys.stderr.write("\n")
                print(
                    "Scanned %(root)s: %(tracks)d tracks, "
                    "%(artwork)d covers, %(pending)d pending "
                    "(%(discovered)d files)" % result
                )
                for error in result.get("errors") or []:
                    print("  warning: %s" % error, file=sys.stderr)
        except KeyboardInterrupt:
            sys.stderr.write("\nScan cancelled.\n")
            return 130
        except Exception as exc:
            sys.stderr.write("\nScan failed: %s\n" % exc)
            return 1
        total_errors = sum(len(s.get("errors") or []) for s in summaries)
        print(
            "Done: %d tracks read, %d covers written, %d warnings." % (
                sum(s["tracks"] for s in summaries),
                sum(s["artwork"] for s in summaries),
                total_errors,
            )
        )
        return 0

    if not config.read_only:
        LibraryDB(config.database).ensure_schema()
        os.makedirs(config.artwork, exist_ok=True)

    if not shutil.which("ffprobe"):
        print(
            "warning: ffprobe was not found; only FLAC metadata can be read "
            "until ffmpeg is installed.",
            file=sys.stderr,
        )
    if config.logs_dir:
        configure_logging(
            config.logs_dir, config.log_level, config.log_format
        )
        runtime_log.info(
            "WebTracks starting; config=%s database=%s artwork=%s logs=%s",
            config.config_path, config.database, config.artwork,
            config.logs_dir,
        )
    app = create_app(config)
    print("WebTracks")
    print("  config:   %s" % config.config_path)
    print("  database: %s" % config.database)
    print("  artwork:  %s (%dpx, JPEG %d)" % (
        config.artwork, config.cover_size, config.jpeg_quality
    ))
    try:
        libraries = LibraryDB(
            config.database, read_only=True
        ).load_libraries()
    except Exception:
        libraries = []
    for library in libraries:
        print("  library:  %s [%s] %s" % (
            library.get("identifier") or "?",
            library.get("id_namespace") or "?",
            library.get("path"),
        ))
    print("  mode:     %s" % (
        "read-only" if config.read_only else "read/write"
    ))
    print("  transcode cache: %d MiB (memory)" % config.transcode_cache_mb)
    if config.logs_dir:
        print("  logs:     %s (%s, %s)" % (
            config.logs_dir, config.log_level, config.log_format
        ))
    print("  rate:     %s" % (
        "%d items/min/IP" % config.rate_limit
        if config.rate_limit else "off"
    ))
    print("  listening: http://%s:%d" % (host, port))
    app.run(
        host=host,
        port=port,
        debug=args.debug,
        threaded=True,
        use_reloader=False,
        request_handler=_request_handler(config.server_header),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
