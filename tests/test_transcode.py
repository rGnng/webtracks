import os
import shutil
import tempfile
import unittest

from tracks_transcode import DEFAULT_QUALITY, TranscodeCache


class TranscodeCacheTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tracks-transcode-test-")
        self.paths = []
        for index in range(4):
            path = os.path.join(self.root, "song%d.flac" % index)
            with open(path, "wb") as handle:
                handle.write(b"x" * (index + 1))
            self.paths.append(path)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _cache(self, max_bytes, payloads):
        cache = TranscodeCache(max_bytes=max_bytes)
        cache._encode = lambda path, quality: payloads[path]
        return cache

    def test_lru_evicts_least_recently_hit(self):
        payloads = {
            path: bytes([65 + index]) * 400
            for index, path in enumerate(self.paths)
        }
        cache = self._cache(1000, payloads)
        cache.get_or_encode("id0", self.paths[0])
        cache.get_or_encode("id1", self.paths[1])
        # Hit id0 so id1 becomes the least recently used entry.
        cache.get_or_encode("id0", self.paths[0])
        cache.get_or_encode("id2", self.paths[2])
        stats = cache.stats()
        self.assertEqual(stats["entries"], 2)
        self.assertEqual(stats["usedBytes"], 800)
        self.assertEqual(stats["evictions"], 1)
        self.assertEqual(stats["hits"], 1)
        # id1 was evicted; id0 and id2 remain in LRU order.
        self.assertEqual(
            [key[0] for key in cache._cache.keys()],
            ["id0", "id2"],
        )

    def test_zero_budget_serves_without_retaining(self):
        cache = self._cache(0, {self.paths[0]: b"z" * 10})
        data, etag = cache.get_or_encode("id0", self.paths[0])
        self.assertEqual(data, b"z" * 10)
        self.assertTrue(etag.startswith('"'))
        self.assertEqual(cache.stats()["entries"], 0)

    def test_entry_larger_than_budget_is_served_but_not_retained(self):
        cache = self._cache(100, {self.paths[0]: b"q" * 500})
        data, _etag = cache.get_or_encode("id0", self.paths[0])
        self.assertEqual(len(data), 500)
        self.assertEqual(cache.stats()["entries"], 0)

    def test_unsupported_quality_is_rejected(self):
        cache = self._cache(100, {self.paths[0]: b"x"})
        with self.assertRaises(ValueError):
            cache.get_or_encode("id0", self.paths[0], "flac")


class TranscodeQualityTests(unittest.TestCase):
    def setUp(self):
        import wave

        self.root = tempfile.mkdtemp(prefix="tracks-transcode-api-test-")
        self.audio_path = os.path.join(self.root, "tone.wav")
        with wave.open(self.audio_path, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 8000)

        from server import Config, create_app
        from tracks_db import LibraryDB
        from tracks_scanner import ScannedTrack

        self.db_path = os.path.join(self.root, "library.sqlite")
        db = LibraryDB(self.db_path)
        db.ensure_schema()
        db.ensure_library(os.path.dirname(self.audio_path), "api")
        db.sync_tracks(
            [
                ScannedTrack(
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
            ],
            db.load_libraries(),
        )
        app = create_app(
            Config(
                self.db_path,
                os.path.join(self.root, "Artwork"),
                read_only=False,
                transcode_cache_mb=1,
            )
        )
        self.client = app.test_client()
        songs = self.client.get("/api/songs").get_json()["songs"]
        self.song_id = songs[0]["id"]

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_quality_catalog_and_cache_endpoints(self):
        payload = self.client.get("/api/quality").get_json()
        self.assertEqual(payload["default"], DEFAULT_QUALITY)
        self.assertEqual(payload["qualities"][0]["id"], DEFAULT_QUALITY)
        self.assertEqual(payload["qualities"][0]["bitrate"], 128)
        self.assertNotIn("cache", payload)
        cache = self.client.get("/api/cache").get_json()
        self.assertEqual(cache["mode"], "memory")
        self.assertEqual(cache["maxBytes"], 1024 * 1024)
        self.assertEqual(cache["entries"], 0)
        self.assertEqual(cache["qualities"][0]["id"], DEFAULT_QUALITY)
        self.assertEqual(cache["qualities"][0]["entries"], 0)

    def test_aac_streaming_and_range(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg is required for AAC encoding")
        response = self.client.get(
            "/api/audio/%s?quality=aac128" % self.song_id
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "audio/mp4")
        self.assertGreater(len(response.data), 0)
        self.assertEqual(response.headers["Accept-Ranges"], "bytes")
        full_length = len(response.data)

        ranged = self.client.get(
            "/api/audio/%s?quality=aac128" % self.song_id,
            headers={"Range": "bytes=0-99"},
        )
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.headers["Content-Range"],
                         "bytes 0-99/%d" % full_length)
        self.assertEqual(len(ranged.data), 100)

        cache = self.client.get("/api/cache").get_json()
        self.assertEqual(cache["entries"], 1)
        self.assertEqual(cache["usedBytes"], full_length)
        self.assertGreaterEqual(cache["hits"], 1)
        self.assertEqual(cache["qualities"][0]["entries"], 1)
        self.assertEqual(cache["qualities"][0]["usedBytes"], full_length)


if __name__ == "__main__":
    unittest.main()
