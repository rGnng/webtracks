"""SQLite storage compatible with the Tracks macOS application.

Only the schema owned by Tracks is used.  The web player keeps its own
``library.sqlite`` (by default ``data/library.sqlite`` next to ``server.py``)
and writes only the metadata Tracks itself writes (stars, play counts and
playlists when not in read-only mode).  Library files are never modified.
"""

import os
import sqlite3
import threading
import time
import unicodedata
import uuid

from tracks_identity import (
    album_uuid,
    artist_uuid,
    canonical_path,
    is_under,
    normalize_identifier,
    relative_path,
    song_uuid,
    stable_uuid,
    swift_lower,
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS artists (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS albums (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    artist_id TEXT NOT NULL,
    genre TEXT NOT NULL DEFAULT '',
    year TEXT NOT NULL DEFAULT '',
    artwork BLOB,
    UNIQUE(title, artist_id)
);
CREATE TABLE IF NOT EXISTS songs (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    library_id TEXT,
    title TEXT NOT NULL,
    artist_id TEXT NOT NULL,
    album_id TEXT NOT NULL,
    genre TEXT NOT NULL DEFAULT '',
    year TEXT NOT NULL DEFAULT '',
    track INTEGER NOT NULL DEFAULT 0,
    track_total INTEGER NOT NULL DEFAULT 0,
    disc INTEGER NOT NULL DEFAULT 0,
    disc_total INTEGER NOT NULL DEFAULT 0,
    comments TEXT NOT NULL DEFAULT '',
    duration REAL NOT NULL DEFAULT 0,
    date_added REAL NOT NULL,
    play_count INTEGER NOT NULL DEFAULT 0,
    rating INTEGER NOT NULL DEFAULT 0,
    loved INTEGER NOT NULL DEFAULT 0,
    last_played REAL,
    skip_count INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS libraries (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'external',
    name TEXT NOT NULL,
    identifier TEXT NOT NULL DEFAULT '',
    id_namespace TEXT NOT NULL DEFAULT '',
    date_added REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS playlists (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    date_created REAL NOT NULL,
    date_modified REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS playlist_items (
    playlist_id TEXT NOT NULL,
    song_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    added_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (playlist_id, song_id)
);
CREATE TABLE IF NOT EXISTS player_queue (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    queue_entries TEXT NOT NULL DEFAULT '[]',
    base_queue_entries TEXT NOT NULL DEFAULT '[]',
    next_queue_id INTEGER NOT NULL DEFAULT 1,
    current_index INTEGER NOT NULL DEFAULT -1,
    elapsed REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_songs_album ON songs(album_id);
CREATE INDEX IF NOT EXISTS idx_songs_artist ON songs(artist_id);
CREATE INDEX IF NOT EXISTS idx_songs_library ON songs(library_id);
CREATE INDEX IF NOT EXISTS idx_playlist_items ON playlist_items(playlist_id, position);
CREATE UNIQUE INDEX IF NOT EXISTS idx_libraries_identifier
    ON libraries(identifier COLLATE NOCASE);
"""

REQUIRED_TABLES = {
    "artists", "albums", "songs", "libraries", "playlists", "playlist_items",
}


class ReadOnlyError(RuntimeError):
    pass


def connect(path, read_only=False):
    if read_only and not os.path.exists(path):
        raise FileNotFoundError(path)
    if not read_only:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    if read_only:
        conn.execute("PRAGMA query_only=ON")
    else:
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _now():
    return time.time()


def _has_schema(conn):
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    except sqlite3.DatabaseError:
        return False
    return REQUIRED_TABLES.issubset({row[0] for row in rows})


def _looks_like_library_number(value):
    value = normalize_identifier(value)
    if not value.startswith("library"):
        return None
    suffix = value[len("library"):]
    if not suffix or not suffix.isdigit():
        return None
    return int(suffix)


class LibraryDB:
    """Low-level access to a Tracks database."""

    def __init__(self, path, read_only=False):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.read_only = bool(read_only)

    def ensure_schema(self):
        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        conn = connect(self.path, read_only=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA_SQL)
            conn.commit()
        finally:
            conn.close()

    def check_schema(self):
        if not os.path.exists(self.path):
            return False
        try:
            conn = connect(self.path, read_only=True)
            try:
                return _has_schema(conn)
            finally:
                conn.close()
        except (sqlite3.DatabaseError, FileNotFoundError):
            return False

    # ------------------------------------------------------------------
    # Libraries

    def load_libraries(self):
        if not os.path.exists(self.path):
            return []
        try:
            conn = connect(self.path, read_only=True)
            try:
                rows = conn.execute(
                    "SELECT id, path, name, identifier, id_namespace, enabled, "
                    "date_added FROM libraries ORDER BY "
                    "identifier COLLATE NOCASE, name COLLATE NOCASE"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            return []
        return [
            {
                "id": row["id"],
                "path": row["path"],
                "name": row["name"],
                "identifier": row["identifier"],
                "id_namespace": row["id_namespace"],
                "enabled": bool(row["enabled"]),
                "date_added": float(row["date_added"] or 0),
            }
            for row in rows
        ]

    def _next_identifier(self, conn):
        used = set()
        highest = 0
        for row in conn.execute("SELECT identifier, id_namespace FROM libraries"):
            for value in (row["identifier"], row["id_namespace"]):
                value = normalize_identifier(value or "")
                if not value:
                    continue
                used.add(value)
                number = _looks_like_library_number(value)
                if number is not None:
                    highest = max(highest, number)
        number = highest + 1
        while str(number) and ("library%d" % number) in used:
            number += 1
        return "library%d" % number

    def backup(self, label="backup"):
        """Write a timestamped SQLite backup next to the live database."""

        if not os.path.exists(self.path):
            raise FileNotFoundError(self.path)
        stamp = int(time.time() * 1000)
        backup_path = "%s.%s-%d.sqlite" % (self.path, label, stamp)
        source = connect(self.path, read_only=True)
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return backup_path

    def ensure_library(self, root, identifier=None):
        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        root = canonical_path(root)
        if not os.path.isdir(root):
            raise ValueError("Music folder does not exist: %s" % root)
        if not os.access(root, os.R_OK):
            raise ValueError("Music folder is not readable: %s" % root)

        self.ensure_schema()
        conn = connect(self.path, read_only=False)
        try:
            existing = conn.execute(
                "SELECT id, path, name, identifier, id_namespace, enabled, "
                "date_added FROM libraries WHERE path = ?",
                (root,),
            ).fetchone()
            if existing:
                return self._library_row(existing)

            libraries = conn.execute(
                "SELECT id, identifier, id_namespace FROM libraries"
            ).fetchall()

            if identifier is None or not str(identifier).strip():
                identifier = self._next_identifier(conn)
            identifier = str(identifier).strip()
            if any(unicodedata.category(ch) == "Cc" for ch in identifier):
                raise ValueError("Library identifier contains control characters")
            namespace = normalize_identifier(identifier)
            if not namespace:
                raise ValueError("Library identifier must not be empty")
            for row in libraries:
                for value in (row["identifier"], row["id_namespace"]):
                    if normalize_identifier(value or "") == namespace:
                        raise ValueError(
                            "Library identifier is already in use: %s" % identifier
                        )

            name = os.path.basename(root) or "Music Library"
            now = _now()
            conn.execute(
                "INSERT INTO libraries"
                "(id, path, kind, name, identifier, id_namespace, date_added)"
                " VALUES(?, ?, 'external', ?, ?, ?, ?)",
                (stable_uuid("library|" + root), root, name,
                 identifier, namespace, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id, path, name, identifier, id_namespace, enabled, "
                "date_added FROM libraries WHERE path = ?",
                (root,),
            ).fetchone()
            return self._library_row(row)
        finally:
            conn.close()

    def set_library_identifier(self, root, identifier):
        """Update a library's identifier without changing its namespace.

        The namespace is the snapshot used for song UUID generation.  It stays
        unchanged until ReKey recalculates it from the new identifier.
        """

        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        root = canonical_path(root)
        identifier = str(identifier or "").strip()
        if any(unicodedata.category(char) == "Cc" for char in identifier):
            raise ValueError("Library identifier contains control characters")
        namespace = normalize_identifier(identifier)
        if not namespace:
            raise ValueError("Library identifier must not be empty")

        self.ensure_schema()
        conn = connect(self.path, read_only=False)
        try:
            existing = conn.execute(
                "SELECT id, path, name, identifier, id_namespace, enabled, "
                "date_added FROM libraries WHERE path = ?",
                (root,),
            ).fetchone()
            if existing is None:
                raise ValueError("Library not found: %s" % root)
            for row in conn.execute(
                "SELECT id, path, identifier, id_namespace FROM libraries"
            ):
                if row["id"] == existing["id"]:
                    continue
                for value in (row["identifier"], row["id_namespace"]):
                    if normalize_identifier(value or "") == namespace:
                        raise ValueError(
                            "Library identifier/namespace is already in use: %s"
                            % identifier
                        )
            with conn:
                conn.execute(
                    "UPDATE libraries SET identifier = ? WHERE id = ?",
                    (identifier, existing["id"]),
                )
            row = conn.execute(
                "SELECT id, path, name, identifier, id_namespace, enabled, "
                "date_added FROM libraries WHERE id = ?",
                (existing["id"],),
            ).fetchone()
            return self._library_row(row)
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                "Library identifier or namespace is already in use"
            ) from exc
        finally:
            conn.close()

    def relocate_library(self, root, new_root):
        """Move a library to a new root while preserving indexed metadata.

        Every song beneath ``root`` is matched by its relative path under
        ``new_root``.  Missing files are kept (their paths are still remapped)
        so stars, play counts, and playlist entries survive until a prune scan.
        """

        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        root = canonical_path(root)
        new_root = canonical_path(new_root)
        if not os.path.isdir(new_root):
            raise ValueError("Music folder does not exist: %s" % new_root)
        if not os.access(new_root, os.R_OK):
            raise ValueError("Music folder is not readable: %s" % new_root)

        self.ensure_schema()
        conn = connect(self.path, read_only=False)
        try:
            existing = conn.execute(
                "SELECT id, path, name, identifier, id_namespace, enabled, "
                "date_added FROM libraries WHERE path = ?",
                (root,),
            ).fetchone()
            if existing is None:
                raise ValueError("Library not found: %s" % root)

            songs = conn.execute(
                "SELECT id, path FROM songs WHERE library_id = ?",
                (existing["id"],),
            ).fetchall()
            updates = []
            for song in songs:
                relative = relative_path(song["path"], root)
                if relative is None:
                    continue
                candidate = (
                    canonical_path(os.path.join(new_root, relative))
                    if relative else new_root
                )
                updates.append((song["id"], candidate))

            name = os.path.basename(new_root) or "Music Library"
            with conn:
                conn.execute(
                    "UPDATE libraries SET path = ?, name = ? WHERE id = ?",
                    (new_root, name, existing["id"]),
                )
                for song_id, path in updates:
                    conn.execute(
                        "UPDATE songs SET path = ? WHERE id = ? AND library_id = ?",
                        (path, song_id, existing["id"]),
                    )
            row = conn.execute(
                "SELECT id, path, name, identifier, id_namespace, enabled, "
                "date_added FROM libraries WHERE id = ?",
                (existing["id"],),
            ).fetchone()
            missing = sum(
                1 for _, path in updates if not os.path.isfile(path)
            )
            return {
                "library": self._library_row(row),
                "moved": len(updates),
                "total": len(songs),
                "missing": missing,
            }
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                "The new location conflicts with an existing song path"
            ) from exc
        finally:
            conn.close()

    def sync_libraries(self, libraries):
        """Ensure every supplied folder has a library row.

        Runtime libraries live in the database.  This method is used to import
        legacy config paths once; existing rows are never updated, so library
        identifiers and namespaces stay database state.  New rows receive the
        next ``libraryN`` identifier and matching namespace.
        """

        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        self.ensure_schema()
        conn = connect(self.path, read_only=False)
        try:
            with conn:
                for entry in libraries or []:
                    root = canonical_path(
                        entry if isinstance(entry, str) else entry["path"]
                    )
                    existing = conn.execute(
                        "SELECT id FROM libraries WHERE path = ?",
                        (root,),
                    ).fetchone()
                    if existing is not None:
                        continue
                    identifier = self._next_identifier(conn)
                    namespace = normalize_identifier(identifier)
                    conn.execute(
                        "INSERT INTO libraries"
                        "(id, path, kind, name, identifier, id_namespace,"
                        " date_added) VALUES(?, ?, 'external', ?, ?, ?, ?)",
                        (
                            stable_uuid("library|" + root),
                            root,
                            os.path.basename(root) or "Music Library",
                            identifier,
                            namespace,
                            _now(),
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                "Library identifier or namespace is already in use"
            ) from exc
        finally:
            conn.close()
        return self.load_libraries()

    @staticmethod
    def _library_row(row):
        return {
            "id": row["id"],
            "path": row["path"],
            "name": row["name"],
            "identifier": row["identifier"],
            "id_namespace": row["id_namespace"],
            "enabled": bool(row["enabled"]),
            "date_added": float(row["date_added"] or 0),
        }

    # ------------------------------------------------------------------
    # Scanning / synchronizing

    def known_files(self, library_id=None):
        if not os.path.exists(self.path):
            return {}
        conn = connect(self.path, read_only=True)
        try:
            if not _has_schema(conn):
                return {}
            if library_id is None:
                rows = conn.execute(
                    "SELECT path, mtime, file_size FROM songs"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT path, mtime, file_size FROM songs"
                    " WHERE library_id = ?", (library_id,)
                ).fetchall()
        finally:
            conn.close()
        return {
            row["path"]: (float(row["mtime"] or 0), int(row["file_size"] or 0))
            for row in rows
        }

    def sync_tracks(self, tracks, libraries, discovered_at=None):
        """Upsert scanned tracks using Tracks' stable IDs and conflict rules."""

        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        if not tracks:
            return {}
        if discovered_at is None:
            discovered_at = _now()

        conn = connect(self.path, read_only=False)
        artists = {}
        albums = {}
        owners = [
            (lib, canonical_path(lib["path"]))
            for lib in libraries
            if lib.get("enabled", True)
        ]

        def artist_id(name):
            if name in artists:
                return artists[name]
            fallback = stable_uuid("artist|" + swift_lower(name))
            conn.execute(
                "INSERT OR IGNORE INTO artists(id, name) VALUES(?, ?)",
                (fallback, name),
            )
            row = conn.execute(
                "SELECT id FROM artists WHERE name = ?", (name,)
            ).fetchone()
            result = row["id"] if row else fallback
            artists[name] = result
            return result

        def ensure_album(title, album_artist_id, genre, year):
            fallback = stable_uuid(
                "album|" + album_artist_id + "|" + swift_lower(title)
            )
            conn.execute(
                "INSERT INTO albums(id, title, artist_id, genre, year, artwork)"
                " VALUES(?, ?, ?, ?, ?, NULL)"
                " ON CONFLICT DO UPDATE SET"
                "   genre = excluded.genre,"
                "   year = excluded.year,"
                "   artwork = COALESCE(excluded.artwork, albums.artwork)",
                (fallback, title, album_artist_id, genre, year),
            )
            row = conn.execute(
                "SELECT id FROM albums WHERE title = ? AND artist_id = ?",
                (title, album_artist_id),
            ).fetchone()
            return row["id"] if row else fallback

        def owner_for(path):
            candidates = [
                (lib, root) for lib, root in owners
                if path == root or path.startswith(root.rstrip("/") + "/")
            ]
            if not candidates:
                return None
            return max(candidates, key=lambda pair: len(pair[1]))[0]

        try:
            conn.execute("BEGIN")
            for track in tracks:
                track_path = canonical_path(track.path)
                track_artist_id = artist_id(track.artist)
                album_artist_id = artist_id(track.album_artist)
                key = (track.album_artist, swift_lower(track.album))
                if key not in albums:
                    albums[key] = ensure_album(
                        track.album, album_artist_id, track.genre, track.year
                    )
                album_id = albums[key]

                owner = owner_for(track_path)
                if owner is not None:
                    rel = relative_path(track_path, owner["path"])
                else:
                    rel = None
                if owner is not None and rel is not None:
                    song_id = song_uuid(
                        owner.get("id_namespace") or "", rel
                    )
                else:
                    song_id = stable_uuid(track_path)

                date_added = (
                    track.mtime if track.mtime and track.mtime > 0
                    else discovered_at
                )
                conn.execute(
                    "INSERT INTO songs"
                    "(id, path, library_id, title, artist_id, album_id, genre,"
                    " year, track, track_total, disc, disc_total, comments,"
                    " duration, date_added, mtime, file_size)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(path) DO UPDATE SET"
                    "   library_id = excluded.library_id,"
                    "   title = excluded.title,"
                    "   artist_id = excluded.artist_id,"
                    "   album_id = excluded.album_id,"
                    "   genre = excluded.genre,"
                    "   year = excluded.year,"
                    "   track = excluded.track,"
                    "   track_total = excluded.track_total,"
                    "   disc = excluded.disc,"
                    "   disc_total = excluded.disc_total,"
                    "   comments = excluded.comments,"
                    "   duration = excluded.duration,"
                    "   mtime = excluded.mtime,"
                    "   file_size = excluded.file_size",
                    (
                        song_id,
                        track_path,
                        owner["id"] if owner else None,
                        track.title,
                        track_artist_id,
                        album_id,
                        track.genre,
                        track.year,
                        int(track.track or 0),
                        int(track.track_total or 0),
                        int(track.disc or 0),
                        int(track.disc_total or 0),
                        track.comments or "",
                        float(track.duration or 0),
                        date_added,
                        float(track.mtime or 0),
                        int(track.file_size or 0),
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        # The caller writes artwork under the album's actual database ID, not
        # just the deterministic fallback (an existing row can legitimately
        # win a title/artist conflict with a different ID).
        return {key: album_id for key, album_id in albums.items()}

    def prune_missing(self, paths):
        """Delete explicit missing song rows and return orphan album IDs."""

        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        paths = [path for path in paths if path]
        if not paths:
            return 0, []
        conn = connect(self.path, read_only=False)
        try:
            before = {
                row[0] for row in conn.execute("SELECT id FROM albums")
            }
            conn.execute("BEGIN")
            removed = 0
            for start in range(0, len(paths), 400):
                chunk = paths[start:start + 400]
                marks = ",".join("?" for _ in chunk)
                song_rows = conn.execute(
                    "SELECT id FROM songs WHERE path IN (%s)" % marks, chunk
                ).fetchall()
                ids = [row[0] for row in song_rows]
                if ids:
                    id_marks = ",".join("?" for _ in ids)
                    conn.execute(
                        "DELETE FROM playlist_items WHERE song_id IN (%s)" % id_marks,
                        ids,
                    )
                    conn.execute(
                        "DELETE FROM songs WHERE id IN (%s)" % id_marks, ids
                    )
                    removed += len(ids)
            conn.execute(
                "DELETE FROM albums WHERE id NOT IN "
                "(SELECT DISTINCT album_id FROM songs)"
            )
            conn.execute(
                "DELETE FROM artists WHERE id NOT IN ("
                " SELECT artist_id FROM songs UNION SELECT artist_id FROM albums)"
            )
            after = {
                row[0] for row in conn.execute("SELECT id FROM albums")
            }
            conn.commit()
            return removed, sorted(before - after)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Metadata mutations

    def set_loved(self, song_id, loved):
        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        conn = connect(self.path, read_only=False)
        try:
            conn.execute(
                "UPDATE songs SET loved = ? WHERE id = ?",
                (1 if loved else 0, song_id),
            )
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    def record_play(self, song_id):
        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        conn = connect(self.path, read_only=False)
        try:
            conn.execute(
                "UPDATE songs SET play_count = play_count + 1, last_played = ?"
                " WHERE id = ?",
                (_now(), song_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT play_count FROM songs WHERE id = ?", (song_id,)
            ).fetchone()
            return int(row["play_count"]) if row else None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Playlists

    def create_playlist(self, name):
        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        name = (name or "").strip()
        if not name:
            raise ValueError("Playlist name must not be empty")
        playlist_id = str(uuid.uuid4()).upper()
        now = _now()
        conn = connect(self.path, read_only=False)
        try:
            conn.execute(
                "INSERT INTO playlists(id, name, date_created, date_modified)"
                " VALUES(?, ?, ?, ?)",
                (playlist_id, name, now, now),
            )
            conn.commit()
        finally:
            conn.close()
        return playlist_id

    def rename_playlist(self, playlist_id, name):
        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        name = (name or "").strip()
        if not name:
            raise ValueError("Playlist name must not be empty")
        conn = connect(self.path, read_only=False)
        try:
            cur = conn.execute(
                "UPDATE playlists SET name = ?, date_modified = ? WHERE id = ?",
                (name, _now(), playlist_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def delete_playlist(self, playlist_id):
        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        conn = connect(self.path, read_only=False)
        try:
            conn.execute("BEGIN")
            conn.execute(
                "DELETE FROM playlist_items WHERE playlist_id = ?",
                (playlist_id,),
            )
            cur = conn.execute(
                "DELETE FROM playlists WHERE id = ?", (playlist_id,)
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_songs_to_playlist(self, playlist_id, song_ids):
        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        song_ids = [s for s in song_ids if s]
        if not song_ids:
            return 0
        conn = connect(self.path, read_only=False)
        try:
            playlist = conn.execute(
                "SELECT id FROM playlists WHERE id = ?", (playlist_id,)
            ).fetchone()
            if not playlist:
                raise KeyError("Playlist not found")
            valid = set()
            for start in range(0, len(song_ids), 400):
                chunk = song_ids[start:start + 400]
                marks = ",".join("?" for _ in chunk)
                valid.update(
                    row[0] for row in conn.execute(
                        "SELECT id FROM songs WHERE id IN (%s)" % marks, chunk
                    )
                )
            row = conn.execute(
                "SELECT COALESCE(MAX(position), -1) AS pos FROM playlist_items"
                " WHERE playlist_id = ?",
                (playlist_id,),
            ).fetchone()
            position = int(row["pos"]) + 1
            now = _now()
            added = 0
            conn.execute("BEGIN")
            for song_id in song_ids:
                if song_id not in valid:
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO playlist_items"
                    "(playlist_id, song_id, position, added_at) VALUES(?, ?, ?, ?)",
                    (playlist_id, song_id, position, now),
                )
                if cur.rowcount:
                    added += 1
                    position += 1
            conn.execute(
                "UPDATE playlists SET date_modified = ? WHERE id = ?",
                (now, playlist_id),
            )
            conn.commit()
            return added
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def remove_songs_from_playlist(self, playlist_id, song_ids):
        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        song_ids = [s for s in song_ids if s]
        if not song_ids:
            return 0
        conn = connect(self.path, read_only=False)
        try:
            removed = 0
            conn.execute("BEGIN")
            for start in range(0, len(song_ids), 300):
                chunk = song_ids[start:start + 300]
                marks = ",".join("?" for _ in chunk)
                cur = conn.execute(
                    "DELETE FROM playlist_items WHERE playlist_id = ?"
                    " AND song_id IN (%s)" % marks,
                    [playlist_id] + chunk,
                )
                removed += cur.rowcount
            conn.execute(
                "UPDATE playlists SET date_modified = ? WHERE id = ?",
                (_now(), playlist_id),
            )
            conn.commit()
            return removed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


    def rekey_libraries(self, identifiers_by_path=None):
        """Recalculate stable IDs for every library in the database.

        Artists are derived from names, albums from the album artist and
        title, and songs from the library namespace plus relative path.  Stars,
        play counts, playlists, dates, and all other metadata are preserved.
        A SQLite backup is written next to the database before the rewrite.
        """

        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        conn = connect(self.path, read_only=False)
        try:
            libraries = [
                dict(row) for row in conn.execute(
                    "SELECT id, path, name, identifier, id_namespace"
                    " FROM libraries ORDER BY path"
                ).fetchall()
            ]
            identifiers_by_path = {
                canonical_path(path): identifier
                for path, identifier in (identifiers_by_path or {}).items()
                if identifier is not None
            }
            namespaces = {}
            namespace_by_path = {}
            for library in libraries:
                source = identifiers_by_path.get(
                    canonical_path(library["path"]), library["identifier"]
                )
                namespace = normalize_identifier(source)
                if not namespace:
                    raise ValueError(
                        "Library identifier must not be empty: %s"
                        % library["path"]
                    )
                if namespace in namespaces.values():
                    raise ValueError(
                        "Library namespace is used more than once: %s"
                        % namespace
                    )
                namespaces[library["id"]] = namespace
                namespace_by_path[
                    canonical_path(library["path"])
                ] = namespace
            if not libraries:
                return {
                    "songIDMap": {}, "albumIDMap": {},
                    "songCount": 0, "albumCount": 0, "artistCount": 0,
                    "namespaceByPath": {}, "backup": None,
                }

            backup_path = "%s.rekey-backup-%d.sqlite" % (
                self.path, int(time.time() * 1000)
            )
            backup = sqlite3.connect(backup_path)
            try:
                conn.backup(backup)
            finally:
                backup.close()

            artists = {
                row["id"]: row["name"] for row in conn.execute(
                    "SELECT id, name FROM artists"
                ).fetchall()
            }
            albums = {
                row["id"]: dict(row) for row in conn.execute(
                    "SELECT id, title, artist_id, genre, year, artwork"
                    " FROM albums"
                ).fetchall()
            }
            songs = [
                dict(row) for row in conn.execute(
                    "SELECT id, path, library_id, title, artist_id, album_id,"
                    " genre, year, track, track_total, disc, disc_total,"
                    " comments, duration, date_added, play_count, rating,"
                    " loved, last_played, skip_count, mtime, file_size"
                    " FROM songs"
                ).fetchall()
            ]
            playlist_items = [
                dict(row) for row in conn.execute(
                    "SELECT playlist_id, song_id, position, added_at"
                    " FROM playlist_items"
                ).fetchall()
            ]

            new_artists = {}

            def new_artist_id(name):
                identifier = artist_uuid(name)
                new_artists[identifier] = name
                return identifier

            old_to_new_album = {}
            new_albums = {}
            for album in albums.values():
                artist_name = artists.get(album["artist_id"], "")
                album_artist_id = new_artist_id(artist_name)
                new_id = album_uuid(album_artist_id, album["title"])
                old_to_new_album[album["id"]] = new_id
                existing = new_albums.get(new_id)
                if existing is None:
                    new_albums[new_id] = {
                        "id": new_id,
                        "title": album["title"],
                        "artist_id": album_artist_id,
                        "genre": album["genre"],
                        "year": album["year"],
                        "artwork": album["artwork"],
                    }
                else:
                    if not existing["genre"]:
                        existing["genre"] = album["genre"]
                    if not existing["year"]:
                        existing["year"] = album["year"]
                    if existing["artwork"] is None:
                        existing["artwork"] = album["artwork"]

            library_by_id = {
                library["id"]: library for library in libraries
            }
            libraries_by_depth = sorted(
                libraries, key=lambda item: len(item["path"]), reverse=True
            )

            def owner_library(song):
                owner = library_by_id.get(song["library_id"])
                if owner is not None:
                    return owner
                song_path = canonical_path(song["path"])
                for library in libraries_by_depth:
                    if is_under(song_path, canonical_path(library["path"])):
                        return library
                return None

            old_to_new_song = {}
            new_to_old_song = {}
            rekeyed_songs = []
            for song in songs:
                owner = owner_library(song)
                new_song_id = song["id"]
                if owner is not None:
                    relative = relative_path(
                        song["path"], owner["path"]
                    )
                    if relative is not None:
                        new_song_id = song_uuid(
                            namespaces[owner["id"]], relative
                        )
                if new_song_id in new_to_old_song:
                    raise ValueError(
                        "ReKey produced a duplicate song ID for %s"
                        % song["path"]
                    )
                new_to_old_song[new_song_id] = song["id"]
                old_to_new_song[song["id"]] = new_song_id

                track_artist_id = new_artist_id(
                    artists.get(song["artist_id"], "")
                )
                old_album = albums.get(song["album_id"])
                if old_album is not None:
                    album_artist_id = new_artist_id(
                        artists.get(old_album["artist_id"], "")
                    )
                    new_album_id = old_to_new_album.get(old_album["id"])
                    if new_album_id is None:
                        new_album_id = album_uuid(
                            album_artist_id, old_album["title"]
                        )
                        old_to_new_album[old_album["id"]] = new_album_id
                        new_albums.setdefault(new_album_id, {
                            "id": new_album_id,
                            "title": old_album["title"],
                            "artist_id": album_artist_id,
                            "genre": old_album["genre"],
                            "year": old_album["year"],
                            "artwork": old_album["artwork"],
                        })
                else:
                    album_artist_id = new_artist_id("")
                    new_album_id = album_uuid(album_artist_id, "")
                    new_albums.setdefault(new_album_id, {
                        "id": new_album_id,
                        "title": "",
                        "artist_id": album_artist_id,
                        "genre": "",
                        "year": "",
                        "artwork": None,
                    })
                rekeyed_songs.append({
                    **song,
                    "id": new_song_id,
                    "library_id": owner["id"] if owner else song["library_id"],
                    "artist_id": track_artist_id,
                    "album_id": new_album_id,
                })

            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM playlist_items")
                conn.execute("DELETE FROM songs")
                conn.execute("DELETE FROM albums")
                conn.execute("DELETE FROM artists")
                conn.executemany(
                    "INSERT INTO artists(id, name) VALUES(?, ?)",
                    sorted(new_artists.items(), key=lambda item: item[1]),
                )
                conn.executemany(
                    "INSERT INTO albums(id, title, artist_id, genre, year,"
                    " artwork) VALUES(?, ?, ?, ?, ?, ?)",
                    [
                        (album["id"], album["title"], album["artist_id"],
                         album["genre"], album["year"], album["artwork"])
                        for album in new_albums.values()
                    ],
                )
                conn.executemany(
                    "INSERT INTO songs(id, path, library_id, title, artist_id,"
                    " album_id, genre, year, track, track_total, disc,"
                    " disc_total, comments, duration, date_added, play_count,"
                    " rating, loved, last_played, skip_count, mtime, file_size)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                    " ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            song["id"], song["path"], song["library_id"],
                            song["title"], song["artist_id"], song["album_id"],
                            song["genre"], song["year"], song["track"],
                            song["track_total"], song["disc"],
                            song["disc_total"], song["comments"],
                            song["duration"], song["date_added"],
                            song["play_count"], song["rating"], song["loved"],
                            song["last_played"], song["skip_count"],
                            song["mtime"], song["file_size"],
                        )
                        for song in rekeyed_songs
                    ],
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO playlist_items"
                    "(playlist_id, song_id, position, added_at)"
                    " VALUES(?, ?, ?, ?)",
                    [
                        (
                            item["playlist_id"],
                            old_to_new_song.get(item["song_id"], item["song_id"]),
                            item["position"], item["added_at"],
                        )
                        for item in playlist_items
                    ],
                )
                conn.executemany(
                    "UPDATE libraries SET id_namespace = ? WHERE id = ?",
                    [
                        (namespaces[library["id"]], library["id"])
                        for library in libraries
                    ],
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

        return {
            "songIDMap": old_to_new_song,
            "albumIDMap": old_to_new_album,
            "songCount": len(rekeyed_songs),
            "albumCount": len(new_albums),
            "artistCount": len(new_artists),
            "namespaceByPath": namespace_by_path,
            "backup": os.path.basename(backup_path),
        }

class LibraryCatalog:
    """In-memory read model of the Tracks library."""

    def __init__(self, db_path, read_only=False):
        self.db = LibraryDB(db_path, read_only=read_only)
        self.read_only = bool(read_only)
        self.lock = threading.RLock()
        self._last_fingerprint = None
        self._reset()
        self.reload()

    def _reset(self):
        self.available = False
        self.needs_setup = True
        self.libraries = []
        self.albums = []
        self.album_by_id = {}
        self.all_songs = []
        self.song_by_id = {}
        self.songs_by_album = {}
        self.playlists = []
        self.playlist_by_id = {}
        self.playlist_details = {}
        self.starred_songs = []
        self.library_stats = {
            "songCount": 0, "albumCount": 0, "artistCount": 0,
            "genreCount": 0, "duration": 0.0, "size": 0,
        }
        self.starred_stats = {
            "songCount": 0, "albumCount": 0, "duration": 0.0, "size": 0,
        }

    def _fingerprint(self):
        path = self.db.path
        if not os.path.exists(path):
            return None
        parts = []
        for suffix in ("", "-wal"):
            try:
                stat = os.stat(path + suffix)
                parts.append((stat.st_mtime_ns, stat.st_size))
            except OSError:
                parts.append(None)
        return tuple(parts)

    def maybe_refresh(self):
        if self._fingerprint() != self._last_fingerprint:
            self.reload()

    def reload(self):
        with self.lock:
            self._reset()
            fingerprint = self._fingerprint()
            self._last_fingerprint = fingerprint
            if fingerprint is None:
                return
            try:
                conn = connect(self.db.path, read_only=True)
            except (sqlite3.DatabaseError, FileNotFoundError):
                return
            try:
                if not _has_schema(conn):
                    return
                libraries = [
                    dict(row) for row in conn.execute(
                        "SELECT id, path, name, identifier, id_namespace, "
                        "enabled, date_added FROM libraries ORDER BY "
                        "identifier COLLATE NOCASE, name COLLATE NOCASE"
                    )
                ]
                for lib in libraries:
                    lib["enabled"] = bool(lib["enabled"])
                    lib["date_added"] = float(lib["date_added"] or 0)

                albums, songs = self._load_songs(conn)
                playlists, details = self._load_playlists(conn, songs)
                counts = {
                    row["library_id"]: int(row["n"]) for row in conn.execute(
                        "SELECT library_id, COUNT(*) AS n FROM songs"
                        " WHERE library_id IS NOT NULL GROUP BY library_id"
                    )
                }
                for lib in libraries:
                    lib["songCount"] = counts.get(lib["id"], 0)

                starred = [song for song in songs if song["loved"]]
                stats = self._stats_for_songs(songs)
                stats["albumCount"] = len(albums)
                stats["artistCount"] = len({a["artist"] for a in albums})
                stats["genreCount"] = len({a["genre"] for a in albums})

                self.libraries = libraries
                self.albums = albums
                self.album_by_id = {album["id"]: album for album in albums}
                self.all_songs = songs
                self.song_by_id = {song["id"]: song for song in songs}
                self.songs_by_album = {
                    album["id"]: album["songs"] for album in albums
                }
                self.playlists = playlists
                self.playlist_by_id = {
                    playlist["id"]: playlist for playlist in playlists
                }
                self.playlist_details = details
                self.starred_songs = starred
                self.starred_stats = self._stats_for_songs(starred)
                self.library_stats = stats
                self.needs_setup = len(libraries) == 0
                self.available = True
            finally:
                conn.close()

    def _load_songs(self, conn):
        rows = conn.execute(
            "SELECT s.id, s.path, s.title, s.genre, s.year, s.track,"
            "       s.track_total, s.disc, s.disc_total,"
            "       s.duration, s.date_added, s.play_count, s.rating,"
            "       s.loved, s.mtime,"
            "       s.file_size, s.album_id,"
            "       al.title AS album_title, al.genre AS album_genre,"
            "       al.year AS album_year, ar.name AS album_artist,"
            "       sar.name AS song_artist"
            " FROM songs s"
            " JOIN albums al ON al.id = s.album_id"
            " JOIN artists ar ON ar.id = al.artist_id"
            " JOIN artists sar ON sar.id = s.artist_id"
            " LEFT JOIN libraries l ON l.id = s.library_id"
            " WHERE s.library_id IS NULL OR COALESCE(l.enabled, 1) = 1"
            " ORDER BY al.title COLLATE NOCASE,"
            "          CASE WHEN s.disc = 0 THEN 1 ELSE s.disc END,"
            "          s.track, s.title"
        ).fetchall()

        albums = []
        album_by_id = {}
        songs = []
        for row in rows:
            album_id = row["album_id"]
            album = album_by_id.get(album_id)
            if album is None:
                album = {
                    "id": album_id,
                    "title": row["album_title"] or "",
                    "artist": row["album_artist"] or "",
                    "genre": row["album_genre"] or "",
                    "year": row["album_year"] or "",
                    "songCount": 0,
                    "duration": 0.0,
                    "size": 0,
                    "addedAt": 0.0,
                    "songs": [],
                }
                album_by_id[album_id] = album
                albums.append(album)
            song = {
                "id": row["id"],
                "title": row["title"] or "",
                "artist": row["song_artist"] or "",
                "albumArtist": row["album_artist"] or "",
                "album": row["album_title"] or "",
                "albumId": album_id,
                "genre": row["genre"] or "",
                "year": row["year"] or "",
                "track": int(row["track"] or 0),
                "trackTotal": int(row["track_total"] or 0),
                "disc": int(row["disc"] or 0),
                "discTotal": int(row["disc_total"] or 0),
                "duration": float(row["duration"] or 0),
                "dateAdded": float(row["date_added"] or 0),
                "plays": int(row["play_count"] or 0),
                "rating": int(row["rating"] or 0),
                "loved": bool(row["loved"]),
                "mtime": float(row["mtime"] or 0),
                "fileSize": int(row["file_size"] or 0),
                "_path": row["path"],
            }
            album["songs"].append(song)
            album["songCount"] += 1
            album["duration"] += song["duration"]
            album["size"] += song["fileSize"]
            album["addedAt"] = max(album["addedAt"], song["dateAdded"])
            songs.append(song)
        return albums, songs

    def _load_playlists(self, conn, songs):
        by_id = {song["id"]: song for song in songs}
        playlists = []
        details = {}
        for row in conn.execute(
            "SELECT id, name, date_created, date_modified FROM playlists"
            " ORDER BY name COLLATE NOCASE"
        ):
            items = conn.execute(
                "SELECT song_id, position, added_at FROM playlist_items"
                " WHERE playlist_id = ? ORDER BY position, added_at",
                (row["id"],),
            ).fetchall()
            entries = []
            for item in items:
                song = by_id.get(item["song_id"])
                if song is None:
                    continue
                added = float(item["added_at"] or 0)
                if added <= 0:
                    base = float(row["date_created"] or row["date_modified"] or 0)
                    added = base + int(item["position"] or 0)
                entries.append((int(item["position"] or 0), added, song))
            order = sorted(
                range(len(entries)),
                key=lambda index: (entries[index][1], entries[index][0]),
            )
            sequence = {index: n + 1 for n, index in enumerate(order)}
            playlist_songs = []
            for index, (_position, added, song) in enumerate(entries):
                copy = dict(song)
                copy["addSequence"] = sequence[index]
                copy["addedAt"] = added
                playlist_songs.append(copy)
            summary = {
                "id": row["id"],
                "name": row["name"] or "",
                "dateModified": float(row["date_modified"] or 0),
                "songCount": len(playlist_songs),
                "duration": sum(song["duration"] for song in playlist_songs),
                "size": sum(song["fileSize"] for song in playlist_songs),
            }
            playlists.append(summary)
            details[row["id"]] = {
                "id": row["id"],
                "name": row["name"] or "",
                "dateModified": summary["dateModified"],
                "songs": playlist_songs,
            }
        return playlists, details

    @staticmethod
    def _stats_for_songs(songs):
        return {
            "songCount": len(songs),
            "duration": sum(float(song.get("duration") or 0) for song in songs),
            "size": sum(int(song.get("fileSize") or 0) for song in songs),
        }

    # ------------------------------------------------------------------
    # Serialization

    @staticmethod
    def public_song(song):
        return {
            key: value for key, value in song.items()
            if not key.startswith("_")
        }

    @staticmethod
    def public_album(album):
        return {key: value for key, value in album.items() if key != "songs"}

    def bootstrap_payload(self):
        self.maybe_refresh()
        with self.lock:
            return {
                "available": self.available,
                "needsSetup": self.needs_setup,
                "readOnly": self.read_only,
                "libraryStats": self.library_stats,
                "starredStats": self.starred_stats,
                "albums": [self.public_album(album) for album in self.albums],
                "playlists": list(self.playlists),
                "libraries": [dict(lib) for lib in self.libraries],
            }

    def songs_payload(self, scope="all"):
        self.maybe_refresh()
        with self.lock:
            if scope == "starred":
                songs = self.starred_songs
            else:
                songs = self.all_songs
            return {"songs": [self.public_song(song) for song in songs]}

    def album_payload(self, album_id):
        with self.lock:
            album = self.album_by_id.get(album_id)
            if album is None:
                return None
            return {
                "album": self.public_album(album),
                "songs": [self.public_song(song) for song in album["songs"]],
            }

    def playlist_payload(self, playlist_id):
        with self.lock:
            detail = self.playlist_details.get(playlist_id)
            if detail is None:
                return None
            return {
                "id": detail["id"],
                "name": detail["name"],
                "dateModified": detail["dateModified"],
                "songs": [
                    self.public_song(song) for song in detail["songs"]
                ],
            }

    def song_internal(self, song_id):
        with self.lock:
            return self.song_by_id.get(song_id)

    # ------------------------------------------------------------------
    # Mutations

    def _reload_playlists_only(self):
        conn = connect(self.db.path, read_only=True)
        try:
            playlists, details = self._load_playlists(conn, self.all_songs)
        finally:
            conn.close()
        self.playlists = playlists
        self.playlist_by_id = {
            playlist["id"]: playlist for playlist in playlists
        }
        self.playlist_details = details

    def update_loved(self, song_id, loved):
        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        with self.lock:
            song = self.song_by_id.get(song_id)
            if song is None:
                raise KeyError("Song not found")
            self.db.set_loved(song_id, loved)
            song["loved"] = bool(loved)
            self.starred_songs = [
                item for item in self.all_songs if item["loved"]
            ]
            self.starred_stats = self._stats_for_songs(self.starred_songs)
            self._reload_playlists_only()
            return song["loved"]

    def record_play(self, song_id):
        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        with self.lock:
            song = self.song_by_id.get(song_id)
            if song is None:
                raise KeyError("Song not found")
            plays = self.db.record_play(song_id)
            if plays is not None:
                song["plays"] = plays
            return song["plays"]

    def create_playlist(self, name):
        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        with self.lock:
            playlist_id = self.db.create_playlist(name)
            self._reload_playlists_only()
            return self.playlist_by_id.get(playlist_id)

    def rename_playlist(self, playlist_id, name):
        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        with self.lock:
            if playlist_id not in self.playlist_by_id:
                raise KeyError("Playlist not found")
            self.db.rename_playlist(playlist_id, name)
            self._reload_playlists_only()
            return self.playlist_by_id.get(playlist_id)

    def delete_playlist(self, playlist_id):
        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        with self.lock:
            if playlist_id not in self.playlist_by_id:
                raise KeyError("Playlist not found")
            self.db.delete_playlist(playlist_id)
            self._reload_playlists_only()

    def add_to_playlist(self, playlist_id, song_ids):
        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        with self.lock:
            if playlist_id not in self.playlist_by_id:
                raise KeyError("Playlist not found")
            added = self.db.add_songs_to_playlist(playlist_id, song_ids)
            self._reload_playlists_only()
            return added

    def remove_from_playlist(self, playlist_id, song_ids):
        if self.read_only:
            raise ReadOnlyError("The database is open in read-only mode")
        with self.lock:
            if playlist_id not in self.playlist_by_id:
                raise KeyError("Playlist not found")
            removed = self.db.remove_songs_from_playlist(
                playlist_id, song_ids
            )
            self._reload_playlists_only()
            return removed
