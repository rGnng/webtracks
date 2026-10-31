/* WebTracks — local-first web player for a Tracks SQLite library. */
(() => {
  "use strict";

  const PREF_KEY = "tracksWeb.prefs.v1";
  const ADMIN_TOKEN_KEY = "tracksWeb.adminToken";
  const PLAYER_KEY = "tracksWeb.player.v1";
  const PLAYER_STATE_KEY = "tracksWeb.player.state.v1";
  const PLAYER_QUEUE_KEY = "tracksWeb.player.queue.v1";
  const HISTORY_LIMIT = 50;
  const MAX_PERSIST_QUEUE = 2500;
  const DEFAULT_ALBUM_PAGE_CAPACITY = 60;
  const ALBUM_GRID_PADDING_TOP = 16;
  const ALBUM_GRID_PADDING_BOTTOM = 34;
  const ALBUM_GRID_OVERSCAN_ROWS = 2;

  const ALBUM_SORT_KEYS = ["recentlyAdded", "album", "artist", "year"];
  const SECTIONS = [
    { id: "albums", label: "Albums", icon: "albums" },
    { id: "artists", label: "Artists", icon: "artists" },
    { id: "songs", label: "Songs", icon: "music" },
    { id: "playlists", label: "Playlists", icon: "star" },
  ];

  const state = {
    boot: null,
    albums: [],
    albumMap: new Map(),
    playlists: [],
    libraries: [],
    musicDirs: [],
    stats: emptyStats(),
    starredStats: emptyStats(),
    readOnly: false,
    needsSetup: true,
    artVersion: 1,
    allSongs: null,
    allSongsPromise: null,
    songById: new Map(),
    songsByAlbum: new Map(),
    albumDetails: new Map(),
    playlistDetails: new Map(),
    section: "albums",
    playlistId: null,
    albumDetail: null,
    albumPage: 1,
    albumPageMetrics: null,
    albumVirtual: null,
    albumReturnScroll: 0,
    selectedAlbums: new Set(),
    albumAnchor: null,
    selectedSongs: new Set(),
    songAnchor: null,
    selectedArtist: null,
    selectedGenre: null,
    query: "",
    albumSort: "recentlyAdded",
    albumSortAsc: false,
    albumSortInitialized: false,
    urlIdFormat: "uuid",
    songSorts: {},
    audioQuality: "",
    audioQualities: [],
    defaultAudioQuality: "",
    songRows: [],
    songContext: { kind: "songs", playlistId: null },
    songVirtual: null,
    browserGroups: [],
    browserKind: null,
    browserFocus: "list",
    browserAlbumAnchor: null,
    viewToken: 0,
    contentCleanup: null,
    queueOpen: false,
    queueTab: "next",
    queueItems: [],
    queueIsNext: true,
    showRemaining: false,
    lcdSongId: null,
    seekPreview: null,
    scanJob: null,
    scanTimer: null,
    scanRefreshed: false,
    setupOpen: false,
    setupInfo: null,
    setupError: "",
    adminAuthenticated: false,
    rekeyJob: null,
    rekeyTimer: null,
    artworkJob: null,
    artworkTimer: null,
    setupStats: null,
    statsError: "",
    statsTimer: null,
    statsInFlight: false,
    modalMode: null,
    confirmCallback: null,
    browserListVirtual: null,
    playlistDialog: null,
    libraryIdTarget: null,
    addToPlaylistIds: [],
    lastPersistAt: 0,
    doneInitialLoad: false,
  };

  const player = {
    queue: [],
    baseQueue: [],
    index: -1,
    playing: false,
    elapsed: 0,
    history: [],
    shuffle: false,
    repeat: "off",
    pendingRecord: false,
    playId: "",
    buffering: false,
    loadedSongId: null,
    pendingSeek: null,
    pendingPlay: false,
    missing: new Set(),
  };

  const el = {};

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  function init() {
    [
      "app", "toolbar", "section-tabs", "content", "statusbar",
      "status-text", "status-cache", "status-playing", "status-system",
      "status-online", "status-online-count", "setup-button",
      "prev-button", "play-button", "play-icon",
      "next-button", "quality-button", "lcd", "lcd-idle", "lcd-playing",
      "lcd-art", "lcd-art-wrap", "lcd-loading", "lcd-title", "lcd-subtitle",
      "lcd-shuffle", "lcd-star", "lcd-repeat", "lcd-elapsed",
      "lcd-duration", "lcd-seek", "lcd-seek-fill", "lcd-title-button",
      "queue-button", "queue-popover", "queue-list", "queue-clear",
      "queue-count", "search-field", "search-input", "search-clear",
      "audio", "menu-root", "modal-root", "toast-root",
    ].forEach((id) => {
      el[id.replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = document.getElementById(id);
    });
    el.playUse = el.playIcon.querySelector("use");
    el.lcdStarUse = el.lcdStar.querySelector("use");
    el.lcdRepeatUse = el.lcdRepeat.querySelector("use");
    el.queueUse = el.queueButton.querySelector("use");
    el.statusPlayUse = el.statusPlaying.querySelector("use");
    el.lcdArt.addEventListener("error", () => {
      el.lcdArtWrap.classList.add("no-art");
    });

    loadPrefs();
    restorePlayer();
    bindEvents();
    applyAudioQualityUI();
    refreshSystemStatus();
    setInterval(refreshSystemStatus, 5000);
    renderPlayerUI();
    loadInitial().then(async () => {
      await refreshAdminState();
      await initializeRoute();
    });
  }

  async function refreshAdminState() {
    if (!adminToken()) {
      state.adminAuthenticated = false;
      return;
    }
    try {
      const info = await api("/api/setup", { headers: adminHeaders() });
      state.adminAuthenticated = Boolean(info.authenticated);
      if (info.authenticated) state.setupInfo = info;
      else setAdminToken("");
    } catch (_) {
      state.adminAuthenticated = false;
    }
    if (state.section === "playlists" && !state.albumDetail) renderContent();
  }

  // ---------------------------------------------------------------------
  // Small helpers

  function $(selector, root = document) {
    return root.querySelector(selector);
  }

  function $$(selector, root = document) {
    return Array.from(root.querySelectorAll(selector));
  }

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);
  }

  function icon(name, className = "") {
    return `<svg class="icon ${className}"><use href="#i-${name}"></use></svg>`;
  }

  function spinnerHtml(className = "") {
    const bars = Array.from(
      { length: 12 },
      (_, index) => `<span style="--i:${index}"></span>`,
    ).join("");
    return `<span class="apple-spinner${className ? ` ${className}` : ""}" aria-hidden="true">${bars}</span>`;
  }

  function clamp(value, minimum, maximum) {
    return Math.min(Math.max(value, minimum), maximum);
  }

  function emptyStats() {
    return { songCount: 0, duration: 0, size: 0, albumCount: 0 };
  }

  function fmtTime(seconds) {
    const value = Math.max(0, Math.round(Number(seconds) || 0));
    return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
  }

  function fmtRemaining(seconds) {
    return `-${fmtTime(seconds)}`;
  }

  function fmtDurationLong(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    if (total >= 3600) {
      return `${Math.floor(total / 3600)}:${String(Math.floor((total % 3600) / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
    }
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  }

  function fmtSize(bytes) {
    const value = Number(bytes) || 0;
    if (value >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(1)} GB`;
    if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)} MB`;
    if (value >= 1_000) return `${Math.round(value / 1_000)} KB`;
    return `${Math.round(value)} B`;
  }

  function fmtCacheSize(bytes) {
    let value = Math.max(0, Number(bytes) || 0);
    const units = ["B", "KB", "MB", "GB"];
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    const digits = unit === 0 ? 0 : value >= 100 ? 0 : 1;
    return `${value.toFixed(digits)} ${units[unit]}`;
  }

  function statsText(stats) {
    if (!stats) return "";
    const count = Number(stats.songCount) || 0;
    const total = Math.max(0, Math.round(Number(stats.duration) || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const duration = hours > 0 ? `${hours} hr ${minutes} min` : `${minutes} min`;
    const items = count === 1 ? "1 item" : `${count.toLocaleString()} items`;
    return `${items}, ${duration}, ${fmtSize(stats.size)}`;
  }

  function statsForSongs(songs) {
    let duration = 0;
    let size = 0;
    for (const song of songs || []) {
      duration += Number(song.duration) || 0;
      size += Number(song.fileSize) || 0;
    }
    return { songCount: (songs || []).length, duration, size };
  }

  function statsForAlbums(albums) {
    const result = { songCount: 0, duration: 0, size: 0 };
    for (const album of albums || []) {
      result.songCount += Number(album.songCount) || 0;
      result.duration += Number(album.duration) || 0;
      result.size += Number(album.size) || 0;
    }
    return result;
  }

  function yearNumber(value) {
    const match = String(value || "").match(/^\s*(\d{1,4})/);
    return match ? Number(match[1]) : 0;
  }

  function albumCompare(a, b) {
    return a.title.localeCompare(b.title, undefined, {
      numeric: true, sensitivity: "variant",
    });
  }

  function sortedAlbums(albums) {
    const list = (albums || []).slice();
    if (state.albumSort === "recentlyAdded") {
      list.sort((a, b) => {
        const difference = (a.addedAt || 0) - (b.addedAt || 0);
        return Math.abs(difference) < 0.5 ? albumCompare(a, b) : difference;
      });
    } else if (state.albumSort === "artist") {
      list.sort((a, b) => {
        const artistOrder = a.artist.localeCompare(b.artist, undefined, {
          numeric: true, sensitivity: "variant",
        });
        if (artistOrder !== 0) return artistOrder;
        return albumCompare(a, b);
      });
    } else if (state.albumSort === "year") {
      list.sort((a, b) => {
        const difference = yearNumber(a.year) - yearNumber(b.year);
        return difference !== 0 ? difference : albumCompare(a, b);
      });
    } else {
      list.sort(albumCompare);
    }
    if (!state.albumSortAsc) list.reverse();
    return list;
  }

  function matchesQuery(value) {
    return String(value || "").toLowerCase().includes(state.query);
  }

  function debounce(fn, wait) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  }

  async function api(path, options = {}) {
    const headers = Object.assign({ Accept: "application/json" }, options.headers || {});
    if (options.body && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    const response = await fetch(path, { ...options, headers });
    let data = null;
    try {
      data = await response.json();
    } catch (_) {
      data = null;
    }
    if (!response.ok) {
      const message = (data && data.error) || `${response.status} ${response.statusText}`;
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function adminToken() {
    try {
      return sessionStorage.getItem(ADMIN_TOKEN_KEY) || "";
    } catch (_) {
      return "";
    }
  }

  function setAdminToken(token) {
    try {
      if (token) sessionStorage.setItem(ADMIN_TOKEN_KEY, token);
      else sessionStorage.removeItem(ADMIN_TOKEN_KEY);
    } catch (_) {
      // Session storage is optional.
    }
  }

  function adminHeaders(headers = {}) {
    const token = adminToken();
    return token ? { ...headers, Authorization: `Bearer ${token}` } : { ...headers };
  }

  function toast(message, options = {}) {
    if (!message) return;
    const node = document.createElement("div");
    node.className = `toast${options.error ? " error" : ""}`;
    node.textContent = message;
    el.toastRoot.appendChild(node);
    while (el.toastRoot.children.length > 3) {
      el.toastRoot.firstElementChild.remove();
    }
    window.setTimeout(() => {
      node.style.opacity = "0";
      node.style.transition = "opacity 160ms ease";
      window.setTimeout(() => node.remove(), 180);
    }, options.duration || 2400);
  }

  function setContent(html) {
    state.songVirtual = null;
    state.albumVirtual = null;
    el.content.innerHTML = html;
  }

  function renderLoading(message = "Loading…") {
    setContent(`
      <div class="center-state">
        ${spinnerHtml()}
        <div class="state-note">${esc(message)}</div>
      </div>`);
  }

  function renderErrorState(message) {
    setContent(`
      <div class="center-state">
        <div class="state-title">Could not load the library</div>
        <div class="state-note">${esc(message)}</div>
        <button class="secondary-button" data-action="reload-app">Try Again</button>
      </div>`);
  }

  // ---------------------------------------------------------------------
  // URL routes

  let routeApplying = false;
  let lastRoute = null;

  function formatUuid(hex) {
    const value = String(hex || "").toUpperCase();
    return `${value.slice(0, 8)}-${value.slice(8, 12)}-${value.slice(12, 16)}`
      + `-${value.slice(16, 20)}-${value.slice(20, 32)}`;
  }

  function parseUuid(value) {
    const text = String(value || "").trim();
    if (/^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$/.test(text)) {
      return text.toUpperCase();
    }
    const compact = text.replace(/-/g, "");
    if (/^[0-9A-Fa-f]{32}$/.test(compact)) return formatUuid(compact);
    return null;
  }

  function decodeRouteToken(raw) {
    let text;
    try {
      text = decodeURIComponent(String(raw || ""));
    } catch (_) {
      return null;
    }
    const direct = parseUuid(text);
    if (direct) return direct;
    let encoded = text.replace(/-/g, "+").replace(/_/g, "/")
      .replace(/\s+/g, "");
    if (!encoded || !/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)) return null;
    encoded += "=".repeat((4 - encoded.length % 4) % 4);
    try {
      const binary = atob(encoded);
      if (binary.length === 16) {
        let hex = "";
        for (let index = 0; index < binary.length; index += 1) {
          hex += binary.charCodeAt(index).toString(16).padStart(2, "0");
        }
        return formatUuid(hex);
      }
      if (binary.length === 36) return parseUuid(binary);
    } catch (_) {
      return null;
    }
    return null;
  }

  function encodeUrlId(identifier) {
    if (state.urlIdFormat !== "base64url") return identifier;
    const hex = String(identifier || "").replace(/-/g, "");
    if (!/^[0-9a-fA-F]{32}$/.test(hex)) return identifier;
    const bytes = new Uint8Array(16);
    for (let index = 0; index < 16; index += 1) {
      bytes[index] = parseInt(hex.slice(index * 2, index * 2 + 2), 16);
    }
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_")
      .replace(/=+$/g, "");
  }

  function albumRoute(albumId) {
    return `#/album/${encodeURIComponent(encodeUrlId(albumId))}`;
  }

  function songRoute(songId) {
    return `#/song/${encodeURIComponent(encodeUrlId(songId))}`;
  }

  function playlistRoute(playlistId) {
    return `#/playlist/${encodeURIComponent(encodeUrlId(playlistId))}`;
  }

  function artistRoute(name) {
    return `#/artist/${encodeURIComponent(name)}`;
  }

  function routeForSection(section, playlistId = null) {
    switch (section) {
      case "artists":
        return state.selectedArtist
          ? artistRoute(state.selectedArtist) : "#/artists";
      case "songs":
        return "#/songs";
      case "playlists":
        return playlistId ? playlistRoute(playlistId) : "#/playlists";
      case "playlist":
        return playlistId ? playlistRoute(playlistId) : "#/playlists";
      case "starred":
        return "#/starred";
      case "genres":
        return "#/genres";
      default:
        return "#/albums";
    }
  }

  function syncRoute(route, options = {}) {
    if (!route || routeApplying) return;
    if (location.hash === route) {
      lastRoute = route;
      return;
    }
    try {
      const method = options.replace ? "replaceState" : "pushState";
      history[method](null, "", route);
      lastRoute = route;
    } catch (_) {
      location.hash = route;
      lastRoute = route;
    }
  }

  function webPlayerBaseUrl() {
    const protocol = window.location.protocol;
    const host = window.location.host;
    let path = window.location.pathname || "/";
    if (!path.endsWith("/")) {
      const leaf = path.slice(path.lastIndexOf("/") + 1);
      if (leaf.includes(".")) {
        path = path.slice(0, path.lastIndexOf("/") + 1);
      } else {
        path += "/";
      }
    }
    return `${protocol}//${host}${path}`;
  }

  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        toast("Link copied");
        return;
      }
    } catch (_) {
      // Fall back to execCommand below.
    }
    try {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      area.style.pointerEvents = "none";
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
      toast("Link copied");
    } catch (_) {
      toast("Could not copy the link", { error: true });
    }
  }

  function copyRouteLink(route) {
    copyText(webPlayerBaseUrl() + route);
  }

  function parseLocationRoute() {
    const raw = (window.location.hash || "").replace(/^#/, "").split("?")[0];
    const clean = raw.replace(/^\/+|\/+$/g, "");
    if (!clean) return { name: "albums" };
    const match = clean.match(/^([^/]+)(?:\/(.*))?$/);
    if (!match) return { name: "unknown" };
    let head;
    try {
      head = decodeURIComponent(match[1]).toLowerCase();
    } catch (_) {
      return { name: "unknown" };
    }
    const rest = match[2] || "";
    const plain = ["albums", "artists", "songs", "playlists", "starred", "genres"];
    if (!rest && plain.includes(head)) return { name: head };
    if (!rest) return { name: "bare", token: match[1] };
    if (["album", "song", "playlist", "artist"].includes(head)) {
      return { name: head, token: rest };
    }
    return { name: "unknown" };
  }

  async function fetchSongById(songId) {
    const data = await api(`/api/songs/${encodeURIComponent(songId)}`);
    if (!data || !data.song) throw new Error("Song not found");
    return data.song;
  }

  async function resolveBareLink(token) {
    const songId = decodeRouteToken(token);
    if (!songId) throw new Error("Invalid link");
    if (state.albumMap.has(songId)) {
      return { type: "album", id: songId };
    }
    const song = await fetchSongById(songId);
    return { type: "song", id: song.id, song };
  }

  async function applyAlbumRoute(token) {
    const albumId = decodeRouteToken(token);
    if (!albumId) throw new Error("Invalid album link");
    if (!state.albumMap.has(albumId)) throw new Error("Album not found");
    await setSection("albums");
    await openAlbum(albumId);
    if (!state.albumDetail || state.albumDetail.loading) {
      throw new Error("Album not found");
    }
  }

  async function applySongObject(song) {
    if (!state.albumMap.has(song.albumId)) {
      throw new Error("Album not found for this song");
    }
    await setSection("albums");
    await openAlbum(song.albumId);
    if (!state.albumDetail || state.albumDetail.loading) {
      throw new Error("Album not found for this song");
    }
    highlightLinkedSong(song.id);
  }

  async function applySongRoute(token) {
    const songId = decodeRouteToken(token);
    if (!songId) throw new Error("Invalid song link");
    const song = await fetchSongById(songId);
    await applySongObject(song);
  }

  function highlightLinkedSong(songId) {
    window.requestAnimationFrame(() => {
      const row = document.querySelector(
        `.track-row[data-song-id="${songId}"]`
      );
      if (!row) return;
      row.classList.add("linked");
      row.scrollIntoView({ block: "center" });
      window.setTimeout(() => row.classList.remove("linked"), 4000);
    });
  }

  async function applyArtistRoute(token) {
    let name;
    try {
      name = decodeURIComponent(token);
    } catch (_) {
      throw new Error("Invalid artist link");
    }
    state.selectedArtist = name;
    await setSection("artists", null, { force: true });
    if (!(state.browserGroups || []).some((group) => group.name === name)) {
      state.selectedArtist = null;
      throw new Error("Artist not found");
    }
  }

  async function applyLocationRoute() {
    if (routeApplying) return;
    const hash = window.location.hash || "";
    if (lastRoute && hash === lastRoute) return;
    routeApplying = true;
    try {
      if (!hash || hash === "#" || hash === "#/") {
        const route = routeForSection(state.section, state.playlistId);
        try { history.replaceState(null, "", route); } catch (_) {}
        lastRoute = route;
        await setSection(state.section, state.playlistId, { force: true });
        return;
      }
      const route = parseLocationRoute();
      if (route.name === "bare") {
        const resolved = await resolveBareLink(route.token);
        const canonical = resolved.type === "album"
          ? albumRoute(resolved.id) : songRoute(resolved.id);
        try { history.replaceState(null, "", canonical); } catch (_) {}
        lastRoute = canonical;
        if (resolved.type === "album") await applyAlbumRoute(resolved.id);
        else await applySongObject(resolved.song);
        return;
      }
      switch (route.name) {
        case "albums":
          await setSection("albums");
          break;
        case "artists":
          await setSection("artists");
          break;
        case "songs":
          await setSection("songs");
          break;
        case "playlists":
          await setSection("playlists");
          break;
        case "starred":
          await setSection("playlists");
          break;
        case "album":
          await applyAlbumRoute(route.token);
          break;
        case "song":
          await applySongRoute(route.token);
          break;
        case "playlist": {
          const playlistId = decodeRouteToken(route.token);
          if (!playlistId
              || !state.playlists.some((item) => item.id === playlistId)) {
            throw new Error("Playlist not found");
          }
          await setSection("playlists", playlistId);
          break;
        }
        case "artist":
          await applyArtistRoute(route.token);
          break;
        case "genres":
          await setSection("genres");
          break;
        default:
          throw new Error("Link not found");
      }
      lastRoute = window.location.hash || lastRoute;
    } catch (error) {
      toast(error.message || "Link not found", { error: true });
      try { history.replaceState(null, "", "#/albums"); } catch (_) {}
      lastRoute = "#/albums";
      await setSection("albums", null, { force: true });
    } finally {
      routeApplying = false;
    }
  }

  async function initializeRoute() {
    if (state.needsSetup) return;
    const hash = window.location.hash || "";
    if (hash && hash !== "#" && hash !== "#/") {
      await applyLocationRoute();
      return;
    }
    const route = routeForSection(state.section, state.playlistId);
    try { history.replaceState(null, "", route); } catch (_) {}
    lastRoute = route;
  }

  // ---------------------------------------------------------------------
  // Preferences

  function loadPrefs() {
    let prefs = {};
    try {
      prefs = JSON.parse(localStorage.getItem(PREF_KEY) || "{}") || {};
    } catch (_) {
      prefs = {};
    }
    const sections = SECTIONS.map((section) => section.id);
    const legacyStarred = prefs.section === "starred";
    if (sections.includes(prefs.section)) {
      state.section = prefs.section;
    } else if (legacyStarred || prefs.section === "playlist") {
      state.section = "playlists";
    }
    state.playlistId = state.section === "playlists" && !legacyStarred
      ? (prefs.playlistId || null) : null;
    if (prefs.songSorts && typeof prefs.songSorts === "object") {
      state.songSorts = prefs.songSorts;
    }
    if (typeof prefs.audioQuality === "string") {
      state.audioQuality = prefs.audioQuality;
    }
  }

  function savePrefs() {
    try {
      localStorage.setItem(PREF_KEY, JSON.stringify({
        section: state.section,
        playlistId: state.playlistId,
        songSorts: state.songSorts,
        audioQuality: state.audioQuality,
      }));
    } catch (_) {
      // Preferences are optional.
    }
  }

  // ---------------------------------------------------------------------
  // Bootstrap

  function artURL(albumId) {
    return `/api/artwork/${encodeURIComponent(albumId)}?v=${state.artVersion}`;
  }

  function audioURL(song) {
    const version = Math.round(
      (Number(song.mtime) || Number(song.dateAdded) || 0) * 1000
    );
    const query = [];
    if (state.audioQuality) {
      query.push(`quality=${encodeURIComponent(state.audioQuality)}`);
    }
    if (player.playId) {
      query.push(`play=${encodeURIComponent(player.playId)}`);
    }
    query.push(`v=${version}`);
    return `/api/audio/${encodeURIComponent(song.id)}?${query.join("&")}`;
  }

  async function refreshSystemStatus() {
    try {
      const info = await api("/api/status", {
        method: "POST",
        headers: adminHeaders(),
      });
      if (!info) return;
      renderCacheStatus(info.cache);
      const online = Math.max(0, Number(info.online) || 0);
      if (el.statusOnlineCount) {
        el.statusOnlineCount.textContent = online.toLocaleString();
      }
      if (el.statusOnline) {
        el.statusOnline.title = `${online} online`;
      }
      if (!el.statusSystem) return;
      const state = info.state || "ready";
      let label = "Ready";
      if (state === "scanning") {
        const total = Number(info.total) || 0;
        const completed = Number(info.completed) || 0;
        const percent = total > 0
          ? ` ${Math.min(100, Math.round(completed / total * 100))}%` : "";
        label = `Scanning · ${info.phase || "Working"}${percent}`;
      } else if (state === "rekeying") {
        label = `ReKeying · ${info.phase || "Working"}`;
      } else if (state === "artwork") {
        const total = Number(info.total) || 0;
        const completed = Number(info.completed) || 0;
        const percent = total > 0
          ? ` ${Math.min(100, Math.round(completed / total * 100))}%` : "";
        label = `Artwork · ${info.phase || "Working"}${percent}`;
      }
      el.statusSystem.textContent = label;
      el.statusSystem.classList.toggle("active", state !== "ready");
      const details = [];
      if (info.phase) details.push(info.phase);
      if (Number(info.total) > 0) {
        details.push(
          `${Number(info.completed).toLocaleString()} / ${Number(info.total).toLocaleString()}`
        );
      }
      if (info.message) details.push(info.message);
      el.statusSystem.title = details.length
        ? details.join(" · ") : "System status";
    } catch (_) {
      if (el.statusSystem) el.statusSystem.title = "System status unavailable";
    }
  }

  function renderCacheStatus(cache) {
    if (!cache || !el.statusCache) return;
    const max = Math.max(0, Number(cache.maxBytes) || 0);
    const used = Math.max(0, Number(cache.usedBytes) || 0);
    const entries = Math.max(0, Number(cache.entries) || 0);
    const itemLabel = entries === 1 ? "item" : "items";
    if (max === 0) {
      el.statusCache.textContent = "Cache off";
      el.statusCache.title =
        "Transcode cache disabled; set transcode_cache_mb above 0 to enable it.";
      return;
    }
    el.statusCache.textContent =
      `Server Cache ${fmtCacheSize(used)} / ${fmtCacheSize(max)} · ${entries} ${itemLabel}`;
    const breakdown = (cache.qualities || [])
      .filter((quality) => Number(quality.entries) > 0)
      .map((quality) => {
        const count = Number(quality.entries) || 0;
        return `${quality.label} · ${quality.detail}: `
          + `${fmtCacheSize(quality.usedBytes)}, ${count} ${count === 1 ? "item" : "items"}`;
      });
    el.statusCache.title =
      `Transcode cache (memory): ${fmtCacheSize(used)} of ${fmtCacheSize(max)} used, `
      + `${entries} ${itemLabel}`;
    if (breakdown.length) {
      el.statusCache.title += `\n${breakdown.join("\n")}`;
    }
  }

  function audioQualityLabel(quality) {
    if (!quality) return state.audioQuality || "Auto";
    const detail = quality.detail
      || (quality.bitrate ? `${quality.bitrate} kbps` : "");
    return detail ? `${quality.label} · ${detail}` : quality.label;
  }

  function applyAudioQualityUI() {
    const quality = state.audioQualities.find(
      (item) => item.id === state.audioQuality
    );
    const label = audioQualityLabel(quality);
    el.qualityButton.title = `Audio quality: ${label}`;
    el.qualityButton.setAttribute("aria-label", `Audio quality: ${label}`);
  }

  async function loadAudioQualities() {
    try {
      const info = await api("/api/quality");
      const qualities = (info && Array.isArray(info.qualities))
        ? info.qualities : [];
      if (!qualities.length) return;
      state.audioQualities = qualities;
      state.defaultAudioQuality = info.default || qualities[0].id;
      if (!qualities.some((item) => item.id === state.audioQuality)) {
        state.audioQuality = state.defaultAudioQuality;
      }
      applyAudioQualityUI();
    } catch (_) {
      // Keep the current selection when the catalog is unavailable.
    }
  }

  async function openQualityMenu(anchor) {
    const rect = anchor.getBoundingClientRect();
    if (!state.audioQualities.length) {
      await loadAudioQualities();
    }
    if (!state.audioQualities.length) {
      state.audioQualities = [{
        id: "aac128", label: "AAC", detail: "128 kbps",
      }];
    }
    const items = state.audioQualities.map((quality) => ({
      label: audioQualityLabel(quality),
      checked: state.audioQuality === quality.id,
      action: () => selectAudioQuality(quality.id),
    }));
    showMenu(rect.left, rect.bottom + 4, items);
  }

  function selectAudioQuality(quality) {
    if (!quality || quality === state.audioQuality) return;
    state.audioQuality = quality;
    savePrefs();
    applyAudioQualityUI();
    const song = currentSong();
    if (!song) return;
    const wasPlaying = player.playing;
    const elapsed = player.elapsed;
    player.pendingRecord = false;
    player.pendingSeek = elapsed;
    player.pendingPlay = wasPlaying;
    player.loadedSongId = null;
    audio.src = audioURL(song);
    audio.volume = 1;
    audio.load();
    setAudioBuffering(true);
    if (wasPlaying) {
      const promise = audio.play();
      if (promise && promise.catch) {
        promise.catch(() => {});
      }
    }
    renderPlayerUI();
    refreshCurrentIndicators();
    savePlayerState();
  }

  function applyBootstrap(data) {
    state.artVersion += 1;
    state.boot = data || {};
    state.albums = (data && data.albums) || [];
    state.playlists = (data && data.playlists) || [];
    state.libraries = (data && data.libraries) || [];
    state.musicDirs = (data && data.musicDirs) || [];
    state.stats = (data && data.libraryStats) || emptyStats();
    state.starredStats = (data && data.starredStats) || emptyStats();
    state.readOnly = Boolean(data && data.readOnly);
    state.needsSetup = Boolean(data && data.needsSetup);
    if (!state.albumSortInitialized) {
      if (data && ALBUM_SORT_KEYS.includes(data.albumSort)) {
        state.albumSort = data.albumSort;
      }
      if (data && typeof data.albumSortDescending === "boolean") {
        state.albumSortAsc = !data.albumSortDescending;
      }
      state.albumSortInitialized = true;
    }
    if (data && ["uuid", "base64url"].includes(data.urlIdFormat)) {
      state.urlIdFormat = data.urlIdFormat;
    }
    state.albumMap = new Map(state.albums.map((album) => [album.id, album]));
    if (state.section === "playlist" && !state.playlists.some((p) => p.id === state.playlistId)) {
      state.section = "albums";
      state.playlistId = null;
    }
  }

  async function loadInitial() {
    try {
      const data = await api("/api/bootstrap");
      applyBootstrap(data);
      await loadAudioQualities();
      renderSectionTabs();
      if (state.needsSetup) {
        renderWelcome();
        updateStatus();
        return;
      }
      await setSection(state.section, state.playlistId, { initial: true });
      state.doneInitialLoad = true;
      if (data.available === false && state.albums.length === 0) {
        renderWelcome();
      }
    } catch (error) {
      renderErrorState(error.message || String(error));
      renderSectionTabs();
    }
  }

  async function refreshAfterScan() {
    let previousSection = state.section;
    let previousPlaylist = state.playlistId;
    try {
      const data = await api("/api/bootstrap");
      state.allSongs = null;
      state.allSongsPromise = null;
      state.songById = new Map();
      state.songsByAlbum = new Map();
      state.albumDetails.clear();
      state.playlistDetails.clear();
      state.albumDetail = null;
      state.selectedAlbums.clear();
      state.selectedSongs.clear();
      state.songAnchor = null;
      state.albumAnchor = null;
      state.lcdSongId = null;
      applyBootstrap(data);
      if (previousSection === "playlist"
          && !state.playlists.some((playlist) => playlist.id === previousPlaylist)) {
        previousSection = "albums";
        previousPlaylist = null;
      }
      renderSectionTabs();
      if (state.needsSetup) {
        renderWelcome();
        updateStatus();
        return;
      }
      await setSection(previousSection, previousPlaylist, {
        keepQuery: true,
        force: true,
      });
      hydratePlayerSongs();
      renderPlayerUI();
    } catch (error) {
      toast(`Could not refresh the library: ${error.message}`, { error: true });
    }
  }

  // ---------------------------------------------------------------------
  // Section tabs

  function sectionTabActive(sectionId) {
    if (sectionId === "playlists") {
      return ["playlists", "starred", "playlist"].includes(state.section);
    }
    return state.section === sectionId;
  }

  function renderSectionTabs() {
    const tabs = $$(".section-tab", el.sectionTabs);
    if (!tabs.length) {
      el.sectionTabs.innerHTML = SECTIONS.map((section) => `
        <button class="section-tab" type="button" data-section="${section.id}"
                title="${esc(section.label)}"
                aria-label="${esc(section.label)}">
          ${icon(section.icon)}
        </button>`).join("");
    }
    $$(".section-tab", el.sectionTabs).forEach((tab) => {
      const active = sectionTabActive(tab.dataset.section || "");
      tab.classList.toggle("active", active);
      if (active) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    });
  }

  function openPlaylistDialog(mode, playlist) {
    if (state.readOnly) {
      toast("The server is running in read-only mode", { error: true });
      return;
    }
    if (!state.adminAuthenticated) {
      toast(
        mode === "create"
          ? "Log in from Setup to create playlists."
          : "Log in from Setup to rename playlists.",
        { error: true },
      );
      return;
    }
    state.modalMode = "playlist";
    state.playlistDialog = {
      mode,
      id: playlist ? playlist.id : null,
    };
    const title = mode === "create" ? "New Playlist" : "Rename Playlist";
    const value = playlist ? playlist.name : "";
    el.modalRoot.innerHTML = `
      <div class="sheet" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="sheet-header">
          <div>
            <div class="sheet-title">${esc(title)}</div>
            <div class="sheet-subtitle">${mode === "create"
              ? "Create a new playlist in the library."
              : "Change the playlist name. Songs are not affected."}</div>
          </div>
          <button class="sheet-close" type="button" data-action="playlist-cancel"
                  title="Close">×</button>
        </div>
        <div class="sheet-body">
          <label class="field">
            <span class="field-label">Name</span>
            <input id="playlist-name-input" class="text-input" type="text"
                   maxlength="200" autocomplete="off" spellcheck="false"
                   value="${esc(value)}" aria-label="Playlist name">
          </label>
        </div>
        <div class="sheet-actions">
          <button class="secondary-button" type="button" data-action="playlist-cancel">Cancel</button>
          <button class="primary-button" type="button" data-action="playlist-save">
            ${mode === "create" ? "Create" : "Save"}
          </button>
        </div>
      </div>`;
    el.modalRoot.classList.remove("hidden");
    const input = document.getElementById("playlist-name-input");
    input.focus();
    input.select();
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        savePlaylistDialog();
      }
    });
  }

  async function savePlaylistDialog() {
    const dialog = state.playlistDialog;
    if (!dialog) return;
    const input = document.getElementById("playlist-name-input");
    const name = (input && input.value || "").trim();
    if (!name) {
      if (input) input.focus();
      return;
    }
    const { mode, id } = dialog;
    closeModal();
    try {
      if (mode === "create") {
        const data = await api("/api/playlists", {
          method: "POST",
          headers: adminHeaders(),
          body: JSON.stringify({ name }),
        });
        const summary = data.playlist;
        state.playlists.push(summary);
        state.playlists.sort((a, b) => a.name.localeCompare(b.name));
        state.playlistDetails.set(summary.id, {
          id: summary.id,
          name: summary.name,
          dateModified: summary.dateModified,
          songs: [],
        });
        renderSectionTabs();
        await setSection("playlists", summary.id);
      } else if (mode === "rename" && id) {
        const data = await api(`/api/playlists/${encodeURIComponent(id)}`, {
          method: "PATCH",
          body: JSON.stringify({ name }),
        });
        const summary = data.playlist;
        const existing = state.playlists.find((playlist) => playlist.id === id);
        if (existing) {
          existing.name = summary.name;
          existing.dateModified = summary.dateModified || existing.dateModified;
        }
        const detail = state.playlistDetails.get(id);
        if (detail) detail.name = summary.name;
        state.playlists.sort((a, b) => a.name.localeCompare(b.name));
        renderSectionTabs();
        if ((state.section === "playlist" && state.playlistId === id)
            || state.section === "playlists") {
          renderContent();
        }
      }
    } catch (error) {
      toast(error.message, { error: true });
    }
  }

  function openAddToPlaylistDialog(songs) {
    const unique = [];
    const seen = new Set();
    (songs || []).forEach((song) => {
      if (!song || !song.id || seen.has(song.id)) return;
      seen.add(song.id);
      unique.push(song);
    });
    if (!unique.length) return;
    state.modalMode = "add-to-playlist";
    state.addToPlaylistIds = unique.map((song) => song.id);
    state.addToPlaylistSongs = unique;
    renderAddToPlaylistDialog();
  }

  function renderAddToPlaylistDialog() {
    const ids = state.addToPlaylistIds || [];
    const songs = state.addToPlaylistSongs || [];
    const count = ids.length;
    const rows = state.playlists.map((playlist) => {
      const songCount = Number(playlist.songCount) || 0;
      return `
        <button class="playlist-row" type="button"
                data-action="add-to-playlist-pick"
                data-playlist-id="${esc(playlist.id)}">
          <span class="playlist-tile">${icon("note-list")}</span>
          <span class="playlist-copy">
            <span class="playlist-name">${esc(playlist.name)}</span>
            <span class="playlist-meta">${songCount.toLocaleString()} ${songCount === 1 ? "song" : "songs"}</span>
          </span>
          ${icon("chevron-right", "playlist-chevron")}
        </button>`;
    }).join("");
    const body = rows
      ? `<div class="playlist-picker">${rows}</div>`
      : `<div class="field-help">No playlists yet.</div>`;
    el.modalRoot.innerHTML = `
      <div class="sheet setup-sheet" role="dialog" aria-modal="true" aria-label="Add to Playlist">
        <div class="sheet-header">
          <img class="app-mark-small" src="/static/app-icon.svg" alt="">
          <div>
            <div class="sheet-title">Add to Playlist</div>
            <div class="sheet-subtitle">${count.toLocaleString()} ${count === 1 ? "song" : "songs"}</div>
          </div>
          <button class="sheet-close" type="button" data-action="close-modal"
                  title="Close">×</button>
        </div>
        <div class="sheet-body">
          ${songs.length === 1 ? `<div class="field-help">${esc(songs[0].title || "")}</div>` : ""}
          ${body}
          <div class="sheet-actions">
            <button class="secondary-button" type="button" data-action="close-modal">Cancel</button>
          </div>
        </div>
      </div>`;
    el.modalRoot.classList.remove("hidden");
  }

  async function addToPlaylistFromDialog(playlistId) {
    if (!playlistId) return;
    const ids = (state.addToPlaylistIds || []).slice();
    closeModal();
    if (ids.length) await addSongsToPlaylist(playlistId, ids);
  }

  async function setSection(section, playlistId = null, options = {}) {
    if (!options.initial && !options.force
        && state.section === section
        && state.playlistId === playlistId
        && !state.albumDetail) {
      focusContent();
      return;
    }
    const previousAlbumId = state.albumDetail
      ? state.albumDetail.id : null;
    if (section !== state.section) {
      state.browserFocus = "list";
      state.browserAlbumAnchor = null;
    }
    state.section = section;
    state.playlistId = playlistId;
    state.albumDetail = null;
    state.albumPage = 1;
    state.selectedAlbums.clear();
    state.selectedSongs.clear();
    state.songAnchor = null;
    state.albumAnchor = null;
    state.selectedArtist = state.selectedArtist || null;
    state.query = "";
    el.searchInput.value = "";
    updateSearchClear();
    savePrefs();
    if (!options.initial && !routeApplying) {
      syncRoute(routeForSection(section, playlistId));
    }
    const token = ++state.viewToken;
    renderSectionTabs();

    const needsSongs = ["songs", "starred", "artists", "genres"].includes(section);
    if (!state.needsSetup && needsSongs && !state.allSongs) {
      renderLoading(section === "songs" ? "Loading songs…" : "Loading library…");
      try {
        await ensureAllSongs();
      } catch (error) {
        if (token === state.viewToken) renderErrorState(error.message);
        return;
      }
    }
    if (section === "playlist" && playlistId) {
      if (!state.playlistDetails.has(playlistId)) {
        renderLoading("Loading playlist…");
        try {
          await ensurePlaylistDetail(playlistId);
        } catch (error) {
          if (token === state.viewToken) {
            toast("Playlist could not be loaded", { error: true });
            state.section = "albums";
            state.playlistId = null;
            savePrefs();
            renderSectionTabs();
          }
        }
      }
    }
    if (token !== state.viewToken) return;
    renderContent();
    updateStatus();
    focusContent();
    if (section === "albums" && previousAlbumId) {
      window.requestAnimationFrame(() => {
        revealAlbumInGrid(previousAlbumId);
      });
    }
  }

  function focusContent() {
    try {
      el.content.focus({ preventScroll: true });
    } catch (_) {
      el.content.focus();
    }
  }

  // ---------------------------------------------------------------------
  // Content routing

  function renderPlaylistsSection() {
    if (state.playlistId
        && !state.playlists.some((playlist) => playlist.id === state.playlistId)) {
      state.playlistId = null;
      savePrefs();
    }
    const selectedId = state.playlistId || null;
    const selectedPlaylist = selectedId
      ? state.playlists.find((playlist) => playlist.id === selectedId) : null;
    const starredCount = Number(
      (state.starredStats && state.starredStats.songCount) || 0
    );

    const rows = [];
    rows.push(`
      <button class="playlist-row starred${selectedId ? "" : " selected"}"
              type="button" data-action="select-starred"
              ${selectedId ? "" : 'aria-current="true"'}>
        <span class="playlist-tile">${icon("star")}</span>
        <span class="playlist-copy">
          <span class="playlist-name">Stared</span>
          <span class="playlist-meta">${starredCount.toLocaleString()} ${starredCount === 1 ? "song" : "songs"}</span>
        </span>
        ${icon("chevron-right", "playlist-chevron")}
      </button>`);
    state.playlists.forEach((playlist) => {
      const count = Number(playlist.songCount) || 0;
      const active = playlist.id === selectedId;
      rows.push(`
        <button class="playlist-row${active ? " selected" : ""}" type="button"
                data-action="select-playlist" data-playlist-id="${esc(playlist.id)}"
                ${active ? 'aria-current="true"' : ""}>
          <span class="playlist-tile">${icon("note-list")}</span>
          <span class="playlist-copy">
            <span class="playlist-name">${esc(playlist.name)}</span>
            <span class="playlist-meta">${count.toLocaleString()} ${count === 1 ? "song" : "songs"}</span>
          </span>
          ${icon("chevron-right", "playlist-chevron")}
        </button>`);
    });

    let main = "";
    let ready = false;
    let context = null;
    let songs = [];
    if (selectedId) {
      const detail = state.playlistDetails.get(selectedId);
      if (!detail) {
        main = playlistLoadingHtml("Loading playlist…");
        ensurePlaylistDetail(selectedId).then(() => {
          if (state.section === "playlists" && state.playlistId === selectedId) {
            renderPlaylistsSection();
            updateStatus();
          }
        }).catch(() => {
          if (state.section === "playlists" && state.playlistId === selectedId) {
            state.playlistId = null;
            savePrefs();
            renderPlaylistsSection();
            updateStatus();
            toast("Playlist could not be loaded", { error: true });
          }
        });
      } else {
        context = { kind: "playlist", playlistId: selectedId };
        songs = songsSorted(
          filterSongsForQuery(detail.songs || []),
          songSortKeyFor(context)
        );
        const title = detail.name
          || (selectedPlaylist && selectedPlaylist.name) || "Playlist";
        main = playlistContentHtml(title, songs, context);
        ready = true;
      }
    } else {
      context = { kind: "starred" };
      if (!state.allSongs) {
        main = playlistLoadingHtml("Loading songs…");
        ensureAllSongs().then(() => {
          if (state.section === "playlists" && !state.playlistId) {
            renderPlaylistsSection();
            updateStatus();
          }
        }).catch((error) => {
          if (state.section === "playlists") renderErrorState(error.message);
        });
      } else {
        songs = songsSorted(
          filterSongsForQuery(
            (state.allSongs || []).filter((song) => song.loved)
          ),
          songSortKeyFor(context)
        );
        main = playlistContentHtml("Stared", songs, context);
        ready = true;
      }
    }

    const subtitle = `${(state.playlists.length + 1).toLocaleString()} lists`;
    setContent(`
      <div class="playlists-view">
        <div class="view-bar">
          <div class="view-title">Playlists</div>
          <div class="view-subtitle">${esc(subtitle)}</div>
          <div class="view-spacer"></div>
          <button class="primary-button" type="button" data-action="new-playlist"
                  ${state.readOnly || !state.adminAuthenticated ? "disabled" : ""}
                  title="${state.readOnly || !state.adminAuthenticated
                    ? "Log in from Setup to create playlists"
                    : "New Playlist"}">
            ${icon("plus")} New Playlist
          </button>
        </div>
        <div class="playlists-split">
          <aside class="playlist-nav" aria-label="Playlists">${rows.join("")}</aside>
          <section id="playlist-main" class="playlist-main">${main}</section>
        </div>
      </div>`);

    if (ready) {
      const root = $("#playlist-main", el.content);
      if (root) bindSongTable(root, context, songs);
    }
    updateStatus();
  }

  function selectPlaylistView(playlistId) {
    state.section = "playlists";
    state.playlistId = playlistId || null;
    state.albumDetail = null;
    state.selectedSongs.clear();
    state.songAnchor = null;
    savePrefs();
    if (!routeApplying) {
      syncRoute(playlistId ? playlistRoute(playlistId) : "#/starred");
    }
    renderSectionTabs();
    renderPlaylistsSection();
    focusContent();
  }

  function playlistLoadingHtml(message) {
    return `<div class="center-state">
      ${spinnerHtml()}
      <div class="state-note">${esc(message)}</div>
    </div>`;
  }

  function playlistContentHtml(title, songs, context) {
    const count = songs.length;
    const meta = `${count.toLocaleString()} ${count === 1 ? "song" : "songs"}`;
    return `
      <div class="playlist-main-head">
        <div class="playlist-main-copy">
          <div class="playlist-main-title">${esc(title)}</div>
          <div class="playlist-main-meta">${esc(meta)}</div>
        </div>
        <div class="playlist-main-actions">
          <button class="album-action-button" type="button"
                  data-action="playlist-view-play" ${count ? "" : "disabled"}>
            ${icon("play")} Play
          </button>
          <button class="album-action-button" type="button"
                  data-action="playlist-view-shuffle" ${count ? "" : "disabled"}>
            ${icon("shuffle")} Shuffle
          </button>
        </div>
      </div>
      <div class="playlist-main-body">${songTableHtml(context, songs)}</div>`;
  }

  function renderContent() {
    if (state.needsSetup) {
      renderWelcome();
      return;
    }
    if (state.albumDetail) {
      renderAlbumDetailView();
      return;
    }
    switch (state.section) {
      case "albums":
        renderAlbumGridSection();
        break;
      case "songs":
        renderSongSection({ kind: "songs" });
        break;
      case "starred":
        renderSongSection({ kind: "starred" });
        break;
      case "playlist":
        renderSongSection({ kind: "playlist", playlistId: state.playlistId });
        break;
      case "playlists":
        renderPlaylistsSection();
        break;
      case "artists":
        renderBrowserSection("artists");
        break;
      case "genres":
        renderBrowserSection("genres");
        break;
      default:
        renderAlbumGridSection();
    }
  }

  function renderWelcome() {
    const readOnly = state.readOnly
      ? `<div class="state-note">The server is in read-only mode.</div>` : "";
    setContent(`
      <div class="center-state">
        <img class="app-mark" src="/static/app-icon.svg" alt="">
        <div class="state-title">No Music Library</div>
        <div class="state-note">
          Point WebTracks at a folder of FLAC, MP3, M4A, WAV, AIFF or Ogg
          files. It builds a Tracks-compatible database and artwork cache
          inside this project, without touching the macOS app's library.
        </div>
        ${readOnly}
        <button class="primary-button" data-action="open-setup">
          ${icon("gear")} Open Setup…
        </button>
      </div>`);
  }

  // ---------------------------------------------------------------------
  // Album grid

  function currentSectionAlbumList() {
    let list = state.albums.slice();
    if (state.query) {
      list = list.filter((album) => matchesQuery(album.title)
        || matchesQuery(album.artist) || matchesQuery(album.genre));
    }
    return sortedAlbums(list);
  }

  function measureAlbumPageMetrics() {
    const scroller = $("#grid-scroll", el.content);
    const grid = $(".album-grid", el.content);
    const card = grid && grid.querySelector(".album-card");
    if (!scroller || !grid || !card) return null;
    const style = getComputedStyle(grid);
    const columns = (style.gridTemplateColumns || "")
      .split(" ").filter(Boolean).length;
    const rowGap = parseFloat(style.rowGap) || 0;
    const paddingTop = ALBUM_GRID_PADDING_TOP;
    const paddingBottom = ALBUM_GRID_PADDING_BOTTOM;
    const cardHeight = card.getBoundingClientRect().height;
    if (!columns || !cardHeight) return null;
    const available = scroller.clientHeight - paddingTop - paddingBottom;
    const rows = Math.max(
      1, Math.floor((available + rowGap) / (cardHeight + rowGap))
    );
    return {
      columns,
      rows,
      capacity: Math.max(1, columns * rows),
      stride: Math.max(1, rows * (cardHeight + rowGap)),
    };
  }

  function albumPageCapacity() {
    return (state.albumPageMetrics && state.albumPageMetrics.capacity)
      || DEFAULT_ALBUM_PAGE_CAPACITY;
  }

  function albumPageTotal(count) {
    return Math.max(1, Math.ceil(count / albumPageCapacity()));
  }

  let albumPagerFrame = null;

  function scheduleAlbumPagerUpdate() {
    if (albumPagerFrame) return;
    albumPagerFrame = window.requestAnimationFrame(() => {
      albumPagerFrame = null;
      updateAlbumPagerFromScroll();
    });
  }

  function updateAlbumPagerFromScroll(forcedPage = null) {
    if (state.albumDetail
        || (state.section !== "albums" && state.section !== "recent")) {
      return;
    }
    const scroller = $("#grid-scroll", el.content);
    const albums = currentSectionAlbumList();
    const metrics = state.albumPageMetrics || measureAlbumPageMetrics();
    if (!scroller || !metrics) return;
    const totalPages = albumPageTotal(albums.length);
    let page = forcedPage;
    const atBottom = scroller.scrollTop
      >= Math.max(0, scroller.scrollHeight - scroller.clientHeight) - 2;
    if (page == null) {
      page = atBottom
        ? totalPages
        : Math.floor((scroller.scrollTop + 2) / metrics.stride) + 1;
    }
    page = clamp(Math.floor(Number(page) || 1), 1, totalPages);
    state.albumPage = page;
    const start = (page - 1) * metrics.capacity;
    const end = Math.min(albums.length, start + metrics.capacity);
    const subtitle = document.querySelector(".view-subtitle");
    if (subtitle) {
      subtitle.textContent = albums.length
        ? `${(start + 1).toLocaleString()}–${end.toLocaleString()} of ${albums.length.toLocaleString()} albums`
        : "0 albums";
    }
    const pageInput = $("#album-page-input", el.content);
    if (pageInput) pageInput.value = String(page);
    const total = $(".page-total", el.content);
    if (total) total.textContent = `/ ${totalPages}`;
    const first = $('[data-action="album-page-first"]', el.content);
    const prev = $('[data-action="album-page-prev"]', el.content);
    const next = $('[data-action="album-page-next"]', el.content);
    const last = $('[data-action="album-page-last"]', el.content);
    const atStart = page <= 1 || albums.length === 0;
    const atEnd = page >= totalPages || albums.length === 0;
    if (first) first.disabled = atStart;
    if (prev) prev.disabled = atStart;
    if (next) next.disabled = atEnd;
    if (last) last.disabled = atEnd;
  }

  function goToAlbumPage(page) {
    const scroller = $("#grid-scroll", el.content);
    const albums = currentSectionAlbumList();
    const metrics = measureAlbumPageMetrics() || state.albumPageMetrics;
    if (!scroller || !metrics) return;
    state.albumPageMetrics = metrics;
    const totalPages = albumPageTotal(albums.length);
    const target = clamp(Math.floor(Number(page) || 1), 1, totalPages);
    const maxScroll = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
    const top = Math.min(maxScroll, (target - 1) * metrics.stride);
    scroller.scrollTo({ top, behavior: "auto" });
    updateAlbumPagerFromScroll(target);
  }

  function refreshAlbumPagerLayout() {
    if (state.albumDetail
        || (state.section !== "albums" && state.section !== "recent")) {
      return;
    }
    if (state.albumVirtual) state.albumVirtual.refresh();
    const metrics = measureAlbumPageMetrics();
    if (metrics) state.albumPageMetrics = metrics;
    updateAlbumPagerFromScroll();
  }

  function albumPageControls(totalPages, count) {
    const page = state.albumPage;
    const atStart = count === 0 || page <= 1;
    const atEnd = count === 0 || page >= totalPages;
    return `
      <div class="page-controls" role="group" aria-label="Album pages">
        <button class="page-button rotate-up" type="button" data-action="album-page-first"
                title="First page" aria-label="First page" ${atStart ? "disabled" : ""}>
          ${icon("first-page")}
        </button>
        <button class="page-button" type="button" data-action="album-page-prev"
                title="Previous page" aria-label="Previous page" ${atStart ? "disabled" : ""}>
          ${icon("chevron-up")}
        </button>
        <label class="page-number-wrap" title="Jump to page">
          <input id="album-page-input" class="page-number" type="text"
                 inputmode="numeric" autocomplete="off" spellcheck="false"
                 value="${page}" aria-label="Page number">
          <span class="page-total">/ ${totalPages}</span>
        </label>
        <button class="page-button" type="button" data-action="album-page-next"
                title="Next page" aria-label="Next page" ${atEnd ? "disabled" : ""}>
          ${icon("chevron-down")}
        </button>
        <button class="page-button rotate-down" type="button" data-action="album-page-last"
                title="Last page" aria-label="Last page" ${atEnd ? "disabled" : ""}>
          ${icon("last-page")}
        </button>
      </div>`;
  }

  function renderAlbumGridSection() {
    const albums = currentSectionAlbumList();
    const title = "Albums";
    const count = albums.length;
    const subtitle = count
      ? `${count.toLocaleString()} albums`
      : "0 albums";
    const sortControls = `
      <button class="view-menu-button sort-button" type="button" data-action="album-sort-menu"
              title="Sort albums" aria-label="Sort albums">
        ${icon("sort")}
        ${esc(albumSortLabel(state.albumSort))}
        ${icon("chevron-down")}
      </button>`;

    const body = albums.length
      ? `<div class="album-grid-virtual">
           <div id="album-grid-spacer" class="album-grid-spacer"></div>
           <div id="album-grid-window" class="album-grid album-grid-window"></div>
         </div>`
      : `<div class="center-state"><div class="state-note">${state.query ? "No Results" : "No Albums"}</div></div>`;

    setContent(`
      <div class="grid-view">
        <div class="view-bar">
          <div class="view-title">${esc(title)}</div>
          ${sortControls}
          <div class="view-subtitle">${esc(subtitle)}</div>
          <div class="view-spacer"></div>
          ${albumPageControls(albumPageTotal(count), count)}
        </div>
        <div class="grid-scroll" id="grid-scroll">
          ${body}
        </div>
      </div>`);
    const gridScroll = $("#grid-scroll", el.content);
    if (gridScroll) {
      gridScroll.addEventListener("click", (event) => {
        if (event.target.closest(".album-card")) return;
        if (!state.selectedAlbums.size && !state.albumAnchor) return;
        state.selectedAlbums.clear();
        state.albumAnchor = null;
        updateAlbumCardSelection();
        updateStatus();
      });
      gridScroll.addEventListener("scroll", scheduleAlbumPagerUpdate, {
        passive: true,
      });
    }
    const pageInput = $("#album-page-input", el.content);
    if (pageInput) {
      pageInput.addEventListener("focus", () => pageInput.select());
      pageInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          pageInput.blur();
        } else if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          pageInput.value = String(state.albumPage);
          pageInput.blur();
        }
      });
      pageInput.addEventListener("blur", () => {
        const requested = Number.parseInt(pageInput.value, 10);
        if (Number.isFinite(requested)) {
          goToAlbumPage(requested);
        } else {
          pageInput.value = String(state.albumPage);
        }
      });
    }
    state.albumVirtual = setupAlbumVirtual(albums);
    updateStatus();
    window.requestAnimationFrame(() => {
      if (state.albumVirtual) state.albumVirtual.refresh();
      state.albumPageMetrics = measureAlbumPageMetrics();
      updateAlbumPagerFromScroll(1);
    });
  }

  function albumCardHtml(album) {
    const selected = state.selectedAlbums.has(album.id);
    return `
      <article class="album-card${selected ? " selected" : ""}"
               data-album-id="${esc(album.id)}"
               data-action="album-card"
               tabindex="-1"
               aria-selected="${selected ? "true" : "false"}">
        <div class="album-art-wrap" draggable="true">
          <img src="${artURL(album.id)}" alt="${esc(album.title)}"
               loading="lazy" decoding="async" draggable="false">
          <button class="album-play" type="button" data-action="play-album"
                  data-album-id="${esc(album.id)}" title="Play ${esc(album.title)}"
                  aria-label="Play ${esc(album.title)}">
            ${icon("play")}
          </button>
        </div>
        <div class="album-meta">
          <div class="album-title" title="${esc(album.title)}">${esc(album.title)}</div>
          <div class="album-artist" title="${esc(album.artist)}">${esc(album.artist)}</div>
        </div>
      </article>`;
  }

  function setupAlbumVirtual(albums) {
    const scroller = $("#grid-scroll", el.content);
    const gridWindow = $("#album-grid-window", el.content);
    const spacer = $("#album-grid-spacer", el.content);
    if (!scroller || !gridWindow || !spacer || !albums.length) return null;

    let metrics = null;
    let firstRow = -1;
    let lastRow = -1;

    const renderRange = (start, end) => {
      const rows = [];
      for (let index = start; index < end; index += 1) {
        rows.push(albumCardHtml(albums[index]));
      }
      gridWindow.innerHTML = rows.join("");
    };

    const measure = () => {
      const style = getComputedStyle(gridWindow);
      const columns = (style.gridTemplateColumns || "")
        .split(" ").filter(Boolean).length;
      const rowGap = parseFloat(style.rowGap) || 0;
      const card = gridWindow.querySelector(".album-card");
      const cardHeight = card ? card.getBoundingClientRect().height : 0;
      if (!columns || !cardHeight) return null;
      const rowStride = cardHeight + rowGap;
      const rowCount = Math.max(1, Math.ceil(albums.length / columns));
      return {
        columns,
        rowGap,
        cardHeight,
        rowStride,
        rowCount,
        totalHeight: ALBUM_GRID_PADDING_TOP
          + rowCount * cardHeight
          + Math.max(0, rowCount - 1) * rowGap
          + ALBUM_GRID_PADDING_BOTTOM,
      };
    };

    const update = (force = false) => {
      if (!metrics) return;
      const scrollTop = scroller.scrollTop;
      const viewHeight = scroller.clientHeight || 1;
      const first = Math.max(
        0,
        Math.floor(
          (scrollTop - ALBUM_GRID_PADDING_TOP) / metrics.rowStride
        ) - ALBUM_GRID_OVERSCAN_ROWS,
      );
      const last = Math.min(
        metrics.rowCount,
        Math.ceil(
          (scrollTop + viewHeight - ALBUM_GRID_PADDING_TOP)
          / metrics.rowStride
        ) + ALBUM_GRID_OVERSCAN_ROWS,
      );
      const safeLast = Math.min(
        metrics.rowCount, Math.max(first + 1, last)
      );
      if (!force && first === firstRow && safeLast === lastRow) return;
      firstRow = first;
      lastRow = safeLast;
      renderRange(
        first * metrics.columns,
        Math.min(albums.length, safeLast * metrics.columns),
      );
      gridWindow.style.top = `${
        ALBUM_GRID_PADDING_TOP + first * metrics.rowStride
      }px`;
    };

    const refresh = () => {
      if (!gridWindow.querySelector(".album-card")) {
        renderRange(0, Math.min(albums.length, 120));
      }
      const next = measure();
      if (!next) return;
      metrics = next;
      spacer.style.height = `${metrics.totalHeight}px`;
      firstRow = -1;
      lastRow = -1;
      update(true);
    };

    const scrollToIndex = (index) => {
      if (!metrics) return;
      const row = Math.floor(index / metrics.columns);
      const top = ALBUM_GRID_PADDING_TOP + row * metrics.rowStride;
      const bottom = top + metrics.cardHeight;
      const viewTop = scroller.scrollTop;
      const viewBottom = viewTop + scroller.clientHeight;
      if (top < viewTop + 4) {
        scroller.scrollTop = Math.max(0, top - 4);
      } else if (bottom > viewBottom - 4) {
        scroller.scrollTop = Math.min(
          scroller.scrollHeight - scroller.clientHeight,
          bottom - scroller.clientHeight + 4,
        );
      }
      update(true);
    };

    renderRange(0, Math.min(albums.length, 120));
    refresh();
    scroller.addEventListener("scroll", () => update(), { passive: true });
    return {
      metrics: () => metrics,
      render: () => update(),
      refresh,
      scrollToIndex,
    };
  }

  function revealAlbumInGrid(albumId) {
    if (!albumId || state.section !== "albums" || state.albumDetail) {
      return false;
    }
    const virtual = state.albumVirtual;
    if (!virtual) return false;
    const index = currentSectionAlbumList().findIndex(
      (album) => album.id === albumId
    );
    if (index < 0) return false;
    virtual.refresh();
    virtual.scrollToIndex(index);
    updateAlbumPagerFromScroll();
    return true;
  }

  function albumSortLabel(value) {
    if (value === "recentlyAdded") return "Recently Added";
    if (value === "artist") return "Artist";
    if (value === "year") return "Year";
    return "Album Name";
  }

  function defaultSortAscending(value) {
    return value !== "recentlyAdded" && value !== "year";
  }

  function openAlbumSortMenu(anchor) {
    const rect = anchor.getBoundingClientRect();
    const items = [
      ["recentlyAdded", "Recently Added"],
      ["album", "Album Name"],
      ["artist", "Artist"],
      ["year", "Year"],
    ].map(([value, label]) => ({
      label,
      checked: state.albumSort === value,
      action: () => {
        state.albumSort = value;
        state.albumSortAsc = defaultSortAscending(value);
        state.albumPage = 1;
        renderContent();
      },
    }));
    items.push({ separator: true });
    items.push({
      label: "Ascending",
      checked: state.albumSortAsc,
      action: () => {
        if (state.albumSortAsc) return;
        state.albumSortAsc = true;
        state.albumPage = 1;
        renderContent();
      },
    });
    items.push({
      label: "Descending",
      checked: !state.albumSortAsc,
      action: () => {
        if (!state.albumSortAsc) return;
        state.albumSortAsc = false;
        state.albumPage = 1;
        renderContent();
      },
    });
    showMenu(rect.left, rect.bottom + 4, items);
  }

  function handleAlbumCardInteraction(albumId, event) {
    const album = state.albumMap.get(albumId);
    if (!album) return;
    const command = event.metaKey || event.ctrlKey;
    const shift = event.shiftKey;
    if (shift && state.albumAnchor && state.albumMap.has(state.albumAnchor)) {
      const list = currentSectionAlbumList();
      const from = list.findIndex((item) => item.id === state.albumAnchor);
      const to = list.findIndex((item) => item.id === albumId);
      if (from >= 0 && to >= 0) {
        const range = from <= to ? list.slice(from, to + 1) : list.slice(to, from + 1);
        if (!command) state.selectedAlbums.clear();
        range.forEach((item) => state.selectedAlbums.add(item.id));
        state.albumAnchor = albumId;
        updateAlbumCardSelection();
        updateStatus();
        return;
      }
    }
    if (command) {
      if (state.selectedAlbums.has(albumId)) state.selectedAlbums.delete(albumId);
      else state.selectedAlbums.add(albumId);
      state.albumAnchor = state.selectedAlbums.size ? albumId : null;
      updateAlbumCardSelection();
      updateStatus();
      return;
    }
    state.selectedAlbums.clear();
    state.selectedAlbums.add(albumId);
    state.albumAnchor = albumId;
    updateAlbumCardSelection();
    openAlbum(albumId);
  }

  function updateAlbumCardSelection() {
    $$(".album-card", el.content).forEach((card) => {
      const selected = state.selectedAlbums.has(card.dataset.albumId);
      card.classList.toggle("selected", selected);
      card.setAttribute("aria-selected", selected ? "true" : "false");
    });
  }

  async function openAlbum(albumId) {
    state.albumReturnScroll = 0;
    const scroller = $(".grid-scroll", el.content);
    if (scroller) state.albumReturnScroll = scroller.scrollTop;
    state.albumDetail = { id: albumId, loading: true };
    state.selectedSongs.clear();
    state.songAnchor = null;
    renderContent();
    updateStatus();
    const token = ++state.viewToken;
    try {
      const data = await api(`/api/albums/${encodeURIComponent(albumId)}`);
      if (token !== state.viewToken || !state.albumDetail || state.albumDetail.id !== albumId) return;
      state.albumDetail = {
        id: albumId,
        album: data.album,
        songs: data.songs || [],
        loading: false,
      };
      state.albumDetails.set(albumId, data);
      state.selectedSongs.clear();
      state.songAnchor = null;
      if (!routeApplying) syncRoute(albumRoute(albumId));
      renderContent();
      updateStatus();
    } catch (error) {
      if (token !== state.viewToken) return;
      state.albumDetail = null;
      renderContent();
      toast(`Could not open album: ${error.message}`, { error: true });
    }
  }

  function closeAlbumDetail() {
    const albumId = state.albumDetail ? state.albumDetail.id : null;
    const scroll = state.albumReturnScroll;
    state.albumDetail = null;
    state.selectedSongs.clear();
    state.songAnchor = null;
    if (!routeApplying) {
      syncRoute(routeForSection(state.section, state.playlistId));
    }
    renderContent();
    updateStatus();
    window.requestAnimationFrame(() => {
      if (albumId && revealAlbumInGrid(albumId)) return;
      const scroller = $(".grid-scroll", el.content);
      if (scroller) scroller.scrollTop = scroll;
    });
  }

  function renderAlbumDetailView() {
    if (!state.albumDetail) {
      renderContent();
      return;
    }
    if (state.albumDetail.loading) {
      renderLoading("Opening album…");
      return;
    }
    const album = state.albumDetail.album;
    const songs = state.albumDetail.songs || [];
    state.songRows = songs;
    const multiDisc = isMultiDisc(songs);
    const infoParts = [];
    if (album.genre) infoParts.push(album.genre);
    if (album.year) infoParts.push(album.year);
    if (multiDisc) {
      const discs = new Set(songs.map((song) => song.disc).filter((disc) => disc > 0));
      infoParts.push(`${discs.size || 1} discs`);
    }
    infoParts.push(`${songs.length} song${songs.length === 1 ? "" : "s"}`);
    const backLabel = state.section === "albums" ? "All Albums" : "Back";
    const rows = songs.map((song, index) => {
      const selected = state.selectedSongs.has(song.id);
      const previous = index > 0 && state.selectedSongs.has(songs[index - 1].id);
      const next = index + 1 < songs.length && state.selectedSongs.has(songs[index + 1].id);
      return trackRowHtml(song, {
        index,
        selected,
        previous,
        next,
        multiDisc,
        albumArtist: album.artist,
        albumId: album.id,
      });
    }).join("");

    setContent(`
      <div class="album-detail-scroll">
        <div class="album-detail">
          <button class="back-link" type="button" data-action="close-album">
            ${icon("chevron-left")} ${esc(backLabel)}
          </button>
          <div class="album-hero">
            <div class="album-cover-large">
              <img src="${artURL(album.id)}" alt="${esc(album.title)}">
            </div>
            <div class="album-info">
              <h1>${esc(album.title)}</h1>
              <button class="album-artist-link" type="button"
                      data-action="open-artist" data-artist="${esc(album.artist)}">
                ${esc(album.artist)}
              </button>
              <div class="album-subinfo">${esc(infoParts.join(" · "))}</div>
              <div class="album-actions">
                <button class="album-action-button" type="button"
                        data-action="play-detail-album">
                  ${icon("play")} Play
                </button>
                <button class="album-action-button" type="button"
                        data-action="shuffle-detail-album">
                  ${icon("shuffle")} Shuffle
                </button>
              </div>
              <div class="hairline"></div>
              <div class="album-tracks">${rows || '<div class="song-table-empty">No Songs</div>'}</div>
            </div>
          </div>
        </div>
      </div>`);
    state.songContext = { kind: "album", albumId: album.id };
    const scroller = $(".album-detail-scroll", el.content);
    if (scroller) {
      scroller.addEventListener("click", (event) => {
        if (event.target.closest(".track-row")
            || event.target.closest("button")) return;
        state.selectedSongs.clear();
        state.songAnchor = null;
        refreshSelectionClasses();
        updateStatus();
      });
    }
    updateStatus();
  }

  function isMultiDisc(songs) {
    let maxDisc = 0;
    let declaresTotal = false;
    for (const song of songs || []) {
      if (Number(song.disc) > maxDisc) maxDisc = Number(song.disc);
      if (Number(song.discTotal) > 1) declaresTotal = true;
    }
    return maxDisc > 1 || declaresTotal;
  }

  function trackLabel(song, multiDisc) {
    if (multiDisc && Number(song.disc) > 0 && Number(song.track) > 0) {
      return `${song.disc}-${String(song.track).padStart(2, "0")}`;
    }
    return Number(song.track) > 0 ? String(song.track) : "";
  }

  function trackRowHtml(song, options) {
    const selected = Boolean(options.selected);
    const classes = [
      "track-row",
      selected ? "selected" : "",
      selected && options.previous ? "joined-top" : "",
      selected && options.next ? "joined-bottom" : "",
      options.multiDisc ? "multidisc" : "",
    ].filter(Boolean).join(" ");
    const isCurrent = currentSong() && currentSong().id === song.id;
    const track = trackLabel(song, options.multiDisc);
    const status = isCurrent
      ? icon(player.playing ? "play" : "pause")
      : esc(track);
    const inlineArtist = song.artist && song.artist !== options.albumArtist
      ? `<span class="track-artist-inline">${esc(song.artist)}</span>` : "";
    return `
      <div class="${classes}" data-action="track-row" data-song-id="${esc(song.id)}"
           data-album-id="${esc(options.albumId || "")}" draggable="true">
        <div class="track-number" data-status-song="${esc(song.id)}"
             data-original="${esc(track)}">${status || esc(track)}</div>
        <div class="track-title">${esc(song.title)}${inlineArtist}</div>
        <div class="track-time">${fmtTime(song.duration)}</div>
        <div class="track-star">
          <button class="star-button${song.loved ? " loved" : ""}" type="button"
                  data-action="toggle-star" data-star-id="${esc(song.id)}"
                  title="${song.loved ? "Remove from Stared" : "Add to Stared"}"
                  aria-label="${song.loved ? "Remove from Stared" : "Add to Stared"}">
            ${icon(song.loved ? "star-fill" : "star")}
          </button>
        </div>
      </div>`;
  }

  // ---------------------------------------------------------------------
  // Data loading

  async function ensureAllSongs() {
    if (state.allSongs) return state.allSongs;
    if (state.allSongsPromise) return state.allSongsPromise;
    state.allSongsPromise = api("/api/songs").then((data) => {
      state.allSongs = data.songs || [];
      state.songById = new Map(state.allSongs.map((song) => [song.id, song]));
      const byAlbum = new Map();
      for (const song of state.allSongs) {
        if (!byAlbum.has(song.albumId)) byAlbum.set(song.albumId, []);
        byAlbum.get(song.albumId).push(song);
      }
      state.songsByAlbum = byAlbum;
      state.starredStats = statsForSongs(
        state.allSongs.filter((song) => song.loved)
      );
      hydratePlayerSongs();
      return state.allSongs;
    }).finally(() => {
      state.allSongsPromise = null;
    });
    return state.allSongsPromise;
  }

  async function ensurePlaylistDetail(playlistId) {
    if (state.playlistDetails.has(playlistId)) {
      return state.playlistDetails.get(playlistId);
    }
    const data = await api(`/api/playlists/${encodeURIComponent(playlistId)}`);
    state.playlistDetails.set(playlistId, data);
    const summary = state.playlists.find((item) => item.id === playlistId);
    if (summary) {
      summary.songCount = data.songs.length;
      summary.duration = data.songs.reduce(
        (total, song) => total + (Number(song.duration) || 0), 0
      );
      summary.size = data.songs.reduce(
        (total, song) => total + (Number(song.fileSize) || 0), 0
      );
      summary.dateModified = data.dateModified || summary.dateModified;
    }
    return data;
  }

  function getSongById(songId) {
    if (state.songById.has(songId)) return state.songById.get(songId);
    const groups = [
      state.songRows,
      player.queue,
      player.baseQueue,
      player.history,
    ];
    for (const group of groups) {
      const found = (group || []).find((song) => song && song.id === songId);
      if (found) return found;
    }
    for (const detail of state.albumDetails.values()) {
      const found = (detail.songs || []).find((song) => song.id === songId);
      if (found) return found;
    }
    for (const detail of state.playlistDetails.values()) {
      const found = (detail.songs || []).find((song) => song.id === songId);
      if (found) return found;
    }
    if (state.albumDetail && state.albumDetail.songs) {
      const found = state.albumDetail.songs.find((song) => song.id === songId);
      if (found) return found;
    }
    return null;
  }

  function patchSong(songId, patch) {
    const seen = new Set();
    const apply = (song) => {
      if (!song || song.id !== songId || seen.has(song)) return;
      Object.assign(song, patch);
      seen.add(song);
    };
    apply(state.songById.get(songId));
    (state.songRows || []).forEach(apply);
    (player.queue || []).forEach(apply);
    (player.baseQueue || []).forEach(apply);
    (player.history || []).forEach(apply);
    for (const detail of state.albumDetails.values()) {
      (detail.songs || []).forEach(apply);
    }
    for (const detail of state.playlistDetails.values()) {
      (detail.songs || []).forEach(apply);
    }
    if (state.albumDetail && state.albumDetail.songs) {
      state.albumDetail.songs.forEach(apply);
    }
  }

  // ---------------------------------------------------------------------
  // Song tables (Songs / Stared / Playlist)

  function songSortKeyFor(context) {
    const key = context.kind === "playlist"
      ? `playlist:${context.playlistId}` : context.kind;
    const stored = state.songSorts[key];
    if (stored && ["name", "time", "artist", "albumArtist", "album", "genre", "addSequence"].includes(stored.key)) {
      return stored;
    }
    return { key: "name", asc: true };
  }

  function saveSongSort(context, sort) {
    const key = context.kind === "playlist"
      ? `playlist:${context.playlistId}` : context.kind;
    state.songSorts[key] = sort;
    savePrefs();
  }

  function songsSorted(songs, sort) {
    const list = (songs || []).slice();
    const direction = sort.asc ? 1 : -1;
    const lexical = (a, b) => a < b ? -1 : a > b ? 1 : 0;
    list.sort((a, b) => {
      switch (sort.key) {
        case "time":
          return (Number(a.duration) - Number(b.duration)) * direction;
        case "artist":
          return lexical(a.artist + a.album, b.artist + b.album) * direction;
        case "albumArtist":
          return lexical(
            a.albumArtist + a.album + String(a.track),
            b.albumArtist + b.album + String(b.track)
          ) * direction;
        case "album":
          return lexical(a.album + String(a.track), b.album + String(b.track)) * direction;
        case "genre":
          return lexical(a.genre, b.genre) * direction;
        case "addSequence": {
          const left = a.addSequence == null ? Number.MAX_SAFE_INTEGER : a.addSequence;
          const right = b.addSequence == null ? Number.MAX_SAFE_INTEGER : b.addSequence;
          if (left === right) return lexical(a.title, b.title) * direction;
          return (left - right) * direction;
        }
        default:
          return lexical(a.title, b.title) * direction;
      }
    });
    return list;
  }

  function songColumns(context) {
    const columns = [
      { key: "status", label: "", template: "28px" },
      { key: "name", label: "Name", template: "minmax(120px, 3fr)", sort: "name" },
      { key: "artist", label: "Artist", template: "minmax(60px, 1.45fr)", sort: "artist" },
      { key: "albumArtist", label: "Album Artist", template: "minmax(70px, 1.45fr)", sort: "albumArtist" },
      { key: "album", label: "Album", template: "minmax(60px, 1.8fr)", sort: "album" },
      { key: "genre", label: "Genre", template: "minmax(50px, 1.2fr)", sort: "genre" },
    ];
    if (context.kind === "playlist") {
      columns.push({
        key: "addSequence", label: "Add Seq",
        template: "minmax(54px, 0.6fr)", sort: "addSequence", numeric: true,
      });
    }
    columns.push(
      { key: "plays", label: "Plays", template: "minmax(40px, 0.55fr)", numeric: true },
      { key: "time", label: "Time", template: "minmax(44px, 0.55fr)", sort: "time", numeric: true },
      { key: "star", label: "", template: "28px" }
    );
    return columns;
  }

  function renderSongSection(context) {
    state.songContext = context;
    if (context.kind !== "playlist" && !state.allSongs) {
      renderLoading("Loading songs…");
      const section = context.kind;
      ensureAllSongs().then(() => {
        if (state.section === section && !state.albumDetail) renderContent();
      }).catch((error) => {
        if (state.section === section) renderErrorState(error.message);
      });
      return;
    }
    let source;
    if (context.kind === "songs") source = state.allSongs || [];
    else if (context.kind === "starred") source = (state.allSongs || []).filter((song) => song.loved);
    else {
      const detail = state.playlistDetails.get(context.playlistId);
      if (!detail) {
        renderLoading("Loading playlist…");
        ensurePlaylistDetail(context.playlistId).then(() => {
          if (state.section === "playlist" && state.playlistId === context.playlistId) {
            renderContent();
          }
        }).catch(() => {
          state.section = "albums";
          state.playlistId = null;
          savePrefs();
          renderSectionTabs();
          renderContent();
        });
        return;
      }
      source = detail.songs || [];
    }

    let songs = filterSongsForQuery(source);
    const sort = songSortKeyFor(context);
    songs = songsSorted(songs, sort);
    state.songRows = songs;

    const detail = context.kind === "playlist"
      ? (state.playlistDetails.get(context.playlistId) || {})
      : {};
    const title = context.kind === "songs"
      ? "Songs"
      : context.kind === "starred"
        ? "Stared"
        : (detail.name || "Playlist");
    const count = songs.length;
    const table = songTableHtml(context, songs);
    setContent(`
      <div class="song-view">
        <div class="view-bar song-view-bar">
          <div class="view-title">${esc(title)}</div>
          <div class="view-subtitle">${count.toLocaleString()} ${count === 1 ? "song" : "songs"}</div>
          <div class="view-spacer"></div>
          <div class="song-view-actions">
            <button class="album-action-button" type="button"
                    data-action="song-view-play" ${count ? "" : "disabled"}>
              ${icon("play")} Play
            </button>
            <button class="album-action-button" type="button"
                    data-action="song-view-shuffle" ${count ? "" : "disabled"}>
              ${icon("shuffle")} Shuffle
            </button>
          </div>
        </div>
        ${table}
      </div>`);
    bindSongTable(el.content, context, songs);
    updateStatus();
  }

  function filterSongsForQuery(songs) {
    if (!state.query) return songs;
    return songs.filter((song) =>
      matchesQuery(song.title) || matchesQuery(song.artist)
      || matchesQuery(song.albumArtist) || matchesQuery(song.album)
      || matchesQuery(song.genre));
  }

  function songTableHtml(context, songs) {
    const sort = songSortKeyFor(context);
    const columns = songColumns(context);
    const template = columns.map((column) => column.template).join(" ");
    const header = columns.map((column) => {
      if (column.key === "status" || column.key === "star") {
        return '<div class="header-cell"></div>';
      }
      const active = column.sort && sort.key === column.sort;
      const classes = [
        "header-cell",
        column.numeric ? "num" : "",
        column.sort ? "sortable" : "",
      ].filter(Boolean).join(" ");
      const aria = active ? ` aria-sort="${sort.asc ? "ascending" : "descending"}"` : "";
      const action = column.sort
        ? ` data-action="song-sort" data-sort="${column.sort}"` : "";
      return `<div class="${classes}"${action}${aria} title="${column.sort ? "Sort by " + esc(column.label) : ""}">
        ${esc(column.label)}
        ${active ? icon(sort.asc ? "chevron-up" : "chevron-down") : ""}
      </div>`;
    }).join("");

    const emptyText = state.query
      ? "No Results"
      : context.kind === "starred"
        ? "No Stared Songs"
        : context.kind === "playlist"
          ? "This Playlist Is Empty" : "No Songs";

    return `
      <div class="song-table" style="--song-cols:${template}">
        <div class="song-header">${header}</div>
        <div class="song-scroll" id="song-scroll">
          ${songs.length ? "" : `<div class="song-table-empty">${emptyText}</div>`}
        </div>
      </div>`;
  }

  function bindSongTable(root, context, songs) {
    state.songContext = context;
    state.songRows = songs;
    const container = root.querySelector("#song-scroll");
    if (songs.length && container) {
      state.songVirtual = setupVirtual(container, songs, {
        rowHeight: 26,
        topPad: 6,
        bottomPad: 10,
        renderRow: (song, index, top) => songRowHtml(song, songs, index, top, context),
      });
      container.addEventListener("click", (event) => {
        if (event.target === container) {
          clearSongSelection();
        }
      });
    } else {
      state.songVirtual = null;
    }
  }

  function songRowHtml(song, list, index, top, context) {
    const selected = state.selectedSongs.has(song.id);
    const previous = index > 0 && state.selectedSongs.has(list[index - 1].id);
    const next = index + 1 < list.length && state.selectedSongs.has(list[index + 1].id);
    const rowClasses = [
      "song-row",
      index % 2 === 1 ? "alt" : "",
      selected ? "selected" : "",
      selected && previous ? "joined-top" : "",
      selected && next ? "joined-bottom" : "",
    ].filter(Boolean).join(" ");
    const isCurrent = currentSong() && currentSong().id === song.id;
    const status = isCurrent ? icon(player.playing ? "play" : "pause") : "";
    const sequenceCell = context.kind === "playlist"
      ? `<div class="cell num">${esc(song.addSequence == null ? "—" : song.addSequence)}</div>`
      : "";
    return `
      <div class="song-virtual-row" style="top:${top}px">
        <div class="${rowClasses}" data-action="song-row"
             data-song-id="${esc(song.id)}" draggable="true"
             aria-selected="${selected ? "true" : "false"}">
          <div class="status-cell" data-status-song="${esc(song.id)}"
               data-original="">${status}</div>
          <div class="cell name" title="${esc(song.title)}">${esc(song.title)}</div>
          <div class="cell" title="${esc(song.artist)}">${esc(song.artist)}</div>
          <div class="cell" title="${esc(song.albumArtist)}">${esc(song.albumArtist)}</div>
          <div class="cell" title="${esc(song.album)}">${esc(song.album)}</div>
          <div class="cell" title="${esc(song.genre)}">${esc(song.genre)}</div>
          ${sequenceCell}
          <div class="cell num">${Number(song.plays) || 0}</div>
          <div class="cell num time">${fmtTime(song.duration)}</div>
          <div class="star-cell">
            <button class="star-button${song.loved ? " loved" : ""}" type="button"
                    data-action="toggle-star" data-star-id="${esc(song.id)}"
                    title="${song.loved ? "Remove from Stared" : "Add to Stared"}"
                    aria-label="${song.loved ? "Remove from Stared" : "Add to Stared"}">
              ${icon(song.loved ? "star-fill" : "star")}
            </button>
          </div>
        </div>
      </div>`;
  }

  function clearSongSelection() {
    state.selectedSongs.clear();
    state.songAnchor = null;
    if (state.songVirtual) state.songVirtual.rerender();
    refreshSelectionClasses();
    updateStatus();
  }

  function handleSongRowClick(songId, event) {
    const list = state.songRows || [];
    const index = list.findIndex((song) => song.id === songId);
    if (index < 0) return;
    const command = event.metaKey || event.ctrlKey;
    const shift = event.shiftKey;
    if (shift && state.songAnchor) {
      const anchorIndex = list.findIndex((song) => song.id === state.songAnchor);
      if (anchorIndex >= 0) {
        const from = Math.min(anchorIndex, index);
        const to = Math.max(anchorIndex, index);
        if (!command) state.selectedSongs.clear();
        for (let i = from; i <= to; i += 1) state.selectedSongs.add(list[i].id);
        state.songAnchor = songId;
        if (state.songVirtual) state.songVirtual.rerender();
        refreshSelectionClasses();
        updateStatus();
        focusContent();
        return;
      }
    }
    if (command) {
      if (state.selectedSongs.has(songId)) state.selectedSongs.delete(songId);
      else state.selectedSongs.add(songId);
      state.songAnchor = state.selectedSongs.size ? songId : null;
      if (state.songVirtual) state.songVirtual.rerender();
      refreshSelectionClasses();
      updateStatus();
      focusContent();
      return;
    }
    state.selectedSongs.clear();
    state.selectedSongs.add(songId);
    state.songAnchor = songId;
    if (state.songVirtual) state.songVirtual.rerender();
    refreshSelectionClasses();
    updateStatus();
    focusContent();
  }

  function refreshSelectionClasses() {
    document.querySelectorAll(".track-row, .browser-song-row").forEach((row) => {
      const selected = row.dataset.songId && state.selectedSongs.has(row.dataset.songId);
      row.classList.toggle("selected", Boolean(selected));
      if (selected) {
        const previous = row.previousElementSibling;
        const next = row.nextElementSibling;
        row.classList.toggle("joined-top", Boolean(
          previous && previous.dataset.songId
          && state.selectedSongs.has(previous.dataset.songId)
        ));
        row.classList.toggle("joined-bottom", Boolean(
          next && next.dataset.songId
          && state.selectedSongs.has(next.dataset.songId)
        ));
      } else {
        row.classList.remove("joined-top", "joined-bottom");
      }
    });
  }

  function setupVirtual(container, items, options) {
    container.innerHTML = "";
    const spacer = document.createElement("div");
    spacer.className = "song-spacer";
    const layer = document.createElement("div");
    layer.className = "song-layer";
    const topPad = options.topPad || 0;
    const bottomPad = options.bottomPad == null ? 8 : options.bottomPad;
    const rowHeight = options.rowHeight || 26;
    spacer.style.height = `${topPad + items.length * rowHeight + bottomPad}px`;
    container.append(spacer, layer);
    let start = -1;
    let end = -1;
    const render = (force) => {
      const height = container.clientHeight || 1;
      const scrollTop = container.scrollTop;
      const first = Math.max(0, Math.floor((scrollTop - topPad) / rowHeight) - 8);
      const last = Math.min(items.length, Math.ceil((scrollTop + height - topPad) / rowHeight) + 8);
      if (!force && first === start && last === end) return;
      start = first;
      end = last;
      let html = "";
      for (let index = first; index < last; index += 1) {
        html += options.renderRow(items[index], index, topPad + index * rowHeight);
      }
      layer.innerHTML = html;
    };
    container.addEventListener("scroll", () => render(false), { passive: true });
    const virtual = {
      items,
      container,
      render: () => render(false),
      rerender: () => render(true),
      scrollToIndex(index, align = "nearest") {
        const itemTop = topPad + index * rowHeight;
        const itemBottom = itemTop + rowHeight;
        const viewTop = container.scrollTop;
        const viewBottom = viewTop + container.clientHeight;
        if (align === "center") {
          container.scrollTop = itemTop - container.clientHeight / 2 + rowHeight / 2;
        } else if (itemTop < viewTop + 6) {
          container.scrollTop = Math.max(0, itemTop - 6);
        } else if (itemBottom > viewBottom - 6) {
          container.scrollTop = itemBottom - container.clientHeight + 6;
        }
        virtual.rerender();
      },
    };
    virtual.rerender();
    return virtual;
  }

  // ---------------------------------------------------------------------
  // Artists / genres browser

  function buildArtistGroups() {
    const map = new Map();
    for (const album of state.albums) {
      if (!map.has(album.artist)) map.set(album.artist, []);
      map.get(album.artist).push(album);
    }
    return Array.from(map, ([name, albums]) => ({ name, albums }))
      .sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
  }

  function buildGenreGroups() {
    const map = new Map();
    for (const album of state.albums) {
      if (!map.has(album.genre)) map.set(album.genre, []);
      map.get(album.genre).push(album);
    }
    return Array.from(map, ([name, albums]) => ({
      name,
      displayName: name || "#Unknown",
      placeholder: !name,
      albums,
    })).sort((a, b) => {
      if (a.placeholder !== b.placeholder) return a.placeholder ? 1 : -1;
      return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
    });
  }

  function browserGroupSelectedName(kind) {
    return kind === "artists" ? state.selectedArtist : state.selectedGenre;
  }

  function setBrowserSelectedName(kind, name) {
    if (kind === "artists") state.selectedArtist = name;
    else state.selectedGenre = name;
  }

  function renderBrowserSection(kind) {
    if (!state.allSongs) {
      renderLoading("Loading library…");
      ensureAllSongs().then(() => {
        if (state.section === kind && !state.albumDetail) renderContent();
      }).catch((error) => renderErrorState(error.message));
      return;
    }
    if (state.browserKind !== kind) {
      state.browserFocus = "list";
      state.browserAlbumAnchor = null;
    }
    state.browserKind = kind;
    let groups = kind === "artists" ? buildArtistGroups() : buildGenreGroups();
    if (state.query) {
      groups = groups.filter((group) => kind === "artists"
        ? (matchesQuery(group.name)
          || group.albums.some((album) => matchesQuery(album.title)))
        : matchesQuery(group.displayName));
    }
    state.browserGroups = groups;
    if (!groups.length) {
      setContent(`<div class="center-state"><div class="state-note">No ${kind === "artists" ? "Artists" : "Genres"}</div></div>`);
      updateStatus();
      return;
    }
    let selected = groups.find((group) => group.name === browserGroupSelectedName(kind));
    if (!selected) {
      selected = groups[0];
      setBrowserSelectedName(kind, selected.name);
    }

    const listRows = groups.map((group, index) => `
      <div class="browser-row${group.name === selected.name ? " selected" : ""}${group.placeholder ? " placeholder" : ""}"
           data-action="${kind === "artists" ? "select-artist" : "select-genre"}"
           data-name="${esc(group.name)}" data-index="${index}">
        <span class="browser-label">${esc(group.displayName || group.name)}</span>
        <span class="browser-detail">${group.albums.length}</span>
      </div>`).join("");

    setContent(`
      <div class="browser">
        <div class="browser-list">
          <div class="browser-list-scroll" id="browser-list-scroll">
            ${listRows}
          </div>
        </div>
        <div class="browser-pane">
          <div id="browser-pane-header" class="browser-pane-header"></div>
          <div class="hairline"></div>
          <div id="browser-pane-scroll" class="browser-pane-scroll"></div>
        </div>
      </div>`);

    state.songRows = [];
    state.browserListVirtual = null;
    updateBrowserPane(kind, selected, 0);
    updateStatus();
  }

  function selectBrowserGroup(kind, name) {
    state.browserFocus = "list";
    setBrowserSelectedName(kind, name);
    if (kind === "artists" && !routeApplying) {
      syncRoute(artistRoute(name));
    }
    state.selectedSongs.clear();
    state.songAnchor = null;
    const groups = state.browserGroups || [];
    const selected = groups.find((group) => group.name === name);
    if (!selected) return;
    $$(".browser-row", el.content).forEach((row) => {
      row.classList.toggle("selected", row.dataset.name === name);
    });
    updateBrowserPane(kind, selected, 0);
    updateStatus();
    focusContent();
  }

  function updateBrowserPane(kind, group, scrollTop = 0) {
    const header = $("#browser-pane-header", el.content);
    const body = $("#browser-pane-scroll", el.content);
    if (!header || !body) return;
    const albums = sortedAlbums(group.albums);
    const orderedSongs = albums.flatMap((album) =>
      state.songsByAlbum.get(album.id) || []);
    state.songRows = orderedSongs;
    if (!state.browserAlbumAnchor
        || !albums.some((album) => album.id === state.browserAlbumAnchor)) {
      state.browserAlbumAnchor = albums.length ? albums[0].id : null;
    }
    const title = kind === "genres"
      ? (group.name || "#Unknown")
      : group.name;
    header.innerHTML = `
      <div class="browser-pane-heading">
        <div class="browser-pane-title">${esc(title)}</div>
        <div class="browser-pane-subtitle">${albums.length} album${albums.length === 1 ? "" : "s"}</div>
      </div>
      <div class="browser-pane-actions">
        <button class="album-action-button" type="button" data-action="browser-play-all">
          ${icon("play")} Play
        </button>
        <button class="album-action-button" type="button" data-action="browser-shuffle-all">
          ${icon("shuffle")} Shuffle
        </button>
      </div>`;
    const sections = albums.map((album) => {
      const songs = state.songsByAlbum.get(album.id) || [];
      const info = [album.genre, album.year,
        `${songs.length} song${songs.length === 1 ? "" : "s"}`]
        .filter(Boolean).join(" · ");
      const rows = songs.map((song, index) => {
        const selected = state.selectedSongs.has(song.id);
        const previous = index > 0 && state.selectedSongs.has(songs[index - 1].id);
        const next = index + 1 < songs.length && state.selectedSongs.has(songs[index + 1].id);
        const isCurrent = currentSong() && currentSong().id === song.id;
        const status = isCurrent
          ? icon(player.playing ? "play" : "pause")
          : esc(trackLabel(song, false));
        return `
          <div class="browser-song-row${selected ? " selected" : ""}${selected && previous ? " joined-top" : ""}${selected && next ? " joined-bottom" : ""}"
               data-action="browser-song-row" data-song-id="${esc(song.id)}"
               data-album-id="${esc(album.id)}" draggable="true">
            <div class="browser-song-number" data-status-song="${esc(song.id)}"
                 data-original="${esc(trackLabel(song, false))}">${status}</div>
            <div class="browser-song-title">${esc(song.title)}</div>
            <div class="browser-song-time">${fmtTime(song.duration)}</div>
            <button class="star-button${song.loved ? " loved" : ""}" type="button"
                    data-action="toggle-star" data-star-id="${esc(song.id)}"
                    title="${song.loved ? "Remove from Stared" : "Add to Stared"}"
                    aria-label="${song.loved ? "Remove from Stared" : "Add to Stared"}">
              ${icon(song.loved ? "star-fill" : "star")}
            </button>
          </div>`;
      }).join("");
      return `
        <div class="browser-album${album.id === state.browserAlbumAnchor ? " selected" : ""}"
             data-browser-album-id="${esc(album.id)}">
          <div class="browser-album-head">
            <div class="browser-album-art">
              <img src="${artURL(album.id)}" alt="" loading="lazy">
            </div>
            <div class="browser-album-meta">
              <div class="browser-album-title">${esc(album.title)}</div>
              <div class="browser-album-artist">${esc(album.artist)}</div>
              <div class="browser-album-info">${esc(info)}</div>
            </div>
            <button class="browser-album-play" type="button"
                    data-action="browser-play-album" data-album-id="${esc(album.id)}"
                    title="Play ${esc(album.title)}">${icon("play")}</button>
          </div>
          ${rows}
        </div>
        <div class="hairline"></div>`;
    }).join("");
    body.innerHTML = sections || `<div class="song-table-empty">No Albums</div>`;
    body.scrollTop = scrollTop;
  }

  // ---------------------------------------------------------------------
  // Selection indicators and status bar

  function refreshCurrentIndicators() {
    const song = currentSong();
    $$("[data-status-song]", document).forEach((cell) => {
      const current = song && cell.dataset.statusSong === song.id;
      const original = cell.dataset.original || "";
      if (current) {
        cell.innerHTML = icon(player.playing ? "play" : "pause");
      } else if (cell.innerHTML.indexOf("<svg") === 0) {
        cell.textContent = original;
      }
    });
    updateStatus();
    updateLcdStar();
  }

  function updateStatus() {
    let stats = null;
    if (state.selectedSongs.size) {
      const songs = Array.from(state.selectedSongs)
        .map((id) => getSongById(id))
        .filter(Boolean);
      stats = statsForSongs(songs);
    } else if (state.albumDetail && state.albumDetail.album) {
      stats = statsForAlbums([state.albumDetail.album]);
    } else if (state.section === "albums") {
      if (state.selectedAlbums.size) {
        const albums = Array.from(state.selectedAlbums)
          .map((id) => state.albumMap.get(id))
          .filter(Boolean);
        stats = statsForAlbums(albums);
      } else {
        stats = state.stats;
      }
    } else if (state.section === "songs") {
      stats = state.stats;
    } else if (state.section === "starred") {
      stats = state.allSongs
        ? statsForSongs(state.allSongs.filter((song) => song.loved))
        : state.starredStats;
    } else if (state.section === "playlist") {
      const summary = state.playlists.find((playlist) => playlist.id === state.playlistId);
      stats = summary || null;
    } else if (state.section === "playlists") {
      if (state.playlistId) {
        const summary = state.playlists.find(
          (playlist) => playlist.id === state.playlistId
        );
        stats = summary || null;
      } else {
        stats = state.allSongs
          ? statsForSongs(state.allSongs.filter((song) => song.loved))
          : state.starredStats;
      }
    } else if (state.section === "artists") {
      const group = (state.browserGroups || []).find(
        (item) => item.name === state.selectedArtist
      );
      stats = group ? statsForAlbums(group.albums) : state.stats;
    } else if (state.section === "genres") {
      const group = (state.browserGroups || []).find(
        (item) => item.name === state.selectedGenre
      );
      stats = group ? statsForAlbums(group.albums) : state.stats;
    }
    const statusLabel = stats ? statsText(stats) : "";
    el.statusText.textContent = statusLabel;
    el.statusText.title = statusLabel;
    const song = currentSong();
    if (song) {
      const isPlaying = Boolean(audio && !audio.paused && !audio.ended);
      el.statusPlaying.classList.remove("hidden");
      el.statusPlayUse.setAttribute("href", isPlaying ? "#i-play" : "#i-pause");
      el.statusPlaying.title = isPlaying ? "Playing" : "Paused";
    } else {
      el.statusPlaying.classList.add("hidden");
    }
  }

  // ---------------------------------------------------------------------
  // Playback state and persistence

  function currentSong() {
    if (player.index < 0 || player.index >= player.queue.length) return null;
    return player.queue[player.index] || null;
  }

  function currentDuration() {
    const song = currentSong();
    if (audio && Number.isFinite(audio.duration) && audio.duration > 0
        && (!song || !song.duration)) {
      return audio.duration;
    }
    return Math.max(0, Number(song && song.duration) || 0);
  }

  function compactSong(song) {
    if (!song || !song.id) return null;
    return {
      id: song.id,
      title: song.title || "",
      artist: song.artist || "",
      albumArtist: song.albumArtist || song.artist || "",
      album: song.album || "",
      albumId: song.albumId || "",
      genre: song.genre || "",
      year: song.year || "",
      track: Number(song.track) || 0,
      trackTotal: Number(song.trackTotal) || 0,
      disc: Number(song.disc) || 0,
      discTotal: Number(song.discTotal) || 0,
      duration: Number(song.duration) || 0,
      dateAdded: Number(song.dateAdded) || 0,
      mtime: Number(song.mtime) || 0,
      plays: Number(song.plays) || 0,
      loved: Boolean(song.loved),
      fileSize: Number(song.fileSize) || 0,
    };
  }

  function parseStored(key) {
    try {
      return JSON.parse(localStorage.getItem(key) || "null");
    } catch (_) {
      return null;
    }
  }

  function restorePlayer() {
    let saved = parseStored(PLAYER_STATE_KEY);
    let savedQueue = parseStored(PLAYER_QUEUE_KEY);
    if (!saved || !savedQueue) {
      const legacy = parseStored(PLAYER_KEY);
      if (legacy && typeof legacy === "object") {
        saved = saved || legacy;
        savedQueue = savedQueue || legacy;
      }
    }
    if (!savedQueue || typeof savedQueue !== "object") {
      return;
    }
    const queue = Array.isArray(savedQueue.queue)
      ? savedQueue.queue.filter((song) => song && song.id)
        .map(compactSong).filter(Boolean)
      : [];
    player.queue = queue;
    player.baseQueue = Array.isArray(savedQueue.baseQueue) && savedQueue.baseQueue.length
      ? savedQueue.baseQueue.filter((song) => song && song.id)
        .map(compactSong).filter(Boolean)
      : queue.slice();
    player.index = saved && Number.isInteger(saved.index) ? saved.index : -1;
    if (player.index >= queue.length) player.index = queue.length - 1;
    if (player.index < -1) player.index = -1;
    player.elapsed = saved ? Math.max(0, Number(saved.elapsed) || 0) : 0;
    player.history = Array.isArray(savedQueue.history)
      ? savedQueue.history.filter((song) => song && song.id)
        .map(compactSong).filter(Boolean)
      : [];
    player.shuffle = Boolean(saved && saved.shuffle);
    player.repeat = saved && ["off", "all", "one"].includes(saved.repeat)
      ? saved.repeat : "off";
    state.showRemaining = Boolean(saved && saved.showRemaining);
    if (localStorage.getItem(PLAYER_KEY)) {
      try { localStorage.removeItem(PLAYER_KEY); } catch (_) {}
    }
  }

  function persistWindowStart(length, index) {
    if (length <= MAX_PERSIST_QUEUE || index < 0) return 0;
    return clamp(index - 300, 0, length - MAX_PERSIST_QUEUE);
  }

  function savePlayerState() {
    try {
      const offset = persistWindowStart(player.queue.length, player.index);
      localStorage.setItem(PLAYER_STATE_KEY, JSON.stringify({
        index: player.index >= 0 ? player.index - offset : -1,
        queueOffset: offset,
        elapsed: player.elapsed,
        shuffle: player.shuffle,
        repeat: player.repeat,
        showRemaining: state.showRemaining,
      }));
    } catch (_) {
      // Playback persistence is best-effort.
    }
  }

  function savePlayerQueue() {
    try {
      const offset = persistWindowStart(player.queue.length, player.index);
      const queue = player.queue
        .slice(offset, offset + MAX_PERSIST_QUEUE)
        .map(compactSong).filter(Boolean);
      const baseQueue = player.shuffle
        ? player.baseQueue
          .slice(0, MAX_PERSIST_QUEUE)
          .map(compactSong).filter(Boolean)
        : [];
      const history = player.history.slice(0, HISTORY_LIMIT)
        .map(compactSong).filter(Boolean);
      localStorage.setItem(PLAYER_QUEUE_KEY, JSON.stringify({
        queue,
        baseQueue,
        history,
      }));
    } catch (_) {
      // Playback persistence is best-effort.
    }
  }

  function savePlayer() {
    savePlayerState();
    savePlayerQueue();
  }

  function hydratePlayerSongs() {
    if (!state.allSongs) return;
    const byId = state.songById;
    const replace = (song) => byId.get(song.id) || song;
    player.queue = player.queue.map(replace);
    player.baseQueue = player.baseQueue.map(replace);
    player.history = player.history.map(replace);
    renderPlayerUI();
    if (state.queueOpen) renderQueuePopover();
    refreshCurrentIndicators();
  }

  function addHistory(song) {
    if (!song) return;
    player.history = player.history.filter((item) => item.id !== song.id);
    player.history.unshift(song);
    if (player.history.length > HISTORY_LIMIT) {
      player.history.length = HISTORY_LIMIT;
    }
    savePlayerQueue();
    if (state.queueOpen) renderQueuePopover();
  }

  function newPlayId() {
    try {
      if (window.crypto && window.crypto.randomUUID) {
        return window.crypto.randomUUID();
      }
    } catch (_) {
      // Fall through to the timestamp-based value.
    }
    return `play-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  function recordPlaybackStart(song) {
    if (!song) return;
    addHistory(song);
    if (state.readOnly) return;
    const previous = Number(song.plays) || 0;
    patchSong(song.id, { plays: previous + 1 });
    if (state.songVirtual) state.songVirtual.rerender();
  }

  function shuffleArray(items) {
    const array = items.slice();
    for (let i = array.length - 1; i > 0; i -= 1) {
      const j = Math.floor(Math.random() * (i + 1));
      [array[i], array[j]] = [array[j], array[i]];
    }
    return array;
  }

  function playCollection(songs, startIndex = 0, options = {}) {
    const collection = (songs || []).filter((song) => song && song.id);
    if (!collection.length) return;
    audio.pause();
    player.baseQueue = collection.slice();
    if (options.shuffle) {
      player.shuffle = true;
      player.queue = shuffleArray(collection);
      player.index = 0;
    } else {
      player.shuffle = false;
      player.repeat = "off";
      player.queue = collection.slice();
      player.index = clamp(startIndex, 0, collection.length - 1);
    }
    player.elapsed = 0;
    startCurrent(true);
  }

  function playShuffled(songs) {
    const collection = (songs || []).filter((song) => song && song.id);
    if (!collection.length) return;
    audio.pause();
    player.baseQueue = collection.slice();
    player.shuffle = true;
    player.queue = shuffleArray(collection);
    player.index = 0;
    player.elapsed = 0;
    startCurrent(true);
  }

  function startCurrent(countPlay) {
    const song = currentSong();
    if (!song) return;
    player.playing = true;
    player.elapsed = 0;
    player.pendingRecord = countPlay ? song.id : false;
    if (countPlay) player.playId = newPlayId();
    player.loadedSongId = null;
    player.pendingSeek = 0;
    player.pendingPlay = true;
    audio.volume = 1;
    audio.src = audioURL(song);
    audio.load();
    setAudioBuffering(true);
    // Start inside the originating click when possible. Safari can reject an
    // asynchronous play() call from loadedmetadata if it was not user-driven.
    const initialPlay = audio.play();
    if (initialPlay && initialPlay.catch) {
      initialPlay.catch(() => {});
    }
    state.lcdSongId = null;
    renderPlayerUI();
    refreshCurrentIndicators();
    if (state.queueOpen) renderQueuePopover();
    updateStatus();
    savePlayer();
  }

  function restartCurrent(countPlay) {
    const song = currentSong();
    if (!song) return;
    player.playing = true;
    player.elapsed = 0;
    player.pendingRecord = countPlay ? song.id : false;
    if (countPlay) player.playId = newPlayId();
    player.pendingSeek = 0;
    player.pendingPlay = true;
    player.loadedSongId = null;
    audio.src = audioURL(song);
    audio.volume = 1;
    audio.load();
    setAudioBuffering(true);
    const initialPlay = audio.play();
    if (initialPlay && initialPlay.catch) {
      initialPlay.catch(() => {});
    }
    renderPlayerUI();
    refreshCurrentIndicators();
    savePlayerState();
  }

  function togglePlayPause() {
    let song = currentSong();
    if (!song && player.queue.length) {
      player.index = 0;
      startCurrent(true);
      return;
    }
    if (!song) return;
    if (player.playing) {
      player.elapsed = Number.isFinite(audio.currentTime)
        ? audio.currentTime : player.elapsed;
      player.playing = false;
      player.pendingPlay = false;
      player.pendingRecord = false;
      audio.pause();
      setAudioBuffering(false);
      renderPlayerUI();
      refreshCurrentIndicators();
      updateStatus();
      savePlayerState();
      return;
    }
    if (player.loadedSongId !== song.id || !audio.src) {
      player.playing = true;
      player.pendingRecord = false;
      player.pendingSeek = player.elapsed;
      player.pendingPlay = true;
      player.loadedSongId = song.id;
      audio.src = audioURL(song);
      audio.volume = 1;
      audio.load();
      setAudioBuffering(true);
      const initialPlay = audio.play();
      if (initialPlay && initialPlay.catch) {
        initialPlay.catch(() => {});
      }
    } else if (Number.isFinite(audio.duration) && audio.duration > 0
        && audio.currentTime >= audio.duration - 0.1) {
      restartCurrent(false);
      return;
    } else {
      player.playing = true;
      const promise = audio.play();
      if (promise && promise.catch) {
        promise.catch((error) => {
          player.playing = false;
          renderPlayerUI();
          toast(`Could not start playback: ${error.message}`, { error: true });
        });
      }
    }
    renderPlayerUI();
    refreshCurrentIndicators();
    updateStatus();
    savePlayerState();
  }

  function nextTrack() {
    if (!player.queue.length) return;
    if (player.index + 1 < player.queue.length) {
      player.index += 1;
      startCurrent(true);
    } else if (player.repeat !== "off") {
      player.index = 0;
      startCurrent(true);
    } else {
      finishQueue();
    }
  }

  function previousTrack() {
    const song = currentSong();
    if (!song) {
      if (player.queue.length) {
        player.index = 0;
        startCurrent(true);
      }
      return;
    }
    if (player.elapsed > 3 || player.index <= 0) {
      restartCurrent(false);
    } else {
      player.index -= 1;
      startCurrent(true);
    }
  }

  function finishQueue() {
    player.playing = false;
    player.pendingRecord = false;
    audio.pause();
    player.elapsed = currentDuration();
    renderPlayerUI();
    refreshCurrentIndicators();
    updateStatus();
    savePlayerState();
  }

  function toggleShuffle() {
    player.shuffle = !player.shuffle;
    if (player.shuffle) {
      if (player.queue.length) {
        if (player.index >= 0) {
          const prefix = player.queue.slice(0, player.index + 1);
          const tail = shuffleArray(player.queue.slice(player.index + 1));
          player.queue = prefix.concat(tail);
        } else {
          player.queue = shuffleArray(player.queue);
        }
      }
    } else {
      const restored = player.baseQueue.length ? player.baseQueue.slice() : player.queue.slice();
      const current = currentSong();
      if (current) {
        const index = restored.findIndex((song) => song.id === current.id);
        if (index >= 0) {
          player.queue = restored;
          player.index = index;
        }
      } else {
        player.queue = restored;
      }
    }
    renderPlayerUI();
    if (state.queueOpen) renderQueuePopover();
    savePlayer();
  }

  function cycleRepeat() {
    player.repeat = player.repeat === "off"
      ? "all" : player.repeat === "all" ? "one" : "off";
    renderPlayerUI();
    savePlayerState();
  }

  function playNext(songs) {
    const collection = (songs || []).filter((song) => song && song.id);
    if (!collection.length) return;
    if (!currentSong()) {
      playCollection(collection, 0);
      return;
    }
    const insertAt = player.index + 1;
    player.queue.splice(insertAt, 0, ...collection);
    const currentId = currentSong().id;
    const baseIndex = player.baseQueue.findIndex((song) => song.id === currentId);
    if (baseIndex >= 0) {
      player.baseQueue.splice(baseIndex + 1, 0, ...collection);
    } else {
      player.baseQueue.push(...collection);
    }
    if (state.queueOpen) renderQueuePopover();
    savePlayer();
  }

  function playLater(songs) {
    const collection = (songs || []).filter((song) => song && song.id);
    if (!collection.length) return;
    if (!player.queue.length) {
      player.queue = collection.slice();
      player.baseQueue = collection.slice();
      player.index = -1;
    } else {
      player.queue.push(...collection);
      player.baseQueue.push(...collection);
    }
    if (state.queueOpen) renderQueuePopover();
    savePlayer();
  }

  function clearUpNext() {
    if (player.index >= 0) {
      player.queue = player.queue.slice(0, player.index + 1);
      const currentId = currentSong() && currentSong().id;
      const baseIndex = player.baseQueue.findIndex((song) => song.id === currentId);
      if (baseIndex >= 0) player.baseQueue = player.baseQueue.slice(0, baseIndex + 1);
    } else {
      player.queue = [];
      player.baseQueue = [];
    }
    if (state.queueOpen) renderQueuePopover();
    savePlayer();
  }

  function playQueueIndex(index) {
    if (index < 0 || index >= player.queue.length) return;
    player.index = index;
    startCurrent(true);
  }

  function playFromHistory(song) {
    if (!song) return;
    const index = player.queue.findIndex((item) => item.id === song.id);
    if (index >= 0) {
      player.index = index;
      startCurrent(true);
    } else {
      playCollection([song], 0);
    }
  }

  function seekToFraction(fraction) {
    if (!currentSong()) return;
    const duration = currentDuration();
    if (duration <= 0) return;
    const position = clamp(fraction, 0, 1) * duration;
    player.elapsed = position;
    if (player.loadedSongId === currentSong().id
        && audio.readyState >= 1) {
      try {
        audio.currentTime = position;
        player.elapsed = audio.currentTime;
      } catch (_) {
        // Some formats may not be seekable yet.
      }
    }
    updateProgressUI();
    savePlayerState();
  }

  // ---------------------------------------------------------------------
  // Player chrome

  function updateLcdStar() {
    const song = currentSong();
    const loved = Boolean(song && song.loved);
    el.lcdStar.classList.toggle("active", loved);
    el.lcdStar.title = loved ? "Remove current song from Stared" : "Add current song to Stared";
    el.lcdStarUse.setAttribute("href", loved ? "#i-star-fill" : "#i-star");
  }

  function updateProgressUI() {
    const duration = currentDuration();
    const elapsed = clamp(
      state.seekPreview == null ? player.elapsed : state.seekPreview,
      0,
      duration || Number.MAX_SAFE_INTEGER
    );
    const fraction = duration > 0 ? elapsed / duration : 0;
    el.lcdSeekFill.style.width = `${fraction * 100}%`;
    el.lcdSeek.setAttribute("aria-valuenow", String(Math.round(fraction * 100)));
    el.lcdElapsed.textContent = fmtTime(elapsed);
    el.lcdDuration.textContent = state.showRemaining
      ? fmtRemaining(Math.max(0, duration - elapsed))
      : fmtTime(duration);
    el.lcdDuration.title = state.showRemaining
      ? "Show Total Time" : "Show Remaining Time";
  }

  function renderPlayerUI() {
    const song = currentSong();
    const isPlaying = player.playing;
    el.playUse.setAttribute("href", isPlaying ? "#i-pause" : "#i-play");
    el.playButton.title = isPlaying ? "Pause" : "Play";
    el.playButton.setAttribute("aria-label", isPlaying ? "Pause" : "Play");

    el.lcdShuffle.classList.toggle("active", player.shuffle);
    el.lcdRepeat.classList.toggle("active", player.repeat !== "off");
    el.lcdRepeatUse.setAttribute(
      "href", player.repeat === "one" ? "#i-repeat-one" : "#i-repeat"
    );
    el.lcdRepeat.title = player.repeat === "off"
      ? "Repeat Off" : player.repeat === "all" ? "Repeat All" : "Repeat One";
    el.lcdShuffle.title = player.shuffle ? "Shuffle On" : "Shuffle Off";

    if (!song) {
      setAudioBuffering(false);
      el.lcd.classList.add("idle");
      el.lcdPlaying.classList.add("hidden");
      el.lcdIdle.classList.remove("hidden");
      el.lcdTitle.textContent = "";
      el.lcdSubtitle.textContent = "";
      state.lcdSongId = null;
      updateProgressUI();
      updateStatus();
      return;
    }

    el.lcd.classList.remove("idle");
    el.lcdPlaying.classList.remove("hidden");
    el.lcdIdle.classList.add("hidden");

    if (state.lcdSongId !== song.id) {
      state.lcdSongId = song.id;
      if (song.albumId) {
        el.lcdArt.src = artURL(song.albumId);
        el.lcdArtWrap.classList.remove("no-art");
      } else {
        el.lcdArt.removeAttribute("src");
        el.lcdArtWrap.classList.add("no-art");
      }
    }
    el.lcdTitle.textContent = song.title || "";
    el.lcdSubtitle.textContent = song.album
      ? `${song.artist || "Unknown Artist"} — ${song.album}`
      : (song.artist || "");
    el.lcdTitleButton.title = song.album ? "Show album" : "";
    updateLcdStar();
    updateProgressUI();
    updateStatus();
  }

  // ---------------------------------------------------------------------
  // Queue panel

  function toggleQueue(force) {
    const open = force == null ? !state.queueOpen : Boolean(force);
    state.queueOpen = open;
    el.queueButton.classList.toggle("active", open);
    if (!open) {
      el.queuePopover.classList.add("hidden");
      return;
    }
    renderQueuePopover();
    positionQueuePopover();
  }

  function positionQueuePopover() {
    const anchor = el.queueButton.getBoundingClientRect();
    const width = 320;
    const height = el.queuePopover.offsetHeight || 380;
    let left = anchor.left + anchor.width / 2 - width / 2;
    left = clamp(left, 8, window.innerWidth - width - 8);
    let top = anchor.bottom + 5;
    if (top + height > window.innerHeight - 8) {
      top = Math.max(8, anchor.top - height - 5);
    }
    el.queuePopover.style.left = `${Math.round(left)}px`;
    el.queuePopover.style.top = `${Math.round(top)}px`;
    el.queuePopover.classList.remove("hidden");
  }

  function renderQueuePopover() {
    if (!state.queueOpen) return;
    $$(".queue-tab", el.queuePopover).forEach((tab) => {
      const active = tab.dataset.queueTab === state.queueTab;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    const isNext = state.queueTab === "next";
    const upNext = player.index >= 0
      ? player.queue.slice(player.index + 1)
      : player.queue.slice();
    const items = isNext ? upNext : player.history;
    state.queueItems = items;
    state.queueIsNext = isNext;
    if (!items.length) {
      el.queueList.innerHTML = `<div class="queue-empty">${isNext ? "Nothing Up Next" : "No History"}</div>`;
    } else {
      el.queueList.innerHTML = '<div class="queue-virtual-spacer"></div><div class="queue-virtual-layer"></div>';
      const spacer = el.queueList.firstElementChild;
      spacer.style.height = `${4 + items.length * 48}px`;
      el.queueList.scrollTop = 0;
      renderQueueWindow();
    }
    el.queueClear.disabled = !isNext || !upNext.length;
    el.queueClear.classList.toggle("hidden", !isNext);
    el.queueCount.textContent = isNext
      ? `${upNext.length} item${upNext.length === 1 ? "" : "s"}`
      : `${player.history.length} item${player.history.length === 1 ? "" : "s"}`;
  }

  function renderQueueWindow() {
    const items = state.queueItems || [];
    const layer = el.queueList.querySelector(".queue-virtual-layer");
    if (!items.length || !layer) return;
    const height = el.queueList.clientHeight || 300;
    const scrollTop = el.queueList.scrollTop;
    const first = Math.max(0, Math.floor((scrollTop - 2) / 48) - 5);
    const last = Math.min(
      items.length,
      Math.ceil((scrollTop + height - 2) / 48) + 5
    );
    let html = "";
    for (let index = first; index < last; index += 1) {
      html += queueRowHtml(items[index], index, state.queueIsNext);
    }
    layer.innerHTML = html;
  }

  function queueRowHtml(song, index, isNext) {
    const globalIndex = isNext
      ? (player.index >= 0 ? player.index + 1 : 0) + index : -1;
    const action = isNext
      ? `data-action="queue-play" data-index="${globalIndex}"`
      : `data-action="history-play" data-song-id="${esc(song.id)}"`;
    return `
      <div class="queue-virtual-row" style="top:${2 + index * 48}px">
        <div class="queue-row" ${action}>
          <div class="queue-row-main">
            <div class="queue-row-title">
              <span>${esc(song.title)}</span>
              ${song.loved ? icon("star-fill") : ""}
            </div>
            <div class="queue-row-artist">${esc(song.artist || "")}</div>
            <div class="queue-row-album">${esc(song.album || "")}</div>
          </div>
          <div class="queue-row-time">${fmtTime(song.duration)}</div>
        </div>
      </div>`;
  }

  // ---------------------------------------------------------------------
  // Context menus

  function showMenu(x, y, items) {
    if (!items || !items.length) return;
    closeMenu();
    const menu = document.createElement("div");
    menu.className = "context-menu";
    menu.setAttribute("role", "menu");
    const hasIcons = items.some((item) => !item.separator && item.icon);
    menu.innerHTML = items.map((item, index) => {
      if (item.separator) return '<div class="menu-separator"></div>';
      const classes = [
        "menu-item",
        item.disabled ? "disabled" : "",
        item.danger ? "danger" : "",
      ].filter(Boolean).join(" ");
      const check = item.checked
        ? `<span class="menu-check">${icon("check")}</span>`
        : '<span class="menu-check"></span>';
      const glyph = item.icon
        ? `<span class="menu-icon">${icon(item.icon)}</span>`
        : hasIcons ? '<span class="menu-icon"></span>' : "";
      return `
        <div class="${classes}" role="menuitem" data-menu-index="${index}"
             aria-disabled="${item.disabled ? "true" : "false"}">
          ${glyph}${check}<span class="menu-label">${esc(item.label || "")}</span>
        </div>`;
    }).join("");
    el.menuRoot.innerHTML = "";
    el.menuRoot.appendChild(menu);
    el.menuRoot.classList.remove("hidden");
    const rect = menu.getBoundingClientRect();
    const left = clamp(x, 8, Math.max(8, window.innerWidth - rect.width - 8));
    const top = clamp(y, 8, Math.max(8, window.innerHeight - rect.height - 8));
    menu.style.left = `${Math.round(left)}px`;
    menu.style.top = `${Math.round(top)}px`;
    menu.addEventListener("click", (event) => {
      const itemEl = event.target.closest(".menu-item");
      if (!itemEl) return;
      const item = items[Number(itemEl.dataset.menuIndex)];
      if (!item || item.disabled) return;
      event.stopPropagation();
      closeMenu();
      try {
        const result = item.action && item.action();
        if (result && result.catch) {
          result.catch((error) => toast(error.message, { error: true }));
        }
      } catch (error) {
        toast(error.message, { error: true });
      }
    });
  }

  function closeMenu() {
    el.menuRoot.classList.add("hidden");
    el.menuRoot.innerHTML = "";
  }

  function contextMenuForAlbum(album, event) {
    event.preventDefault();
    event.stopPropagation();
    let albums = [album];
    if (state.selectedAlbums.has(album.id) && state.selectedAlbums.size > 1) {
      albums = Array.from(state.selectedAlbums)
        .map((id) => state.albumMap.get(id))
        .filter(Boolean);
    }
    const loadSongs = () => songsForAlbums(albums);
    const items = [
      {
        label: albums.length > 1 ? `Play ${albums.length} Albums` : "Play",
        icon: "play",
        action: async () => playCollection(await loadSongs(), 0),
      },
      {
        label: "Shuffle",
        icon: "shuffle",
        action: async () => playShuffled(await loadSongs()),
      },
      { separator: true },
      {
        label: "Play Next",
        icon: "list",
        action: async () => playNext(await loadSongs()),
      },
      {
        label: "Play Later",
        icon: "list",
        action: async () => playLater(await loadSongs()),
      },
      { separator: true },
      {
        label: "Add to Playlist",
        icon: "note-list",
        action: async () => {
          openAddToPlaylistDialog(await loadSongs());
        },
      },
      {
        label: "Copy Album Link",
        icon: "link",
        action: () => copyRouteLink(albumRoute(album.id)),
      },
    ];
    showMenu(event.clientX, event.clientY, items);
  }

  async function songsForAlbums(albums) {
    if (!albums || !albums.length) return [];
    if (state.allSongs) {
      return albums.flatMap((album) => state.songsByAlbum.get(album.id) || []);
    }
    if (albums.length > 3) {
      await ensureAllSongs();
      return albums.flatMap((album) => state.songsByAlbum.get(album.id) || []);
    }
    const lists = await Promise.all(albums.map((album) => getAlbumSongs(album.id)));
    return lists.flat();
  }

  async function getAlbumSongs(albumId) {
    if (state.allSongs && state.songsByAlbum.has(albumId)) {
      return state.songsByAlbum.get(albumId);
    }
    const cached = state.albumDetails.get(albumId);
    if (cached) return cached.songs || [];
    const data = await api(`/api/albums/${encodeURIComponent(albumId)}`);
    state.albumDetails.set(albumId, data);
    return data.songs || [];
  }

  function songMenuContext(song, row) {
    let songs = null;
    let index = -1;
    if (state.selectedSongs.has(song.id) && state.selectedSongs.size > 1) {
      songs = state.songRows.filter((item) => state.selectedSongs.has(item.id));
      index = 0;
    } else if (row && row.classList.contains("track-row") && state.albumDetail) {
      songs = state.albumDetail.songs || [];
      index = songs.findIndex((item) => item.id === song.id);
    } else if (row && row.classList.contains("browser-song-row")) {
      const section = row.closest("[data-browser-album-id]");
      const albumId = section && section.dataset.browserAlbumId;
      songs = albumId ? (state.songsByAlbum.get(albumId) || []) : state.songRows;
      index = songs.findIndex((item) => item.id === song.id);
    } else {
      songs = state.songRows || [];
      index = songs.findIndex((item) => item.id === song.id);
    }
    if (!songs || !songs.length) songs = [song];
    if (index < 0) index = 0;
    return { songs, index };
  }

  async function goToAlbum(song) {
    if (!song || !song.albumId) return;
    await setSection("albums");
    await openAlbum(song.albumId);
  }

  function contextMenuForSong(song, row, event) {
    event.preventDefault();
    event.stopPropagation();
    const context = songMenuContext(song, row);
    const selectedIds = state.selectedSongs.has(song.id) && state.selectedSongs.size > 1
      ? Array.from(state.selectedSongs)
      : [song.id];
    const selectionSongs = state.songRows.filter((item) => selectedIds.includes(item.id));
    const targetSongs = selectionSongs.length ? selectionSongs : context.songs;
    const items = [
      {
        label: "Play",
        icon: "play",
        action: () => playCollection(context.songs, context.index),
      },
      {
        label: "Play Next",
        icon: "list",
        action: () => playNext(targetSongs),
      },
      {
        label: "Play Later",
        icon: "list",
        action: () => playLater(targetSongs),
      },
      { separator: true },
      {
        label: song.loved ? "Remove from Stared" : "Add to Stared",
        icon: song.loved ? "star-fill" : "star",
        action: () => toggleStar(song.id),
      },
      {
        label: "Add to Playlist",
        icon: "note-list",
        action: () => openAddToPlaylistDialog(targetSongs),
      },
    ];
    if (song.albumId && [
      "songs", "starred", "playlist", "playlists",
    ].includes(state.section)) {
      items.push({
        label: "Go to Album",
        icon: "albums",
        action: () => goToAlbum(song),
      });
    }
    items.push({
      label: "Copy Song Link",
      icon: "link",
      action: () => copyRouteLink(songRoute(song.id)),
    });
    if ((state.section === "playlist" || state.section === "playlists")
        && state.playlistId && !state.albumDetail) {
      items.push({ separator: true });
      items.push({
        label: "Remove from Playlist",
        icon: "trash",
        danger: true,
        action: () => removeFromPlaylist(state.playlistId, targetSongs.map((item) => item.id)),
      });
    }
    showMenu(event.clientX, event.clientY, items);
  }

  function contextMenuForPlaylist(playlist, event) {
    event.preventDefault();
    event.stopPropagation();
    showMenu(event.clientX, event.clientY, [
      {
        label: "Play",
        icon: "play",
        action: async () => {
          const detail = await ensurePlaylistDetail(playlist.id);
          const songs = songsSorted(detail.songs || [], songSortKeyFor({ kind: "playlist", playlistId: playlist.id }));
          if (songs.length) playCollection(songs, 0);
        },
      },
      {
        label: "Shuffle",
        icon: "shuffle",
        action: async () => {
          const detail = await ensurePlaylistDetail(playlist.id);
          if ((detail.songs || []).length) playShuffled(detail.songs);
        },
      },
      {
        label: "Copy Playlist Link",
        icon: "link",
        action: () => copyRouteLink(playlistRoute(playlist.id)),
      },
      { separator: true },
      {
        label: "Rename",
        icon: "pencil",
        action: () => openPlaylistDialog("rename", playlist),
      },
      { separator: true },
      {
        label: "Delete",
        icon: "trash",
        danger: true,
        action: () => {
          if (!state.adminAuthenticated) {
            toast("Log in from Setup to delete playlists.", { error: true });
            return;
          }
          confirmDialog({
            title: "Delete Playlist?",
            message: `“${playlist.name}” will be removed from the database. The music files are not touched.`,
            confirmLabel: "Delete",
            danger: true,
            onConfirm: () => deletePlaylist(playlist.id),
          });
        },
      },
    ]);
  }

  // ---------------------------------------------------------------------
  // Star and playlist mutations

  function updateStarButtons() {
    $$("[data-star-id]").forEach((button) => {
      const song = getSongById(button.dataset.starId);
      if (!song) return;
      button.classList.toggle("loved", Boolean(song.loved));
      button.innerHTML = icon(song.loved ? "star-fill" : "star");
      button.title = song.loved ? "Remove from Stared" : "Add to Stared";
      button.setAttribute(
        "aria-label", song.loved ? "Remove from Stared" : "Add to Stared"
      );
    });
    updateLcdStar();
  }

  async function toggleStar(songId) {
    const song = getSongById(songId);
    if (!song) return;
    if (state.readOnly) {
      toast("The server is running in read-only mode", { error: true });
      return;
    }
    const before = Boolean(song.loved);
    const desired = !before;
    patchSong(songId, { loved: desired });
    if (state.allSongs) {
      state.starredStats = statsForSongs(
        state.allSongs.filter((item) => item.loved)
      );
    }
    updateStarButtons();
    updateStatus();
    renderPlayerUI();
    if (state.queueOpen) renderQueuePopover();
    let scroll = 0;
    if ((state.section === "starred"
        || (state.section === "playlists" && !state.playlistId))
        && !state.albumDetail) {
      const scroller = $(".song-scroll", el.content);
      if (scroller) scroll = scroller.scrollTop;
      state.selectedSongs.delete(songId);
      if (state.songAnchor === songId) state.songAnchor = null;
      renderContent();
      window.requestAnimationFrame(() => {
        const next = $(".song-scroll", el.content);
        if (next) next.scrollTop = scroll;
      });
    }
    try {
      const data = await api(`/api/songs/${encodeURIComponent(songId)}/star`, {
        method: "POST",
        body: JSON.stringify({ loved: desired }),
      });
      if (data && typeof data.loved === "boolean") {
        patchSong(songId, { loved: data.loved });
        updateStarButtons();
        updateStatus();
      }
    } catch (error) {
      patchSong(songId, { loved: before });
      updateStarButtons();
      updateStatus();
      if ((state.section === "starred"
        || (state.section === "playlists" && !state.playlistId))
        && !state.albumDetail) renderContent();
      toast(error.message, { error: true });
    }
  }

  function summaryFromPlaylistDetail(detail) {
    return {
      id: detail.id,
      name: detail.name,
      songCount: (detail.songs || []).length,
      duration: (detail.songs || []).reduce(
        (total, song) => total + (Number(song.duration) || 0), 0
      ),
      size: (detail.songs || []).reduce(
        (total, song) => total + (Number(song.fileSize) || 0), 0
      ),
      dateModified: detail.dateModified || Date.now() / 1000,
    };
  }

  function applyPlaylistPayload(payload) {
    if (!payload || !payload.id) return;
    state.playlistDetails.set(payload.id, payload);
    const summary = summaryFromPlaylistDetail(payload);
    const index = state.playlists.findIndex((playlist) => playlist.id === payload.id);
    if (index >= 0) state.playlists[index] = summary;
    else {
      state.playlists.push(summary);
      state.playlists.sort((a, b) => a.name.localeCompare(b.name));
    }
    renderSectionTabs();
    if (state.section === "playlists") renderContent();
  }

  async function addSongsToPlaylist(playlistId, songIds) {
    if (state.readOnly) {
      toast("The server is running in read-only mode", { error: true });
      return;
    }
    if (!songIds.length) return;
    try {
      const data = await api(`/api/playlists/${encodeURIComponent(playlistId)}/songs`, {
        method: "POST",
        body: JSON.stringify({ songIds }),
      });
      applyPlaylistPayload(data.playlist);
      const added = Number(data.added) || 0;
      toast(added
        ? `Added ${added} song${added === 1 ? "" : "s"} to “${data.playlist.name}”`
        : "Those songs are already in the playlist");
      if (state.section === "playlist" && state.playlistId === playlistId) {
        const scroll = $(".song-scroll", el.content)
          ? $(".song-scroll", el.content).scrollTop : 0;
        renderContent();
        updateStatus();
        window.requestAnimationFrame(() => {
          const scroller = $(".song-scroll", el.content);
          if (scroller) scroller.scrollTop = scroll;
        });
      }
    } catch (error) {
      toast(error.message, { error: true });
    }
  }

  async function removeFromPlaylist(playlistId, songIds) {
    if (state.readOnly) {
      toast("The server is running in read-only mode", { error: true });
      return;
    }
    if (!songIds.length) return;
    try {
      const data = await api(`/api/playlists/${encodeURIComponent(playlistId)}/songs`, {
        method: "DELETE",
        body: JSON.stringify({ songIds }),
      });
      applyPlaylistPayload(data.playlist);
      songIds.forEach((id) => state.selectedSongs.delete(id));
      if (state.section === "playlist" && state.playlistId === playlistId) {
        renderContent();
        updateStatus();
      }
      toast(`Removed ${Number(data.removed) || 0} song${Number(data.removed) === 1 ? "" : "s"}`);
    } catch (error) {
      toast(error.message, { error: true });
    }
  }

  async function deletePlaylist(playlistId) {
    if (!state.adminAuthenticated) {
      toast("Log in from Setup to delete playlists.", { error: true });
      return;
    }
    try {
      await api(`/api/playlists/${encodeURIComponent(playlistId)}`, { method: "DELETE" });
      state.playlists = state.playlists.filter((playlist) => playlist.id !== playlistId);
      state.playlistDetails.delete(playlistId);
      if (state.section === "playlist" && state.playlistId === playlistId) {
        state.section = "playlists";
        state.playlistId = null;
        savePrefs();
        renderSectionTabs();
        renderContent();
        updateStatus();
      } else if (state.section === "playlists") {
        renderSectionTabs();
        renderContent();
        updateStatus();
      } else {
        renderSectionTabs();
      }
      toast("Playlist deleted");
    } catch (error) {
      toast(error.message, { error: true });
    }
  }

  async function playAllBrowserGroup() {
    const kind = state.browserKind;
    if (!kind) return;
    const selectedName = browserGroupSelectedName(kind);
    const group = (state.browserGroups || []).find((item) => item.name === selectedName);
    if (!group) return;
    const albums = sortedAlbums(group.albums);
    const songs = albums.flatMap((album) => state.songsByAlbum.get(album.id) || []);
    playCollection(songs, 0);
  }

  function playBrowserGroupShuffled() {
    const kind = state.browserKind;
    if (!kind) return;
    const selectedName = browserGroupSelectedName(kind);
    const group = (state.browserGroups || []).find((item) => item.name === selectedName);
    if (!group) return;
    const albums = sortedAlbums(group.albums);
    const songs = albums.flatMap((album) => state.songsByAlbum.get(album.id) || []);
    playShuffled(songs);
  }

  // ---------------------------------------------------------------------
  // Modal sheets (scan + confirm)

  function closeModal() {
    state.setupOpen = false;
    stopSetupStatsPoll();
    state.modalMode = null;
    state.confirmCallback = null;
    state.playlistDialog = null;
    state.libraryIdTarget = null;
    state.addToPlaylistIds = [];
    state.addToPlaylistSongs = [];
    el.modalRoot.classList.add("hidden");
    el.modalRoot.innerHTML = "";
  }

  function confirmDialog(options) {
    state.modalMode = "confirm";
    state.confirmCallback = options.onConfirm || null;
    el.modalRoot.innerHTML = `
      <div class="sheet" role="dialog" aria-modal="true" aria-label="${esc(options.title || "Confirm")}">
        <div class="sheet-header">
          <div>
            <div class="sheet-title">${esc(options.title || "Confirm")}</div>
            <div class="sheet-subtitle">${esc(options.message || "")}</div>
          </div>
          <button class="sheet-close" type="button" data-action="confirm-cancel"
                  title="Close">×</button>
        </div>
        <div class="sheet-actions">
          <button class="secondary-button" type="button" data-action="confirm-cancel">Cancel</button>
          <button class="${options.danger ? "danger-button" : "primary-button"}"
                  type="button" data-action="confirm-ok">
            ${esc(options.confirmLabel || "OK")}
          </button>
        </div>
      </div>`;
    el.modalRoot.classList.remove("hidden");
  }

  async function openSetupModal() {
    state.modalMode = "setup";
    state.setupOpen = true;
    state.setupError = "";
    el.modalRoot.classList.remove("hidden");
    el.modalRoot.innerHTML = `
      <div class="sheet setup-sheet" role="dialog" aria-modal="true" aria-label="Setup">
        <div class="sheet-header">
          <img class="app-mark-small" src="/static/app-icon.svg" alt="">
          <div>
            <div class="sheet-title">Setup</div>
            <div class="sheet-subtitle">Loading configuration…</div>
          </div>
          <button class="sheet-close" type="button" data-action="close-modal"
                  title="Close">×</button>
        </div>
        <div class="sheet-body">
          <div class="center-state">
            ${spinnerHtml()}
            <div class="state-note">Loading Setup…</div>
          </div>
        </div>
      </div>`;
    try {
      const [info, stats] = await Promise.all([
        api("/api/setup", { headers: adminHeaders() }),
        api("/api/setup/stats", { headers: adminHeaders() })
          .catch(() => null),
      ]);
      if (stats) {
        state.setupStats = stats;
        state.statsError = "";
      }
      applySetupInfo(info);
      if (info.rekey && info.rekey.running) {
        renderRekeyProgress(info.rekey);
        scheduleRekeyPoll();
        return;
      }
      if (info.scan && info.scan.running) {
        renderScanProgress(info.scan);
        scheduleScanPoll();
        return;
      }
      if (info.artworkJob && info.artworkJob.running) {
        renderArtworkProgress(info.artworkJob);
        scheduleArtworkPoll();
        return;
      }
      renderSetup();
    } catch (error) {
      state.setupError = error.message;
      renderSetup();
    }
  }

  function applySetupInfo(info) {
    state.setupInfo = info || {};
    state.adminAuthenticated = Boolean(info && info.authenticated);
    if (!state.adminAuthenticated) setAdminToken("");
    if (info && info.scan) state.scanJob = info.scan;
    if (info && info.rekey) state.rekeyJob = info.rekey;
    if (info && info.artworkJob) state.artworkJob = info.artworkJob;
    if (info && ["uuid", "base64url"].includes(info.urlIdFormat)) {
      state.urlIdFormat = info.urlIdFormat;
    }
    if (info && ALBUM_SORT_KEYS.includes(info.albumSort)) {
      state.albumSort = info.albumSort;
    }
    if (info && typeof info.albumSortDescending === "boolean") {
      state.albumSortAsc = !info.albumSortDescending;
    }
  }

  function setupLibrariesHtml(canEdit, showPath) {
    const libraries = (state.setupInfo && state.setupInfo.libraries) || [];
    if (!libraries.length) {
      return `<div class="field-help">No libraries are in the database. Add one with
        <code>python3 server.py scan --root PATH</code>.</div>`;
    }
    return `<div class="setup-libraries">${libraries.map((library) => `
      <div class="setup-library">
        <div class="setup-library-head">
          <div class="setup-library-title">
            <span class="setup-library-name">${esc(
              library.identifier || library.name || library.path || "—"
            )}</span>
            <span class="setup-library-namespace">current namespace: ${esc(
              library.namespace || "—"
            )}</span>
          </div>
          <button class="setup-library-edit" type="button"
                  data-action="edit-library-id"
                  data-path="${esc(library.path || "")}"
                  title="${canEdit ? "Change Library ID" : "Log in to change the Library ID"}"
                  aria-label="Change Library ID" ${canEdit ? "" : "disabled"}>
            ${icon("pencil")}
          </button>
        </div>
        ${showPath && library.path
          ? `<div class="setup-library-path">${esc(library.path)}</div>`
          : ""}
      </div>`).join("")}</div>`;
  }

  function fmtStatsTime(value, full = false) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value || "";
    return full
      ? date.toLocaleString()
      : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function setupStatsHtml() {
    const stats = state.setupStats;
    const detailed = state.adminAuthenticated;
    const totals = (stats && stats.totals) || {};
    const cards = [
      ["Visitors", "visitors", false],
      ["Requests", "requests", false],
      ["Transferred", "bytesSent", true],
      ["Songs played", "songsPlayed", false],
      ["Unique songs", "uniqueSongs", false],
      ["Playlist actions", "playlistActions", false],
    ].map(([label, key, bytes]) => {
      const value = stats
        ? (bytes
          ? fmtCacheSize(totals[key])
          : Number(totals[key] || 0).toLocaleString())
        : "—";
      return `
        <div class="setup-stat${stats ? "" : " placeholder"}">
          <span class="setup-stat-value">${esc(value)}</span>
          <span class="setup-stat-label">${esc(label)}</span>
        </div>`;
    }).join("");
    const since = stats
      ? `Since ${fmtStatsTime(stats.since, true)}`
      : (state.statsError || "Loading…");
    const head = `
      <div class="setup-stats-head">
        <div class="field-label">Usage statistics</div>
        <div class="field-help">${esc(since)}</div>
      </div>
      <div class="setup-stats-grid">${cards}</div>`;
    if (!detailed) {
      return head;
    }
    if (!stats) {
      return `${head}
        <div class="setup-stats-details">
          <div class="field-help">Loading detailed statistics…</div>
        </div>`;
    }
    const actions = [
      ["Logins", totals.adminLogins],
      ["Admin bruteforce blocked", totals.adminBruteforceBlocked],
      ["Scans", totals.scans],
      ["ReKeys", totals.rekeys],
      ["Artwork rebuilds", totals.artworkRebuilds],
      ["Settings changes", totals.settingsChanges],
      ["Rate limited", totals.rateLimited],
    ].map(([label, value]) => `
      <span><strong>${Number(value || 0).toLocaleString()}</strong> ${esc(label)}</span>`
    ).join("");
    const clients = stats.clients || [];
    const rows = clients.map((client) => `
      <tr title="First seen ${esc(fmtStatsTime(client.firstSeen, true))} · ${esc(client.userAgent || "")}">
        <td class="setup-stat-ip">${esc(client.ip || "")}</td>
        <td class="num">${Number(client.online || 0).toLocaleString()}</td>
        <td>${esc(fmtStatsTime(client.lastSeen))}</td>
        <td class="num">${Number(client.requests || 0).toLocaleString()}</td>
        <td class="num">${fmtCacheSize(client.bytesSent)}</td>
        <td class="num">${Number(client.songsPlayed || 0).toLocaleString()}</td>
        <td class="num">${Number(client.playlistActions || 0).toLocaleString()}</td>
      </tr>`).join("");
    const clientHtml = clients.length
      ? `<div class="setup-stats-table-wrap">
           <table class="setup-stats-table">
             <thead><tr>
               <th>Client</th><th class="num">Conn</th><th>Last</th>
               <th class="num">Req</th><th class="num">Data</th>
               <th class="num">Songs</th><th class="num">Playlists</th>
             </tr></thead>
             <tbody>${rows}</tbody>
           </table>
         </div>
         ${Number(stats.clientCount || 0) > clients.length
           ? `<div class="field-help">Showing ${clients.length} of ${Number(stats.clientCount).toLocaleString()} clients</div>`
           : ""}`
      : `<div class="field-help">No traffic recorded yet.</div>`;
    return `${head}
      <div class="setup-stats-details">
        <div class="setup-stats-actions">${actions}</div>
        ${clientHtml}
      </div>`;
  }

  function updateSetupStatsView() {
    const node = $("#setup-stats", el.modalRoot);
    if (node) node.innerHTML = setupStatsHtml();
  }

  function scheduleSetupStatsPoll() {
    if (state.statsTimer) clearTimeout(state.statsTimer);
    state.statsTimer = null;
    if (state.setupOpen && state.modalMode === "setup") {
      state.statsTimer = setTimeout(refreshSetupStats, 5000);
    }
  }

  function stopSetupStatsPoll() {
    if (state.statsTimer) clearTimeout(state.statsTimer);
    state.statsTimer = null;
  }

  async function refreshSetupStats() {
    if (!state.setupOpen || state.modalMode !== "setup"
        || state.statsInFlight) {
      return;
    }
    state.statsInFlight = true;
    try {
      const stats = await api("/api/setup/stats", {
        headers: adminHeaders(),
      });
      state.setupStats = stats;
      state.statsError = "";
    } catch (error) {
      if (error.status === 401) {
        setAdminToken("");
        state.adminAuthenticated = false;
        state.setupStats = null;
        state.setupError = "Session expired. Enter the admin password again.";
        stopSetupStatsPoll();
        renderSetup();
        return;
      }
      state.statsError = error.message;
    } finally {
      state.statsInFlight = false;
    }
    updateSetupStatsView();
    scheduleSetupStatsPoll();
  }

  function setupStatusHtml() {
    const scan = state.scanJob;
    const rekey = state.rekeyJob;
    if (rekey && !rekey.running && rekey.phase === "Error") {
      return `<div class="field-help setup-error">ReKey failed: ${esc(rekey.message || "unknown error")}</div>`;
    }
    if (rekey && !rekey.running && rekey.phase === "Done" && rekey.summary) {
      return `<div class="field-help">ReKey complete. Reload the player to use the new IDs.</div>
        <div class="setup-actions">
          <button class="primary-button" type="button" data-action="rekey-reload">Reload Player</button>
        </div>`;
    }
    if (scan && !scan.running && scan.phase === "Error") {
      return `<div class="field-help setup-error">Last scan failed: ${esc(scan.message || "unknown error")}</div>`;
    }
    if (scan && !scan.running && scan.phase === "Done" && scan.summary) {
      const summary = scan.summary;
      return `<div class="field-help">Last scan: ${Number(summary.tracks || 0).toLocaleString()} tracks · `
        + `${Number(summary.artwork || 0).toLocaleString()} covers · `
        + `${Number(summary.discovered || 0).toLocaleString()} files discovered</div>`;
    }
    return "";
  }

  function renderSetup() {
    if (state.modalMode !== "setup" || !state.setupOpen) return;
    const info = state.setupInfo || {};
    const readOnly = Boolean(info.readOnly ?? state.readOnly);
    const scan = state.scanJob || info.scan || {};
    const rekey = state.rekeyJob || info.rekey || {};
    const artwork = state.artworkJob || info.artworkJob || {};
    const busy = Boolean(scan.running || rekey.running || artwork.running);
    const enabled = state.adminAuthenticated && !readOnly && !busy;
    const disabled = enabled ? "" : "disabled";
    const settingsEnabled = state.adminAuthenticated && !readOnly;
    const settingsDisabled = settingsEnabled ? "" : "disabled";
    const urlIdFormat = ["uuid", "base64url"].includes(info.urlIdFormat)
      ? info.urlIdFormat : state.urlIdFormat;
    const albumSort = ALBUM_SORT_KEYS.includes(info.albumSort)
      ? info.albumSort : state.albumSort;
    const albumSortDescending =
      typeof info.albumSortDescending === "boolean"
        ? info.albumSortDescending : !state.albumSortAsc;
    const coverSize = Number.isFinite(Number(info.coverSize))
      ? Number(info.coverSize) : 500;
    const jpegQuality = Number.isFinite(Number(info.jpegQuality))
      ? Number(info.jpegQuality) : 80;
    const sortOptions = [
      ["recentlyAdded", "Recently Added"],
      ["album", "Album Name"],
      ["artist", "Artist"],
      ["year", "Year"],
    ].map(([value, label]) => `<option value="${value}"`
      + `${albumSort === value ? " selected" : ""}>${label}</option>`).join("");
    const settingsHtml = `
      <div class="setup-section">
        <div class="field-label">Settings</div>
        <div class="setup-setting-row">
          <label class="field-label" for="setup-url-format">URL ID format</label>
          <select id="setup-url-format" class="select-input"
                  aria-label="URL ID format" ${settingsDisabled}>
            <option value="uuid"${urlIdFormat === "uuid" ? " selected" : ""}>UUID</option>
            <option value="base64url"${urlIdFormat === "base64url" ? " selected" : ""}>URL-safe Base64</option>
          </select>
        </div>
        <div class="setup-setting-row">
          <label class="field-label" for="setup-album-sort">Default album sort</label>
          <select id="setup-album-sort" class="select-input"
                  aria-label="Default album sort" ${settingsDisabled}>
            ${sortOptions}
          </select>
        </div>
        <div class="setup-setting-row">
          <label class="field-label" for="setup-album-order">Sort order</label>
          <select id="setup-album-order" class="select-input"
                  aria-label="Default album sort order" ${settingsDisabled}>
            <option value="desc"${albumSortDescending ? " selected" : ""}>Descending</option>
            <option value="asc"${!albumSortDescending ? " selected" : ""}>Ascending</option>
          </select>
        </div>
        <div class="setup-setting-row">
          <label class="field-label" for="setup-cover-size">Cover size</label>
          <input id="setup-cover-size" class="text-input setup-number-input"
                 type="number" min="16" max="2000" step="1"
                 value="${coverSize}" title="Applied by the next full scan"
                 aria-label="Album art cover size in pixels" ${settingsDisabled}>
          <span class="setup-setting-unit">px</span>
        </div>
        <div class="setup-setting-row">
          <label class="field-label" for="setup-jpeg-quality">JPEG quality</label>
          <input id="setup-jpeg-quality" class="text-input setup-number-input"
                 type="number" min="15" max="100" step="1"
                 value="${jpegQuality}" title="Applied by the next full scan"
                 aria-label="Album art JPEG quality" ${settingsDisabled}>
          <span class="setup-setting-unit">%</span>
        </div>
        <div class="setup-settings-actions">
          <button class="primary-button" type="button"
                  data-action="setup-save-settings" ${settingsDisabled}>
            Save Settings
          </button>
        </div>
        <div id="setup-settings-error" class="field-help setup-error"></div>
      </div>`;
    const authHtml = state.adminAuthenticated
      ? `<div class="setup-auth-row">
           <span class="setup-auth-state">Admin authenticated</span>
           <button class="secondary-button" type="button"
                   data-action="setup-change-password">Change Password</button>
           <button class="secondary-button" type="button" data-action="setup-logout">Log Out</button>
         </div>`
      : `<div class="field">
           <label class="field-label" for="setup-password">Enter admin password</label>
           <div class="setup-auth-row">
             <input id="setup-password" class="text-input" type="password"
                    autocomplete="current-password" spellcheck="false"
                    aria-label="Enter admin password">
             <button class="primary-button" type="button" data-action="setup-login">Log In</button>
           </div>
         </div>`;
    const errorHtml = state.setupError
      ? `<div class="field-help setup-error">${esc(state.setupError)}</div>` : "";
    const statsHtml = `
      <div class="setup-section setup-stats-section">
        <div id="setup-stats" class="setup-stats">${setupStatsHtml()}</div>
      </div>`;
    const adminHtml = `
      <div class="setup-section setup-admin-section">
        <div class="field-label">Admin</div>
        ${authHtml}
        ${errorHtml}
      </div>`;
    el.modalRoot.innerHTML = `
      <div class="sheet setup-sheet" role="dialog" aria-modal="true" aria-label="Setup">
        <div class="sheet-header">
          <img class="app-mark-small" src="/static/app-icon.svg" alt="">
          <div>
            <div class="sheet-title">Setup</div>
            <div class="sheet-subtitle">
              Manage library IDs, scan folders, and rebuild database identifiers.
            </div>
          </div>
          <button class="sheet-close" type="button" data-action="close-modal"
                  title="Close">×</button>
        </div>
        <div class="sheet-body">
          ${readOnly ? '<div class="field-help">This server is running in read-only mode. Scanning and ReKey are disabled.</div>' : ""}
          <div class="setup-section">
            <div class="field-label">Libraries</div>
            ${setupLibrariesHtml(enabled, state.adminAuthenticated)}
            <div class="setup-actions">
              <button class="secondary-button" type="button"
                      data-action="setup-quick-scan" ${disabled}>
                Quick Scan
              </button>
              <button class="secondary-button" type="button"
                      data-action="setup-full-scan" ${disabled}>
                Full Scan
              </button>
              <button class="secondary-button" type="button"
                      data-action="setup-rebuild-artwork" ${disabled}>
                Rebuild Artwork
              </button>
              <button class="danger-button" type="button"
                      data-action="setup-rekey" ${disabled}>
                ReKey Database
              </button>
            </div>
            ${setupStatusHtml()}
          </div>
          ${settingsHtml}
          ${statsHtml}
          ${adminHtml}
          <div class="sheet-actions">
            <button class="secondary-button" type="button" data-action="close-modal">Close</button>
          </div>
        </div>
      </div>`;
    if (!state.adminAuthenticated) {
      window.requestAnimationFrame(() => {
        const input = $("#setup-password", el.modalRoot);
        if (input) input.focus();
      });
    }
    const passwordInput = $("#setup-password", el.modalRoot);
    if (passwordInput) {
      passwordInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          loginAdmin();
        }
      });
    }
    refreshSetupStats();
  }

  function openLibraryIdDialog(libraryPath) {
    if (!state.adminAuthenticated) {
      toast("Log in from Setup to change the Library ID.", { error: true });
      return;
    }
    const libraries = (state.setupInfo && state.setupInfo.libraries) || [];
    const library = libraries.find((item) => item.path === libraryPath);
    if (!library) return;
    state.modalMode = "library-id";
    state.libraryIdTarget = {
      path: library.path,
      identifier: library.identifier || "",
      namespace: library.namespace || "",
      name: library.name || library.path,
    };
    el.modalRoot.innerHTML = `
      <div class="sheet setup-sheet" role="dialog" aria-modal="true" aria-label="Change Library ID">
        <div class="sheet-header">
          <img class="app-mark-small" src="/static/app-icon.svg" alt="">
          <div>
            <div class="sheet-title">Change Library ID</div>
            <div class="sheet-subtitle">${esc(library.path)}</div>
          </div>
          <button class="sheet-close" type="button" data-action="library-id-cancel"
                  title="Close">×</button>
        </div>
        <div class="sheet-body">
          <label class="field">
            <span class="field-label" for="library-id-input">Library ID</span>
            <input id="library-id-input" class="text-input" type="text"
                   autocomplete="off" spellcheck="false"
                   value="${esc(library.identifier || "")}"
                   aria-label="Library ID">
          </label>
          <div class="field-help">Current namespace: ${esc(library.namespace || "—")}</div>
          <div class="field-help">ReKey is required to apply the new namespace and song IDs.</div>
          <div id="library-id-error" class="field-help setup-error"></div>
          <div class="sheet-actions">
            <button class="secondary-button" type="button"
                    data-action="library-id-cancel">Cancel</button>
            <button class="primary-button" type="button"
                    data-action="library-id-save">Save</button>
          </div>
        </div>
      </div>`;
    el.modalRoot.classList.remove("hidden");
    const input = $("#library-id-input", el.modalRoot);
    if (input) {
      input.focus();
      input.select();
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          saveLibraryIdDialog();
        }
      });
    }
  }

  async function saveLibraryIdDialog() {
    const target = state.libraryIdTarget;
    const input = $("#library-id-input", el.modalRoot);
    const errorNode = $("#library-id-error", el.modalRoot);
    if (!target || !input) return;
    const identifier = input.value.trim();
    if (!identifier) {
      if (errorNode) errorNode.textContent = "Library ID must not be empty.";
      input.focus();
      return;
    }
    const button = $('[data-action="library-id-save"]', el.modalRoot);
    if (button) button.disabled = true;
    try {
      const data = await api("/api/setup/library", {
        method: "PATCH",
        headers: adminHeaders(),
        body: JSON.stringify({
          path: target.path,
          identifier,
        }),
      });
      closeModal();
      await openSetupModal();
      toast(data.rekeyRequired
        ? "Library ID updated. Run ReKey to apply the new namespace."
        : "Library ID updated");
    } catch (error) {
      if (button) button.disabled = false;
      if (error.status === 401) {
        setAdminToken("");
        state.adminAuthenticated = false;
        closeModal();
        await openSetupModal();
        state.setupError = "Session expired. Enter the admin password again.";
        renderSetup();
        return;
      }
      if (errorNode) errorNode.textContent = error.message;
    }
  }

  async function saveSetupSettings() {
    if (!state.adminAuthenticated) {
      state.setupError = "Log in to change settings.";
      renderSetup();
      return;
    }
    const urlSelect = $("#setup-url-format", el.modalRoot);
    const sortSelect = $("#setup-album-sort", el.modalRoot);
    const orderSelect = $("#setup-album-order", el.modalRoot);
    const coverInput = $("#setup-cover-size", el.modalRoot);
    const jpegInput = $("#setup-jpeg-quality", el.modalRoot);
    if (!urlSelect || !sortSelect || !orderSelect
        || !coverInput || !jpegInput) return;
    const button = $('[data-action="setup-save-settings"]', el.modalRoot);
    if (button) {
      button.disabled = true;
      button.textContent = "Saving…";
    }
    try {
      const data = await api("/api/setup/settings", {
        method: "PATCH",
        headers: adminHeaders(),
        body: JSON.stringify({
          urlIdFormat: urlSelect.value,
          albumSort: sortSelect.value,
          albumSortDescending: orderSelect.value === "desc",
          coverSize: Number(coverInput.value),
          jpegQuality: Number(jpegInput.value),
        }),
      });
      state.urlIdFormat = data.urlIdFormat;
      state.albumSort = data.albumSort;
      state.albumSortAsc = !data.albumSortDescending;
      state.albumPage = 1;
      if (state.setupInfo) {
        state.setupInfo.urlIdFormat = data.urlIdFormat;
        state.setupInfo.albumSort = data.albumSort;
        state.setupInfo.albumSortDescending = data.albumSortDescending;
        state.setupInfo.coverSize = data.coverSize;
        state.setupInfo.jpegQuality = data.jpegQuality;
      }
      if (button) {
        button.disabled = false;
        button.textContent = "Save Settings";
      }
      toast("Settings updated");
    } catch (error) {
      if (button) {
        button.disabled = false;
        button.textContent = "Save Settings";
      }
      if (error.status === 401) {
        setAdminToken("");
        state.adminAuthenticated = false;
        state.setupError = "Session expired. Enter the admin password again.";
        renderSetup();
        return;
      }
      const note = $("#setup-settings-error", el.modalRoot);
      if (note) note.textContent = error.message;
    }
  }

  function openChangePasswordDialog() {
    if (!state.adminAuthenticated) {
      state.setupError = "Log in to change the admin password.";
      renderSetup();
      return;
    }
    stopSetupStatsPoll();
    state.modalMode = "admin-password";
    el.modalRoot.innerHTML = `
      <div class="sheet setup-sheet" role="dialog" aria-modal="true" aria-label="Change Admin Password">
        <div class="sheet-header">
          <img class="app-mark-small" src="/static/app-icon.svg" alt="">
          <div>
            <div class="sheet-title">Change Admin Password</div>
            <div class="sheet-subtitle">All admin sessions will be signed out.</div>
          </div>
          <button class="sheet-close" type="button" data-action="admin-password-cancel"
                  title="Close">×</button>
        </div>
        <div class="sheet-body">
          <label class="field">
            <span class="field-label" for="setup-new-password">New password</span>
            <input id="setup-new-password" class="text-input" type="password"
                   autocomplete="new-password" spellcheck="false"
                   aria-label="New admin password">
          </label>
          <label class="field">
            <span class="field-label" for="setup-confirm-password">Confirm new password</span>
            <input id="setup-confirm-password" class="text-input" type="password"
                   autocomplete="new-password" spellcheck="false"
                   aria-label="Confirm new admin password">
          </label>
          <div id="admin-password-error" class="field-help setup-error"></div>
          <div class="sheet-actions">
            <button class="secondary-button" type="button"
                    data-action="admin-password-cancel">Cancel</button>
            <button class="primary-button" type="button"
                    data-action="admin-password-save">Change Password</button>
          </div>
        </div>
      </div>`;
    el.modalRoot.classList.remove("hidden");
    const input = $("#setup-new-password", el.modalRoot);
    if (input) {
      input.focus();
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          changeAdminPassword();
        }
      });
    }
  }

  async function changeAdminPassword() {
    if (!state.adminAuthenticated) {
      state.setupError = "Log in to change the admin password.";
      state.modalMode = "setup";
      renderSetup();
      return;
    }
    const newInput = $("#setup-new-password", el.modalRoot);
    const confirmInput = $("#setup-confirm-password", el.modalRoot);
    const note = $("#admin-password-error", el.modalRoot);
    const password = newInput ? newInput.value : "";
    if (!password) {
      if (note) note.textContent = "Enter a new password.";
      if (newInput) newInput.focus();
      return;
    }
    if (password !== (confirmInput ? confirmInput.value : "")) {
      if (note) note.textContent = "The passwords do not match.";
      if (confirmInput) confirmInput.focus();
      return;
    }
    if (note) note.textContent = "";
    const button = $('[data-action="admin-password-save"]', el.modalRoot);
    if (button) {
      button.disabled = true;
      button.textContent = "Changing…";
    }
    try {
      await api("/api/setup/password", {
        method: "POST",
        headers: adminHeaders(),
        body: JSON.stringify({ newPassword: password }),
      });
      setAdminToken("");
      state.adminAuthenticated = false;
      state.setupStats = null;
      state.statsError = "";
      state.modalMode = "setup";
      stopSetupStatsPoll();
      if (state.setupInfo) state.setupInfo.authenticated = false;
      state.setupError = "";
      renderSetup();
      toast("Admin password changed. Log in with the new password.");
    } catch (error) {
      if (button) {
        button.disabled = false;
        button.textContent = "Change Password";
      }
      if (error.status === 401) {
        setAdminToken("");
        state.adminAuthenticated = false;
        state.setupStats = null;
        state.modalMode = "setup";
        state.setupError = "Session expired. Enter the admin password again.";
        stopSetupStatsPoll();
        renderSetup();
        return;
      }
      if (note) note.textContent = error.message;
    }
  }

  async function loginAdmin() {
    const input = $("#setup-password", el.modalRoot);
    const password = input ? input.value : "";
    if (!password) {
      state.setupError = "Enter the admin password.";
      renderSetup();
      return;
    }
    const button = $('[data-action="setup-login"]', el.modalRoot);
    if (button) {
      button.disabled = true;
      button.textContent = "Checking…";
    }
    try {
      const data = await api("/api/setup/login", {
        method: "POST",
        body: JSON.stringify({ password }),
      });
      setAdminToken(data.token);
      const [info, stats] = await Promise.all([
        api("/api/setup", { headers: adminHeaders() }),
        api("/api/setup/stats", { headers: adminHeaders() })
          .catch(() => null),
      ]);
      if (stats) {
        state.setupStats = stats;
        state.statsError = "";
      }
      applySetupInfo(info);
      state.setupError = "";
      renderSetup();
      if (state.section === "playlists" && !state.albumDetail) renderContent();
    } catch (error) {
      setAdminToken("");
      state.adminAuthenticated = false;
      state.setupError = error.status === 401
        ? "Incorrect admin password" : error.message;
      renderSetup();
      if (state.section === "playlists" && !state.albumDetail) renderContent();
    }
  }

  async function logoutAdmin() {
    try {
      await api("/api/setup/logout", {
        method: "POST",
        headers: adminHeaders(),
      });
    } catch (_) {
      // The local token is cleared below even if the server is unreachable.
    }
    setAdminToken("");
    state.adminAuthenticated = false;
    state.setupError = "";
    state.setupStats = null;
    state.statsError = "";
    stopSetupStatsPoll();
    if (state.setupInfo) state.setupInfo.authenticated = false;
    renderSetup();
    if (state.section === "playlists" && !state.albumDetail) renderContent();
  }

  async function startSetupScan(mode) {
    if (!state.adminAuthenticated) {
      state.setupError = "Log in to run a scan.";
      renderSetup();
      return;
    }
    try {
      const data = await api("/api/setup/scan", {
        method: "POST",
        headers: adminHeaders(),
        body: JSON.stringify({ mode, force: mode === "full" }),
      });
      state.scanJob = data.job;
      state.scanRefreshed = false;
      renderScanProgress(data.job);
      refreshSystemStatus();
      scheduleScanPoll();
    } catch (error) {
      if (error.status === 401) {
        setAdminToken("");
        state.adminAuthenticated = false;
        state.setupError = "Session expired. Enter the admin password again.";
        renderSetup();
        return;
      }
      toast(error.message, { error: true });
    }
  }

  function promptSetupRekey() {
    if (!state.adminAuthenticated) {
      state.setupError = "Log in to run a ReKey.";
      renderSetup();
      return;
    }
    confirmDialog({
      title: "ReKey Database?",
      message: "Recalculate every song, album, and artist ID. "
        + "Playlists, stars, and play counts are preserved. "
        + "A database backup is created first.",
      confirmLabel: "ReKey",
      danger: true,
      onConfirm: () => startSetupRekey(),
    });
  }

  async function startSetupRekey() {
    state.modalMode = "setup";
    state.setupOpen = true;
    el.modalRoot.classList.remove("hidden");
    el.modalRoot.innerHTML = `
      <div class="sheet setup-sheet" role="dialog" aria-modal="true" aria-label="Setup">
        <div class="sheet-header">
          <img class="app-mark-small" src="/static/app-icon.svg" alt="">
          <div>
            <div class="sheet-title">ReKey Database</div>
            <div class="sheet-subtitle">Starting ReKey…</div>
          </div>
          <button class="sheet-close" type="button" data-action="close-modal"
                  title="Close">×</button>
        </div>
        <div class="sheet-body">
          <div class="center-state">
            ${spinnerHtml()}
            <div class="state-note">Starting ReKey…</div>
          </div>
        </div>
      </div>`;
    try {
      const data = await api("/api/setup/rekey", {
        method: "POST",
        headers: adminHeaders(),
      });
      state.rekeyJob = data.job;
      renderRekeyProgress(data.job);
      refreshSystemStatus();
      scheduleRekeyPoll();
    } catch (error) {
      if (error.status === 401) {
        setAdminToken("");
        state.adminAuthenticated = false;
        state.setupError = "Session expired. Enter the admin password again.";
        renderSetup();
        return;
      }
      toast(error.message, { error: true });
      openSetupModal();
    }
  }

  function renderScanProgress(job) {
    state.modalMode = "setup";
    state.setupOpen = true;
    el.modalRoot.classList.remove("hidden");
    const phase = job.phase || "Scanning";
    const completed = Number(job.completed) || 0;
    const total = Number(job.total) || 0;
    const running = Boolean(job.running);
    const percent = total > 0 ? clamp(completed / total, 0, 1) : 0;
    const errors = (job.errors || []).slice(0, 8);
    const summary = job.summary || null;
    const progressClass = total > 0 ? "" : "indeterminate";
    let actions;
    if (running) {
      actions = '<button class="secondary-button" type="button" data-action="close-modal">Hide</button>';
    } else if (job.phase === "Error") {
      actions = '<button class="secondary-button" type="button" data-action="setup-back">Back to Setup</button>'
        + '<button class="secondary-button" type="button" data-action="close-modal">Close</button>';
    } else {
      actions = '<button class="secondary-button" type="button" data-action="setup-back">Back to Setup</button>'
        + '<button class="primary-button" type="button" data-action="close-modal">Done</button>';
    }
    let summaryHtml = "";
    if (!running && summary) {
      summaryHtml = `
        <div class="field-help">
          ${Number(summary.tracks || 0).toLocaleString()} tracks scanned ·
          ${Number(summary.artwork || 0).toLocaleString()} covers written ·
          ${Number(summary.discovered || 0).toLocaleString()} files discovered
          ${summary.missing ? ` · ${Number(summary.missing).toLocaleString()} missing removed` : ""}
        </div>`;
    }
    el.modalRoot.innerHTML = `
      <div class="sheet setup-sheet" role="dialog" aria-modal="true" aria-label="Setup">
        <div class="sheet-header">
          <img class="app-mark-small" src="/static/app-icon.svg" alt="">
          <div>
            <div class="sheet-title">${running ? "Scanning Libraries" : job.phase === "Error" ? "Scan Failed" : "Scan Complete"}</div>
            <div class="sheet-subtitle">${running ? "Indexing the configured libraries and generating artwork." : "The database and artwork cache are up to date."}</div>
          </div>
          <button class="sheet-close" type="button" data-action="close-modal"
                  title="Close">×</button>
        </div>
        <div class="sheet-body">
          <div class="scan-progress">
            <div class="scan-progress-head">
              ${running ? spinnerHtml("compact") : ""}
              <span>${esc(phase)}</span>
              <span class="tabular" style="margin-left:auto;color:var(--text-2)">
                ${total > 0 ? `${completed.toLocaleString()} / ${total.toLocaleString()}` : ""}
              </span>
            </div>
            <div class="progress-track">
              <div class="progress-fill ${progressClass}" style="${total > 0 ? `width:${(percent * 100).toFixed(1)}%` : ""}"></div>
            </div>
            <div class="scan-progress-detail">${esc(job.message || "")}</div>
            ${summaryHtml}
          </div>
          ${errors.length ? `<ul class="scan-error-list">${errors.map((error) => `<li>${esc(error)}</li>`).join("")}</ul>` : ""}
          <div class="sheet-actions">${actions}</div>
        </div>
      </div>`;
  }

  function scheduleScanPoll() {
    if (state.scanTimer) clearTimeout(state.scanTimer);
    state.scanTimer = setTimeout(pollScan, 650);
  }

  async function pollScan() {
    state.scanTimer = null;
    try {
      const job = await api("/api/scan", {
        headers: adminHeaders(),
      });
      state.scanJob = job;
      if (state.setupOpen && state.modalMode === "setup") {
        renderScanProgress(job);
      }
      refreshSystemStatus();
      if (job.running) {
        scheduleScanPoll();
        return;
      }
      if (!state.scanRefreshed && job.phase !== "Error") {
        state.scanRefreshed = true;
        await refreshAfterScan();
        if (state.setupOpen && state.modalMode === "setup") {
          renderScanProgress(job);
        }
      }
    } catch (error) {
      toast(`Scan status unavailable: ${error.message}`, { error: true });
    }
  }

  function renderRekeyProgress(job) {
    state.modalMode = "setup";
    state.setupOpen = true;
    el.modalRoot.classList.remove("hidden");
    const running = Boolean(job.running);
    const phase = job.phase || "Rekeying";
    const summary = job.summary || null;
    const errors = (job.errors || []).slice(0, 8);
    let actions;
    if (running) {
      actions = '<button class="secondary-button" type="button" data-action="close-modal">Hide</button>';
    } else if (job.phase === "Error") {
      actions = '<button class="secondary-button" type="button" data-action="setup-back">Back to Setup</button>'
        + '<button class="secondary-button" type="button" data-action="close-modal">Close</button>';
    } else {
      actions = '<button class="secondary-button" type="button" data-action="setup-back">Back to Setup</button>'
        + '<button class="primary-button" type="button" data-action="rekey-reload">Reload Player</button>';
    }
    let summaryHtml = "";
    if (!running && summary) {
      summaryHtml = `
        <div class="field-help">
          ${Number(summary.songs || 0).toLocaleString()} songs ·
          ${Number(summary.albums || 0).toLocaleString()} albums ·
          ${Number(summary.artists || 0).toLocaleString()} artists ·
          ${Number(summary.artwork || 0).toLocaleString()} artwork files moved
          ${summary.backup ? ` · backup: ${esc(summary.backup)}` : ""}
        </div>`;
    }
    el.modalRoot.innerHTML = `
      <div class="sheet setup-sheet" role="dialog" aria-modal="true" aria-label="Setup">
        <div class="sheet-header">
          <img class="app-mark-small" src="/static/app-icon.svg" alt="">
          <div>
            <div class="sheet-title">${running ? "ReKeying Database" : job.phase === "Error" ? "ReKey Failed" : "ReKey Complete"}</div>
            <div class="sheet-subtitle">${running
              ? "Recalculating stable IDs and moving artwork."
              : "Playlists, stars, and play counts were preserved."}</div>
          </div>
          <button class="sheet-close" type="button" data-action="close-modal"
                  title="Close">×</button>
        </div>
        <div class="sheet-body">
          <div class="scan-progress">
            <div class="scan-progress-head">
              ${running ? spinnerHtml("compact") : ""}
              <span>${esc(phase)}</span>
            </div>
            <div class="progress-track">
              <div class="progress-fill ${running ? "indeterminate" : ""}"></div>
            </div>
            <div class="scan-progress-detail">${esc(job.message || "")}</div>
            ${summaryHtml}
          </div>
          ${errors.length ? `<ul class="scan-error-list">${errors.map((error) => `<li>${esc(error)}</li>`).join("")}</ul>` : ""}
          <div class="field-help">Reload the player after ReKey so the updated song IDs are loaded.</div>
          <div class="sheet-actions">${actions}</div>
        </div>
      </div>`;
  }

  function scheduleRekeyPoll() {
    if (state.rekeyTimer) clearTimeout(state.rekeyTimer);
    state.rekeyTimer = setTimeout(pollRekey, 650);
  }

  async function pollRekey() {
    state.rekeyTimer = null;
    if (!state.setupOpen) return;
    try {
      const job = await api("/api/setup/rekey", {
        headers: adminHeaders(),
      });
      state.rekeyJob = job;
      if (state.setupOpen && state.modalMode === "setup") {
        renderRekeyProgress(job);
      }
      refreshSystemStatus();
      if (job.running) scheduleRekeyPoll();
    } catch (error) {
      toast(`ReKey status unavailable: ${error.message}`, { error: true });
    }
  }

  function reloadAfterRekey() {
    for (const key of [PLAYER_KEY, PLAYER_STATE_KEY, PLAYER_QUEUE_KEY]) {
      try { localStorage.removeItem(key); } catch (_) {}
    }
    window.location.reload();
  }

  async function startSetupArtwork() {
    if (!state.adminAuthenticated) {
      state.setupError = "Log in to rebuild artwork.";
      renderSetup();
      return;
    }
    state.modalMode = "setup";
    state.setupOpen = true;
    el.modalRoot.classList.remove("hidden");
    el.modalRoot.innerHTML = `
      <div class="sheet setup-sheet" role="dialog" aria-modal="true" aria-label="Setup">
        <div class="sheet-header">
          <img class="app-mark-small" src="/static/app-icon.svg" alt="">
          <div>
            <div class="sheet-title">Rebuild Artwork</div>
            <div class="sheet-subtitle">Starting artwork rebuild…</div>
          </div>
          <button class="sheet-close" type="button" data-action="close-modal"
                  title="Close">×</button>
        </div>
        <div class="sheet-body">
          <div class="center-state">
            ${spinnerHtml()}
            <div class="state-note">Preparing cover cache…</div>
          </div>
        </div>
      </div>`;
    try {
      const data = await api("/api/setup/artwork", {
        method: "POST",
        headers: adminHeaders(),
      });
      state.artworkJob = data.job;
      renderArtworkProgress(data.job);
      refreshSystemStatus();
      scheduleArtworkPoll();
    } catch (error) {
      if (error.status === 401) {
        setAdminToken("");
        state.adminAuthenticated = false;
        state.setupError = "Session expired. Enter the admin password again.";
        renderSetup();
        return;
      }
      toast(error.message, { error: true });
      openSetupModal();
    }
  }

  function renderArtworkProgress(job) {
    state.modalMode = "setup";
    state.setupOpen = true;
    el.modalRoot.classList.remove("hidden");
    const running = Boolean(job.running);
    const phase = job.phase || "Rebuilding Artwork";
    const completed = Number(job.completed) || 0;
    const total = Number(job.total) || 0;
    const percent = total > 0 ? clamp(completed / total, 0, 1) : 0;
    const errors = (job.errors || []).slice(0, 8);
    const summary = job.summary || null;
    const progressClass = total > 0 ? "" : "indeterminate";
    let actions;
    if (running) {
      actions = '<button class="secondary-button" type="button" data-action="close-modal">Hide</button>';
    } else if (job.phase === "Error") {
      actions = '<button class="secondary-button" type="button" data-action="setup-back">Back to Setup</button>'
        + '<button class="secondary-button" type="button" data-action="close-modal">Close</button>';
    } else {
      actions = '<button class="secondary-button" type="button" data-action="setup-back">Back to Setup</button>'
        + '<button class="primary-button" type="button" data-action="artwork-reload">Reload Player</button>';
    }
    let summaryHtml = "";
    if (!running && summary) {
      summaryHtml = `
        <div class="field-help">
          ${Number(summary.artwork || 0).toLocaleString()} covers written ·
          ${Number(summary.missing || 0).toLocaleString()} albums without art ·
          ${Number(summary.albums || 0).toLocaleString()} albums checked
          ${summary.orphansRemoved ? ` · ${Number(summary.orphansRemoved).toLocaleString()} stale covers removed` : ""}
        </div>`;
    }
    el.modalRoot.innerHTML = `
      <div class="sheet setup-sheet" role="dialog" aria-modal="true" aria-label="Setup">
        <div class="sheet-header">
          <img class="app-mark-small" src="/static/app-icon.svg" alt="">
          <div>
            <div class="sheet-title">${running ? "Rebuilding Artwork" : job.phase === "Error" ? "Artwork Rebuild Failed" : "Artwork Rebuilt"}</div>
            <div class="sheet-subtitle">${running
              ? "Re-extracting covers using the configured size and quality."
              : "Reload the player to fetch the new cached covers."}</div>
          </div>
          <button class="sheet-close" type="button" data-action="close-modal"
                  title="Close">×</button>
        </div>
        <div class="sheet-body">
          <div class="scan-progress">
            <div class="scan-progress-head">
              ${running ? spinnerHtml("compact") : ""}
              <span>${esc(phase)}</span>
              <span class="tabular" style="margin-left:auto;color:var(--text-2)">
                ${total > 0 ? `${completed.toLocaleString()} / ${total.toLocaleString()}` : ""}
              </span>
            </div>
            <div class="progress-track">
              <div class="progress-fill ${progressClass}" style="${total > 0 ? `width:${(percent * 100).toFixed(1)}%` : ""}"></div>
            </div>
            <div class="scan-progress-detail">${esc(job.message || "")}</div>
            ${summaryHtml}
          </div>
          ${errors.length ? `<ul class="scan-error-list">${errors.map((error) => `<li>${esc(error)}</li>`).join("")}</ul>` : ""}
          <div class="sheet-actions">${actions}</div>
        </div>
      </div>`;
  }

  function scheduleArtworkPoll() {
    if (state.artworkTimer) clearTimeout(state.artworkTimer);
    state.artworkTimer = setTimeout(pollArtwork, 650);
  }

  async function pollArtwork() {
    state.artworkTimer = null;
    if (!state.setupOpen) return;
    try {
      const job = await api("/api/setup/artwork", {
        headers: adminHeaders(),
      });
      state.artworkJob = job;
      if (state.setupOpen && state.modalMode === "setup") {
        renderArtworkProgress(job);
      }
      refreshSystemStatus();
      if (job.running) scheduleArtworkPoll();
    } catch (error) {
      toast(`Artwork rebuild status unavailable: ${error.message}`, {
        error: true,
      });
    }
  }

  // ---------------------------------------------------------------------
  // Content actions

  async function handleContentClick(event) {
    const actionEl = event.target.closest("[data-action]");
    if (!actionEl || !el.content.contains(actionEl)) return;
    const action = actionEl.dataset.action;
    switch (action) {
      case "open-setup":
        openSetupModal();
        break;
      case "new-playlist":
        openPlaylistDialog("create", null);
        break;
      case "select-starred":
        selectPlaylistView(null);
        break;
      case "select-playlist":
        selectPlaylistView(actionEl.dataset.playlistId);
        break;
      case "playlist-view-play":
        if (state.songRows.length) playCollection(state.songRows, 0);
        break;
      case "playlist-view-shuffle":
        if (state.songRows.length) playShuffled(state.songRows);
        break;
      case "song-view-play":
        if (state.songRows.length) playCollection(state.songRows, 0);
        break;
      case "song-view-shuffle":
        if (state.songRows.length) playShuffled(state.songRows);
        break;
      case "open-starred":
        setSection("starred");
        break;
      case "open-playlist":
        setSection("playlist", actionEl.dataset.playlistId);
        break;
      case "reload-app":
        window.location.reload();
        break;
      case "album-card":
        handleAlbumCardInteraction(actionEl.dataset.albumId, event);
        break;
      case "play-album": {
        event.stopPropagation();
        const albumId = actionEl.dataset.albumId;
        try {
          await playCollection(await getAlbumSongs(albumId), 0);
        } catch (error) {
          toast(error.message, { error: true });
        }
        break;
      }
      case "album-sort-menu":
        event.stopPropagation();
        openAlbumSortMenu(actionEl);
        break;
      case "album-page-first":
        goToAlbumPage(1);
        break;
      case "album-page-prev":
        goToAlbumPage(state.albumPage - 1);
        break;
      case "album-page-next":
        goToAlbumPage(state.albumPage + 1);
        break;
      case "album-page-last": {
        goToAlbumPage(albumPageTotal(currentSectionAlbumList().length));
        break;
      }
      case "close-album":
        closeAlbumDetail();
        break;
      case "open-artist": {
        const name = actionEl.dataset.artist || "";
        if (!name) break;
        state.selectedArtist = name;
        state.albumDetail = null;
        setSection("artists");
        break;
      }
      case "play-detail-album":
        if (state.albumDetail && state.albumDetail.songs) {
          playCollection(state.albumDetail.songs, 0);
        }
        break;
      case "shuffle-detail-album":
        if (state.albumDetail && state.albumDetail.songs) {
          playShuffled(state.albumDetail.songs);
        }
        break;
      case "song-sort": {
        const key = actionEl.dataset.sort;
        const sort = songSortKeyFor(state.songContext);
        if (sort.key === key) sort.asc = !sort.asc;
        else {
          sort.key = key;
          sort.asc = true;
        }
        saveSongSort(state.songContext, sort);
        renderContent();
        updateStatus();
        break;
      }
      case "song-row":
      case "track-row":
      case "browser-song-row":
        handleSongRowClick(actionEl.dataset.songId, event);
        break;
      case "toggle-star":
        event.stopPropagation();
        await toggleStar(actionEl.dataset.starId);
        break;
      case "select-artist":
        selectBrowserGroup("artists", actionEl.dataset.name);
        break;
      case "select-genre":
        selectBrowserGroup("genres", actionEl.dataset.name);
        break;
      case "browser-play-all":
        playAllBrowserGroup();
        break;
      case "browser-shuffle-all":
        playBrowserGroupShuffled();
        break;
      case "browser-play-album": {
        const albumId = actionEl.dataset.albumId;
        state.browserFocus = "pane";
        state.browserAlbumAnchor = albumId;
        updateBrowserAlbumSelection();
        const songs = state.songsByAlbum.get(albumId) || [];
        if (songs.length) playCollection(songs, 0);
        break;
      }
      default:
        break;
    }
  }

  function handleContentDoubleClick(event) {
    const browserRow = event.target.closest(
      ".browser-row[data-action='select-artist'], .browser-row[data-action='select-genre']"
    );
    if (browserRow) {
      event.preventDefault();
      selectBrowserGroup(
        browserRow.dataset.action === "select-artist" ? "artists" : "genres",
        browserRow.dataset.name
      );
      playAllBrowserGroup();
      return;
    }
    if (event.target.closest("button")) return;
    const row = event.target.closest("[data-song-id]");
    if (!row) return;
    const songId = row.dataset.songId;
    if (!songId) return;
    event.preventDefault();
    if (row.classList.contains("song-row")) {
      const index = state.songRows.findIndex((song) => song.id === songId);
      if (index >= 0) playCollection(state.songRows, index);
      return;
    }
    if (row.classList.contains("track-row")) {
      const songs = (state.albumDetail && state.albumDetail.songs) || state.songRows;
      const index = songs.findIndex((song) => song.id === songId);
      if (index >= 0) playCollection(songs, index);
      return;
    }
    if (row.classList.contains("browser-song-row")) {
      const section = row.closest("[data-browser-album-id]");
      const albumId = section && section.dataset.browserAlbumId;
      const songs = albumId ? (state.songsByAlbum.get(albumId) || []) : state.songRows;
      const index = songs.findIndex((song) => song.id === songId);
      if (index >= 0) playCollection(songs, index);
    }
  }

  function handleContentContextMenu(event) {
    const playlistRow = event.target.closest(".playlist-row[data-playlist-id]");
    if (playlistRow) {
      const playlist = state.playlists.find(
        (item) => item.id === playlistRow.dataset.playlistId
      );
      if (playlist) contextMenuForPlaylist(playlist, event);
      return;
    }
    const albumCard = event.target.closest(".album-card");
    if (albumCard) {
      const album = state.albumMap.get(albumCard.dataset.albumId);
      if (album) contextMenuForAlbum(album, event);
      return;
    }
    const row = event.target.closest("[data-song-id]");
    if (row) {
      const song = getSongById(row.dataset.songId);
      if (song) contextMenuForSong(song, row, event);
    }
  }

  // ---------------------------------------------------------------------
  // Drag and drop to playlists

  function dragPayloadSongs(event) {
    const row = event.target.closest("[data-song-id]");
    if (!row || event.target.closest("button")) return false;
    const songId = row.dataset.songId;
    let ids = [songId];
    if (state.selectedSongs.has(songId) && state.selectedSongs.size > 1) {
      ids = state.songRows
        .filter((song) => state.selectedSongs.has(song.id))
        .map((song) => song.id);
    }
    event.dataTransfer.effectAllowed = "copy";
    event.dataTransfer.setData("application/x-tracks-songs", JSON.stringify(ids));
    event.dataTransfer.setData("text/plain", ids.length > 1
      ? `${ids.length} songs` : (getSongById(songId) || {}).title || "");
    return true;
  }

  function handleContentDragStart(event) {
    const albumArt = event.target.closest(".album-art-wrap");
    if (albumArt) {
      const card = albumArt.closest(".album-card");
      if (card && card.dataset.albumId) {
        event.dataTransfer.effectAllowed = "copy";
        event.dataTransfer.setData(
          "application/x-tracks-album", card.dataset.albumId
        );
        event.dataTransfer.setData("text/plain", "Album");
      }
      return;
    }
    dragPayloadSongs(event);
  }

  // ---------------------------------------------------------------------
  // Audio element and progress controls

  function setAudioBuffering(value) {
    const active = Boolean(value && currentSong());
    if (player.buffering === active) return;
    player.buffering = active;
    if (el.lcdLoading) {
      el.lcdLoading.classList.toggle("hidden", !active);
    }
  }

  function handleLoadedMetadata() {
    const song = currentSong();
    if (!song) return;
    player.loadedSongId = song.id;
    if (Number.isFinite(audio.duration) && audio.duration > 0 && !song.duration) {
      patchSong(song.id, { duration: audio.duration });
    }
    if (player.pendingSeek != null) {
      const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
      const position = duration > 0
        ? clamp(player.pendingSeek, 0, duration) : player.pendingSeek;
      try {
        audio.currentTime = position;
      } catch (_) {
        // Ignore formats that cannot seek yet.
      }
      player.elapsed = position;
      player.pendingSeek = null;
    }
    updateProgressUI();
    if (player.pendingPlay) {
      player.pendingPlay = false;
      const promise = audio.play();
      if (promise && promise.catch) {
        promise.catch((error) => {
          player.playing = false;
          renderPlayerUI();
          toast(`Could not start playback: ${error.message}`, { error: true });
        });
      }
    }
  }

  function handleAudioPlaying() {
    setAudioBuffering(false);
    player.playing = true;
    const song = currentSong();
    if (song && player.pendingRecord === song.id) {
      player.pendingRecord = false;
      recordPlaybackStart(song);
    }
    renderPlayerUI();
    refreshCurrentIndicators();
    updateStatus();
    savePlayerState();
  }

  function handleAudioTimeUpdate() {
    if (state.seekPreview != null) return;
    if (Number.isFinite(audio.currentTime)) {
      player.elapsed = audio.currentTime;
    }
    updateProgressUI();
    if (player.playing && Date.now() - state.lastPersistAt > 5000) {
      state.lastPersistAt = Date.now();
      savePlayerState();
    }
  }

  function handleAudioEnded() {
    setAudioBuffering(false);
    if (player.repeat === "one") {
      player.elapsed = 0;
      player.pendingRecord = false;
      try {
        audio.currentTime = 0;
      } catch (_) {
        // Ignore.
      }
      const promise = audio.play();
      if (promise && promise.catch) {
        promise.catch(() => { player.playing = false; renderPlayerUI(); });
      }
      updateProgressUI();
      return;
    }
    nextTrack();
  }

  async function handleAudioError() {
    if (!audio.src) return;
    const song = currentSong();
    const source = audio.src;
    setAudioBuffering(false);
    player.playing = false;
    player.pendingPlay = false;
    player.pendingRecord = false;
    player.loadedSongId = null;
    let limited = false;
    try {
      const info = await api("/api/rate-limit");
      limited = Boolean(info && info.limited);
    } catch (_) {
      // If the probe fails, keep the normal playback error path.
    }
    if (audio.src !== source) return;
    if (song && !limited) player.missing.add(song.id);
    renderPlayerUI();
    refreshCurrentIndicators();
    updateStatus();
    savePlayerState();
    if (!limited) {
      toast("This track could not be played. The file may be missing or unsupported.", {
        error: true,
      });
    }
  }

  // ---------------------------------------------------------------------
  // Keyboard and global shortcuts

  function selectedSongId() {
    if (state.songAnchor && state.songRows.some((song) => song.id === state.songAnchor)) {
      return state.songAnchor;
    }
    const first = state.selectedSongs.values().next().value;
    if (first) return first;
    return null;
  }

  function selectSongByIndex(index) {
    const list = state.songRows || [];
    if (!list.length) return;
    const safe = clamp(index, 0, list.length - 1);
    const song = list[safe];
    state.selectedSongs = new Set([song.id]);
    state.songAnchor = song.id;
    if (state.songVirtual) {
      state.songVirtual.scrollToIndex(safe);
    }
    refreshSelectionClasses();
    const row = document.querySelector(`[data-song-id="${song.id}"]`);
    if (row) row.scrollIntoView({ block: "nearest" });
    updateStatus();
  }

  function moveSongSelection(delta) {
    const list = state.songRows || [];
    if (!list.length) return;
    const currentId = selectedSongId();
    let index = currentId
      ? list.findIndex((song) => song.id === currentId) : -1;
    if (index < 0) index = delta > 0 ? 0 : list.length - 1;
    else index = clamp(index + delta, 0, list.length - 1);
    selectSongByIndex(index);
  }

  function songPageSize() {
    const scroller = $("#song-scroll", el.content);
    return Math.max(
      1, Math.floor((scroller ? scroller.clientHeight : 400) / 26)
    );
  }

  function moveAlbumSelection(horizontalDelta, verticalDelta) {
    const list = currentSectionAlbumList();
    if (!list.length) return;
    const currentId = state.albumAnchor
      || state.selectedAlbums.values().next().value;
    let index = currentId
      ? list.findIndex((album) => album.id === currentId) : -1;
    if (index < 0) index = (horizontalDelta + verticalDelta) > 0 ? 0 : list.length - 1;
    else {
      let delta = horizontalDelta;
      if (verticalDelta) {
        const grid = $(".album-grid", el.content);
        let columns = 1;
        if (grid) {
          const template = getComputedStyle(grid).gridTemplateColumns;
          columns = template ? template.split(" ").filter(Boolean).length : 1;
        }
        delta = verticalDelta * Math.max(columns, 1);
      }
      index = clamp(index + delta, 0, list.length - 1);
    }
    const album = list[index];
    state.selectedAlbums = new Set([album.id]);
    state.albumAnchor = album.id;
    if (state.albumVirtual) state.albumVirtual.scrollToIndex(index);
    updateAlbumCardSelection();
    const card = document.querySelector(`.album-card[data-album-id="${album.id}"]`);
    if (card) card.scrollIntoView({ block: "nearest" });
    updateStatus();
  }

  function moveBrowserSelection(delta) {
    const groups = state.browserGroups || [];
    if (!groups.length) return;
    const selectedName = browserGroupSelectedName(state.browserKind);
    let index = groups.findIndex((group) => group.name === selectedName);
    if (index < 0) index = delta > 0 ? 0 : groups.length - 1;
    else index = clamp(index + delta, 0, groups.length - 1);
    selectBrowserGroupAt(index);
  }

  function selectBrowserGroupAt(index) {
    const groups = state.browserGroups || [];
    if (!groups.length) return;
    const safe = clamp(index, 0, groups.length - 1);
    const group = groups[safe];
    selectBrowserGroup(state.browserKind, group.name);
    const row = Array.from(document.querySelectorAll(".browser-row"))
      .find((item) => item.dataset.name === group.name);
    if (row) row.scrollIntoView({ block: "nearest" });
  }

  function currentBrowserGroup() {
    const groups = state.browserGroups || [];
    const selectedName = browserGroupSelectedName(state.browserKind);
    return groups.find((group) => group.name === selectedName)
      || groups[0] || null;
  }

  function browserAlbumList() {
    const group = currentBrowserGroup();
    return group ? sortedAlbums(group.albums) : [];
  }

  function updateBrowserAlbumSelection() {
    $$(".browser-album", el.content).forEach((node) => {
      node.classList.toggle(
        "selected",
        node.dataset.browserAlbumId === state.browserAlbumAnchor
      );
    });
  }

  function selectBrowserAlbum(albumId) {
    state.browserAlbumAnchor = albumId;
    updateBrowserAlbumSelection();
    const node = Array.from(
      document.querySelectorAll(".browser-album")
    ).find((item) => item.dataset.browserAlbumId === albumId);
    if (node) node.scrollIntoView({ block: "nearest" });
  }

  function selectBrowserAlbumAt(index) {
    const albums = browserAlbumList();
    if (!albums.length) return;
    selectBrowserAlbum(albums[clamp(index, 0, albums.length - 1)].id);
  }

  function moveBrowserAlbumSelection(delta) {
    const albums = browserAlbumList();
    if (!albums.length) return;
    let index = albums.findIndex(
      (album) => album.id === state.browserAlbumAnchor
    );
    if (index < 0) index = delta > 0 ? 0 : albums.length - 1;
    else index = clamp(index + delta, 0, albums.length - 1);
    selectBrowserAlbumAt(index);
  }

  function playSelectedBrowserAlbum() {
    const songs = state.browserAlbumAnchor
      ? (state.songsByAlbum.get(state.browserAlbumAnchor) || []) : [];
    if (songs.length) playCollection(songs, 0);
  }

  function browserListPageSize() {
    const scroller = $("#browser-list-scroll", el.content);
    return Math.max(
      1, Math.floor((scroller ? scroller.clientHeight : 400) / 26)
    );
  }

  function browserAlbumPageSize() {
    const pane = $("#browser-pane-scroll", el.content);
    return Math.max(
      1, Math.floor((pane ? pane.clientHeight : 400) / 140)
    );
  }

  function playSelectedSongs() {
    const list = state.songRows || [];
    if (!list.length) return;
    const id = selectedSongId();
    let index = id ? list.findIndex((song) => song.id === id) : 0;
    if (index < 0) index = 0;
    playCollection(list, index);
  }

  function handleGlobalKeydown(event) {
    const target = event.target;
    const typing = target && (
      target.tagName === "INPUT"
      || target.tagName === "TEXTAREA"
      || target.tagName === "SELECT"
      || target.isContentEditable
    );
    if (event.key === "Escape") {
      if (state.modalMode) {
        closeModal();
        event.preventDefault();
        return;
      }
      if (!el.menuRoot.classList.contains("hidden")) {
        closeMenu();
        event.preventDefault();
        return;
      }
      if (state.queueOpen) {
        toggleQueue(false);
        event.preventDefault();
        return;
      }
      if (typing) return;
      if (state.albumDetail) {
        closeAlbumDetail();
        event.preventDefault();
        return;
      }
      if (state.query) {
        clearSearch();
        event.preventDefault();
        return;
      }
      if (state.selectedSongs.size || state.selectedAlbums.size) {
        state.selectedSongs.clear();
        state.selectedAlbums.clear();
        state.songAnchor = null;
        state.albumAnchor = null;
        if (state.songVirtual) state.songVirtual.rerender();
        updateAlbumCardSelection();
        refreshSelectionClasses();
        updateStatus();
      }
      return;
    }

    const command = event.metaKey || event.ctrlKey;
    if (command && event.key.toLowerCase() === "f") {
      event.preventDefault();
      el.searchInput.focus();
      el.searchInput.select();
      return;
    }
    if (state.modalMode) return;
    if (typing) return;
    if (command || event.altKey) return;

    if (state.albumDetail && event.key === "Backspace") {
      event.preventDefault();
      closeAlbumDetail();
      return;
    }

    if (event.code === "Space" || event.key === " ") {
      event.preventDefault();
      if (!event.repeat) togglePlayPause();
      return;
    }
    if (["1", "2", "3", "4"].includes(event.key)) {
      const section = SECTIONS[Number(event.key) - 1];
      if (section) {
        event.preventDefault();
        setSection(section.id);
      }
      return;
    }
    if (event.key === "8") {
      event.preventDefault();
      setSection("playlists");
      return;
    }

    if (state.albumDetail && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
      event.preventDefault();
      moveSongSelection(event.key === "ArrowUp" ? -1 : 1);
      return;
    }
    if (state.albumDetail && event.key === "Enter") {
      event.preventDefault();
      playSelectedSongs();
      return;
    }
    if (state.section === "songs" || state.section === "starred"
        || ((state.section === "playlist" || state.section === "playlists")
            && !state.albumDetail)) {
      if (event.key === "ArrowUp" || event.key === "ArrowDown") {
        event.preventDefault();
        moveSongSelection(event.key === "ArrowUp" ? -1 : 1);
        return;
      }
      if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        selectSongByIndex(event.key === "Home" ? 0 : state.songRows.length - 1);
        return;
      }
      if (event.key === "PageUp" || event.key === "PageDown") {
        event.preventDefault();
        moveSongSelection(
          (event.key === "PageDown" ? 1 : -1) * songPageSize()
        );
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        playSelectedSongs();
        return;
      }
    }
    if (state.section === "albums" && !state.albumDetail) {
      if (event.key === "PageUp" || event.key === "PageDown") {
        event.preventDefault();
        goToAlbumPage(state.albumPage + (event.key === "PageDown" ? 1 : -1));
        return;
      }
      if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        goToAlbumPage(
          event.key === "Home"
            ? 1 : albumPageTotal(currentSectionAlbumList().length)
        );
        return;
      }
      if (event.key === "ArrowLeft" || event.key === "ArrowRight"
          || event.key === "ArrowUp" || event.key === "ArrowDown") {
        event.preventDefault();
        const horizontal = event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
        const vertical = event.key === "ArrowUp" ? -1 : event.key === "ArrowDown" ? 1 : 0;
        moveAlbumSelection(horizontal, vertical);
        return;
      }
      if (event.key === "Enter" && state.albumAnchor) {
        event.preventDefault();
        openAlbum(state.albumAnchor);
        return;
      }
    }
    if (state.section === "artists" || state.section === "genres") {
      const paneFocused = state.browserFocus === "pane";
      if (event.key === "ArrowRight" && !paneFocused) {
        event.preventDefault();
        state.browserFocus = "pane";
        const albums = browserAlbumList();
        if (state.browserAlbumAnchor) {
          selectBrowserAlbum(state.browserAlbumAnchor);
        } else if (albums.length) {
          selectBrowserAlbum(albums[0].id);
        }
        return;
      }
      if (event.key === "ArrowLeft" && paneFocused) {
        event.preventDefault();
        state.browserFocus = "list";
        const row = document.querySelector(".browser-row.selected");
        if (row) row.scrollIntoView({ block: "nearest" });
        return;
      }
      if (event.key === "ArrowUp" || event.key === "ArrowDown") {
        event.preventDefault();
        const delta = event.key === "ArrowUp" ? -1 : 1;
        if (paneFocused) moveBrowserAlbumSelection(delta);
        else moveBrowserSelection(delta);
        return;
      }
      if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        if (paneFocused) {
          selectBrowserAlbumAt(
            event.key === "Home" ? 0 : browserAlbumList().length - 1
          );
        } else {
          selectBrowserGroupAt(
            event.key === "Home"
              ? 0 : (state.browserGroups || []).length - 1
          );
        }
        return;
      }
      if (event.key === "PageUp" || event.key === "PageDown") {
        event.preventDefault();
        const direction = event.key === "PageDown" ? 1 : -1;
        if (paneFocused) {
          moveBrowserAlbumSelection(direction * browserAlbumPageSize());
        } else {
          moveBrowserSelection(direction * browserListPageSize());
        }
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        if (paneFocused) playSelectedBrowserAlbum();
        else playAllBrowserGroup();
      }
    }
  }

  function clearSearch() {
    state.query = "";
    el.searchInput.value = "";
    updateSearchClear();
    state.selectedSongs.clear();
    state.songAnchor = null;
    state.selectedAlbums.clear();
    state.albumAnchor = null;
    if (!state.albumDetail) {
      renderContent();
      updateStatus();
    }
  }

  function updateSearchClear() {
    el.searchClear.classList.toggle("hidden", !el.searchInput.value);
  }

  // ---------------------------------------------------------------------
  // Event wiring

  function bindEvents() {
    el.prevButton.addEventListener("click", previousTrack);
    el.playButton.addEventListener("click", togglePlayPause);
    el.nextButton.addEventListener("click", nextTrack);
    el.qualityButton.addEventListener("click", (event) => {
      event.stopPropagation();
      openQualityMenu(el.qualityButton);
    });

    el.lcdShuffle.addEventListener("click", toggleShuffle);
    el.lcdRepeat.addEventListener("click", cycleRepeat);
    el.lcdStar.addEventListener("click", () => {
      const song = currentSong();
      if (song) toggleStar(song.id);
    });
    el.lcdDuration.addEventListener("click", () => {
      state.showRemaining = !state.showRemaining;
      updateProgressUI();
      savePlayerState();
    });
    el.lcdTitleButton.addEventListener("click", () => {
      const song = currentSong();
      if (song && song.albumId) openAlbum(song.albumId);
    });

    bindSeekbar();

    el.queueButton.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleQueue();
    });
    el.queueList.addEventListener("scroll", renderQueueWindow, { passive: true });
    el.queuePopover.addEventListener("click", (event) => {
      const tab = event.target.closest("[data-queue-tab]");
      if (tab) {
        state.queueTab = tab.dataset.queueTab;
        renderQueuePopover();
        return;
      }
      if (event.target.closest("#queue-clear")) {
        clearUpNext();
        return;
      }
      const queueRow = event.target.closest('[data-action="queue-play"]');
      if (queueRow) {
        playQueueIndex(Number(queueRow.dataset.index));
        return;
      }
      const historyRow = event.target.closest('[data-action="history-play"]');
      if (historyRow) {
        const song = getSongById(historyRow.dataset.songId);
        if (song) playFromHistory(song);
      }
    });

    const applySearch = debounce(() => {
      state.query = el.searchInput.value.trim().toLowerCase();
      if (state.query && state.albumDetail) {
        state.albumDetail = null;
      }
      state.selectedSongs.clear();
      state.songAnchor = null;
      state.selectedAlbums.clear();
      state.albumAnchor = null;
      state.albumPage = 1;
      renderContent();
      updateStatus();
    }, 80);
    el.searchInput.addEventListener("input", () => {
      updateSearchClear();
      applySearch();
    });
    el.searchInput.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        clearSearch();
        el.searchInput.blur();
      } else if (event.key === "Enter") {
        event.preventDefault();
        el.searchInput.blur();
        focusContent();
      }
    });
    el.searchClear.addEventListener("click", () => {
      clearSearch();
      el.searchInput.focus();
    });

    el.sectionTabs.addEventListener("click", (event) => {
      const tab = event.target.closest("[data-section]");
      if (!tab) return;
      if (tab.dataset.section === "playlists"
          && state.section === "playlists") {
        focusContent();
        return;
      }
      setSection(tab.dataset.section);
    });

    el.setupButton.addEventListener("click", openSetupModal);

    el.content.addEventListener("click", handleContentClick);
    el.content.addEventListener("dblclick", handleContentDoubleClick);
    el.content.addEventListener("contextmenu", handleContentContextMenu);
    el.content.addEventListener("dragstart", handleContentDragStart);

    el.modalRoot.addEventListener("click", handleModalClick);

    el.menuRoot.addEventListener("contextmenu", (event) => event.preventDefault());

    bindAudioEvents();

    document.addEventListener("click", (event) => {
      if (!el.menuRoot.classList.contains("hidden")
          && !el.menuRoot.contains(event.target)) {
        closeMenu();
      }
      if (state.queueOpen
          && !el.queuePopover.contains(event.target)
          && !el.queueButton.contains(event.target)) {
        toggleQueue(false);
      }
    });
    document.addEventListener("keydown", handleGlobalKeydown);
    window.addEventListener("hashchange", () => { applyLocationRoute(); });
    window.addEventListener("popstate", () => { applyLocationRoute(); });
    window.addEventListener("resize", () => {
      if (state.songVirtual) state.songVirtual.render();
      if (state.queueOpen) {
        renderQueueWindow();
        positionQueuePopover();
      }
      window.requestAnimationFrame(refreshAlbumPagerLayout);
    });
    window.addEventListener("beforeunload", () => {
      if (Number.isFinite(audio.currentTime) && audio.src) {
        player.elapsed = audio.currentTime;
      }
      savePlayerState();
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) savePlayerState();
    });
  }

  function handleModalClick(event) {
    const actionEl = event.target.closest("[data-action]");
    if (actionEl) {
      const action = actionEl.dataset.action;
      if (action === "close-modal") {
        closeModal();
      } else if (action === "setup-login") {
        loginAdmin();
      } else if (action === "setup-logout") {
        logoutAdmin();
      } else if (action === "setup-quick-scan") {
        startSetupScan("quick");
      } else if (action === "setup-full-scan") {
        startSetupScan("full");
      } else if (action === "setup-rekey") {
        promptSetupRekey();
      } else if (action === "setup-rebuild-artwork") {
        startSetupArtwork();
      } else if (action === "artwork-reload") {
        window.location.reload();
      } else if (action === "setup-save-settings") {
        saveSetupSettings();
      } else if (action === "setup-change-password") {
        openChangePasswordDialog();
      } else if (action === "admin-password-save") {
        changeAdminPassword();
      } else if (action === "admin-password-cancel") {
        state.modalMode = "setup";
        renderSetup();
      } else if (action === "setup-back") {
        openSetupModal();
      } else if (action === "rekey-reload") {
        reloadAfterRekey();
      } else if (action === "edit-library-id") {
        openLibraryIdDialog(actionEl.dataset.path || "");
      } else if (action === "library-id-save") {
        saveLibraryIdDialog();
      } else if (action === "library-id-cancel") {
        closeModal();
        openSetupModal();
      } else if (action === "add-to-playlist-pick") {
        addToPlaylistFromDialog(actionEl.dataset.playlistId);
      } else if (action === "playlist-cancel") {
        closeModal();
      } else if (action === "playlist-save") {
        savePlaylistDialog();
      } else if (action === "confirm-cancel") {
        closeModal();
      } else if (action === "confirm-ok") {
        const callback = state.confirmCallback;
        closeModal();
        if (callback) {
          try {
            const result = callback();
            if (result && result.catch) {
              result.catch((error) => toast(error.message, { error: true }));
            }
          } catch (error) {
            toast(error.message, { error: true });
          }
        }
      }
      return;
    }
    if (event.target === el.modalRoot) {
      closeModal();
    }
  }

  function bindSeekbar() {
    if (el.lcdSeek.dataset.bound === "1") return;
    el.lcdSeek.dataset.bound = "1";
    const seek = el.lcdSeek;
    const fractionFor = (event) => {
      const rect = seek.getBoundingClientRect();
      return clamp((event.clientX - rect.left) / Math.max(rect.width, 1), 0, 1);
    };
    let dragging = false;

    const preview = (event) => {
      if (!dragging) return;
      const duration = currentDuration();
      if (duration <= 0) return;
      state.seekPreview = fractionFor(event) * duration;
      updateProgressUI();
    };

    const cleanup = () => {
      window.removeEventListener("pointermove", preview, true);
      window.removeEventListener("pointerup", finish, true);
      window.removeEventListener("pointercancel", cancel, true);
      window.removeEventListener("blur", cancel, true);
      seek.classList.remove("dragging");
    };

    const finish = (event) => {
      if (!dragging) return;
      dragging = false;
      const duration = currentDuration();
      const fraction = fractionFor(event);
      state.seekPreview = null;
      if (duration > 0) {
        seekToFraction(fraction);
      } else {
        updateProgressUI();
      }
      try { seek.releasePointerCapture(event.pointerId); } catch (_) {}
      cleanup();
    };

    const cancel = () => {
      if (!dragging) return;
      dragging = false;
      state.seekPreview = null;
      updateProgressUI();
      cleanup();
    };

    seek.addEventListener("pointerdown", (event) => {
      if (!currentSong()) return;
      if (event.button !== 0) return;
      const duration = currentDuration();
      if (duration <= 0) return;
      event.preventDefault();
      dragging = true;
      seek.classList.add("dragging");
      // Capture is an optimisation; window listeners keep the drag working
      // even when pointer capture is unavailable or rejected.
      try { seek.setPointerCapture(event.pointerId); } catch (_) {}
      window.addEventListener("pointermove", preview, true);
      window.addEventListener("pointerup", finish, true);
      window.addEventListener("pointercancel", cancel, true);
      window.addEventListener("blur", cancel, true);
      preview(event);
    });

    seek.addEventListener("keydown", (event) => {
      const duration = currentDuration();
      if (!duration) return;
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        event.stopPropagation();
        const delta = event.key === "ArrowLeft" ? -5 : 5;
        seekToFraction((player.elapsed + delta) / duration);
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        event.stopPropagation();
        seekToFraction(event.key === "Home" ? 0 : 1);
      }
    });
  }

  function bindAudioEvents() {
    audio.addEventListener("loadedmetadata", handleLoadedMetadata);
    audio.addEventListener("loadeddata", () => {
      setAudioBuffering(false);
      refreshSystemStatus();
    });
    audio.addEventListener("canplay", () => setAudioBuffering(false));
    audio.addEventListener("waiting", () => setAudioBuffering(true));
    audio.addEventListener("stalled", () => setAudioBuffering(true));
    audio.addEventListener("playing", handleAudioPlaying);
    audio.addEventListener("play", updateStatus);
    audio.addEventListener("pause", () => {
      setAudioBuffering(false);
      updateStatus();
    });
    audio.addEventListener("ended", updateStatus);
    audio.addEventListener("timeupdate", handleAudioTimeUpdate);
    audio.addEventListener("ended", handleAudioEnded);
    audio.addEventListener("error", handleAudioError);
  }

  // ---------------------------------------------------------------------
  // Expose a tiny amount of state for debugging in the browser console.

  window.tracksWeb = {
    state,
    player,
    currentSong,
    setSection,
    renderContent,
    openSetupModal,
    webPlayerBaseUrl,
    albumRoute,
    songRoute,
    playlistRoute,
    applyLocationRoute,
  };
})();




