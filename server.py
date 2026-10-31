#!/usr/bin/env python3
"""WebTracks: a local-first web music player for a Tracks-style library.

The SQLite schema and artwork cache layout match the macOS Tracks app, but the
database and cache belong to this project.  They are created under ``data/``
next to ``server.py`` (see ``config.json``) and are never shared with the
native app.  All web assets are served locally; the page makes no external
requests.
"""

import argparse
import gzip
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import sys
import threading
import time

from flask import Flask, Response, request, send_file, send_from_directory

from tracks_transcode import (
    DEFAULT_CACHE_MB,
    DEFAULT_QUALITY,
    QUALITIES,
    QUALITY_BY_ID,
    TranscodeCache,
    TranscodeError,
)
from tracks_config import (
    DEFAULT_ADMIN_PASSWORD_SHA1,
    DEFAULT_CONFIG_PATH,
    ensure_config_file,
    load_settings,
    save_libraries_to_config,
)
from tracks_db import (
    LibraryCatalog,
    LibraryDB,
    ReadOnlyError,
)
from tracks_identity import canonical_path
from tracks_scanner import DEFAULT_JPEG_QUALITY, scan_one

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
                 music_dirs=None, config_path=None, transcode_cache_mb=None,
                 album_sort=None, album_sort_descending=None,
                 admin_password_sha1=None, libraries=None):
        self.database = os.path.abspath(os.path.expanduser(database))
        self.artwork = os.path.abspath(os.path.expanduser(artwork))
        self.read_only = bool(read_only)
        self.jpeg_quality = int(
            jpeg_quality or DEFAULT_JPEG_QUALITY
        )
        self.jpeg_quality = max(15, min(100, self.jpeg_quality))
        if libraries is None:
            libraries = [
                {"path": path} for path in (music_dirs or [])
            ]
        self.libraries = []
        for library in libraries:
            self.libraries.append({
                "identifier": library.get("identifier"),
                "namespace": library.get("namespace"),
                "path": os.path.realpath(os.path.expanduser(library["path"])),
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
        self.album_sort = album_sort or "recentlyAdded"
        self.album_sort_descending = (
            True if album_sort_descending is None
            else bool(album_sort_descending)
        )
        self.admin_password_sha1 = str(
            admin_password_sha1 or DEFAULT_ADMIN_PASSWORD_SHA1
        ).strip().lower()

    @classmethod
    def from_settings(cls, settings):
        return cls(
            settings["database"],
            settings["artwork"],
            read_only=settings.get("read_only", False),
            jpeg_quality=settings.get("jpeg_quality"),
            music_dirs=settings.get("music_dirs") or [],
            config_path=settings.get("config_path"),
            transcode_cache_mb=settings.get("transcode_cache_mb"),
            libraries=settings.get("libraries") or [],
            album_sort=settings.get("album_sort"),
            album_sort_descending=settings.get("album_sort_descending"),
            admin_password_sha1=settings.get("admin_password_sha1"),
        )


class PresenceTracker:
    """Approximate count of open web clients using a heartbeat TTL."""

    def __init__(self, ttl=30):
        self.ttl = max(5, int(ttl))
        self._clients = {}
        self._lock = threading.Lock()

    def touch(self, client_id=None):
        now = time.time()
        with self._lock:
            self._clients = {
                key: seen for key, seen in self._clients.items()
                if now - seen <= self.ttl
            }
            client_id = str(client_id or "").strip()[:128]
            if client_id:
                self._clients[client_id] = now
            return len(self._clients)

    def count(self):
        return self.touch(None)


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


def _configured_scan_roots(config, db=None):
    """Return only libraries explicitly listed in config.libraries."""

    libraries = config.libraries or []
    if not libraries:
        return []
    if db is None:
        db = LibraryDB(config.database, read_only=True)
    stored = {
        canonical_path(library["path"]): library
        for library in db.load_libraries()
    }
    roots = []
    for configured in libraries:
        root = canonical_path(configured["path"])
        library = stored.get(root) or {}
        roots.append({
            "root": root,
            "path": root,
            "name": library.get("name") or os.path.basename(root),
            "identifier": (configured.get("identifier")
                           or library.get("identifier")),
            "namespace": (configured.get("namespace")
                          or library.get("id_namespace")),
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
        if self.config.libraries:
            db.sync_libraries(self.config.libraries)
        mode = payload.get("mode") or "quick"
        if mode not in ("quick", "full"):
            mode = "quick"
        prune = bool(payload.get("prune"))
        force = bool(payload.get("force"))
        roots = _configured_scan_roots(self.config, db)
        if not roots:
            with self.lock:
                self.job["running"] = False
                self.job["phase"] = "Error"
                self.job["message"] = "No libraries configured"
            raise ValueError(
                "No libraries are configured. Add them to "
                "libraries in config.json."
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
            except Exception as exc:
                errors.append(str(exc))
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
            synced = db.sync_libraries(self.config.libraries)
            stored_by_path = {
                canonical_path(library["path"]): library
                for library in synced
            }
            identifiers_by_path = {}
            config_entries = []
            for library in self.config.libraries:
                path = canonical_path(library["path"])
                stored = stored_by_path.get(path, {})
                identifier = library.get("identifier") or stored.get("identifier")
                if identifier is None:
                    raise ValueError(
                        "Library identifier is missing: %s" % path
                    )
                identifiers_by_path[path] = identifier
                config_entries.append({
                    "identifier": identifier,
                    "namespace": (library.get("namespace")
                                  or stored.get("id_namespace")),
                    "path": path,
                })
            self._update(
                phase="Rekeying database",
                message="Recalculating song, album, and artist IDs",
            )
            result = db.rekey_libraries(identifiers_by_path)
            namespaces = result.get("namespaceByPath") or {}
            updated_entries = []
            for entry in config_entries:
                updated_entries.append({
                    "identifier": entry["identifier"],
                    "namespace": namespaces.get(
                        entry["path"], entry["namespace"]
                    ),
                    "path": entry["path"],
                })
            if self.config.config_path and updated_entries:
                self._update(
                    phase="Updating configuration",
                    message="Writing the new library namespaces to config.json",
                )
                save_libraries_to_config(
                    self.config.config_path, updated_entries
                )
                self.config.libraries = updated_entries
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
        except Exception as exc:
            errors.append(str(exc))
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
    catalog = LibraryCatalog(config.database, read_only=config.read_only)
    if not config.read_only and config.libraries:
        catalog.db.sync_libraries(config.libraries)
        catalog.reload()
    scans = ScanManager(config, catalog)
    rekeys = RekeyManager(config, catalog)
    admin_auth = AdminAuth(config.admin_password_sha1)
    presence = PresenceTracker()
    transcode_cache = TranscodeCache(
        config.transcode_cache_mb * 1024 * 1024
    )
    app.extensions["tracks_catalog"] = catalog
    app.extensions["tracks_config"] = config
    app.extensions["tracks_scans"] = scans
    app.extensions["tracks_rekeys"] = rekeys
    app.extensions["tracks_admin_auth"] = admin_auth
    app.extensions["tracks_presence"] = presence
    app.extensions["tracks_transcode_cache"] = transcode_cache

    def _admin_token():
        header = (request.headers.get("Authorization") or "").strip()
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return (request.headers.get("X-Admin-Token") or "").strip()

    def _require_admin():
        if admin_auth.is_authenticated(_admin_token()):
            return None
        return _error("Admin login required", 401)

    @app.after_request
    def security_headers(response):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; media-src 'self'; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; font-src 'self'; object-src 'none'; "
            "base-uri 'self'; form-action 'self'",
        )
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html", max_age=0)

    @app.get("/api/bootstrap")
    def api_bootstrap():
        try:
            payload = catalog.bootstrap_payload()
            payload["musicDirs"] = list(config.music_dirs)
            payload["database"] = config.database
            payload["albumSort"] = config.album_sort
            payload["albumSortDescending"] = config.album_sort_descending
            return _json_response(payload)
        except Exception as exc:
            return _error("Could not read the library: %s" % exc, 500)

    @app.get("/api/songs")
    def api_songs():
        scope = request.args.get("scope", "all")
        try:
            return _json_response(catalog.songs_payload(scope))
        except Exception as exc:
            return _error("Could not read songs: %s" % exc, 500)

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
            return _json_response({"playlist": playlist}, status=201)
        except ReadOnlyError as exc:
            return _error(str(exc), 403)
        except ValueError as exc:
            return _error(str(exc), 400)

    @app.patch("/api/playlists/<playlist_id>")
    def api_rename_playlist(playlist_id):
        body = request.get_json(silent=True) or {}
        try:
            playlist = catalog.rename_playlist(
                playlist_id, body.get("name", "")
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
        try:
            catalog.delete_playlist(playlist_id)
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

    @app.post("/api/songs/<song_id>/played")
    def api_played(song_id):
        try:
            plays = catalog.record_play(song_id)
            return _json_response({"id": song_id, "plays": plays})
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
            "default": DEFAULT_QUALITY,
            "qualities": [
                {
                    key: quality[key]
                    for key in ("id", "label", "detail", "bitrate", "mime")
                }
                for quality in QUALITIES
            ],
        })

    @app.get("/api/cache")
    def api_cache():
        return _json_response(transcode_cache.stats())

    @app.get("/api/audio/<song_id>")
    def api_audio(song_id):
        song = catalog.song_internal(song_id)
        if song is None:
            return _error("Song not found", 404)
        path = song.get("_path") or ""
        if not os.path.isfile(path) or not os.access(path, os.R_OK):
            return _error("Audio file is missing", 404)

        quality = (request.args.get("quality") or "original").strip()
        spec = QUALITY_BY_ID.get(quality)
        if spec is not None:
            try:
                data, etag = transcode_cache.get_or_encode(
                    song_id, path, quality
                )
            except FileNotFoundError:
                return _error("Audio file disappeared while encoding", 404)
            except (TranscodeError, OSError) as exc:
                return _error("Could not transcode audio: %s" % exc, 500)
            return _byte_range_response(data, spec["mime"], etag)
        if quality not in ("", "original"):
            return _error("Unsupported audio quality", 400)

        mime = AUDIO_TYPES.get(
            os.path.splitext(path)[1].lower(),
            mimetypes.guess_type(path)[0] or "application/octet-stream",
        )
        try:
            return send_file(
                path,
                mimetype=mime,
                conditional=True,
                as_attachment=False,
                download_name=os.path.basename(path),
                max_age=0,
            )
        except OSError:
            return _error("Audio file could not be read", 404)

    @app.get("/api/setup")
    def api_setup():
        configured = _configured_scan_roots(config)
        return _json_response({
            "name": "Setup",
            "authenticated": admin_auth.is_authenticated(_admin_token()),
            "libraries": [
                {
                    "name": item["name"],
                    "path": item["path"],
                    "identifier": item["identifier"],
                    "namespace": item["namespace"],
                }
                for item in configured
            ],
            "readOnly": config.read_only,
            "scan": scans.snapshot(),
            "rekey": rekeys.snapshot(),
        })

    @app.post("/api/setup/login")
    def api_setup_login():
        body = request.get_json(silent=True) or {}
        token = admin_auth.authenticate(body.get("password"))
        if not token:
            return _error("Incorrect admin password", 401)
        return _json_response({
            "authenticated": True,
            "token": token,
            "expiresIn": admin_auth.ttl,
        })

    @app.post("/api/setup/logout")
    def api_setup_logout():
        admin_auth.revoke(_admin_token())
        return _json_response({"authenticated": False})

    @app.post("/api/setup/scan")
    def api_setup_scan():
        unauthorized = _require_admin()
        if unauthorized is not None:
            return unauthorized
        if rekeys.snapshot().get("running"):
            return _error("A ReKey is already running", 409)
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
        try:
            job = rekeys.start()
            return _json_response({"job": job}, status=202)
        except ReadOnlyError as exc:
            return _error(str(exc), 403)
        except RuntimeError as exc:
            return _error(str(exc), 409)

    @app.get("/api/setup/rekey")
    def api_setup_rekey_status():
        return _json_response(rekeys.snapshot())

    @app.get("/api/scan")
    @app.get("/api/scan/status")
    def api_scan_status():
        return _json_response(scans.snapshot())

    @app.post("/api/scan")
    def api_scan_start():
        unauthorized = _require_admin()
        if unauthorized is not None:
            return unauthorized
        if rekeys.snapshot().get("running"):
            return _error("A ReKey is already running", 409)
        body = request.get_json(silent=True) or {}
        try:
            job = scans.start({
                "mode": body.get("mode"),
                "force": bool(body.get("force")),
                "prune": bool(body.get("prune")),
            })
            return _json_response({"job": job}, status=202)
        except ReadOnlyError as exc:
            return _error(str(exc), 403)
        except (ValueError, KeyError) as exc:
            return _error(str(exc), 400)
        except RuntimeError as exc:
            return _error(str(exc), 409)

    @app.post("/api/reload")
    def api_reload():
        catalog.reload()
        return _json_response({"reloaded": True})

    @app.post("/api/status")
    def api_status():
        body = request.get_json(silent=True) or {}
        online = presence.touch(body.get("client"))
        scan = scans.snapshot()
        rekey = rekeys.snapshot()
        if rekey.get("running"):
            state = "rekeying"
            job = rekey
        elif scan.get("running"):
            state = "scanning"
            job = scan
        else:
            state = "ready"
            job = None
        return _json_response({
            "online": online,
            "state": state,
            "phase": (job or {}).get("phase") or "",
            "completed": int((job or {}).get("completed") or 0),
            "total": (job or {}).get("total"),
            "message": (job or {}).get("message") or "",
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
        help="path to config.json (default: next to server.py)",
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
        music_dirs=settings["music_dirs"],
        libraries=settings["libraries"],
        config_path=config_path,
        transcode_cache_mb=(
            settings["transcode_cache_mb"]
            if args.transcode_cache_mb is None
            else args.transcode_cache_mb
        ),
        album_sort=settings["album_sort"],
        album_sort_descending=settings["album_sort_descending"],
        admin_password_sha1=settings["admin_password_sha1"],
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
        print("  artwork:  %s" % config.artwork)
        print("Created an empty library. Add libraries to "
              "config.json, then run:")
        print("  python3 server.py scan")
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
            if not config.libraries:
                parser.error(
                    "no libraries configured; add them to libraries in %s"
                    % config.config_path
                )
            db.sync_libraries(config.libraries)
            roots = _configured_scan_roots(config, db)
            if not roots:
                parser.error(
                    "no libraries configured; add them to libraries in %s"
                    % config.config_path
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
    app = create_app(config)
    print("WebTracks")
    print("  config:   %s" % config.config_path)
    print("  database: %s" % config.database)
    print("  artwork:  %s" % config.artwork)
    if config.libraries:
        for library in config.libraries:
            print("  library:  %s [%s] %s" % (
                library.get("identifier") or "?",
                library.get("namespace") or "?",
                library.get("path"),
            ))
    print("  mode:     %s" % (
        "read-only" if config.read_only else "read/write"
    ))
    print("  transcode cache: %d MiB (memory)" % config.transcode_cache_mb)
    print("  listening: http://%s:%d" % (host, port))
    app.run(
        host=host,
        port=port,
        debug=args.debug,
        threaded=True,
        use_reloader=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
