import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from unittest import mock

import yaml

from flask import Flask
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import (  # noqa: E402
    Config, PresenceTracker, create_app, main, _client_ip,
)
from tracks_config import (  # noqa: E402
    DEFAULT_ADMIN_PASSWORD_SHA1,
    DEFAULT_SETTINGS,
    load_settings,
    update_config,
)
from tracks_admin_guard import AdminLoginGuard  # noqa: E402
from tracks_analytics import Analytics  # noqa: E402
from tracks_logging import _compress_archive, configure_logging  # noqa: E402
from tracks_rate_limit import DownloadRateLimiter  # noqa: E402
from tracks_db import LibraryDB, LibraryCatalog  # noqa: E402
from tracks_identity import (  # noqa: E402
    album_uuid,
    artist_uuid,
    library_uuid,
    normalize_relative_path,
    song_uuid,
    stable_uuid,
    swift_lower,
)
from tracks_scanner import (  # noqa: E402
    ScannedTrack,
    rebuild_artwork_cache,
    scan_one,
    write_artwork_cache,
)


class IdentityTests(unittest.TestCase):
    def test_stable_uuid_matches_tracks(self):
        self.assertEqual(
            song_uuid(
                "iTunes",
                "Raver Rage Records/レレレコンピ/01 related.m4a",
            ),
            "0B161D2F-A164-B19B-9D78-B2C4B1E72589",
        )
        self.assertEqual(
            artist_uuid("Raver Rage Records"),
            "43054C1F-0026-CD2F-C882-2A741E57531E",
        )
        self.assertEqual(
            album_uuid(
                "43054C1F-0026-CD2F-C882-2A741E57531E",
                "レレレコンピ",
            ),
            "5411E3DF-F842-42D2-C519-201B354AEA96",
        )

    def test_swift_characterwise_lowercase(self):
        # Python's str.lower() would turn the trailing Sigma into final sigma,
        # while Swift's lowercased() produces the ordinary lowercase sigma.
        self.assertEqual(
            swift_lower("I.M-P.U.L.S.Σ"), "i.m-p.u.l.s.σ"
        )
        self.assertEqual(normalize_relative_path("/A/B.flac"), "A/B.flac")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tracks-db-test-")
        self.music = os.path.join(self.root, "Music")
        os.makedirs(os.path.join(self.music, "Artist", "Album"))
        self.db = LibraryDB(os.path.join(self.root, "library.sqlite"))
        self.db.ensure_schema()
        self.library = self.db.ensure_library(self.music, "testlib")
        self.path = os.path.join(self.music, "Artist", "Album", "01 Song.flac")
        self.track = ScannedTrack(
            path=self.path,
            title="Song",
            artist="Artist",
            album_artist="Artist",
            album="Album",
            genre="Pop",
            year="2024",
            track=1,
            track_total=2,
            disc=0,
            disc_total=0,
            comments="hello",
            duration=123.5,
            has_artwork=True,
            mtime=1_700_000_000.0,
            file_size=1234,
        )
        self.db.sync_tracks([self.track], self.db.load_libraries())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_schema_and_stable_ids(self):
        conn = sqlite3.connect(self.db.path)
        try:
            columns = [
                row[1] for row in conn.execute("PRAGMA table_info(songs)")
            ]
            self.assertEqual(
                columns,
                [
                    "id", "path", "library_id", "title", "artist_id",
                    "album_id", "genre", "year", "track", "track_total",
                    "disc", "disc_total", "comments", "duration",
                    "date_added", "play_count", "rating", "loved",
                    "last_played", "skip_count", "mtime", "file_size",
                ],
            )
            song = conn.execute("SELECT * FROM songs").fetchone()
            artist = conn.execute(
                "SELECT id FROM artists WHERE name = 'Artist'"
            ).fetchone()
            album = conn.execute(
                "SELECT id FROM albums WHERE title = 'Album'"
            ).fetchone()
            self.assertEqual(
                song[0],
                song_uuid("testlib", "Artist/Album/01 Song.flac"),
            )
            self.assertEqual(
                artist[0], artist_uuid("Artist")
            )
            self.assertEqual(
                album[0],
                album_uuid(artist[0], "Album"),
            )
            self.assertEqual(song[14], 1_700_000_000.0)
            self.assertEqual(song[21], 1234)
            self.assertIsNone(conn.execute(
                "SELECT artwork FROM albums"
            ).fetchone()[0])
        finally:
            conn.close()

    def test_upsert_preserves_stars_and_plays(self):
        db_song = sqlite3.connect(self.db.path)
        try:
            song_id = db_song.execute("SELECT id FROM songs").fetchone()[0]
        finally:
            db_song.close()
        self.db.set_loved(song_id, True)
        self.db.record_play(song_id)
        self.track.title = "Song Renamed"
        self.track.duration = 321.0
        self.db.sync_tracks(
            [self.track], self.db.load_libraries()
        )
        conn = sqlite3.connect(self.db.path)
        try:
            row = conn.execute(
                "SELECT title, duration, loved, play_count, date_added"
                " FROM songs WHERE id = ?", (song_id,)
            ).fetchone()
            self.assertEqual(row, ("Song Renamed", 321.0, 1, 1,
                                   1_700_000_000.0))
        finally:
            conn.close()

    def test_catalog_and_playlists(self):
        catalog = LibraryCatalog(self.db.path)
        boot = catalog.bootstrap_payload()
        self.assertEqual(boot["libraryStats"]["songCount"], 1)
        self.assertEqual(len(boot["albums"]), 1)
        for key in ("comments", "lastPlayed", "skipCount"):
            self.assertNotIn(key, catalog.all_songs[0])
        playlist = catalog.create_playlist("Test Playlist")
        catalog.add_to_playlist(
            playlist["id"], [catalog.all_songs[0]["id"]]
        )
        detail = catalog.playlist_payload(playlist["id"])
        self.assertEqual(len(detail["songs"]), 1)
        self.assertEqual(detail["songs"][0]["addSequence"], 1)


    def test_rekey_preserves_metadata_and_stable_ids(self):
        conn = sqlite3.connect(self.db.path)
        conn.row_factory = sqlite3.Row
        song = dict(conn.execute("SELECT * FROM songs").fetchone())
        conn.execute(
            "UPDATE songs SET loved = 1, play_count = 7 WHERE id = ?",
            (song["id"],),
        )
        conn.execute(
            "INSERT INTO playlists(id, name, date_created, date_modified)"
            " VALUES('playlist-1', 'Rekey Test', 1, 1)"
        )
        conn.execute(
            "INSERT INTO playlist_items(playlist_id, song_id, position,"
            " added_at) VALUES('playlist-1', ?, 0, 1)",
            (song["id"],),
        )
        conn.commit()
        conn.close()

        result = self.db.rekey_libraries()
        conn = sqlite3.connect(self.db.path)
        conn.row_factory = sqlite3.Row
        rekeyed = dict(conn.execute("SELECT * FROM songs").fetchone())
        item = dict(conn.execute(
            "SELECT * FROM playlist_items WHERE playlist_id = 'playlist-1'"
        ).fetchone())
        library = dict(conn.execute("SELECT * FROM libraries").fetchone())
        conn.close()
        self.assertEqual(rekeyed["id"], song["id"])
        self.assertEqual(rekeyed["loved"], 1)
        self.assertEqual(rekeyed["play_count"], 7)
        self.assertEqual(item["song_id"], song["id"])
        self.assertTrue(result["backup"])
        self.assertTrue(
            os.path.isfile(
                os.path.join(os.path.dirname(self.db.path), result["backup"])
            )
        )

        conn = sqlite3.connect(self.db.path)
        conn.execute(
            "UPDATE libraries SET identifier = 'renamedlib' WHERE id = ?",
            (library["id"],),
        )
        conn.commit()
        conn.close()
        second = self.db.rekey_libraries()
        conn = sqlite3.connect(self.db.path)
        conn.row_factory = sqlite3.Row
        changed = dict(conn.execute("SELECT * FROM songs").fetchone())
        conn.close()
        expected = song_uuid(
            "renamedlib", normalize_relative_path("Artist/Album/01 Song.flac")
        )
        self.assertEqual(changed["id"], expected)
        self.assertNotEqual(changed["id"], song["id"])
        self.assertIn(song["id"], second["songIDMap"])


    def test_library_identity_comes_from_database(self):
        libraries = self.db.sync_libraries([{
            "path": self.music,
            # These legacy config fields must not overwrite database state.
            "identifier": "renamedlib",
            "namespace": "testlib",
        }])
        library = next(
            item for item in libraries
            if os.path.realpath(item["path"]) == os.path.realpath(self.music)
        )
        self.assertEqual(library["identifier"], "testlib")
        self.assertEqual(library["id_namespace"], "testlib")

        library = self.db.set_library_identifier(self.music, "renamedlib")
        self.assertEqual(library["identifier"], "renamedlib")
        self.assertEqual(library["id_namespace"], "testlib")
        result = self.db.rekey_libraries()
        expected = song_uuid(
            "renamedlib", normalize_relative_path("Artist/Album/01 Song.flac")
        )
        conn = sqlite3.connect(self.db.path)
        conn.row_factory = sqlite3.Row
        song = dict(conn.execute("SELECT * FROM songs").fetchone())
        library = dict(conn.execute("SELECT * FROM libraries").fetchone())
        conn.close()
        self.assertEqual(song["id"], expected)
        self.assertEqual(library["id_namespace"], "renamedlib")
        self.assertEqual(
            result["namespaceByPath"][os.path.realpath(self.music)],
            "renamedlib",
        )


