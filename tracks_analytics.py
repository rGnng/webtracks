"""In-memory, process-lifetime usage analytics."""

import collections
import datetime
import threading
import time


EXCLUDED_PATHS = {
    "/api/cache",
    "/api/setup/stats",
    "/api/status",
}

SETTINGS_ACTIONS = {
    "admin_password_changed",
    "album_sort_changed",
    "artwork_cache_changed",
    "library_identifier_changed",
    "url_id_format_changed",
}


def _iso(value):
    return datetime.datetime.fromtimestamp(
        value
    ).astimezone().isoformat(timespec="seconds")


class Analytics:
    """Aggregate traffic, playback, and operation counters per client IP."""

    def __init__(self, clock=None, max_clients=50):
        self._clock = clock or time.time
        self.max_clients = max(1, int(max_clients))
        self.started_at = self._clock()
        self._lock = threading.RLock()
        self._clients = collections.OrderedDict()
        self._requests = 0
        self._bytes_sent = 0
        self._audio_bytes = 0
        self._artwork_bytes = 0
        self._songs_played = 0
        self._unique_songs = set()
        self._playlist_actions = 0
        self._admin_logins = 0
        self._scans = 0
        self._rekeys = 0
        self._artwork_rebuilds = 0
        self._settings_changes = 0
        self._rate_limited = 0
        self._admin_bruteforce_blocked = 0
        self._action_counts = {}

    def _client(self, address):
        client = self._clients.get(address)
        if client is None:
            now = self._clock()
            client = {
                "firstSeen": now,
                "lastSeen": now,
                "requests": 0,
                "bytesSent": 0,
                "audioBytes": 0,
                "artworkBytes": 0,
                "songsPlayed": 0,
                "uniqueSongs": set(),
                "playlistActions": 0,
                "adminLogins": 0,
                "adminBlocked": 0,
                "rateLimited": 0,
                "lastPath": "",
                "userAgent": "",
            }
            self._clients[address] = client
            while len(self._clients) > self.max_clients:
                self._clients.popitem(last=False)
        else:
            self._clients.move_to_end(address)
        return client

    def record_request(self, address, path, status, bytes_sent, user_agent):
        if not address or path in EXCLUDED_PATHS:
            return
        try:
            size = max(0, int(bytes_sent or 0))
        except (TypeError, ValueError):
            size = 0
        now = self._clock()
        with self._lock:
            client = self._client(address)
            client["lastSeen"] = now
            client["requests"] += 1
            client["bytesSent"] += size
            client["lastPath"] = path
            if user_agent:
                client["userAgent"] = str(user_agent)
            self._requests += 1
            self._bytes_sent += size
            if path.startswith("/api/audio/"):
                client["audioBytes"] += size
                self._audio_bytes += size
            elif path.startswith("/api/artwork/"):
                client["artworkBytes"] += size
                self._artwork_bytes += size

    def record_play(self, address, song_id):
        if not address or not song_id:
            return
        now = self._clock()
        with self._lock:
            client = self._client(address)
            client["lastSeen"] = now
            client["songsPlayed"] += 1
            client["uniqueSongs"].add(song_id)
            self._songs_played += 1
            self._unique_songs.add(song_id)

    def record_operation(self, address, action, result="success"):
        if not address or not action:
            return
        now = self._clock()
        with self._lock:
            client = self._client(address)
            client["lastSeen"] = now
            self._action_counts[action] = (
                self._action_counts.get(action, 0) + 1
            )
            if action.startswith("playlist_") and result == "success":
                client["playlistActions"] += 1
                self._playlist_actions += 1
            if action == "admin_login" and result == "success":
                client["adminLogins"] += 1
                self._admin_logins += 1
            if action == "scan_started" and result == "success":
                self._scans += 1
            elif action == "rekey_started" and result == "success":
                self._rekeys += 1
            elif (
                action == "artwork_rebuild_started"
                and result == "success"
            ):
                self._artwork_rebuilds += 1
            elif action == "download_rate_limited":
                client["rateLimited"] += 1
                self._rate_limited += 1
            elif action in SETTINGS_ACTIONS and result == "success":
                self._settings_changes += 1

    def record_admin_blocked(self, address):
        if not address:
            return
        now = self._clock()
        with self._lock:
            client = self._client(address)
            client["lastSeen"] = now
            client["adminBlocked"] += 1
            self._admin_bruteforce_blocked += 1

    def snapshot(self, limit=12):
        now = self._clock()
        with self._lock:
            clients = []
            for address, client in self._clients.items():
                clients.append({
                    "ip": address,
                    "firstSeen": _iso(client["firstSeen"]),
                    "lastSeen": _iso(client["lastSeen"]),
                    "requests": client["requests"],
                    "bytesSent": client["bytesSent"],
                    "audioBytes": client["audioBytes"],
                    "artworkBytes": client["artworkBytes"],
                    "songsPlayed": client["songsPlayed"],
                    "uniqueSongs": len(client["uniqueSongs"]),
                    "playlistActions": client["playlistActions"],
                    "adminLogins": client["adminLogins"],
                    "adminBlocked": client["adminBlocked"],
                    "rateLimited": client["rateLimited"],
                    "lastPath": client["lastPath"],
                    "userAgent": client["userAgent"],
                })
            clients.sort(
                key=lambda item: (item["lastSeen"], item["bytesSent"]),
                reverse=True,
            )
            return {
                "since": _iso(self.started_at),
                "uptimeSeconds": max(0, int(now - self.started_at)),
                "totals": {
                    "visitors": len(self._clients),
                    "requests": self._requests,
                    "bytesSent": self._bytes_sent,
                    "audioBytes": self._audio_bytes,
                    "artworkBytes": self._artwork_bytes,
                    "songsPlayed": self._songs_played,
                    "uniqueSongs": len(self._unique_songs),
                    "playlistActions": self._playlist_actions,
                    "adminLogins": self._admin_logins,
                    "scans": self._scans,
                    "rekeys": self._rekeys,
                    "artworkRebuilds": self._artwork_rebuilds,
                    "settingsChanges": self._settings_changes,
                    "rateLimited": self._rate_limited,
                    "adminBruteforceBlocked": self._admin_bruteforce_blocked,
                },
                "clients": clients[:max(1, int(limit))],
                "clientCount": len(clients),
            }