class ArtworkTests(unittest.TestCase):
    def test_cache_filename_and_size(self):
        with tempfile.TemporaryDirectory() as root:
            source = Image.new("RGB", (1200, 800), (120, 30, 200))
            buf = os.path.join(root, "source.png")
            source.save(buf)
            with open(buf, "rb") as handle:
                data = handle.read()
            album_id = stable_uuid("album|test")
            cache = os.path.join(root, "Artwork")
            self.assertTrue(
                write_artwork_cache(data, album_id, cache, 80)
            )
            target = os.path.join(cache, album_id + ".jpg")
            self.assertTrue(os.path.isfile(target))
            with Image.open(target) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.size, (500, 333))

    def test_cache_cover_size_is_configurable(self):
        with tempfile.TemporaryDirectory() as root:
            source = Image.new("RGB", (1200, 800), (120, 30, 200))
            buf = os.path.join(root, "source.png")
            source.save(buf)
            with open(buf, "rb") as handle:
                data = handle.read()
            album_id = stable_uuid("album|cover-size")
            cache = os.path.join(root, "Artwork")
            self.assertTrue(
                write_artwork_cache(data, album_id, cache, 70, 300)
            )
            with Image.open(
                os.path.join(cache, album_id + ".jpg")
            ) as image:
                self.assertEqual(image.size, (300, 200))

    def test_rebuild_reads_media_and_replaces_cached_cover(self):
        with tempfile.TemporaryDirectory() as root:
            old_source = Image.new("RGB", (900, 600), (20, 80, 160))
            old_buf = os.path.join(root, "old.png")
            old_source.save(old_buf)
            with open(old_buf, "rb") as handle:
                old_data = handle.read()
            new_source = Image.new("RGB", (840, 560), (210, 30, 20))
            new_buf = os.path.join(root, "new.png")
            new_source.save(new_buf)
            with open(new_buf, "rb") as handle:
                new_data = handle.read()
            album_id = stable_uuid("album|rebuild")
            cache = os.path.join(root, "Artwork")
            self.assertTrue(
                write_artwork_cache(old_data, album_id, cache, 80, 500)
            )
            with mock.patch(
                "tracks_scanner.extract_artwork_path",
                return_value=new_data,
            ) as extract:
                result = rebuild_artwork_cache(
                    [{
                        "id": album_id,
                        "songs": [{"_path": "/media/audio.flac"}],
                    }],
                    cache,
                    jpeg_quality=70,
                    cover_size=300,
                )
            extract.assert_called_once_with("/media/audio.flac")
            self.assertEqual(result["artwork"], 1)
            self.assertEqual(result["missing"], 0)
            with Image.open(
                os.path.join(cache, album_id + ".jpg")
            ) as image:
                self.assertEqual(image.size, (300, 200))
                red, green, blue = image.convert("RGB").getpixel((10, 10))
                self.assertGreater(red, green)
                self.assertGreater(red, blue)

    def test_rebuild_discards_cover_when_media_has_no_art(self):
        with tempfile.TemporaryDirectory() as root:
            source = Image.new("RGB", (500, 500), (20, 80, 160))
            buf = os.path.join(root, "source.png")
            source.save(buf)
            with open(buf, "rb") as handle:
                data = handle.read()
            album_id = stable_uuid("album|rebuild-missing")
            cache = os.path.join(root, "Artwork")
            target = os.path.join(cache, album_id + ".jpg")
            self.assertTrue(
                write_artwork_cache(data, album_id, cache, 80, 500)
            )
            self.assertTrue(os.path.isfile(target))
            with mock.patch(
                "tracks_scanner.extract_artwork_path", return_value=None
            ):
                result = rebuild_artwork_cache(
                    [{
                        "id": album_id,
                        "songs": [{"_path": "/media/audio.flac"}],
                    }],
                    cache,
                    jpeg_quality=70,
                    cover_size=300,
                )
            self.assertEqual(result["artwork"], 0)
            self.assertEqual(result["missing"], 1)
            self.assertFalse(os.path.exists(target))


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
class ScannerEndToEndTests(unittest.TestCase):
    def test_flac_scan_creates_database_and_cover(self):
        with tempfile.TemporaryDirectory() as root:
            music = os.path.join(root, "Library")
            album_dir = os.path.join(music, "Someone", "Test Album")
            os.makedirs(album_dir)
            cover_path = os.path.join(root, "cover.png")
            Image.new("RGB", (640, 640), (10, 120, 90)).save(cover_path)
            flac_path = os.path.join(album_dir, "01 Track.flac")
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-i", cover_path,
                    "-map", "0:a", "-map", "1:v",
                    "-c:a", "flac", "-c:v", "png",
                    "-disposition:v", "attached_pic",
                    "-metadata", "TITLE=Track One",
                    "-metadata", "ARTIST=Track Artist",
                    "-metadata", "ALBUMARTIST=Album Artist",
                    "-metadata", "ALBUM=Test Album",
                    "-metadata", "GENRE=Electronic",
                    "-metadata", "DATE=2024-03-01",
                    "-metadata", "TRACKNUMBER=1/2",
                    "-metadata", "COMMENT=Cover test",
                    flac_path,
                ],
                check=True,
            )
            db = LibraryDB(os.path.join(root, "library.sqlite"))
            artwork = os.path.join(root, "Artwork")
            result = scan_one(
                db, music, identifier="testlib", mode="full",
                artwork_dir=artwork, force=True,
            )
            self.assertEqual(result["tracks"], 1)
            self.assertEqual(result["artwork"], 1)
            conn = sqlite3.connect(db.path)
            try:
                row = conn.execute(
                    "SELECT s.id, s.title, s.duration, s.track, s.track_total,"
                    " s.genre, s.year, aar.name, al.title, al.id"
                    " FROM songs s JOIN albums al ON al.id = s.album_id"
                    " JOIN artists aar ON aar.id = al.artist_id"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(
                row[0], song_uuid("testlib", "Someone/Test Album/01 Track.flac")
            )
            self.assertEqual(row[1], "Track One")
            self.assertAlmostEqual(row[2], 2.0, places=1)
            self.assertEqual(row[3], 1)
            self.assertEqual(row[4], 2)
            self.assertEqual(row[5], "Electronic")
            self.assertEqual(row[6], "2024-03-01")
            self.assertEqual(row[7], "Album Artist")
            self.assertEqual(row[8], "Test Album")
            target = os.path.join(artwork, row[9].upper() + ".jpg")
            self.assertTrue(os.path.isfile(target))
            with Image.open(target) as image:
                self.assertLessEqual(max(image.size), 500)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tracks-api-test-")
        self.music = os.path.join(self.root, "Music")
        os.makedirs(self.music)
        self.audio_path = os.path.join(self.music, "tone.wav")
        with wave.open(self.audio_path, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 8000)
        self.db_path = os.path.join(self.root, "library.sqlite")
        self.db = LibraryDB(self.db_path)
        self.db.ensure_schema()
        self.library = self.db.ensure_library(self.music, "api")
        track = ScannedTrack(
            path=self.audio_path,
            title="Tone",
            artist="Tester",
            album_artist="Tester",
            album="Test Album",
            genre="Test",
            year="2026",
            track=1,
            track_total=1,
            disc=0,
            disc_total=0,
            comments="",
            duration=1.0,
            has_artwork=False,
            mtime=1_700_000_000.0,
            file_size=os.path.getsize(self.audio_path),
        )
        self.db.sync_tracks([track], self.db.load_libraries())
        self.artwork = os.path.join(self.root, "Artwork")
        app = create_app(
            Config(
                self.db_path,
                self.artwork,
                read_only=False,
                audio_formats=[
                    {"index": 0, "format": "aac", "bitrate": 128},
                    {"index": 1, "format": "raw"},
                ],
            )
        )
        self.client = app.test_client()
        boot = self.client.get("/api/bootstrap").get_json()
        self.song_id = boot["albums"][0]["id"]
        songs = self.client.get("/api/songs").get_json()["songs"]
        self.song_id = songs[0]["id"]
        self.album_id = songs[0]["albumId"]

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_bootstrap_songs_audio_and_artwork(self):
        boot = self.client.get("/api/bootstrap").get_json()
        self.assertFalse(boot["needsSetup"])
        self.assertEqual(boot["libraryStats"]["songCount"], 1)
        songs = self.client.get("/api/songs").get_json()["songs"]
        self.assertEqual(len(songs), 1)
        album = self.client.get(
            "/api/albums/" + self.album_id
        )
        self.assertEqual(album.status_code, 200)
        audio = self.client.get(
            "/api/audio/%s?quality=raw" % self.song_id
        )
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.mimetype, "audio/wav")
        audio.close()
        song = self.client.get("/api/songs/" + self.song_id)
        self.assertEqual(song.status_code, 200)
        self.assertEqual(song.get_json()["song"]["id"], self.song_id)
        missing = self.client.get(
            "/api/songs/00000000-0000-0000-0000-000000000000"
        )
        self.assertEqual(missing.status_code, 404)
        fallback = self.client.get("/api/artwork/" + self.album_id)
        self.assertEqual(fallback.status_code, 200)
        self.assertEqual(fallback.mimetype, "image/svg+xml")

    def test_robots_txt_and_noindex_headers(self):
        robots = self.client.get("/robots.txt")
        self.assertEqual(robots.status_code, 200)
        self.assertEqual(robots.mimetype, "text/plain")
        self.assertIn("User-agent: *", robots.get_data(as_text=True))
        self.assertIn("Disallow: /", robots.get_data(as_text=True))
        self.assertIn(
            "noindex", robots.headers.get("X-Robots-Tag", "")
        )

        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(
            "noindex", page.headers.get("X-Robots-Tag", "")
        )
        self.assertIn(
            'name="robots"', page.get_data(as_text=True)
        )

    def test_public_songs_hide_private_metadata(self):
        private_keys = ("comments", "lastPlayed", "skipCount")
        songs = self.client.get("/api/songs").get_json()["songs"]
        for key in private_keys:
            self.assertNotIn(key, songs[0])
        single = self.client.get(
            "/api/songs/" + self.song_id
        ).get_json()["song"]
        for key in private_keys:
            self.assertNotIn(key, single)
        album = self.client.get(
            "/api/albums/" + self.album_id
        ).get_json()["songs"][0]
        for key in private_keys:
            self.assertNotIn(key, album)

    def test_server_header_is_hidden_or_configured(self):
        self.assertNotIn("Server", self.client.get("/api/health").headers)
        app = create_app(
            Config(
                self.db_path,
                self.artwork,
                server_header="WebTracks",
            )
        )
        response = app.test_client().get("/api/health")
        self.assertEqual(response.headers["Server"], "WebTracks")

    def test_media_responses_use_cross_origin_resource_policy(self):
        artwork = self.client.get("/api/artwork/" + self.album_id)
        self.assertEqual(
            artwork.headers["Cross-Origin-Resource-Policy"], "same-origin"
        )
        audio = self.client.head(
            "/api/audio/%s?quality=raw" % self.song_id
        )
        self.assertEqual(
            audio.headers["Cross-Origin-Resource-Policy"], "same-origin"
        )
        audio.close()

    def test_state_changing_requests_validate_origin(self):
        self.assertEqual(
            self.client.post(
                "/api/status", headers={"Origin": "https://evil.example"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/status", headers={"Sec-Fetch-Site": "cross-site"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/status", headers={"Origin": "http://localhost"}
            ).status_code,
            200,
        )

    def test_trusted_hosts_default_and_override(self):
        self.assertEqual(
            self.client.get(
                "/api/health", headers={"Host": "evil.example"}
            ).status_code,
            200,
        )
        app = create_app(
            Config(
                self.db_path,
                self.artwork,
                trusted_hosts=["music.example.com"],
            )
        )
        client = app.test_client()
        self.assertEqual(
            client.get(
                "/api/health", headers={"Host": "music.example.com"}
            ).status_code,
            200,
        )
        self.assertEqual(
            client.get(
                "/api/health", headers={"Host": "evil.example"}
            ).status_code,
            400,
        )

    def test_reload_endpoint_is_removed(self):
        self.assertEqual(
            self.client.post("/api/reload").status_code, 404
        )

    def test_bootstrap_hides_filesystem_paths(self):
        boot = self.client.get("/api/bootstrap").get_json()
        self.assertNotIn("musicDirs", boot)
        self.assertNotIn("database", boot)
        self.assertTrue(boot["libraries"])
        for library in boot["libraries"]:
            self.assertNotIn("path", library)

    def test_audio_rate_limit_is_reported_and_probeable(self):
        app = self.client.application
        limiter = app.extensions["tracks_rate_limit"]
        with mock.patch.object(limiter, "check", return_value=(False, 7)):
            response = self.client.get("/api/audio/" + self.song_id)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers.get("Retry-After"), "7")
        with mock.patch.object(
            limiter, "is_limited", return_value=(True, 6)
        ):
            info = self.client.get("/api/rate-limit").get_json()
        self.assertTrue(info["limited"])
        self.assertEqual(info["retryAfter"], 6)
        self.assertEqual(info["limit"], 28)

    def test_audio_records_play_once_per_play_id(self):
        def plays():
            songs = self.client.get("/api/songs").get_json()["songs"]
            return next(
                song["plays"] for song in songs
                if song["id"] == self.song_id
            )

        clock = [1000.0]
        with mock.patch(
            "server.time.monotonic", side_effect=lambda: clock[0]
        ):
            before = plays()
            for _ in range(2):
                response = self.client.get(
                    "/api/audio/%s?quality=raw&play=session-1"
                    % self.song_id
                )
                self.assertEqual(response.status_code, 200)
                response.close()
            self.assertEqual(plays(), before + 1)

            response = self.client.get(
                "/api/audio/%s?quality=raw&play=session-2"
                % self.song_id
            )
            self.assertEqual(response.status_code, 200)
            response.close()
            self.assertEqual(plays(), before + 1)

            clock[0] += 31
            response = self.client.get(
                "/api/audio/%s?quality=raw&play=session-2"
                % self.song_id
            )
            self.assertEqual(response.status_code, 200)
            response.close()
            self.assertEqual(plays(), before + 2)
        self.assertEqual(
            self.client.post(
                "/api/songs/%s/played" % self.song_id
            ).status_code,
            404,
        )

    def test_setup_stats_public_summary_and_admin_details(self):
        public = self.client.get("/api/setup/stats")
        self.assertEqual(public.status_code, 200)
        public_payload = public.get_json()
        self.assertNotIn("clients", public_payload)
        self.assertEqual(set(public_payload["totals"]), {
            "visitors", "requests", "bytesSent", "songsPlayed",
            "uniqueSongs", "playlistActions",
        })
        token = self.client.post(
            "/api/setup/login", json={"password": "1"}
        ).get_json()["token"]
        headers = {"Authorization": "Bearer " + token}
        self.client.post(
            "/api/status", json={"client": "stats-client"}
        )
        audio = self.client.get(
            "/api/audio/%s?quality=raw&play=stats-play" % self.song_id
        )
        self.assertEqual(audio.status_code, 200)
        audio.close()
        stats = self.client.get(
            "/api/setup/stats", headers=headers
        ).get_json()
        self.assertGreaterEqual(stats["totals"]["songsPlayed"], 1)
        self.assertGreaterEqual(stats["totals"]["uniqueSongs"], 1)
        self.assertGreaterEqual(stats["totals"]["adminLogins"], 1)
        self.assertGreaterEqual(stats["totals"]["visitors"], 1)
        self.assertGreaterEqual(stats["totals"]["online"], 1)
        self.assertTrue(stats["clients"])
        self.assertTrue(any(
            client.get("online", 0) >= 1
            for client in stats["clients"]
        ))

    def test_star_and_playlist(self):
        response = self.client.post(
            "/api/songs/%s/star" % self.song_id,
            json={"loved": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["loved"])
        self.assertEqual(
            self.client.post(
                "/api/playlists", json={"name": "API Playlist"}
            ).status_code,
            401,
        )
        token = self.client.post(
            "/api/setup/login", json={"password": "1"}
        ).get_json()["token"]
        created = self.client.post(
            "/api/playlists",
            json={"name": "API Playlist"},
            headers={"Authorization": "Bearer " + token},
        ).get_json()["playlist"]
        self.assertEqual(
            self.client.patch(
                "/api/playlists/" + created["id"],
                json={"name": "Anonymous Rename"},
            ).status_code,
            401,
        )
        renamed = self.client.patch(
            "/api/playlists/" + created["id"],
            json={"name": "Renamed Playlist"},
            headers={"Authorization": "Bearer " + token},
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(
            renamed.get_json()["playlist"]["name"], "Renamed Playlist"
        )
        response = self.client.post(
            "/api/playlists/%s/songs" % created["id"],
            json={"songIds": [self.song_id]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["added"], 1)
        detail = self.client.get(
            "/api/playlists/" + created["id"]
        ).get_json()
        self.assertEqual(len(detail["songs"]), 1)
        self.assertEqual(detail["songs"][0]["id"], self.song_id)
        removed = self.client.delete(
            "/api/playlists/%s/songs" % created["id"],
            json={"songIds": [self.song_id]},
        )
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.get_json()["removed"], 1)
        self.assertEqual(
            self.client.delete(
                "/api/playlists/" + created["id"]
            ).status_code,
            401,
        )
        deleted = self.client.delete(
            "/api/playlists/" + created["id"],
            headers={"Authorization": "Bearer " + token},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(
            self.client.get(
                "/api/playlists/" + created["id"]
            ).status_code,
            404,
        )


    def test_setup_scan_uses_database_libraries(self):
        app = create_app(
            Config(
                self.db_path,
                self.artwork,
                read_only=False,
            )
        )
        client = app.test_client()
        token = client.post(
            "/api/setup/login", json={"password": "1"}
        ).get_json()["token"]
        headers = {"Authorization": "Bearer " + token}
        started = client.post(
            "/api/setup/scan",
            json={
                "mode": "full",
                "root": "/tmp/not-configured",
                "identifier": "ignored",
            },
            headers=headers,
        )
        self.assertEqual(started.status_code, 202)
        job = started.get_json()["job"]
        for _ in range(100):
            if not job.get("running"):
                break
            time.sleep(0.05)
            job = client.get("/api/scan", headers=headers).get_json()
        self.assertFalse(job["running"])
        self.assertEqual(job["phase"], "Done")
        self.assertEqual(
            job["summary"]["libraries"],
            [os.path.realpath(self.music)],
        )
        public_job = client.get("/api/scan").get_json()
        self.assertIsNone(public_job["summary"])
        self.assertEqual(public_job["errors"], [])
        self.assertEqual(public_job["message"], "")

    def test_setup_rekey_requires_admin_and_finishes(self):
        self.assertEqual(
            self.client.post("/api/setup/rekey").status_code, 401
        )
        token = self.client.post(
            "/api/setup/login", json={"password": "1"}
        ).get_json()["token"]
        headers = {"Authorization": "Bearer " + token}
        started = self.client.post("/api/setup/rekey", headers=headers)
        self.assertEqual(started.status_code, 202)
        job = started.get_json()["job"]
        for _ in range(60):
            if not job.get("running"):
                break
            time.sleep(0.05)
            job = self.client.get(
                "/api/setup/rekey", headers=headers
            ).get_json()
        self.assertFalse(job["running"])
        self.assertEqual(job["phase"], "Done")
        self.assertEqual(job["summary"]["songs"], 1)
        self.assertTrue(job["summary"]["backup"])
        public_job = self.client.get("/api/setup/rekey").get_json()
        self.assertIsNone(public_job["summary"])
        self.assertEqual(public_job["errors"], [])
        self.assertEqual(public_job["message"], "")
        songs = self.client.get("/api/songs").get_json()["songs"]
        self.assertEqual(len(songs), 1)

    def test_admin_login_bruteforce_lock_returns_password_error(self):
        app = create_app(
            Config(
                self.db_path,
                self.artwork,
                read_only=False,
                admin_bruteforce_limit=2,
                admin_bruteforce_window_seconds=60,
                admin_bruteforce_lock_seconds=3600,
            )
        )
        client = app.test_client()
        blocked_ip = {"X-Forwarded-For": "198.51.100.10"}
        for _ in range(3):
            response = client.post(
                "/api/setup/login",
                json={"password": "wrong"},
                headers=blocked_ip,
            )
            self.assertEqual(response.status_code, 401)
            self.assertEqual(
                response.get_json()["error"], "Incorrect admin password"
            )
        # The lock applies even to the correct password and uses the same
        # generic response.
        locked = client.post(
            "/api/setup/login",
            json={"password": "1"},
            headers=blocked_ip,
        )
        self.assertEqual(locked.status_code, 401)
        self.assertEqual(
            locked.get_json()["error"], "Incorrect admin password"
        )
        allowed_ip = {"X-Forwarded-For": "198.51.100.11"}
        token = client.post(
            "/api/setup/login",
            json={"password": "1"},
            headers=allowed_ip,
        ).get_json()["token"]
        stats = client.get(
            "/api/setup/stats",
            headers={
                "Authorization": "Bearer " + token,
                **allowed_ip,
            },
        ).get_json()
        self.assertGreaterEqual(
            stats["totals"]["adminBruteforceBlocked"], 2
        )

    def test_setup_rebuild_artwork_job(self):
        self.assertEqual(
            self.client.post("/api/setup/artwork").status_code, 401
        )
        token = self.client.post(
            "/api/setup/login", json={"password": "1"}
        ).get_json()["token"]
        headers = {"Authorization": "Bearer " + token}
        result = {
            "albums": 2,
            "artwork": 1,
            "missing": 1,
            "errors": [],
        }
        with mock.patch(
            "server.rebuild_artwork_cache", return_value=result
        ):
            started = self.client.post(
                "/api/setup/artwork", headers=headers
            )
            self.assertEqual(started.status_code, 202)
            job = started.get_json()["job"]
            for _ in range(60):
                if not job.get("running"):
                    break
                time.sleep(0.05)
                job = self.client.get(
                    "/api/setup/artwork", headers=headers
                ).get_json()
        self.assertFalse(job["running"])
        self.assertEqual(job["phase"], "Done")
        self.assertEqual(job["summary"]["albums"], 2)
        self.assertEqual(job["summary"]["artwork"], 1)
        self.assertEqual(job["summary"]["missing"], 1)
        public_job = self.client.get("/api/setup/artwork").get_json()
        self.assertIsNone(public_job["summary"])
        self.assertEqual(public_job["errors"], [])
        self.assertEqual(public_job["message"], "")

    def test_rekey_updates_namespace_in_database_only(self):
        config_path = os.path.join(self.root, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({
                "database_dir": "data",
                "libraries": [{"path": self.music}],
            }, handle)
        self.db.set_library_identifier(self.music, "newidentifier")
        app = create_app(
            Config(
                self.db_path,
                self.artwork,
                read_only=False,
                config_path=config_path,
                libraries=[{"path": self.music}],
            )
        )
        client = app.test_client()
        token = client.post(
            "/api/setup/login", json={"password": "1"}
        ).get_json()["token"]
        started = client.post(
            "/api/setup/rekey",
            headers={"Authorization": "Bearer " + token},
        )
        self.assertEqual(started.status_code, 202)
        job = started.get_json()["job"]
        for _ in range(80):
            if not job.get("running"):
                break
            time.sleep(0.05)
            job = client.get("/api/setup/rekey").get_json()
        self.assertFalse(job["running"])
        self.assertEqual(job["phase"], "Done")
        with open(config_path, "r", encoding="utf-8") as handle:
            values = json.load(handle)
        self.assertEqual(values["libraries"], [{"path": self.music}])
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        library = dict(conn.execute("SELECT * FROM libraries").fetchone())
        conn.close()
        self.assertEqual(library["identifier"], "newidentifier")
        self.assertEqual(library["id_namespace"], "newidentifier")

    def test_status_presence_and_ready_state(self):
        first = self.client.post(
            "/api/status",
            headers={"X-Forwarded-For": "203.0.113.10"},
        ).get_json()
        self.assertEqual(first["online"], 1)
        self.assertEqual(first["state"], "ready")
        second = self.client.post(
            "/api/status",
            headers={"X-Forwarded-For": "203.0.113.11"},
        ).get_json()
        self.assertEqual(second["online"], 2)
        repeat = self.client.post(
            "/api/status",
            json={"client": "ignored-client-id"},
            headers={"X-Forwarded-For": "203.0.113.10"},
        ).get_json()
        self.assertEqual(repeat["online"], 2)

    def test_setup_library_id_update_keeps_namespace(self):
        config_path = os.path.join(self.root, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({
                "database_dir": "data",
                "libraries": [{"path": self.music}],
            }, handle)
        app = create_app(
            Config(
                self.db_path,
                self.artwork,
                read_only=False,
                config_path=config_path,
                libraries=[{"path": self.music}],
            )
        )
        client = app.test_client()
        payload = {
            "path": self.music,
            "identifier": "renamed",
        }
        self.assertEqual(
            client.patch("/api/setup/library", json=payload).status_code,
            401,
        )
        token = client.post(
            "/api/setup/login", json={"password": "1"}
        ).get_json()["token"]
        headers = {"Authorization": "Bearer " + token}
        response = client.patch(
            "/api/setup/library", json=payload, headers=headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["rekeyRequired"])
        with open(config_path, "r", encoding="utf-8") as handle:
            values = json.load(handle)
        self.assertEqual(values["libraries"], [{"path": self.music}])
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        library = dict(conn.execute("SELECT * FROM libraries").fetchone())
        conn.close()
        self.assertEqual(library["identifier"], "renamed")
        self.assertEqual(library["id_namespace"], "api")

    def test_setup_add_library_requires_admin_and_validates_overlap(self):
        second = os.path.join(self.root, "Second Library")
        os.makedirs(second)
        payload = {"path": second}
        self.assertEqual(
            self.client.post("/api/setup/libraries", json=payload).status_code,
            401,
        )
        token = self.client.post(
            "/api/setup/login", json={"password": "1"}
        ).get_json()["token"]
        headers = {"Authorization": "Bearer " + token}
        added = self.client.post(
            "/api/setup/libraries", json=payload, headers=headers
        )
        self.assertEqual(added.status_code, 201)
        library = added.get_json()["library"]
        self.assertEqual(
            library["path"], os.path.realpath(second)
        )
        self.assertTrue(library["identifier"])
        self.assertEqual(library["identifier"], library["namespace"])

        duplicate = self.client.post(
            "/api/setup/libraries", json=payload, headers=headers
        )
        self.assertEqual(duplicate.status_code, 409)
        nested = os.path.join(second, "Nested")
        os.makedirs(nested)
        overlap = self.client.post(
            "/api/setup/libraries", json={"path": nested}, headers=headers
        )
        self.assertEqual(overlap.status_code, 409)
        setup = self.client.get(
            "/api/setup", headers=headers
        ).get_json()
        self.assertEqual(len(setup["libraries"]), 2)

    def test_setup_relocate_library_preserves_song_and_database_backup(self):
        moved_root = os.path.join(self.root, "Moved Library")
        os.makedirs(moved_root)
        moved_audio = os.path.join(moved_root, "tone.wav")
        shutil.copy2(self.audio_path, moved_audio)
        payload = {"path": self.music, "newPath": moved_root}
        self.assertEqual(
            self.client.post(
                "/api/setup/library/relocate", json=payload
            ).status_code,
            401,
        )
        token = self.client.post(
            "/api/setup/login", json={"password": "1"}
        ).get_json()["token"]
        headers = {"Authorization": "Bearer " + token}
        response = self.client.post(
            "/api/setup/library/relocate", json=payload, headers=headers
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["moved"], 1)
        self.assertEqual(data["missing"], 0)
        self.assertTrue(
            os.path.isfile(
                os.path.join(self.root, data["backup"])
            )
        )
        self.assertEqual(
            os.path.realpath(data["library"]["path"]),
            os.path.realpath(moved_root),
        )
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        song = dict(conn.execute("SELECT * FROM songs").fetchone())
        library = dict(conn.execute("SELECT * FROM libraries").fetchone())
        conn.close()
        self.assertEqual(song["id"], self.song_id)
        self.assertEqual(
            os.path.realpath(song["path"]), os.path.realpath(moved_audio)
        )
        self.assertEqual(
            os.path.realpath(library["path"]), os.path.realpath(moved_root)
        )
        audio = self.client.get(
            "/api/audio/%s?quality=raw" % self.song_id
        )
        self.assertEqual(audio.status_code, 200)
        audio.close()

    def test_legacy_config_paths_seed_database_once(self):
        legacy_db = os.path.join(self.root, "legacy.sqlite")
        app = create_app(
            Config(
                legacy_db,
                os.path.join(self.root, "LegacyArtwork"),
                read_only=False,
                libraries=[{"path": self.music}],
            )
        )
        self.assertIsNotNone(app)
        libraries = LibraryDB(legacy_db).load_libraries()
        self.assertEqual(len(libraries), 1)
        self.assertEqual(libraries[0]["identifier"], "library1")
        self.assertEqual(libraries[0]["id_namespace"], "library1")
        self.assertEqual(
            os.path.realpath(libraries[0]["path"]),
            os.path.realpath(self.music),
        )

    def test_setup_settings_url_format_and_password(self):
        config_path = os.path.join(self.root, "config.yaml")
        with open(config_path, "w", encoding="utf-8") as handle:
            yaml.safe_dump({
                "database_dir": "data",
                "libraries": [{"path": self.music}],
            }, handle)
        app = create_app(
            Config(
                self.db_path,
                self.artwork,
                read_only=False,
                config_path=config_path,
                libraries=[{"path": self.music}],
            )
        )
        client = app.test_client()
        public_setup = client.get("/api/setup").get_json()
        self.assertNotIn("path", public_setup["libraries"][0])
        self.assertEqual(
            client.patch(
                "/api/setup/settings", json={"urlIdFormat": "uuid"}
            ).status_code,
            401,
        )
        token = client.post(
            "/api/setup/login", json={"password": "1"}
        ).get_json()["token"]
        headers = {"Authorization": "Bearer " + token}
        private_setup = client.get("/api/setup", headers=headers).get_json()
        self.assertEqual(
            private_setup["libraries"][0]["path"],
            os.path.realpath(self.music),
        )
        self.assertEqual(
            client.patch(
                "/api/setup/settings",
                json={"urlIdFormat": "invalid"},
                headers=headers,
            ).status_code,
            400,
        )
        response = client.patch(
            "/api/setup/settings",
            json={"urlIdFormat": "base64url"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["urlIdFormat"], "base64url")
        self.assertEqual(
            client.get("/api/setup", headers=headers)
            .get_json()["urlIdFormat"],
            "base64url",
        )
        response = client.patch(
            "/api/setup/settings",
            json={"albumSort": "artist", "albumSortDescending": False},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["albumSort"], "artist")
        self.assertFalse(response.get_json()["albumSortDescending"])
        setup = client.get("/api/setup", headers=headers).get_json()
        self.assertEqual(setup["albumSort"], "artist")
        self.assertFalse(setup["albumSortDescending"])
        self.assertEqual(
            client.patch(
                "/api/setup/settings",
                json={"albumSort": "invalid"},
                headers=headers,
            ).status_code,
            400,
        )
        self.assertEqual(
            client.patch(
                "/api/setup/settings",
                json={"albumSortDescending": "no"},
                headers=headers,
            ).status_code,
            400,
        )
        response = client.patch(
            "/api/setup/settings",
            json={"coverSize": 320, "jpegQuality": 70},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["coverSize"], 320)
        self.assertEqual(response.get_json()["jpegQuality"], 70)
        setup = client.get("/api/setup", headers=headers).get_json()
        self.assertEqual(setup["coverSize"], 320)
        self.assertEqual(setup["jpegQuality"], 70)
        self.assertEqual(
            client.patch(
                "/api/setup/settings",
                json={"coverSize": 8},
                headers=headers,
            ).status_code,
            400,
        )
        self.assertEqual(
            client.patch(
                "/api/setup/settings",
                json={"jpegQuality": 101},
                headers=headers,
            ).status_code,
            400,
        )
        with open(config_path, "r", encoding="utf-8") as handle:
            values = yaml.safe_load(handle)
        self.assertEqual(values["url_id_format"], "base64url")
        self.assertEqual(values["album_sort"], "artist")
        self.assertFalse(values["album_sort_descending"])
        self.assertEqual(values["cover_size"], 320)
        self.assertEqual(values["jpeg_quality"], 70)
        self.assertEqual(values["libraries"], [{"path": self.music}])

        response = client.post(
            "/api/setup/password",
            json={"newPassword": "s3cret"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["reauthenticate"])
        self.assertFalse(
            client.get("/api/setup", headers=headers)
            .get_json()["authenticated"]
        )
        self.assertEqual(
            client.post(
                "/api/setup/login", json={"password": "1"}
            ).status_code,
            401,
        )
        self.assertEqual(
            client.post(
                "/api/setup/login", json={"password": "s3cret"}
            ).status_code,
            200,
        )
        with open(config_path, "r", encoding="utf-8") as handle:
            values = yaml.safe_load(handle)
        self.assertEqual(
            values["admin_password_sha1"],
            hashlib.sha1(b"s3cret").hexdigest(),
        )

    def test_operation_log_records_admin_actions(self):
        with tempfile.TemporaryDirectory() as logs_dir:
            config_path = os.path.join(self.root, "ops-config.yaml")
            with open(config_path, "w", encoding="utf-8") as handle:
                yaml.safe_dump({
                    "database_dir": "data",
                    "libraries": [{"path": self.music}],
                }, handle)
            runtime, operations = configure_logging(
                logs_dir, "INFO", "json"
            )
            try:
                app = create_app(
                    Config(
                        self.db_path,
                        self.artwork,
                        read_only=False,
                        config_path=config_path,
                        libraries=[{"path": self.music}],
                    )
                )
                client = app.test_client()
                proxy_headers = {
                    "X-Forwarded-For": "203.0.113.7, 10.0.0.2",
                }
                client.post(
                    "/api/setup/login", json={"password": "wrong"},
                    headers=proxy_headers,
                )
                token = client.post(
                    "/api/setup/login", json={"password": "1"},
                    headers=proxy_headers,
                ).get_json()["token"]
                client.patch(
                    "/api/setup/settings",
                    json={"urlIdFormat": "base64url"},
                    headers={"Authorization": "Bearer " + token},
                )
                for handler in operations.handlers:
                    handler.flush()
                with open(
                    os.path.join(logs_dir, "operations.log"),
                    "r",
                    encoding="utf-8",
                ) as handle:
                    records = [
                        json.loads(line) for line in handle if line.strip()
                    ]
            finally:
                for logger in (runtime, operations):
                    for handler in list(logger.handlers):
                        logger.removeHandler(handler)
                        handler.close()
        actions = [record["action"] for record in records]
        self.assertIn("admin_login", actions)
        self.assertIn("url_id_format_changed", actions)
        self.assertTrue(any(
            record["action"] == "admin_login"
            and record["result"] == "failure"
            for record in records
        ))
        for record in records:
            self.assertIn("time", record)
            self.assertIn("remoteIp", record)
            self.assertIn("method", record)
            self.assertIn("path", record)
        login_records = [
            record for record in records
            if record["action"] == "admin_login"
        ]
        self.assertTrue(login_records)
        for record in login_records:
            self.assertEqual(record["remoteIp"], "203.0.113.7")
            self.assertEqual(record["proxyIp"], "127.0.0.1")
            self.assertEqual(
                record["forwardedFor"], "203.0.113.7, 10.0.0.2"
            )

    def test_setup_admin_auth_and_scan_guards(self):
        setup = self.client.get("/api/setup")
        self.assertEqual(setup.status_code, 200)
        payload = setup.get_json()
        self.assertEqual(payload["name"], "Setup")
        self.assertFalse(payload["authenticated"])
        self.assertEqual(len(payload["libraries"]), 1)
        self.assertEqual(payload["libraries"][0]["identifier"], "api")
        self.assertNotIn("path", payload["libraries"][0])
        wrong = self.client.post(
            "/api/setup/login", json={"password": "wrong"}
        )
        self.assertEqual(wrong.status_code, 401)
        login = self.client.post(
            "/api/setup/login", json={"password": "1"}
        )
        self.assertEqual(login.status_code, 200)
        token = login.get_json()["token"]
        headers = {"Authorization": "Bearer " + token}
        authed = self.client.get("/api/setup", headers=headers).get_json()
        self.assertTrue(authed["authenticated"])
        self.assertEqual(
            authed["libraries"][0]["path"], os.path.realpath(self.music)
        )
        self.assertEqual(
            self.client.post(
                "/api/setup/scan", json={"mode": "quick"}
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post("/api/setup/rekey").status_code,
            401,
        )


class LoggingTests(unittest.TestCase):
    @staticmethod
    def _close_loggers(loggers):
        for logger in loggers:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()

    def test_text_logs_and_archive_rotation(self):
        import gzip

        with tempfile.TemporaryDirectory() as root:
            runtime, operations = configure_logging(root, "INFO")
            try:
                event = {
                    "time": "2026-01-01T12:00:00+00:00",
                    "action": "test",
                    "result": "success",
                    "remoteIp": "127.0.0.1",
                }
                runtime.info("runtime test message")
                operations.info("operation", extra={"event": event})
                for logger in (runtime, operations):
                    for handler in logger.handlers:
                        handler.flush()
                runtime_path = os.path.join(root, "runtime.log")
                operations_path = os.path.join(root, "operations.log")
                with open(runtime_path, encoding="utf-8") as handle:
                    runtime_text = handle.read()
                with open(operations_path, encoding="utf-8") as handle:
                    operations_text = handle.read()
                self.assertIn("INFO runtime test message", runtime_text)
                self.assertIn(
                    "action=test result=success remoteIp=127.0.0.1",
                    operations_text,
                )
                self.assertNotIn('"action":"test"', operations_text)
                archive = os.path.join(root, "runtime.log.2026-01-01")
                with open(archive, "w", encoding="utf-8") as handle:
                    handle.write("old log\n")
                _compress_archive(archive)
                self.assertFalse(os.path.exists(archive))
                with gzip.open(
                    archive + ".gz", "rt", encoding="utf-8"
                ) as handle:
                    self.assertEqual(handle.read(), "old log\n")
            finally:
                self._close_loggers((runtime, operations))

    def test_json_logs(self):
        with tempfile.TemporaryDirectory() as root:
            runtime, operations = configure_logging(root, "INFO", "json")
            try:
                event = {
                    "time": "2026-01-01T12:00:00+00:00",
                    "action": "test",
                    "result": "success",
                    "remoteIp": "127.0.0.1",
                }
                runtime.info("runtime test message")
                operations.info("operation", extra={"event": event})
                for logger in (runtime, operations):
                    for handler in logger.handlers:
                        handler.flush()
                with open(
                    os.path.join(root, "runtime.log"),
                    encoding="utf-8",
                ) as handle:
                    runtime_record = json.loads(handle.readline())
                with open(
                    os.path.join(root, "operations.log"),
                    encoding="utf-8",
                ) as handle:
                    operation_record = json.loads(handle.readline())
                self.assertEqual(runtime_record["level"], "INFO")
                self.assertEqual(
                    runtime_record["message"], "runtime test message"
                )
                self.assertIn("time", runtime_record)
                self.assertEqual(operation_record["action"], "test")
                self.assertEqual(
                    operation_record["remoteIp"], "127.0.0.1"
                )
            finally:
                self._close_loggers((runtime, operations))


class AdminLoginGuardTests(unittest.TestCase):
    def test_locks_after_limit_is_exceeded_and_expires(self):
        guard = AdminLoginGuard(
            limit=2, window_seconds=60, lock_seconds=100,
            clock=lambda: 1000.0,
        )
        self.assertFalse(guard.record_failure("ip", now=1000))
        self.assertFalse(guard.record_failure("ip", now=1001))
        self.assertTrue(guard.record_failure("ip", now=1002))
        self.assertEqual(guard.is_locked("ip", now=1003), (True, 99))
        self.assertTrue(guard.record_failure("ip", now=1050))
        self.assertEqual(guard.is_locked("ip", now=1102), (False, 0))

    def test_success_clears_failures_and_zero_limit_disables(self):
        guard = AdminLoginGuard(limit=2, window_seconds=60, lock_seconds=100)
        self.assertFalse(guard.record_failure("ip", now=10))
        self.assertFalse(guard.record_failure("ip", now=11))
        guard.record_success("ip")
        self.assertFalse(guard.record_failure("ip", now=12))
        disabled = AdminLoginGuard(limit=0)
        self.assertFalse(disabled.record_failure("ip", now=1))
        self.assertEqual(disabled.is_locked("ip", now=2), (False, 0))

    def test_failures_outside_window_do_not_accumulate(self):
        guard = AdminLoginGuard(
            limit=2, window_seconds=60, lock_seconds=100,
            clock=lambda: 0.0,
        )
        self.assertFalse(guard.record_failure("ip", now=0))
        self.assertFalse(guard.record_failure("ip", now=30))
        self.assertFalse(guard.record_failure("ip", now=61))
        self.assertFalse(guard.is_locked("ip", now=62)[0])


class AnalyticsTests(unittest.TestCase):
    def test_in_memory_traffic_and_operation_totals(self):
        clock = [1000.0]
        analytics = Analytics(clock=lambda: clock[0])
        analytics.record_request(
            "192.0.2.1", "/api/audio/song-a", 200, 1200, "Browser"
        )
        analytics.record_request(
            "192.0.2.1", "/api/artwork/album-a", 200, 300, "Browser"
        )
        analytics.record_request(
            "192.0.2.1", "/api/status", 200, 999, "Browser"
        )
        analytics.record_play("192.0.2.1", "song-a")
        analytics.record_play("192.0.2.1", "song-a")
        analytics.record_operation(
            "192.0.2.1", "playlist_songs_added", "success"
        )
        analytics.record_operation(
            "192.0.2.1", "admin_login", "success"
        )
        analytics.record_operation(
            "192.0.2.2", "download_rate_limited", "blocked"
        )
        analytics.record_admin_blocked("192.0.2.1")
        clock[0] = 1005.0
        snapshot = analytics.snapshot()
        totals = snapshot["totals"]
        self.assertEqual(totals["visitors"], 2)
        self.assertEqual(totals["requests"], 2)
        self.assertEqual(totals["bytesSent"], 1500)
        self.assertEqual(totals["audioBytes"], 1200)
        self.assertEqual(totals["artworkBytes"], 300)
        self.assertEqual(totals["songsPlayed"], 2)
        self.assertEqual(totals["uniqueSongs"], 1)
        self.assertEqual(totals["playlistActions"], 1)
        self.assertEqual(totals["adminLogins"], 1)
        self.assertEqual(totals["adminBruteforceBlocked"], 1)
        self.assertEqual(totals["rateLimited"], 1)
        self.assertEqual(snapshot["uptimeSeconds"], 5)
        self.assertEqual(snapshot["clientCount"], 2)
        first = next(
            client for client in snapshot["clients"]
            if client["ip"] == "192.0.2.1"
        )
        self.assertEqual(first["requests"], 2)
        self.assertEqual(first["bytesSent"], 1500)
        self.assertEqual(first["songsPlayed"], 2)
        self.assertEqual(first["uniqueSongs"], 1)
        self.assertEqual(first["playlistActions"], 1)
        self.assertEqual(first["adminBlocked"], 1)

    def test_keeps_only_the_most_recent_clients(self):
        clock = [1000.0]
        analytics = Analytics(clock=lambda: clock[0], max_clients=2)
        for address in ("192.0.2.1", "192.0.2.2", "192.0.2.3"):
            clock[0] += 1
            analytics.record_request(
                address, "/api/songs", 200, 10, "Browser"
            )
        snapshot = analytics.snapshot()
        self.assertEqual(snapshot["clientCount"], 2)
        self.assertEqual(snapshot["totals"]["visitors"], 2)
        self.assertEqual(
            {client["ip"] for client in snapshot["clients"]},
            {"192.0.2.2", "192.0.2.3"},
        )

        analytics.record_request(
            "192.0.2.2", "/api/songs", 200, 10, "Browser"
        )
        self.assertEqual(
            {client["ip"] for client in analytics.snapshot()["clients"]},
            {"192.0.2.2", "192.0.2.3"},
        )


class RateLimitTests(unittest.TestCase):
    def test_distinct_items_per_ip_in_sliding_window(self):
        limiter = DownloadRateLimiter(2, window_seconds=60)
        self.assertEqual(limiter.check("ip-a", "song-a", now=100), (True, 0))
        self.assertEqual(limiter.check("ip-a", "song-a", now=101), (True, 0))
        self.assertEqual(limiter.check("ip-a", "song-b", now=102), (True, 0))
        allowed, retry_after = limiter.check(
            "ip-a", "song-c", now=103
        )
        self.assertFalse(allowed)
        self.assertEqual(retry_after, 57)
        self.assertEqual(
            limiter.check("ip-b", "song-c", now=103), (True, 0)
        )
        limited, retry_after = limiter.is_limited("ip-a", now=104)
        self.assertTrue(limited)
        self.assertEqual(retry_after, 56)
        self.assertEqual(
            limiter.check("ip-a", "song-c", now=161), (True, 0)
        )

    def test_zero_limit_disables_rate_limiting(self):
        limiter = DownloadRateLimiter(0)
        for index in range(100):
            self.assertEqual(
                limiter.check("ip", "song-%d" % index), (True, 0)
            )
        self.assertEqual(limiter.is_limited("ip"), (False, 0))


class ClientIpTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def client_ip(self, headers=None, remote_addr="192.0.2.10"):
        environ = {"REMOTE_ADDR": remote_addr}
        with self.app.test_request_context(headers=headers or {},
                                           environ_base=environ):
            return _client_ip()

    def test_cloudflare_connecting_ip_takes_priority(self):
        self.assertEqual(
            self.client_ip({
                "CF-Connecting-IP": "203.0.113.20",
                "X-Forwarded-For": "198.51.100.1",
                "X-Real-IP": "198.51.100.2",
            }),
            "203.0.113.20",
        )
        self.assertEqual(
            self.client_ip({
                "CF-Connecting-IP": "unknown",
                "X-Forwarded-For": "198.51.100.1",
            }),
            "198.51.100.1",
        )

    def test_forwarded_for_takes_priority(self):
        self.assertEqual(
            self.client_ip({
                "X-Forwarded-For": "203.0.113.7, 10.0.0.2, 10.0.0.3",
                "X-Real-IP": "198.51.100.9",
            }),
            "203.0.113.7",
        )

    def test_forwarded_for_skips_invalid_and_normalizes(self):
        self.assertEqual(
            self.client_ip({
                "X-Forwarded-For": "unknown, 203.0.113.7:54321, 10.0.0.2",
            }),
            "203.0.113.7",
        )
        self.assertEqual(
            self.client_ip({
                "X-Forwarded-For": "[2001:db8::1]:443, 10.0.0.2",
            }),
            "2001:db8::1",
        )

    def test_real_ip_and_socket_fallbacks(self):
        self.assertEqual(
            self.client_ip({"X-Real-IP": "198.51.100.9"}),
            "198.51.100.9",
        )
        self.assertEqual(
            self.client_ip(remote_addr="192.0.2.55"),
            "192.0.2.55",
        )


class PresenceTrackerTests(unittest.TestCase):
    def test_presence_is_keyed_by_ip_and_bounded(self):
        tracker = PresenceTracker(ttl=30, max_entries=2)
        self.assertEqual(tracker.touch("192.0.2.1"), 1)
        self.assertEqual(tracker.touch("192.0.2.2"), 2)
        self.assertEqual(tracker.touch("192.0.2.1"), 2)
        self.assertEqual(tracker.touch("192.0.2.3"), 2)
        self.assertEqual(
            tracker.counts_by_ip(),
            {"192.0.2.1": 1, "192.0.2.3": 1},
        )


class ConfigTests(unittest.TestCase):
    def _clean_env(self):
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("WEBTRACKS_")
        }
        return mock.patch.dict(os.environ, env, clear=True)

    def test_paths_are_relative_to_the_config_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    '{"database_dir": "state",'
                    ' "music_dirs": ["music", "~/Music"]}'
                )
            with self._clean_env():
                settings = load_settings(path)
            self.assertEqual(
                settings["database"],
                os.path.join(root, "state", "library.sqlite"),
            )
            self.assertEqual(
                settings["artwork"],
                os.path.join(root, "state", "Artwork"),
            )
            self.assertEqual(
                settings["music_dirs"],
                [os.path.realpath(os.path.join(root, "music")),
                 os.path.realpath(os.path.expanduser("~/Music"))],
            )

    def test_transcode_cache_size_from_config_and_env(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"transcode_cache_mb": 256}')
            with self._clean_env():
                settings = load_settings(path)
                self.assertEqual(settings["transcode_cache_mb"], 256)
                os.environ["WEBTRACKS_TRANSCODE_CACHE_MB"] = "64"
                self.assertEqual(
                    load_settings(path)["transcode_cache_mb"], 64
                )
                del os.environ["WEBTRACKS_TRANSCODE_CACHE_MB"]
                os.environ["WEBTRACKS_AAC_CACHE_MB"] = "32"
                self.assertEqual(
                    load_settings(path)["transcode_cache_mb"], 32
                )

    def test_server_header_default_and_override(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"database_dir": "var"}')
            with self._clean_env():
                settings = load_settings(path)
            self.assertEqual(settings["server_header"], "")
            self.assertEqual(settings["trusted_hosts"], ["*"])

            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "server_header": "WebTracks",
                    "trusted_hosts": ["music.example.com"],
                }, handle)
            with self._clean_env():
                settings = load_settings(path)
            self.assertEqual(settings["server_header"], "WebTracks")
            self.assertEqual(
                settings["trusted_hosts"], ["music.example.com"]
            )
            with self._clean_env():
                os.environ["WEBTRACKS_SERVER_HEADER"] = "Hidden"
                os.environ["WEBTRACKS_TRUSTED_HOSTS"] = "a.example,b.example"
                settings = load_settings(path)
            self.assertEqual(settings["server_header"], "Hidden")
            self.assertEqual(
                settings["trusted_hosts"], ["a.example", "b.example"]
            )

    def test_audio_formats_default_and_override(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"database_dir": "var"}')
            with self._clean_env():
                settings = load_settings(path)
            self.assertEqual(
                settings["audio_formats"],
                [{"index": 0, "format": "aac", "bitrate": 128}],
            )
            self.assertEqual(settings["default_audio_format"], 0)

            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "audio_formats": [
                        {"index": 0, "format": "aac", "bitrate": 128},
                        {"index": 1, "format": "aac", "bitrate": 96},
                        {"index": 2, "format": "raw"},
                    ],
                    "default_audio_format": 1,
                }, handle)
            with self._clean_env():
                settings = load_settings(path)
            self.assertEqual(
                [entry["format"] for entry in settings["audio_formats"]],
                ["aac", "aac", "raw"],
            )
            self.assertEqual(
                settings["audio_formats"][1]["bitrate"], 96
            )
            self.assertEqual(settings["default_audio_format"], 1)

    def test_album_sort_defaults_and_override(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"database_dir": "var"}')
            with self._clean_env():
                settings = load_settings(path)
                self.assertEqual(settings["album_sort"], "recentlyAdded")
                self.assertTrue(settings["album_sort_descending"])
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    '{"database_dir": "var", "album_sort": "artist",'
                    ' "album_sort_descending": false}'
                )
            with self._clean_env():
                settings = load_settings(path)
                self.assertEqual(settings["album_sort"], "artist")
                self.assertFalse(settings["album_sort_descending"])
                os.environ["WEBTRACKS_ALBUM_SORT"] = "year"
                os.environ["WEBTRACKS_ALBUM_SORT_DESCENDING"] = "1"
                settings = load_settings(path)
                self.assertEqual(settings["album_sort"], "year")
                self.assertTrue(settings["album_sort_descending"])
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    '{"database_dir": "var", "album_sort": "unknown"}'
                )
            with self._clean_env():
                self.assertEqual(
                    load_settings(path)["album_sort"], "recentlyAdded"
                )

    def test_admin_password_hash_default_and_override(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"database_dir": "var"}')
            with self._clean_env():
                self.assertEqual(
                    load_settings(path)["admin_password_sha1"],
                    "356a192b7913b04c54574d18c28d46e6395428ab",
                )
                os.environ["WEBTRACKS_ADMIN_PASSWORD_SHA1"] = (
                    "b" * 40
                )
                self.assertEqual(
                    load_settings(path)["admin_password_sha1"], "b" * 40
                )

    def test_library_config_only_stores_paths(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "config.json")
            music = os.path.join(root, "Music")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "database_dir": "var",
                    "libraries": [{
                        "identifier": "main",
                        "namespace": "main",
                        "path": music,
                    }],
                }, handle)
            with self._clean_env():
                settings = load_settings(path)
            self.assertEqual(
                settings["libraries"], [{"path": os.path.realpath(music)}]
            )
            self.assertEqual(settings["music_dirs"], [os.path.realpath(music)])
            update_config(path, url_id_format="base64url")
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            self.assertEqual(raw["libraries"], [{"path": music}])
            self.assertEqual(raw["url_id_format"], "base64url")

    def test_yaml_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "config.yaml")
            music = os.path.join(root, "Music")
            with open(path, "w", encoding="utf-8") as handle:
                yaml.safe_dump({
                    "database_dir": "var",
                    "log_level": "warning",
                    "logs_dir": "logfiles",
                    "libraries": [{
                        "identifier": "main",
                        "namespace": "main",
                        "path": music,
                    }],
                }, handle)
            with self._clean_env():
                settings = load_settings(path)
            self.assertEqual(settings["log_level"], "WARNING")
            self.assertEqual(
                settings["logs_dir"], os.path.realpath(
                    os.path.join(root, "logfiles")
                )
            )
            self.assertEqual(
                settings["libraries"], [{"path": os.path.realpath(music)}]
            )
            update_config(path, log_level="debug")
            with open(path, "r", encoding="utf-8") as handle:
                raw = yaml.safe_load(handle)
            self.assertEqual(raw["libraries"], [{"path": music}])
            self.assertEqual(raw["log_level"], "debug")

    def test_log_format_defaults_and_env_override(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "config.yaml")
            with open(path, "w", encoding="utf-8") as handle:
                yaml.safe_dump({"database_dir": "var"}, handle)
            with self._clean_env():
                self.assertEqual(load_settings(path)["log_format"], "text")
                os.environ["WEBTRACKS_LOG_FORMAT"] = "json"
                self.assertEqual(load_settings(path)["log_format"], "json")
                os.environ["WEBTRACKS_LOG_FORMAT"] = "invalid"
                self.assertEqual(load_settings(path)["log_format"], "text")
            with open(path, "w", encoding="utf-8") as handle:
                yaml.safe_dump({
                    "database_dir": "var",
                    "log_format": "JSON",
                }, handle)
            with self._clean_env():
                self.assertEqual(load_settings(path)["log_format"], "json")

    def test_cover_size_and_jpeg_quality_overrides(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "config.yaml")
            with open(path, "w", encoding="utf-8") as handle:
                yaml.safe_dump({"database_dir": "var"}, handle)
            with self._clean_env():
                settings = load_settings(path)
                self.assertEqual(settings["cover_size"], 500)
                self.assertEqual(settings["jpeg_quality"], 80)
                os.environ["WEBTRACKS_COVER_SIZE"] = "300"
                os.environ["WEBTRACKS_JPEG_QUALITY"] = "65"
                settings = load_settings(path)
                self.assertEqual(settings["cover_size"], 300)
                self.assertEqual(settings["jpeg_quality"], 65)
            with open(path, "w", encoding="utf-8") as handle:
                yaml.safe_dump({
                    "database_dir": "var",
                    "cover_size": 2000,
                    "jpeg_quality": 100,
                }, handle)
            with self._clean_env():
                settings = load_settings(path)
                self.assertEqual(settings["cover_size"], 2000)
                self.assertEqual(settings["jpeg_quality"], 100)

    def test_admin_bruteforce_defaults_and_env_override(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "config.yaml")
            with open(path, "w", encoding="utf-8") as handle:
                yaml.safe_dump({"database_dir": "var"}, handle)
            with self._clean_env():
                settings = load_settings(path)
                self.assertEqual(settings["admin_bruteforce_limit"], 5)
                self.assertEqual(
                    settings["admin_bruteforce_window_seconds"], 60
                )
                self.assertEqual(
                    settings["admin_bruteforce_lock_seconds"], 86400
                )
                os.environ["WEBTRACKS_ADMIN_BRUTEFORCE_LIMIT"] = "3"
                os.environ[
                    "WEBTRACKS_ADMIN_BRUTEFORCE_WINDOW_SECONDS"
                ] = "30"
                os.environ[
                    "WEBTRACKS_ADMIN_BRUTEFORCE_LOCK_SECONDS"
                ] = "120"
                settings = load_settings(path)
                self.assertEqual(settings["admin_bruteforce_limit"], 3)
                self.assertEqual(
                    settings["admin_bruteforce_window_seconds"], 30
                )
                self.assertEqual(
                    settings["admin_bruteforce_lock_seconds"], 120
                )
            with open(path, "w", encoding="utf-8") as handle:
                yaml.safe_dump({
                    "database_dir": "var",
                    "admin_bruteforce_limit": 0,
                }, handle)
            with self._clean_env():
                self.assertEqual(
                    load_settings(path)["admin_bruteforce_limit"], 0
                )

    def test_rate_limit_defaults_and_env_override(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "config.yaml")
            with open(path, "w", encoding="utf-8") as handle:
                yaml.safe_dump({"database_dir": "var"}, handle)
            with self._clean_env():
                self.assertEqual(load_settings(path)["rate_limit"], 28)
                os.environ["WEBTRACKS_RATE_LIMIT"] = "3"
                self.assertEqual(load_settings(path)["rate_limit"], 3)
                os.environ["WEBTRACKS_RATE_LIMIT"] = "invalid"
                self.assertEqual(load_settings(path)["rate_limit"], 28)
            with open(path, "w", encoding="utf-8") as handle:
                yaml.safe_dump({
                    "database_dir": "var",
                    "rate_limit": 0,
                }, handle)
            with self._clean_env():
                self.assertEqual(load_settings(path)["rate_limit"], 0)

    def test_example_config_is_complete_and_not_ignored(self):
        example = os.path.join(ROOT, "config.example.yaml")
        with open(example, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        self.assertEqual(set(raw), set(DEFAULT_SETTINGS))
        self.assertEqual(
            raw["admin_password_sha1"], DEFAULT_ADMIN_PASSWORD_SHA1
        )

        with open(os.path.join(ROOT, ".gitignore"), "r",
                  encoding="utf-8") as handle:
            ignored = {
                line.strip() for line in handle
                if line.strip() and not line.lstrip().startswith("#")
            }
        self.assertIn("config.yaml", ignored)
        self.assertNotIn("config.example.yaml", ignored)

        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "config.yaml")
            shutil.copyfile(example, target)
            with self._clean_env():
                settings = load_settings(target)
            self.assertEqual(settings["port"], 8765)
            self.assertEqual(
                settings["database"],
                os.path.join(root, "data", "library.sqlite"),
            )
            self.assertEqual(
                settings["admin_password_sha1"],
                DEFAULT_ADMIN_PASSWORD_SHA1,
            )

    def test_init_creates_local_database_and_artwork_dir(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"database_dir": "var"}')
            with self._clean_env():
                code = main(["init", "--config", path])
            self.assertEqual(code, 0)
            self.assertTrue(
                LibraryDB(
                    os.path.join(root, "var", "library.sqlite")
                ).check_schema()
            )
            self.assertTrue(
                os.path.isdir(os.path.join(root, "var", "Artwork"))
            )


if __name__ == "__main__":
    unittest.main()
