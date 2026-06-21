#!/usr/bin/env python3
from __future__ import annotations
import aiohttp
import discord
from discord import app_commands
import asyncio
import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import json
import sqlite3
from http.cookiejar import MozillaCookieJar
from html import unescape as html_unescape
from pathlib import Path
from time import monotonic, time
from urllib.parse import parse_qsl, urlencode, unquote, urlparse, urlunparse
from dotenv import load_dotenv
from cove_attribution import friend_post_content, friend_target_post_content

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("cove")


def _require_int_env(name: str, *, allow_zero: bool = True, default: str | None = None) -> int:
    raw = os.getenv(name, default)
    if raw is None or raw == "":
        if default is None:
            sys.exit(f"[Cove] Required env var {name} is missing.")
        raw = default
    try:
        value = int(raw)
    except ValueError:
        sys.exit(f"[Cove] Env var {name} must be an integer (got: {raw!r}).")
    if not allow_zero and value == 0:
        sys.exit(f"[Cove] Env var {name} must be non-zero.")
    return value


def _env_bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


TOKEN           = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    sys.exit("[Cove] Required env var DISCORD_TOKEN is missing.")

GUILD_ID        = _require_int_env("GUILD_ID", allow_zero=False)
FRIEND_GUILD_ID = _require_int_env("FRIEND_GUILD_ID", allow_zero=True, default="0")

_WHITELIST_RAW = os.getenv("WHITELIST_USER_IDS", "")
WHITELIST_IDS = {
    int(uid.strip())
    for uid in _WHITELIST_RAW.split(",")
    if uid.strip().isdigit()
}

COOKIES_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
COOKIES_EXIST = os.path.exists(COOKIES_FILE)
if COOKIES_EXIST:
    try:
        os.chmod(COOKIES_FILE, 0o600)
    except OSError as e:
        log.warning("[Cove] Could not restrict cookies.txt permissions: %s", e)

AUDIO_KBPS           = 128
MAX_DURATION_SECONDS = 600
NYO_EMOJI            = "<:NYO:1312902725750624316>"

MAX_CONCURRENT_JOBS    = _require_int_env("MAX_CONCURRENT_JOBS", default="8")
MAX_QUEUED_JOBS        = _require_int_env("MAX_QUEUED_JOBS", default="16")
SUBPROCESS_TIMEOUT     = _require_int_env("SUBPROCESS_TIMEOUT", default="900")
REDDIT_YTDLP_TIMEOUT   = _require_int_env("REDDIT_YTDLP_TIMEOUT", allow_zero=False, default="45")
DELETE_TTL_SECONDS     = _require_int_env("DELETE_TTL_SECONDS", default="21600")
FRIEND_POST_TTL_SECONDS = _require_int_env("FRIEND_POST_TTL_SECONDS", default="86400")
YT_DLP_FRAGMENTS       = _require_int_env("YT_DLP_FRAGMENTS", default="16")
MAX_FILESIZE_MB        = _require_int_env("MAX_FILESIZE_MB", default="500")
MAX_URL_LENGTH         = _require_int_env("MAX_URL_LENGTH", allow_zero=False, default="2048")
NEET_TTL_SECONDS       = _require_int_env("NEET_TTL_SECONDS", allow_zero=False, default="600")
FAST_SOURCE_MODE       = _env_bool("FAST_SOURCE_MODE", "0")
USE_NVENC              = _env_bool("USE_NVENC", "0")
USER_RATE_LIMIT        = _require_int_env("USER_RATE_LIMIT", default="10")
USER_RATE_WINDOW       = _require_int_env("USER_RATE_WINDOW", default="60")
YT_DLP_MIN_VERSION     = os.getenv("YT_DLP_MIN_VERSION", "2024.01.01")
USE_HEVC               = _env_bool("USE_HEVC", "0")
USE_HWACCEL            = _env_bool("USE_HWACCEL", "1")
NVENC_MAX_SESSIONS     = _require_int_env("NVENC_MAX_SESSIONS", default="5")
PROCESS_NICE           = _require_int_env("PROCESS_NICE", default="10")
FFMPEG_TIMEOUT         = _require_int_env("FFMPEG_TIMEOUT", default="300")
USE_ARIA2C_ENV         = _env_bool("USE_ARIA2C", "1")
PERSISTENT_CACHE       = _env_bool("PERSISTENT_CACHE", "1")
ADMIN_HEALTH_COMMAND   = _env_bool("ADMIN_HEALTH_COMMAND", "1")
REDDIT_PRECHECK_TIMEOUT = _require_int_env("REDDIT_PRECHECK_TIMEOUT", allow_zero=False, default="3")

JOB_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
ENCODE_SEMAPHORE = asyncio.Semaphore(NVENC_MAX_SESSIONS)

YT_DLP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

BOOST_TIER_LIMITS_MB = {
    0: 9.5,
    1: 9.5,
    2: 49.0,
    3: 99.0,
}

GIF_MAX_DURATION = 10.0

AUTO_DOWNLOAD_DOMAINS = {
    "twitter.com",
    "x.com",
    "reddit.com",
    "redd.it",
    "tiktok.com",
    "instagram.com",
    "streamable.com",
    "threads.net",
    "vimeo.com",
    "arazu.io",
    "fixupx.com",
    "fxtwitter.com",
    "vxtwitter.com",
    "twittpr.com",
    "twitch.tv",
    "clips.twitch.tv",
}

BLACKLISTED_DOMAINS = {
    "kkinstagram.com",
    "fixupx.com",
    "fxtwitter.com",
    "vxtwitter.com",
    "twittpr.com",
}

FIXUP_DOMAINS = {
    "fixupx.com",
    "fxtwitter.com",
    "vxtwitter.com",
    "twittpr.com",
}

TWITTER_DOMAINS = {
    "x.com",
    "twitter.com",
}

VIDEO_DOMAINS = {
    "v.redd.it",
    "youtube.com",
    "youtu.be",
    "streamable.com",
    "gfycat.com",
    "redgifs.com",
    "clips.twitch.tv",
    "twitch.tv",
    "vimeo.com",
}

NO_VIDEO_PHRASES = (
    "No video could be found",
    "no video",
    "is not a video",
    "does not have a video",
    "no media",
    "HTTP Error 429",
    "Too Many Requests",
    "Connection timed out",
    "connect timeout",
    "timed out",
    "TransportError",
    "Unable to download webpage",
)

INSTAGRAM_UNAVAILABLE_PHRASES = (
    "empty media response",
    "this content isn't available",
    "content is not available",
    "content unavailable",
    "not available",
    "unavailable",
    "private",
    "login required",
    "you need to log in",
    "sign in",
    "restricted",
    "age-restricted",
    "under 13",
    "has set limits",
    "was deleted",
    "has been deleted",
    "post may have been deleted",
    "account may have been banned",
    "HTTP Error 403",
    "HTTP Error 404",
)

INSTAGRAM_IMAGE_NO_VIDEO_PHRASES = (
    "no video formats found",
    "no video could be found",
    "is not a video",
    "does not have a video",
)

INSTAGRAM_IMAGE_MARKER = "[INSTAGRAM_IMAGE]"
REDDIT_GIF_MARKER = "[REDDIT_GIF]"
REDDIT_IMAGE_MARKER = "[REDDIT_IMAGE]"
TWITTER_IMAGE_MARKER = "[TWITTER_IMAGE]"
REDDIT_VXREDDIT_MARKER = "[REDDIT_VXREDDIT]"
INSTAGRAM_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "heic"}
INSTAGRAM_VIDEO_EXTENSIONS = {"mp4", "m4v", "mov", "webm"}
REDDIT_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
REDDIT_IMAGE_HOSTS = {"i.redd.it", "preview.redd.it"}

REDDIT_SILENT_URL_PATTERNS = (
    "i.redd.it",
    "reddit.com/media",
)

TMP_BASE = "/dev/shm" if os.path.isdir("/dev/shm") else None

URL_RE         = re.compile(r"https?://[^\s]+")
REDDIT_RE      = re.compile(r'href="(https?://(?:old\.)?reddit\.com/r/[^/]+/comments/[^"]+)"')
REDDIT_POST_RE = re.compile(r'reddit\.com/r/[^/]+/comments/')
REDDIT_SHORT_RE = re.compile(r'reddit\.com/r/([^/]+)/s/([^/?#]+)')

REDDIT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

FORMAT_DEFAULT = (
    "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]"
    "/bestvideo[height<=1080]+bestaudio"
    "/best[height<=1080]"
    "/best"
)
FORMAT_FAST = (
    "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]"
    "/bestvideo[height<=720][ext=mp4]+bestaudio"
    "/best[height<=720][ext=mp4]"
    "/best[height<=720]"
    "/best"
)
FORMAT_REDDIT = (
    "bestvideo[height<=1080][ext=mp4][protocol!=m3u8][protocol!=m3u8_native]+bestaudio[ext=m4a][protocol!=m3u8][protocol!=m3u8_native]"
    "/bestvideo[height<=1080][protocol!=m3u8][protocol!=m3u8_native]+bestaudio[protocol!=m3u8][protocol!=m3u8_native]"
    "/best[height<=1080][protocol!=m3u8][protocol!=m3u8_native]"
    "/best[protocol!=m3u8][protocol!=m3u8_native]"
)
FORMAT_REDDIT_FAST = (
    "bestvideo[height<=720][ext=mp4][protocol!=m3u8][protocol!=m3u8_native]+bestaudio[ext=m4a][protocol!=m3u8][protocol!=m3u8_native]"
    "/bestvideo[height<=720][protocol!=m3u8][protocol!=m3u8_native]+bestaudio[protocol!=m3u8][protocol!=m3u8_native]"
    "/best[height<=720][protocol!=m3u8][protocol!=m3u8_native]"
    "/best[protocol!=m3u8][protocol!=m3u8_native]"
)

# YouTube download quality. Controlled at runtime by the /quality admin command
# (persisted to runtime_settings.json) and seeded on first run by the
# YOUTUBE_QUALITY env var. 360p is a single progressive stream (itag 18) and is
# fast; 480p/720p/1080p use separate video+audio streams which are sharper but
# slower, because YouTube throttles its standalone audio stream (~30 KiB/s).
# Applies to YouTube video downloads only; non-YouTube sites and /audio are
# unaffected.
YOUTUBE_DEFAULT_QUALITY = "1080"
YOUTUBE_QUALITY_FORMATS = {
    "360": "18/best[ext=mp4][height<=360]/best[height<=360]/best",
    "480": (
        "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]"
        "/bestvideo[height<=480]+bestaudio/best[height<=480][ext=mp4]/best[height<=480]/best"
    ),
    "720": (
        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]"
        "/bestvideo[height<=720]+bestaudio/best[height<=720][ext=mp4]/best[height<=720]/best"
    ),
    "1080": (
        "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]"
        "/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
    ),
    "1440": (
        "bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]"
        "/bestvideo[height<=1440]+bestaudio/best[height<=1440]/best"
    ),
    "2160": (
        "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]"
        "/bestvideo[height<=2160]+bestaudio/best[height<=2160]/best"
    ),
}

_deletable: dict[int, tuple[int, float]] = {}
_friend_posts: dict[int, tuple[int, float]] = {}
_friend_neet_skip_users: dict[int, float] = {}
_processed_source_messages: dict[int, float] = {}
_user_request_times: dict[int, list[float]] = {}
_active_tasks: set[asyncio.Task] = set()
_http_session: aiohttp.ClientSession | None = None
_reddit_cookie_header_cache: tuple[float, str | None, float] | None = None

# (value, expires_at_monotonic) — TTL caches for Reddit pre-checks.
# Shortlinks resolve deterministically and never change; has_video can shift
# if a post is edited, so it gets a shorter TTL.
_reddit_shortlink_cache: dict[str, tuple[str, float]] = {}
_reddit_has_video_cache: dict[str, tuple[bool, float]] = {}
_instagram_probe_cache: dict[str, tuple[tuple[bool, int | None, str | None], float]] = {}
_twitter_probe_cache: dict[str, tuple[bool, float]] = {}
SHORTLINK_CACHE_TTL = 24 * 3600
HAS_VIDEO_CACHE_TTL = 3600
INSTAGRAM_PROBE_CACHE_TTL = 10 * 60
TWITTER_PROBE_CACHE_TTL = 10 * 60
FXTWITTER_API_TIMEOUT = 8
KKINSTAGRAM_PROBE_TIMEOUT = 8
PROBE_FAILURE_CACHE_TTL = 2 * 60
KKINSTAGRAM_DISCORD_UA = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"
CACHE_MAX_ENTRIES = 512
MAX_HTTP_RESPONSE_BYTES = 1024 * 1024
MAX_SUBPROCESS_OUTPUT_BYTES = 512 * 1024
_ytdlp_version_status: tuple[bool, str] = (False, "yt-dlp version has not been checked yet.")
_ytdlp_admin_warning_sent = False
_cookie_warning_sent_at: float = 0
COOKIE_WARNING_COOLDOWN = 6 * 3600
_queued_jobs = 0


def _cache_get(cache: dict, key: str):
    entry = cache.get(key)
    if entry is None:
        return None
    value, expires = entry
    if monotonic() >= expires:
        cache.pop(key, None)
        return None
    return value


def _cache_set(cache: dict, key: str, value, ttl: float) -> None:
    if len(cache) >= CACHE_MAX_ENTRIES:
        now = monotonic()
        expired = [k for k, (_, exp) in cache.items() if exp <= now]
        for k in expired:
            del cache[k]
        if len(cache) >= CACHE_MAX_ENTRIES:
            to_drop = len(cache) - CACHE_MAX_ENTRIES * 3 // 4
            it = iter(cache)
            for _ in range(to_drop):
                cache.pop(next(it), None)
    cache[key] = (value, monotonic() + ttl)


_inflight_urls: set[str] = set()
SOURCE_MESSAGE_DEDUP_TTL_SECONDS = max(DELETE_TTL_SECONDS, FRIEND_POST_TTL_SECONDS)


def _parse_log_markers(log_text: str) -> tuple[bool, str | None, str | None]:
    novideo = False
    toobig = None
    error = None
    for line in log_text.splitlines():
        stripped = line.strip()
        if stripped == "[NOVIDEO]":
            novideo = True
        elif not toobig and stripped.startswith("[TOOBIG]"):
            toobig = stripped.replace("[TOOBIG] ", "")
        elif not error and stripped.startswith("[ERROR]"):
            error = stripped.replace("[ERROR] ", "")
    return novideo, toobig, error


def canonical_url_for_key(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower() or "https"
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "old.reddit.com":
        host = "reddit.com"
    if host == "youtu.be":
        path_parts = [part for part in parsed.path.split("/") if part]
        if path_parts:
            keep = [("v", path_parts[0])]
        else:
            keep = []
        query = urlencode(keep)
        return urlunparse(("https", "youtube.com", "/watch", "", query, ""))

    path = re.sub(r"/+", "/", parsed.path).rstrip("/").lower()
    query_items = []
    allowed_query_keys = {"v", "list", "t", "start"}
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in {"fbclid", "igsh", "si", "share_id"}:
            continue
        if host_matches(host, {"youtube.com"}) and lowered not in allowed_query_keys:
            continue
        query_items.append((lowered, value))
    query = urlencode(query_items)
    return urlunparse((scheme, host, path, "", query, ""))


def _inflight_key(kind: str, url: str) -> str:
    return f"{kind}:{canonical_url_for_key(url)}"

CACHE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.db")
_cache_db_conn: sqlite3.Connection | None = None


def _get_cache_db() -> sqlite3.Connection | None:
    global _cache_db_conn
    if _cache_db_conn is not None:
        return _cache_db_conn
    if not PERSISTENT_CACHE:
        return None
    try:
        conn = sqlite3.connect(CACHE_DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _cache_db_conn = conn
        return conn
    except Exception as e:
        log.warning("[cache] Could not open DB: %s", e)
        return None


def _init_persistent_cache() -> None:
    conn = _get_cache_db()
    if conn is None:
        return
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS url_cache (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            cache_type TEXT NOT NULL,
            expires_at REAL NOT NULL
        )""")
        conn.execute("DELETE FROM url_cache WHERE expires_at <= ?", (time(),))
        now_mono = monotonic()
        now_wall = time()
        for row in conn.execute("SELECT key, value, cache_type, expires_at FROM url_cache"):
            key, value, cache_type, expires_at = row
            remaining = expires_at - now_wall
            if remaining <= 0:
                continue
            mono_expires = now_mono + remaining
            if cache_type == "shortlink":
                _reddit_shortlink_cache[key] = (value, mono_expires)
            elif cache_type == "has_video":
                _reddit_has_video_cache[key] = (value == "1", mono_expires)
        conn.commit()
        log.info(
            "[Cove] Loaded %d shortlink + %d has_video entries from persistent cache",
            len(_reddit_shortlink_cache), len(_reddit_has_video_cache),
        )
    except Exception as e:
        log.warning("[Cove] Could not load persistent cache: %s", e)


def _persist_cache_entry(key: str, value, cache_type: str, ttl: float) -> None:
    conn = _get_cache_db()
    if conn is None:
        return
    str_value = str(int(value)) if isinstance(value, bool) else str(value)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO url_cache (key, value, cache_type, expires_at) VALUES (?, ?, ?, ?)",
            (key, str_value, cache_type, time() + ttl),
        )
        conn.commit()
    except Exception as e:
        log.warning("[cache] Failed to persist: %s", e)


async def _persist_cache_entry_async(key: str, value, cache_type: str, ttl: float) -> None:
    if not PERSISTENT_CACHE:
        return
    await asyncio.to_thread(_persist_cache_entry, key, value, cache_type, ttl)


_init_persistent_cache()


def _get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": REDDIT_UA},
            connector=aiohttp.TCPConnector(limit=30, limit_per_host=10),
        )
    return _http_session


_COOKIE_HEADER_TTL = 60


def reddit_cookie_header() -> str | None:
    global _reddit_cookie_header_cache
    if not COOKIES_EXIST:
        return None
    try:
        now = monotonic()
        if _reddit_cookie_header_cache:
            cached_mtime, cached_header, cached_at = _reddit_cookie_header_cache
            if now - cached_at < _COOKIE_HEADER_TTL:
                return cached_header

        mtime = os.path.getmtime(COOKIES_FILE)
        if _reddit_cookie_header_cache and _reddit_cookie_header_cache[0] == mtime:
            _reddit_cookie_header_cache = (mtime, _reddit_cookie_header_cache[1], now)
            return _reddit_cookie_header_cache[1]

        jar = MozillaCookieJar(COOKIES_FILE)
        jar.load(ignore_discard=True, ignore_expires=True)
        values = []
        for cookie in jar:
            domain = cookie.domain.lower().lstrip(".")
            if domain.startswith("#httponly_"):
                domain = domain[len("#httponly_"):]
            if domain == "reddit.com" or domain.endswith(".reddit.com"):
                values.append(f"{cookie.name}={cookie.value}")
        header = "; ".join(values) or None
        _reddit_cookie_header_cache = (mtime, header, now)
        return header
    except Exception as e:
        log.warning("[reddit] Could not load cookies.txt for API request: %s", e)
        _reddit_cookie_header_cache = None
        return None


def reddit_json_headers() -> dict[str, str]:
    headers = {"User-Agent": REDDIT_UA, "Accept": "application/json"}
    cookie_header = reddit_cookie_header()
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


async def read_limited_response(resp: aiohttp.ClientResponse, limit: int) -> bytes:
    chunks = []
    total = 0
    async for chunk in resp.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > limit:
            raise ValueError(f"response exceeded {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def _close_http_session() -> None:
    global _http_session
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
    _http_session = None


def _on_task_done(task: asyncio.Task) -> None:
    _active_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("Background task crashed: %s", exc, exc_info=exc)


def spawn_tracked(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _active_tasks.add(task)
    task.add_done_callback(_on_task_done)
    return task


class PipelineTimer:
    def __init__(self, label: str):
        self.label = label
        self.started_at = monotonic()
        self.last_at = self.started_at

    def mark(self, phase: str) -> None:
        now = monotonic()
        log.info(
            "[timing] %s phase=%s elapsed=%.2fs total=%.2fs",
            self.label,
            phase,
            now - self.last_at,
            now - self.started_at,
        )
        self.last_at = now

    def elapsed(self) -> float:
        return monotonic() - self.started_at

    def elapsed_str(self) -> str:
        secs = self.elapsed()
        if secs < 60:
            return f"{secs:.1f}s"
        return f"{int(secs // 60)}m{int(secs % 60)}s"


def _try_reserve_job_slot() -> bool:
    global _queued_jobs
    if _queued_jobs >= MAX_CONCURRENT_JOBS + MAX_QUEUED_JOBS:
        return False
    _queued_jobs += 1
    return True


def _release_job_slot() -> None:
    global _queued_jobs
    _queued_jobs = max(0, _queued_jobs - 1)


def _job_queue_status() -> tuple[int, int]:
    running = max(0, min(MAX_CONCURRENT_JOBS, MAX_CONCURRENT_JOBS - JOB_SEMAPHORE._value))
    waiting = max(0, _queued_jobs - running)
    return running, waiting


def prune_deletable() -> None:
    now = monotonic()
    expired = [mid for mid, (_, expires_at) in _deletable.items() if expires_at <= now]
    for mid in expired:
        _deletable.pop(mid, None)


def prune_friend_posts() -> None:
    now = monotonic()
    expired = [mid for mid, (_, expires_at) in _friend_posts.items() if expires_at <= now]
    for mid in expired:
        _friend_posts.pop(mid, None)


def prune_neet_skips() -> None:
    now = monotonic()
    expired = [uid for uid, expires_at in _friend_neet_skip_users.items() if expires_at <= now]
    for uid in expired:
        _friend_neet_skip_users.pop(uid, None)


def prune_processed_source_messages() -> None:
    now = monotonic()
    expired = [mid for mid, expires_at in _processed_source_messages.items() if expires_at <= now]
    for mid in expired:
        _processed_source_messages.pop(mid, None)


def mark_source_message_processing(message_id: int) -> bool:
    prune_processed_source_messages()
    if message_id in _processed_source_messages:
        return False
    _processed_source_messages[message_id] = monotonic() + SOURCE_MESSAGE_DEDUP_TTL_SECONDS
    return True


_user_rate_last_sweep: float = 0


def _check_user_rate_limit(user_id: int) -> bool:
    global _user_rate_last_sweep
    now = monotonic()
    if now - _user_rate_last_sweep > 300:
        cutoff = now - USER_RATE_WINDOW
        stale = [uid for uid, ts in _user_request_times.items() if not ts or ts[-1] < cutoff]
        for uid in stale:
            del _user_request_times[uid]
        _user_rate_last_sweep = now
    times = _user_request_times.get(user_id, [])
    times = [t for t in times if now - t < USER_RATE_WINDOW]
    if len(times) >= USER_RATE_LIMIT:
        _user_request_times[user_id] = times
        return False
    times.append(now)
    _user_request_times[user_id] = times
    return True


def _sweep_orphaned_tmpdirs(min_age_seconds: float = 900) -> None:
    root = Path(TMP_BASE or tempfile.gettempdir())
    cutoff = time() - min_age_seconds
    try:
        for path in root.glob("cove_*"):
            if not path.is_dir():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime <= cutoff:
                try:
                    shutil.rmtree(path)
                    log.info("[Cove] Removed orphan temp dir: %s", path)
                except OSError as e:
                    log.warning("[Cove] Failed to remove orphan temp dir %s: %s", path, e)
    except OSError as e:
        log.warning("[Cove] Temp sweep failed: %s", e)


# ── Hostname helpers ────────────────────────────────────────────────────────────

def hostname_for(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def host_matches(host: str, domains: set[str]) -> bool:
    return host in domains or any(host.endswith(f".{d}") for d in domains)


RUNTIME_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime_settings.json")


def _load_runtime_settings() -> dict:
    try:
        with open(RUNTIME_SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_runtime_settings(data: dict) -> None:
    try:
        tmp_path = RUNTIME_SETTINGS_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_path, RUNTIME_SETTINGS_PATH)
    except OSError as e:
        log.warning("[cove] Could not persist runtime settings: %s", e)


def _initial_youtube_quality() -> str:
    env_q = os.getenv("YOUTUBE_QUALITY", "").strip()
    if env_q in YOUTUBE_QUALITY_FORMATS:
        return env_q
    if _env_bool("YOUTUBE_FAST_360", "0"):  # backward compat with the old toggle
        return "360"
    return YOUTUBE_DEFAULT_QUALITY


_youtube_quality = _load_runtime_settings().get("youtube_quality")
if _youtube_quality not in YOUTUBE_QUALITY_FORMATS:
    _youtube_quality = _initial_youtube_quality()


def get_youtube_quality() -> str:
    return _youtube_quality


def set_youtube_quality(quality: str) -> None:
    global _youtube_quality
    if quality not in YOUTUBE_QUALITY_FORMATS:
        raise ValueError(f"invalid YouTube quality: {quality!r}")
    _youtube_quality = quality
    settings = _load_runtime_settings()
    settings["youtube_quality"] = quality
    _save_runtime_settings(settings)


def youtube_quality_format(url: str, quality: str | None = None) -> str | None:
    """Format selector for the selected quality, if url is a YouTube link.

    Returns None for non-YouTube URLs so other sites keep their normal selection.
    """
    if not host_matches(hostname_for(url), {"youtube.com", "youtu.be"}):
        return None
    selected_quality = quality or get_youtube_quality()
    return YOUTUBE_QUALITY_FORMATS[selected_quality]


def replace_hostname(url: str, new_host: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(netloc=new_host))


def resolve_fixup_url(url: str) -> str:
    host = hostname_for(url)
    if host_matches(host, FIXUP_DOMAINS):
        rewritten = replace_hostname(url, "x.com")
        log.info("[fixup] Rewrote %s -> %s", url, rewritten)
        return rewritten
    return url


def rewrite_instagram_image_url(url: str, log_text: str) -> str | None:
    # Only rewrite posts that the probe explicitly classified as image/text.
    # If the post could not be embedded, leave it untouched.
    if (
        INSTAGRAM_IMAGE_MARKER in log_text.splitlines()
        and _is_supported_instagram_post_url(url)
    ):
        return replace_hostname(url, "www.kkinstagram.com")
    return None


async def send_instagram_image_rewrite(message: discord.Message, url: str, log_text: str) -> bool:
    rewritten = rewrite_instagram_image_url(url, log_text)
    if not rewritten:
        return False
    try:
        await message.channel.send(rewritten)
    except discord.HTTPException as e:
        log.warning("[cove] Failed to send Instagram image/text rewrite: %s", e)
        return False
    try:
        await message.delete()
    except discord.Forbidden as e:
        log.info("[cove] Could not delete original Instagram image/text message: %s", e)
    except discord.HTTPException as e:
        log.warning("[cove] Failed to delete original Instagram image/text message: %s", e)
    return True


def twitter_fxtwitter_url_from_log(log_text: str) -> str | None:
    lines = log_text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == TWITTER_IMAGE_MARKER and i + 1 < len(lines):
            return lines[i + 1].strip()
    return None


def reddit_vxreddit_url_from_log(log_text: str) -> str | None:
    lines = log_text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == REDDIT_VXREDDIT_MARKER and i + 1 < len(lines):
            return lines[i + 1].strip()
    return None


def reddit_media_url_from_text(text: str, extensions: set[str]) -> str | None:
    for match in URL_RE.finditer(unquote(text)):
        url = match.group(0).rstrip(").,>")
        parsed = urlparse(url)
        ext = parsed.path.rsplit(".", 1)[-1].lower()
        if hostname_for(url) in REDDIT_IMAGE_HOSTS and ext in extensions:
            return url
        if not host_matches(hostname_for(url), {"reddit.com"}):
            continue
        if parsed.path != "/media":
            continue
        for part in parsed.query.split("&"):
            key, _, value = part.partition("=")
            ext = urlparse(value).path.rsplit(".", 1)[-1].lower()
            if key == "url" and ext in extensions:
                return value
    return None


def reddit_media_gif_url_from_text(text: str) -> str | None:
    return reddit_media_url_from_text(text, {"gif"})


def reddit_media_image_url_from_text(text: str) -> str | None:
    return reddit_media_url_from_text(text, REDDIT_IMAGE_EXTENSIONS)


def _reddit_image_url_from_value(value: str) -> str | None:
    image_url = reddit_media_image_url_from_text(html_unescape(value))
    if image_url:
        return image_url
    parsed = urlparse(html_unescape(value))
    if hostname_for(html_unescape(value)) in REDDIT_IMAGE_HOSTS:
        query = parsed.query.lower()
        if "format=pjpg" in query or "format=png" in query or "format=jpg" in query:
            return html_unescape(value)
    return None


def reddit_gif_url_from_log(log_text: str) -> str | None:
    lines = log_text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == REDDIT_GIF_MARKER and index + 1 < len(lines):
            return reddit_media_gif_url_from_text(lines[index + 1])
    return None


def reddit_image_url_from_log(log_text: str) -> str | None:
    lines = log_text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == REDDIT_IMAGE_MARKER and index + 1 < len(lines):
            return reddit_media_image_url_from_text(lines[index + 1])
    return None


async def send_reddit_gif_repost(message: discord.Message, url: str) -> bool:
    if message.guild and message.guild.me:
        perms = message.channel.permissions_for(message.guild.me)
        if not perms.embed_links:
            log.warning("[cove] Cannot repost Reddit GIF without Embed Links permission in #%s.", message.channel)
            return False
    try:
        await message.channel.send(url)
    except discord.HTTPException as e:
        log.warning("[cove] Failed to repost Reddit GIF URL: %s", e)
        return False
    log.info("[cove] Reposted Reddit GIF URL for Discord embed: %s", url)
    try:
        await message.delete()
        log.info("[cove] Deleted original Reddit GIF message after repost.")
    except discord.Forbidden as e:
        log.info("[cove] Could not delete original Reddit GIF message: %s", e)
    except discord.HTTPException as e:
        log.warning("[cove] Failed to delete original Reddit GIF message: %s", e)
    return True


def reddit_image_url_from_post(post: dict) -> str | None:
    for key in ("url_overridden_by_dest", "url"):
        value = post.get(key)
        if not isinstance(value, str):
            continue
        image_url = _reddit_image_url_from_value(value)
        if image_url:
            return image_url

    crossposts = post.get("crosspost_parent_list")
    if isinstance(crossposts, list):
        for crosspost in crossposts:
            if isinstance(crosspost, dict):
                image_url = reddit_image_url_from_post(crosspost)
                if image_url:
                    return image_url

    preview = post.get("preview")
    if isinstance(preview, dict):
        images = preview.get("images")
        if isinstance(images, list):
            for image in images:
                if not isinstance(image, dict):
                    continue
                source = image.get("source")
                if not isinstance(source, dict):
                    continue
                value = source.get("url")
                if isinstance(value, str):
                    image_url = _reddit_image_url_from_value(value)
                    if image_url:
                        return image_url

    media_metadata = post.get("media_metadata")
    if not isinstance(media_metadata, dict):
        return None
    gallery_items = post.get("gallery_data", {}).get("items") if isinstance(post.get("gallery_data"), dict) else None
    if isinstance(gallery_items, list):
        ordered_items = [
            media_metadata[item.get("media_id")]
            for item in gallery_items
            if isinstance(item, dict) and item.get("media_id") in media_metadata
        ]
    else:
        ordered_items = list(media_metadata.values())
    for item in ordered_items:
        if not isinstance(item, dict):
            continue
        source = item.get("s")
        if not isinstance(source, dict):
            continue
        image_url = source.get("u") or source.get("gif")
        if not isinstance(image_url, str):
            continue
        image_url = _reddit_image_url_from_value(image_url)
        if image_url:
            return image_url
    return None


async def reddit_image_url(url: str) -> str | None:
    if not REDDIT_POST_RE.search(url):
        return None
    api_url = reddit_api_url(url)
    try:
        session = _get_http_session()
        async with session.get(
            api_url,
            headers=reddit_json_headers(),
            timeout=aiohttp.ClientTimeout(total=REDDIT_PRECHECK_TIMEOUT),
        ) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "json" not in content_type and "text" not in content_type:
                log.warning("[security] Reddit image API returned unexpected Content-Type: %s", content_type)
                return None
            body = await read_limited_response(resp, MAX_HTTP_RESPONSE_BYTES)
            raw = body.decode(errors="replace")
        data = json.loads(raw)
        post = data[0]["data"]["children"][0]["data"]
        return reddit_image_url_from_post(post)
    except Exception as e:
        log.warning("[reddit-image] Failed to resolve image URL: %s", e)
        return None


async def download_reddit_image(url: str, guild: discord.Guild | None) -> str | None:
    parsed = urlparse(url)
    ext = parsed.path.rsplit(".", 1)[-1].lower()
    if hostname_for(url) not in REDDIT_IMAGE_HOSTS or ext not in REDDIT_IMAGE_EXTENSIONS:
        return None

    target_size = int(get_target_mb(guild) * 1024 * 1024)
    tmp = tempfile.mkdtemp(prefix="cove_reddit_image_", dir=TMP_BASE)
    os.chmod(tmp, 0o700)
    filename = _sanitize_filename(Path(unquote(parsed.path)).name) or f"reddit-image.{ext}"
    filepath = str(Path(tmp) / filename)
    downloaded = False

    try:
        session = _get_http_session()
        async with session.get(
            url,
            headers={"User-Agent": REDDIT_UA, "Accept": "image/*,*/*;q=0.8"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                log.warning("[reddit-image] Image GET returned HTTP %d.", resp.status)
                return None
            content_type = resp.headers.get("Content-Type", "")
            if not content_type.lower().startswith("image/"):
                log.warning("[reddit-image] Unexpected image Content-Type: %s", content_type)
                return None
            content_length = resp.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > target_size:
                        log.info("[reddit-image] Image too large for Discord limit.")
                        return None
                except ValueError:
                    pass
            size = 0
            with open(filepath, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 64):
                    size += len(chunk)
                    if size > target_size:
                        log.info("[reddit-image] Image exceeded Discord limit while downloading.")
                        return None
                    f.write(chunk)
        downloaded = True
        return filepath
    except Exception as e:
        log.warning("[reddit-image] Failed to download image: %s", e)
        return None
    finally:
        if not downloaded:
            shutil.rmtree(tmp, ignore_errors=True)


async def send_reddit_image_repost(
    message: discord.Message, url: str, content: str | None = None
) -> "discord.Message | None":
    filepath = await download_reddit_image(url, message.guild)
    if not filepath:
        return None
    try:
        try:
            sent = await message.channel.send(
                content=content,
                file=discord.File(filepath),
                allowed_mentions=discord.AllowedMentions(users=False, everyone=False, roles=False),
            )
        except discord.HTTPException as e:
            log.warning("[cove] Failed to repost Reddit image file: %s", e)
            return None
        log.info("[cove] Reposted Reddit image file for Discord upload: %s", url)
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        return sent
    finally:
        shutil.rmtree(str(Path(filepath).parent), ignore_errors=True)


def is_twitter_photo_url(url: str) -> bool:
    parsed = urlparse(url)
    return host_matches(hostname_for(url), TWITTER_DOMAINS | FIXUP_DOMAINS) and "/photo/" in parsed.path


def extract_extra_mentions(content: str) -> str:
    return URL_RE.sub("", content).strip()


def parse_timestamp(ts: str) -> float | None:
    ts = ts.strip()
    if not ts:
        return None
    parts = ts.split(":")
    try:
        if len(parts) == 1:
            return max(0.0, float(parts[0]))
        if len(parts) == 2:
            return max(0.0, int(parts[0]) * 60 + float(parts[1]))
        if len(parts) == 3:
            return max(0.0, int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2]))
    except (ValueError, IndexError):
        return None
    return None


def extract_supported_url(content: str) -> str | None:
    for match in URL_RE.finditer(content):
        url = match.group(0).rstrip(").,>")
        if len(url) > MAX_URL_LENGTH:
            continue
        host = hostname_for(url)
        if host_matches(host, BLACKLISTED_DOMAINS):
            log.info("[security] Blocked blacklisted domain: %s", host)
            continue
        if is_twitter_photo_url(url):
            continue
        if urlparse(url).path.lower().endswith(".gif"):
            continue
        if host_matches(host, AUTO_DOWNLOAD_DOMAINS):
            return url
    return None


def _is_internal_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


async def validate_manual_url(url: str) -> tuple[bool, str]:
    if len(url) > MAX_URL_LENGTH:
        return False, "URL is too long."
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Invalid URL."
    if parsed.scheme not in ("http", "https"):
        return False, "Only http(s) URLs are allowed."
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "URL has no host."
    if host in {"localhost", "ip6-localhost", "ip6-loopback"}:
        return False, "URL points to a non-public address."
    if _is_internal_ip(host):
        log.warning("[security] Blocked SSRF attempt to internal IP: %s", host)
        return False, "URL points to a non-public address."
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except socket.gaierror:
        return False, "Could not resolve URL host."
    for info in infos:
        if _is_internal_ip(info[4][0]):
            log.warning("[security] Blocked SSRF attempt: %s resolved to internal IP %s", host, info[4][0])
            return False, "URL points to a non-public address."
    return True, ""


def is_friend_server(guild: discord.Guild | None) -> bool:
    return FRIEND_GUILD_ID != 0 and guild is not None and guild.id == FRIEND_GUILD_ID


def get_target_mb(guild: discord.Guild | None) -> float:
    if guild is None:
        return BOOST_TIER_LIMITS_MB[0]
    return BOOST_TIER_LIMITS_MB.get(guild.premium_tier, 9.5)


def is_admin_interaction(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


def _check_bot_permissions(channel: discord.abc.GuildChannel, bot_member: discord.Member) -> tuple[bool, str]:
    perms = channel.permissions_for(bot_member)
    missing = []
    if not perms.send_messages:
        missing.append("Send Messages")
    if not perms.attach_files:
        missing.append("Attach Files")
    if not perms.add_reactions:
        missing.append("Add Reactions")
    if missing:
        return False, ", ".join(missing)
    return True, ""


def clean_env():
    blocked_prefixes = (
        "LD_",
        "PYTHON",
        "SSLKEYLOGFILE",
        "GIT_",
    )
    blocked_names = {
        "DYLD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "FFREPORT",
        "AV_LOG_FORCE_COLOR",
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in blocked_names and not any(key.startswith(prefix) for prefix in blocked_prefixes)
    }
    return env


ENV = clean_env()


def _check_ytdlp_version() -> tuple[bool, str]:
    global _ytdlp_version_status
    try:
        result = subprocess.run(
            ["yt-dlp", "--version"],
            capture_output=True, text=True, timeout=5, env=ENV,
        )
        version = result.stdout.strip()
        if result.returncode != 0 or not version:
            message = "yt-dlp version check failed."
            _ytdlp_version_status = (False, message)
            log.warning("[security] %s", message)
            return _ytdlp_version_status
        if version < YT_DLP_MIN_VERSION:
            message = f"yt-dlp {version} is older than configured minimum {YT_DLP_MIN_VERSION}."
            _ytdlp_version_status = (False, message)
            log.warning(
                "[security] %s",
                message,
            )
        else:
            message = f"yt-dlp {version} meets configured minimum {YT_DLP_MIN_VERSION}."
            _ytdlp_version_status = (True, message)
            log.info("[Cove] yt-dlp version: %s", version)
    except Exception as e:
        message = f"Could not check yt-dlp version: {e}"
        _ytdlp_version_status = (False, message)
        log.warning("[security] %s", message)
    return _ytdlp_version_status


_check_ytdlp_version()


def _check_aria2c() -> bool:
    try:
        result = subprocess.run(
            ["aria2c", "--version"], capture_output=True, timeout=5, env=ENV,
        )
        if result.returncode == 0:
            log.info("[Cove] aria2c detected — available as external downloader.")
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return False


HAS_ARIA2C = _check_aria2c()
USE_ARIA2C = HAS_ARIA2C and USE_ARIA2C_ENV


def should_use_aria2c(url: str) -> bool:
    if not USE_ARIA2C:
        return False
    host = hostname_for(url)
    if host_matches(host, {"instagram.com", "reddit.com", "redd.it", "youtube.com", "youtu.be"}):
        return False
    return True


def _command_version(command: str, args: list[str]) -> tuple[bool, str]:
    path = shutil.which(command)
    if not path:
        return False, "missing"
    try:
        result = subprocess.run(
            [command, *args],
            capture_output=True,
            text=True,
            timeout=5,
            env=ENV,
        )
    except Exception as e:
        return False, f"error: {e}"
    if result.returncode != 0:
        return False, "error"
    first_line = (result.stdout or result.stderr).strip().splitlines()
    version = first_line[0] if first_line else "ok"
    return True, version[:120]


def build_health_report() -> str:
    checks = []
    ytdlp_ok, ytdlp_message = _check_ytdlp_version()
    checks.append(("yt-dlp", ytdlp_ok, ytdlp_message))
    ffmpeg_ok, ffmpeg_message = _command_version("ffmpeg", ["-version"])
    checks.append(("ffmpeg", ffmpeg_ok, ffmpeg_message))
    ffprobe_ok, ffprobe_message = _command_version("ffprobe", ["-version"])
    checks.append(("ffprobe", ffprobe_ok, ffprobe_message))

    if COOKIES_EXIST:
        try:
            mode = oct(os.stat(COOKIES_FILE).st_mode & 0o777)
            cookies_message = f"present ({mode})"
            cookies_ok = True
        except OSError as e:
            cookies_message = f"stat failed: {e}"
            cookies_ok = False
    else:
        cookies_message = "missing"
        cookies_ok = False
    checks.append(("cookies.txt", cookies_ok, cookies_message))

    tmp_root = TMP_BASE or tempfile.gettempdir()
    try:
        usage = shutil.disk_usage(tmp_root)
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        disk_ok = usage.free > 512 * 1024 * 1024
        disk_message = f"{free_gb:.1f} GB free / {total_gb:.1f} GB total at {tmp_root}"
    except OSError as e:
        disk_ok = False
        disk_message = f"failed: {e}"
    checks.append(("temp disk", disk_ok, disk_message))

    checks.append(("aria2c", HAS_ARIA2C, "enabled site-aware" if USE_ARIA2C else "available but disabled" if HAS_ARIA2C else "missing"))
    checks.append(("NVENC", USE_NVENC, "enabled" if USE_NVENC else "disabled"))
    checks.append(("cache", PERSISTENT_CACHE, "persistent cache enabled" if PERSISTENT_CACHE else "persistent cache disabled"))
    running, waiting = _job_queue_status()

    lines = ["Cove health:"]
    for name, ok, message in checks:
        marker = "OK" if ok else "WARN"
        lines.append(f"{marker} {name}: {message}")
    lines.append(
        f"jobs: running={running}, waiting={waiting}, max={MAX_CONCURRENT_JOBS}, queued_max={MAX_QUEUED_JOBS}"
    )
    lines.append(
        f"download: fragments={YT_DLP_FRAGMENTS}, filesize={MAX_FILESIZE_MB}MB, youtube_quality={get_youtube_quality()}"
    )
    return "\n".join(lines)


def _log_shm_info() -> None:
    if TMP_BASE == "/dev/shm":
        try:
            stat = os.statvfs("/dev/shm")
            total_gb = (stat.f_blocks * stat.f_frsize) / (1024 ** 3)
            avail_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
            log.info("[Cove] /dev/shm: %.1fGB total, %.1fGB available", total_gb, avail_gb)
        except OSError:
            pass


_log_shm_info()

_SENSITIVE_PATTERNS = re.compile(
    r"/(?:home|usr|tmp|etc|var|dev|root|proc|sys)/\S+"
    r"|[A-Z_]{2,}=\S+"
    r"|\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"
    r"|\b[0-9a-fA-F]{32,}\b"
    r"|(?:cookie|token|secret|password|credential)\S*"
    , re.IGNORECASE,
)


def _sanitize_error_line(raw: str) -> str:
    line = raw.strip()
    if not line:
        return "Download failed."
    if line.upper().startswith("ERROR:"):
        line = line[6:].strip()
    if not line:
        return "Download failed."
    cleaned = _SENSITIVE_PATTERNS.sub("[redacted]", line)
    if cleaned.strip() == "[redacted]" or not cleaned.strip():
        return "Download failed."
    return cleaned


def user_facing_download_error(output: str, *, media: str = "video") -> str | None:
    lowered = output.lower()
    if "cookies" in lowered and (
        "expired" in lowered
        or "invalid" in lowered
        or "unauthorized" in lowered
        or "login" in lowered
    ):
        return "Login cookies look expired or invalid. Refresh cookies.txt and try again."
    if "sign in to confirm" in lowered or "confirm you're not a bot" in lowered:
        return "The site is asking for sign-in or bot verification. Cookies may need to be refreshed."
    if "private" in lowered or "login required" in lowered or "this post is private" in lowered:
        return f"That {media} appears to be private or login-only."
    if "empty media response" in lowered:
        return "Link is unavailable. The account may be banned, private, restricted, or the post may have been deleted or hidden."
    if "http error 403" in lowered:
        return "Access denied (403). Cookies may be missing, expired, or not allowed to view this post."
    if "http error 404" in lowered or "not found" in lowered:
        return f"{media.capitalize()} not found or no longer available."
    if "unsupported url" in lowered:
        return "Unsupported, private, or unavailable URL."
    if "bad guest token" in lowered or ("querying api" in lowered and "twitter" in lowered):
        return "Twitter/X download failed (API error). Try again in a moment."
    if "ip address is unable to access" in lowered:
        return "Reddit API blocked the request. The post may be deleted or Reddit is restricting access."
    if "please update" in lowered and "yt-dlp" in lowered:
        return "yt-dlp looks stale for this site. Update yt-dlp and try again."
    return None


def user_facing_upload_error(error: Exception) -> str:
    text = str(error)
    lowered = text.lower()
    if "413" in lowered or "request entity too large" in lowered or "file" in lowered and "large" in lowered:
        return "Discord rejected the upload as too large after processing."
    if "missing access" in lowered or "forbidden" in lowered or "permission" in lowered:
        return "Discord rejected the upload because the bot is missing channel permissions."
    if "rate limit" in lowered:
        return "Discord rate-limited the upload. Try again in a moment."
    return f"Upload failed: {_sanitize_error_line(text)}"


def _sanitize_filename(name: str) -> str:
    if not name or not name.strip():
        return "video"
    name = name.replace("\x00", "").replace("/", "_").replace("\\", "_")
    parts = name.rsplit(".", 1)
    stem = parts[0].replace("..", "_")[:190]
    if stem.endswith("."):
        stem = f"{stem.rstrip('.')}_"[:190]
    ext = parts[1].replace("..", "_") if len(parts) > 1 else ""
    if ext:
        return f"{stem}.{ext[:10]}"
    return stem if stem else "video"


# ── Subprocess ────────────────────────────────────────────────────────────────

async def run_subprocess(
    cmd: list[str], timeout: int = SUBPROCESS_TIMEOUT, nice: bool = False,
) -> tuple[int, str]:
    if nice and PROCESS_NICE > 0:
        cmd = ["nice", "-n", str(PROCESS_NICE)] + cmd
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=ENV,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            output = ""
            return 124, output + "\n[ERROR] Subprocess timed out and did not exit cleanly."
        output = stdout[:MAX_SUBPROCESS_OUTPUT_BYTES].decode(errors="replace")
        return 124, output + "\n[ERROR] Subprocess timed out."
    return proc.returncode, stdout[:MAX_SUBPROCESS_OUTPUT_BYTES].decode(errors="replace")


async def run_subprocess_timeout(cmd: list[str], timeout: int) -> tuple[int, str]:
    try:
        return await run_subprocess(cmd, timeout)
    except TypeError as e:
        if "positional argument" not in str(e) and "unexpected keyword" not in str(e):
            raise
        return await run_subprocess(cmd)


# ── Instagram helpers ────────────────────────────────────────────────────────

def _is_instagram_image_entry(entry: dict) -> bool:
    entries = entry.get("entries")
    if isinstance(entries, list):
        has_image_child = False
        for child in entries:
            if child is None:
                continue
            if not isinstance(child, dict) or not _is_instagram_image_entry(child):
                return False
            has_image_child = True
        return has_image_child

    if entry.get("duration") is not None:
        return False

    ext = str(entry.get("ext") or "").lower()
    if ext in INSTAGRAM_IMAGE_EXTENSIONS:
        return True

    media_url = str(entry.get("url") or "").lower()
    path = urlparse(media_url).path
    if any(path.endswith(f".{ext}") for ext in INSTAGRAM_IMAGE_EXTENSIONS):
        return True

    return False


def _instagram_entry_has_video(entry: dict) -> bool:
    entries = entry.get("entries")
    if isinstance(entries, list):
        return any(
            isinstance(child, dict) and _instagram_entry_has_video(child)
            for child in entries
        )

    if entry.get("duration") is not None:
        return True

    ext = str(entry.get("ext") or "").lower()
    if ext in INSTAGRAM_VIDEO_EXTENSIONS:
        return True

    formats = entry.get("formats")
    if isinstance(formats, list):
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            vcodec = str(fmt.get("vcodec") or "").lower()
            if vcodec and vcodec != "none":
                return True
            format_ext = str(fmt.get("ext") or fmt.get("video_ext") or "").lower()
            if format_ext in INSTAGRAM_VIDEO_EXTENSIONS:
                return True
            format_url = str(fmt.get("url") or "").lower()
            format_path = urlparse(format_url).path
            if any(format_path.endswith(f".{ext}") for ext in INSTAGRAM_VIDEO_EXTENSIONS):
                return True

    media_url = str(entry.get("url") or "").lower()
    path = urlparse(media_url).path
    return any(path.endswith(f".{ext}") for ext in INSTAGRAM_VIDEO_EXTENSIONS)


def _instagram_video_playlist_index(metadata: dict) -> int | None:
    entries = metadata.get("entries")
    if not isinstance(entries, list):
        return None
    for index, entry in enumerate(entries, start=1):
        if isinstance(entry, dict) and _instagram_entry_has_video(entry):
            return index
    return None


def _is_supported_instagram_post_url(url: str) -> bool:
    if hostname_for(url) != "instagram.com":
        return False
    parts = [part for part in urlparse(url).path.split("/") if part]
    return len(parts) == 2 and parts[0] == "p" and bool(parts[1])


def _load_ytdlp_json(output: str) -> dict | None:
    json_start = output.find("{")
    if json_start < 0:
        return None
    try:
        metadata = json.loads(output[json_start:])
    except (ValueError, TypeError):
        return None
    return metadata if isinstance(metadata, dict) else None


async def instagram_post_probe(url: str) -> tuple[bool, int | None, str | None]:
    """Returns (is_image_only, playlist_index, unavailable_reason)."""
    if not _is_supported_instagram_post_url(url):
        return False, None, None
    cache_key = canonical_url_for_key(url)
    cached = _cache_get(_instagram_probe_cache, cache_key)
    if cached is not None:
        log.info("[instagram-probe] Cache hit for %s", cache_key)
        return cached

    cmd = [
        "yt-dlp",
        "--no-config",
        "--dump-single-json",
        "--ignore-no-formats",
        "--skip-download",
        "--extractor-retries",
        "0",
        "--user-agent",
        YT_DLP_UA,
    ]
    if COOKIES_EXIST:
        cmd.extend(["--cookies", COOKIES_FILE])
    cmd.append(url)

    _code, out = await run_subprocess(cmd)

    metadata = _load_ytdlp_json(out)
    if metadata is None:
        out_lower = out.lower()
        if any(phrase.lower() in out_lower for phrase in INSTAGRAM_UNAVAILABLE_PHRASES):
            reason = (
                "This post is unavailable. The account may be banned, private, "
                "restricted, or the post may have been deleted or hidden."
            )
            log.info("[instagram-probe] Post unavailable: %s", cache_key)
            result = (False, None, reason)
            _cache_set(_instagram_probe_cache, cache_key, result, INSTAGRAM_PROBE_CACHE_TTL)
            return result
        return False, None, None

    playlist_index = _instagram_video_playlist_index(metadata)
    if playlist_index is not None or _instagram_entry_has_video(metadata):
        result = (False, playlist_index, None)
        _cache_set(_instagram_probe_cache, cache_key, result, INSTAGRAM_PROBE_CACHE_TTL)
        return result

    if _is_instagram_image_entry(metadata):
        result = (True, None, None)
        _cache_set(_instagram_probe_cache, cache_key, result, INSTAGRAM_PROBE_CACHE_TTL)
        return result

    embeddable = await _kkinstagram_is_embeddable(url)
    if embeddable:
        log.info("[instagram-probe] kkinstagram confirms embeddable image post: %s", cache_key)
        result = (True, None, None)
        _cache_set(_instagram_probe_cache, cache_key, result, INSTAGRAM_PROBE_CACHE_TTL)
        return result

    reason = (
        "This post is unavailable. The account may be banned, private, "
        "restricted, or the post may have been deleted or hidden."
    )
    log.info("[instagram-probe] Not embeddable, treating as unavailable: %s", cache_key)
    result = (False, None, reason)
    _cache_set(_instagram_probe_cache, cache_key, result, INSTAGRAM_PROBE_CACHE_TTL)
    return result


async def _kkinstagram_is_embeddable(url: str) -> bool:
    kk_url = replace_hostname(url, "www.kkinstagram.com")
    try:
        session = _get_http_session()
        async with session.get(
            kk_url,
            timeout=aiohttp.ClientTimeout(total=KKINSTAGRAM_PROBE_TIMEOUT),
            headers={"User-Agent": KKINSTAGRAM_DISCORD_UA},
            allow_redirects=True,
        ) as resp:
            content_type = (resp.content_type or "").lower()
            if "image" in content_type or "video" in content_type:
                log.info("[kkinstagram-probe] Media response (%s) for %s", content_type, url)
                return True
            log.info("[kkinstagram-probe] Non-media response (%s, %d) for %s", content_type, resp.status, url)
            return False
    except Exception as e:
        log.warning("[kkinstagram-probe] Probe failed (%s) for %s, assuming embeddable.", e, url)
        return True


async def instagram_is_image_post(url: str) -> bool:
    image_only, _, _ = await instagram_post_probe(url)
    return image_only


# ── Twitter/X helpers ────────────────────────────────────────────────────────

_TWITTER_STATUS_RE = re.compile(r"(?:twitter\.com|x\.com)/[^/]+/status/(\d+)")


async def twitter_has_video(url: str) -> bool:
    m = _TWITTER_STATUS_RE.search(url)
    if not m:
        return True
    tweet_id = m.group(1)
    cached = _cache_get(_twitter_probe_cache, tweet_id)
    if cached is not None:
        log.info("[twitter-probe] Cache hit for %s", tweet_id)
        return cached
    try:
        session = _get_http_session()
        api_url = f"https://api.fxtwitter.com/status/{tweet_id}"
        async with session.get(
            api_url,
            timeout=aiohttp.ClientTimeout(total=FXTWITTER_API_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                log.warning("[twitter-probe] fxtwitter API returned %d for %s", resp.status, tweet_id)
                _cache_set(_twitter_probe_cache, tweet_id, True, PROBE_FAILURE_CACHE_TTL)
                return True
            body = await read_limited_response(resp, MAX_HTTP_RESPONSE_BYTES)
            data = json.loads(body.decode(errors="replace"))
        tweet = data.get("tweet")
        if not tweet:
            _cache_set(_twitter_probe_cache, tweet_id, True, PROBE_FAILURE_CACHE_TTL)
            return True
        media = tweet.get("media") or {}
        has_vid = bool(media.get("videos"))
        log.info("[twitter-probe] tweet %s has_video=%s", tweet_id, has_vid)
        _cache_set(_twitter_probe_cache, tweet_id, has_vid, TWITTER_PROBE_CACHE_TTL)
        return has_vid
    except Exception as e:
        log.warning("[twitter-probe] Probe failed (%s) — letting yt-dlp try anyway.", e)
        _cache_set(_twitter_probe_cache, tweet_id, True, PROBE_FAILURE_CACHE_TTL)
        return True


# ── Reddit helpers ────────────────────────────────────────────────────────────

async def _reddit_shortlink_location(url: str) -> str | None:
    """
    Resolve a Reddit /s/ shortlink by sending a GET request and reading the
    Location header from the first redirect response — without following it
    or downloading any body.

    Reddit 403s HEAD requests from server IPs, but responds to GET with a
    3xx redirect before any auth check kicks in. We disable redirect-following
    so we capture that first Location header and release the connection
    immediately.
    """
    headers = {
        "User-Agent": REDDIT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    # Force https://www.reddit.com so the single non-followed redirect lands on
    # /comments/ rather than Reddit's scheme/host canonicalization hop.
    parsed = urlparse(url)
    target = urlunparse(("https", "www.reddit.com", parsed.path, parsed.params, parsed.query, ""))
    try:
        session = _get_http_session()
        async with session.get(
            target,
            headers=headers,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            await resp.read()
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if location and "/comments/" in location:
                    if location.startswith("/"):
                        location = f"https://www.reddit.com{location}"
                    return location
            log.warning(
                "[reddit-short] GET returned %d — no usable Location header.",
                resp.status,
            )
    except Exception as e:
        log.warning("[reddit-short] GET request failed: %s", e)
    return None


def reddit_api_url(url: str) -> str:
    parsed = urlparse(url.rstrip("/").split("?")[0])
    path = parsed.path.rstrip("/") + "/.json"
    return urlunparse(("https", "www.reddit.com", path, "", "limit=1", ""))


async def resolve_reddit_shortlink(url: str) -> str:
    """Resolve a Reddit /s/ share shortlink to the real /comments/ URL."""
    if not REDDIT_SHORT_RE.search(url):
        return url

    cached = _cache_get(_reddit_shortlink_cache, url)
    if cached is not None:
        log.info("[reddit-short] Cache hit for %s", url)
        return cached

    log.info("[reddit-short] Resolving shortlink: %s", url)
    resolved = await _reddit_shortlink_location(url)
    if resolved:
        log.info("[reddit-short] Resolved to: %s", resolved)
        _cache_set(_reddit_shortlink_cache, url, resolved, SHORTLINK_CACHE_TTL)
        await _persist_cache_entry_async(url, resolved, "shortlink", SHORTLINK_CACHE_TTL)
        return resolved

    log.warning("[reddit-short] Could not resolve %s — passing to yt-dlp as-is.", url)
    return url


async def reddit_has_video(url: str) -> bool:
    if not REDDIT_POST_RE.search(url):
        return True
    api_url = reddit_api_url(url)
    cached = _cache_get(_reddit_has_video_cache, api_url)
    if cached is not None:
        log.info("[reddit-check] Cache hit for %s", api_url)
        return cached
    try:
        session = _get_http_session()
        async with session.get(
            api_url,
            headers=reddit_json_headers(),
            timeout=aiohttp.ClientTimeout(total=REDDIT_PRECHECK_TIMEOUT),
        ) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "json" not in content_type and "text" not in content_type:
                log.warning("[security] Reddit API returned unexpected Content-Type: %s", content_type)
                return True
            body = await read_limited_response(resp, MAX_HTTP_RESPONSE_BYTES)
            raw = body.decode(errors="replace")
        data = json.loads(raw)
        post = data[0]["data"]["children"][0]["data"]
        if post.get("is_video"):
            _cache_set(_reddit_has_video_cache, api_url, True, HAS_VIDEO_CACHE_TTL)
            await _persist_cache_entry_async(api_url, True, "has_video", HAS_VIDEO_CACHE_TTL)
            return True
        post_url = post.get("url", "")
        if host_matches(hostname_for(post_url), VIDEO_DOMAINS):
            _cache_set(_reddit_has_video_cache, api_url, True, HAS_VIDEO_CACHE_TTL)
            await _persist_cache_entry_async(api_url, True, "has_video", HAS_VIDEO_CACHE_TTL)
            return True
        if post.get("media") or post.get("secure_media"):
            _cache_set(_reddit_has_video_cache, api_url, True, HAS_VIDEO_CACHE_TTL)
            await _persist_cache_entry_async(api_url, True, "has_video", HAS_VIDEO_CACHE_TTL)
            return True
        log.info("[reddit-check] No video in post (url=%s)", post_url)
        _cache_set(_reddit_has_video_cache, api_url, False, HAS_VIDEO_CACHE_TTL)
        await _persist_cache_entry_async(api_url, False, "has_video", HAS_VIDEO_CACHE_TTL)
        return False
    except Exception as e:
        log.warning("[reddit-check] Pre-check failed (%s) — letting yt-dlp try anyway.", e)
        return True


async def resolve_arazu(url: str) -> str:
    if not host_matches(hostname_for(url), {"arazu.io"}):
        return url
    try:
        log.info("[arazu] Resolving: %s", url)
        session = _get_http_session()
        async with session.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; CoveBot/1.0)"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            body = await resp.content.read(MAX_HTTP_RESPONSE_BYTES)
            html = body.decode(errors="replace")
        match = REDDIT_RE.search(html)
        if match:
            reddit_url = match.group(1).replace("old.reddit.com", "www.reddit.com")
            log.info("[arazu] Resolved to: %s", reddit_url)
            return reddit_url
        log.warning("[arazu] Could not find Reddit link in page.")
    except Exception as e:
        log.warning("[arazu] Fetch error: %s", e)
    return url


# ── ffmpeg helpers ────────────────────────────────────────────────────────────

async def get_duration(filepath: str) -> float | None:
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", filepath]
    code, out = await run_subprocess(cmd)
    if code != 0:
        return None
    try:
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return None


async def get_media_info(filepath: str) -> dict | None:
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", filepath]
    code, out = await run_subprocess(cmd)
    if code != 0:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def duration_from_media_info(info: dict | None) -> float | None:
    if not info:
        return None
    try:
        duration = float(info.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def discord_mp4_compatibility(info: dict | None, filepath: str) -> tuple[bool, str]:
    if not info:
        return False, "ffprobe=unreadable"

    fmt = info.get("format") if isinstance(info, dict) else {}
    format_name = str(fmt.get("format_name") or "")
    streams = info.get("streams") if isinstance(info, dict) else []
    if not isinstance(streams, list):
        streams = []

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    video = video_streams[0] if video_streams else {}
    audio = audio_streams[0] if audio_streams else {}

    video_codec = str(video.get("codec_name") or "none")
    pix_fmt = str(video.get("pix_fmt") or "unknown")
    audio_codec = str(audio.get("codec_name") or "none")
    ext = Path(filepath).suffix.lower()
    summary = f"container={format_name} ext={ext} video={video_codec}/{pix_fmt} audio={audio_codec}"

    compatible = (
        ext == ".mp4"
        and "mp4" in format_name
        and video_codec == "h264"
        and pix_fmt == "yuv420p"
        and (not audio_streams or audio_codec == "aac")
    )
    return compatible, summary


async def remux_streamable_mp4(src: str, dest: str) -> tuple[bool, str]:
    cmd = [
        "ffmpeg", "-y",
        "-i", src,
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c", "copy",
        "-sn", "-dn",
        "-movflags", "+faststart",
        "-f", "mp4",
        dest,
    ]
    code, out = await run_subprocess(cmd, timeout=FFMPEG_TIMEOUT, nice=True)
    if code != 0:
        return False, out
    return True, ""


def ffmpeg_video_args(use_nvenc: bool, use_hevc: bool = False) -> list[str]:
    if use_nvenc:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq"]
    if use_hevc:
        log.warning("[ffmpeg] HEVC requested but Discord upload output is forced to H.264.")
    return ["-c:v", "libx264", "-preset", "veryfast"]


async def compress_to_target(
    src: str, dest: str, target_mb: float, duration: float | None = None,
) -> tuple[bool, str]:
    if duration is None:
        duration = await get_duration(src)
    if not duration or duration <= 0:
        return False, "Could not read video duration."

    audio_kbps = 96 if duration < 30 else AUDIO_KBPS
    target_kbits = target_mb * 8 * 1024 * 0.97
    audio_kbits  = audio_kbps * duration
    video_kbps   = max(250, int((target_kbits - audio_kbits) / duration))
    target_size  = int(target_mb * 1024 * 1024)

    source_mb = os.path.getsize(src) / (1024 * 1024)
    overshoot = source_mb / target_mb
    if overshoot > 4:
        initial_scale = max(0.5, target_mb / source_mb * 1.1)
    elif overshoot > 2.5:
        initial_scale = 0.76
    elif overshoot > 1.5:
        initial_scale = 0.88
    else:
        initial_scale = 1.0

    log.info(
        "[ffmpeg] Target=%sMB Duration=%.1fs Video=%sk Audio=%sk Scale=%.2f",
        target_mb, duration, video_kbps, audio_kbps, initial_scale,
    )

    def build_cmd(use_nvenc: bool, bitrate_kbps: int, use_hevc: bool = False) -> list[str]:
        cmd = ["ffmpeg", "-y"]
        if use_nvenc and USE_HWACCEL:
            cmd += ["-hwaccel", "cuda"]
        cmd += ["-i", src]
        cmd += ffmpeg_video_args(use_nvenc, use_hevc)
        if use_nvenc:
            cmd += ["-multipass", "fullres"]
        cmd += [
            "-map", "0:v:0",
            "-map", "0:a?",
            "-pix_fmt", "yuv420p",
            "-profile:v", "high",
            "-tag:v", "avc1",
            "-b:v", f"{bitrate_kbps}k",
            "-maxrate", f"{int(bitrate_kbps * 1.15)}k",
            "-bufsize", f"{int(bitrate_kbps * 2)}k",
            "-c:a", "aac",
            "-b:a", f"{audio_kbps}k",
            "-sn", "-dn",
            "-movflags", "+faststart",
            "-f", "mp4",
            dest,
        ]
        return cmd

    async with ENCODE_SEMAPHORE:
        encoder_attempts: list[tuple[bool, bool]] = []
        if USE_NVENC:
            if USE_HEVC:
                log.warning("[ffmpeg] USE_HEVC is ignored for Discord uploads; using H.264.")
            encoder_attempts.append((True, False))
        encoder_attempts.append((False, False))

        encoder_used = "libx264"
        last_error = ""
        for use_nvenc, use_hevc in encoder_attempts:
            if use_hevc:
                encoder_used = "hevc_nvenc" if use_nvenc else "libx265"
            else:
                encoder_used = "h264_nvenc" if use_nvenc else "libx264"

            base_scales = [1.0, 0.88, 0.76]
            scales = [s for s in base_scales if s <= initial_scale + 0.01]
            if not scales:
                scales = [initial_scale]
            scales[0] = min(scales[0], initial_scale)

            for scale in scales:
                attempt_kbps = max(250, int(video_kbps * scale))
                code, out = await run_subprocess(
                    build_cmd(use_nvenc, attempt_kbps, use_hevc),
                    timeout=FFMPEG_TIMEOUT,
                    nice=True,
                )
                if code != 0:
                    last_error = out
                    break

                final_size = os.path.getsize(dest)
                final_mb = final_size / (1024 * 1024)
                log.info(
                    "[ffmpeg] Encoder=%s Video=%sk Output=%.2f MB",
                    encoder_used, attempt_kbps, final_mb,
                )

                if final_size <= target_size:
                    return True, f"{final_mb:.2f} MB"

                last_error = (
                    f"Compressed file ({final_mb:.2f} MB) still exceeds "
                    f"the {target_mb} MB limit."
                )
                log.warning(
                    "[ffmpeg] Overshot target with %s at %sk: %.2f MB > %.2f MB",
                    encoder_used, attempt_kbps, final_mb, target_mb,
                )

            if use_nvenc:
                log.warning("[ffmpeg] NVENC failed or overshot; retrying with software encoder.")
                continue

            if code != 0:
                log.error("[ffmpeg ERROR]\n%s", last_error)

        if last_error:
            return False, last_error

        return False, "ffmpeg did not produce an output file."


async def convert_to_gif(
    src: str, dest: str, target_mb: float, max_duration: float = GIF_MAX_DURATION,
) -> tuple[bool, str]:
    duration = await get_duration(src)
    if not duration or duration <= 0:
        return False, "Could not read video duration."

    clip_duration = min(duration, max_duration)
    if target_mb < 15:
        clip_duration = min(clip_duration, 6.0)
    elif target_mb < 30:
        clip_duration = min(clip_duration, 8.0)

    target_size = int(target_mb * 1024 * 1024)
    palette_path = dest + ".palette.png"

    quality_levels = [
        (480, 15),
        (380, 12),
        (320, 10),
        (240, 8),
        (180, 6),
    ]
    if target_mb < 15:
        quality_levels = quality_levels[2:]
    elif target_mb < 30:
        quality_levels = quality_levels[1:]

    final_mb = 0.0
    async with ENCODE_SEMAPHORE:
        for width, fps in quality_levels:
            vf = f"fps={fps},scale={width}:-1:flags=lanczos"

            code, out = await run_subprocess([
                "ffmpeg", "-y", "-t", str(clip_duration), "-i", src,
                "-vf", f"{vf},palettegen=stats_mode=diff",
                palette_path,
            ], timeout=FFMPEG_TIMEOUT, nice=True)
            if code != 0:
                return False, out

            code, out = await run_subprocess([
                "ffmpeg", "-y", "-t", str(clip_duration), "-i", src,
                "-i", palette_path,
                "-filter_complex",
                f"[0:v] {vf} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5",
                dest,
            ], timeout=FFMPEG_TIMEOUT, nice=True)
            if code != 0:
                return False, out

            final_size = os.path.getsize(dest)
            final_mb = final_size / (1024 * 1024)
            log.info("[gif] %dp %dfps %.1fs → %.2f MB", width, fps, clip_duration, final_mb)

            if final_size <= target_size:
                try:
                    os.remove(palette_path)
                except OSError:
                    pass
                return True, f"{final_mb:.2f} MB ({clip_duration:.0f}s)"

            log.warning("[gif] Too large at %dp %dfps (%.2fMB), trying lower quality", width, fps, final_mb)

    try:
        os.remove(palette_path)
    except OSError:
        pass
    return False, f"GIF too large even at lowest quality ({final_mb:.2f}MB > {target_mb}MB)."


# ── Download pipeline ─────────────────────────────────────────────────────────

async def download_and_compress(
    url: str,
    guild: discord.Guild | None,
    youtube_quality: str | None = None,
) -> tuple:
    _log = []
    timer = PipelineTimer("video")
    target_mb   = get_target_mb(guild)
    target_size = int(target_mb * 1024 * 1024)

    _log.append(f"[INFO] Boost tier: {guild.premium_tier if guild else 0} — limit: {target_mb}MB")

    url = resolve_fixup_url(url)
    url = await resolve_arazu(url)
    url = await resolve_reddit_shortlink(url)
    _log.append(f"[INFO] URL: {url}")
    timer.mark("resolve")

    is_reddit  = host_matches(hostname_for(url), {"reddit.com", "redd.it"})
    is_twitter = host_matches(hostname_for(url), TWITTER_DOMAINS)
    is_youtube = host_matches(hostname_for(url), {"youtube.com", "youtu.be"})
    is_instagram = host_matches(hostname_for(url), {"instagram.com"})
    is_reddit_short = REDDIT_SHORT_RE.search(url) is not None
    instagram_playlist_item = None

    if is_instagram:
        instagram_image_only, instagram_playlist_item, instagram_unavailable = await instagram_post_probe(url)
        timer.mark("instagram_probe")
        if instagram_unavailable:
            log.info("[cove] Instagram post unavailable: %s", url)
            _log.append(f"[ERROR] {instagram_unavailable}")
            return None, "\n".join(_log)
        if instagram_image_only:
            log.info("[cove] Instagram image/text post detected, sending kkinstagram rewrite.")
            _log.append(INSTAGRAM_IMAGE_MARKER)
            _log.append("[NOVIDEO]")
            return None, "\n".join(_log)

    if is_reddit:
        if is_reddit_short:
            vx_url = replace_hostname(url, "vxreddit.com")
            log.info("[cove] Reddit shortlink unresolved, sending vxreddit rewrite: %s", vx_url)
            _log.append(REDDIT_VXREDDIT_MARKER)
            _log.append(vx_url)
            _log.append("[NOVIDEO]")
            return None, "\n".join(_log)
        has_video = await reddit_has_video(url)
        timer.mark("reddit_probe")
        if not has_video:
            vx_url = replace_hostname(url, "vxreddit.com")
            log.info("[cove] Reddit non-video post detected, sending vxreddit rewrite: %s", vx_url)
            _log.append(REDDIT_VXREDDIT_MARKER)
            _log.append(vx_url)
            _log.append("[NOVIDEO]")
            return None, "\n".join(_log)

    if is_twitter:
        has_vid = await twitter_has_video(url)
        timer.mark("twitter_probe")
        if not has_vid:
            fx_url = replace_hostname(url, "fxtwitter.com")
            log.info("[cove] Twitter image-only post detected, sending fxtwitter rewrite.")
            _log.append(TWITTER_IMAGE_MARKER)
            _log.append(fx_url)
            _log.append("[NOVIDEO]")
            return None, "\n".join(_log)

    tmp = tempfile.mkdtemp(prefix="cove_", dir=TMP_BASE)
    os.chmod(tmp, 0o700)
    output_template = str(Path(tmp) / "%(title)s.%(ext)s")

    if FAST_SOURCE_MODE:
        log.info("[cove] Fast source mode enabled; applying site-specific fast selectors.")

    if is_reddit:
        fmt = FORMAT_REDDIT_FAST if FAST_SOURCE_MODE else FORMAT_REDDIT
    else:
        fmt = FORMAT_DEFAULT
        yt_fmt = youtube_quality_format(url, youtube_quality)
        if yt_fmt is not None:
            fmt = yt_fmt
            if youtube_quality is not None:
                _log.append(f"[INFO] YouTube resolution override: {youtube_quality}p")

    cmd = ["yt-dlp", "--no-config", "--user-agent", YT_DLP_UA]

    cmd += [
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-N", str(YT_DLP_FRAGMENTS),
        "--no-part",
        "--trim-filenames", "150",
        "--extractor-retries", "3" if is_youtube else "0",
        "--max-filesize", f"{MAX_FILESIZE_MB}M",
        "--match-filter", "!duration",
        "--match-filter", f"duration <= {MAX_DURATION_SECONDS}",
        "-o", output_template,
    ]

    if is_instagram and instagram_playlist_item is not None:
        cmd += ["--playlist-items", str(instagram_playlist_item)]
    else:
        cmd.append("--no-playlist")

    if should_use_aria2c(url):
        cmd += ["--downloader", "aria2c", "--downloader-args", "aria2c:-x16 -s16 -k1M"]
    elif is_reddit:
        cmd += ["--http-chunk-size", "10M"]

    if COOKIES_EXIST and not is_youtube:
        cmd.extend(["--cookies", COOKIES_FILE])
        _log.append("[INFO] Using cookies.")
    elif not is_youtube:
        _log.append("[WARN] No cookies.txt — some sites may fail.")

    if is_reddit:
        cmd.extend(["--impersonate", "chrome"])

    cmd.append(url)

    log.info("[yt-dlp] Running: %s", ' '.join(cmd))
    _log.append("[INFO] Downloading...")

    code, out = await run_subprocess_timeout(cmd, REDDIT_YTDLP_TIMEOUT if is_reddit else SUBPROCESS_TIMEOUT)
    timer.mark("download")

    if code != 0 and is_youtube and (
        "Sign in to confirm" in out or "confirm you're not a bot" in out.lower()
    ):
        log.info("[yt-dlp] YouTube bot detection - retrying in 8s...")
        _log.append("[INFO] YouTube bot check - retrying...")
        await asyncio.sleep(8)
        code, out = await run_subprocess_timeout(cmd, SUBPROCESS_TIMEOUT)
        timer.mark("download_retry")

    log.info("[yt-dlp] Exit code: %d", code)
    log.debug("[yt-dlp] Output:\n%s", out)
    _log.append(out.strip())

    if "does not pass filter" in out:
        log.info("[cove] Rejected by match-filter (>%dmin)", MAX_DURATION_SECONDS // 60)
        shutil.rmtree(tmp, ignore_errors=True)
        _log.append(f"[TOOBIG] >{MAX_DURATION_SECONDS // 60}min")
        return None, "\n".join(_log)
    if "larger than max-filesize" in out.lower() or "file is larger than max" in out.lower():
        log.info("[cove] Rejected by max-filesize (>%dMB)", MAX_FILESIZE_MB)
        shutil.rmtree(tmp, ignore_errors=True)
        _log.append(f"[TOOBIG] >{MAX_FILESIZE_MB}MB")
        return None, "\n".join(_log)
    if code == 0 and "HTTP Error 403" in out:
        log.warning("[cove] yt-dlp exited 0 but reported 403 — partial download, treating as failure.")
        shutil.rmtree(tmp, ignore_errors=True)
        _log.append("[ERROR] Access denied (403). Cookies may be needed or expired.")
        return None, "\n".join(_log)

    if code != 0:
        if is_youtube and (
            "Sign in to confirm" in out
            or "confirm you're not a bot" in out.lower()
        ):
            _log.append("[ERROR] YouTube bot detection triggered.")
        elif any(phrase.lower() in out.lower() for phrase in NO_VIDEO_PHRASES):
            if is_instagram:
                log.info("[cove] Instagram no-video response was unavailable, replying without rewrite.")
                _log.append(
                    "[ERROR] Link is unavailable. The account may be banned, private, restricted, "
                    "or the post may have been deleted or hidden."
                )
            else:
                log.info("[cove] No video / network issue — ignoring silently.")
                _log.append("[NOVIDEO]")
        elif is_reddit and "[generic]" in out and not is_reddit_short:
            reddit_gif_url = reddit_media_gif_url_from_text(out)
            if reddit_gif_url:
                log.info("[cove] Reddit post resolved to direct GIF, sending embed repost.")
                _log.append(REDDIT_GIF_MARKER)
                _log.append(reddit_gif_url)
            else:
                reddit_image_url_from_output = reddit_media_image_url_from_text(out)
                if reddit_image_url_from_output:
                    log.info("[cove] Reddit post resolved to direct image, sending file repost.")
                    _log.append(REDDIT_IMAGE_MARKER)
                    _log.append(reddit_image_url_from_output)
                else:
                    log.info("[cove] Reddit link-post (external, no video) — ignoring silently.")
            _log.append("[NOVIDEO]")
        elif "Unsupported URL" in out and is_reddit and any(
            p in unquote(out) for p in REDDIT_SILENT_URL_PATTERNS
        ):
            reddit_gif_url = reddit_media_gif_url_from_text(out)
            if reddit_gif_url:
                log.info("[cove] Reddit post resolved to direct GIF, sending embed repost.")
                _log.append(REDDIT_GIF_MARKER)
                _log.append(reddit_gif_url)
            else:
                reddit_image_url_from_output = reddit_media_image_url_from_text(out)
                if reddit_image_url_from_output:
                    log.info("[cove] Reddit post resolved to direct image, sending file repost.")
                    _log.append(REDDIT_IMAGE_MARKER)
                    _log.append(reddit_image_url_from_output)
                else:
                    log.info("[cove] Reddit GIF/image URL — ignoring silently.")
            _log.append("[NOVIDEO]")
        elif "Unsupported URL" in out and is_twitter:
            log.info("[cove] X/Twitter post has no downloadable video — ignoring silently.")
            _log.append("[NOVIDEO]")
        elif "empty media response" in out.lower():
            log.info("[cove] Instagram empty media response — account banned or post deleted.")
            _log.append("[ERROR] Instagram post is unavailable (private, restricted, deleted, or the account may be banned).")
        elif friendly := user_facing_download_error(out, media="video"):
            _log.append(f"[ERROR] {friendly}")
        elif "Unsupported URL" in out:
            _log.append("[ERROR] Unsupported or private URL.")
        elif "HTTP Error 403" in out:
            _log.append("[ERROR] Access denied (403). Cookies may be needed or expired.")
        elif "HTTP Error 404" in out:
            _log.append("[ERROR] Video not found (404).")
        else:
            raw_last = out.strip().splitlines()[-1] if out.strip() else ""
            _log.append(f"[ERROR] {_sanitize_error_line(raw_last)}")
        shutil.rmtree(tmp, ignore_errors=True)
        return None, "\n".join(_log)

    mp4_files = list(Path(tmp).glob("*.mp4"))
    if not mp4_files:
        log.warning("[cove] Temp dir contents: %s", [f.name for f in Path(tmp).glob("*")])
        _log.append("[ERROR] No MP4 file found after download.")
        shutil.rmtree(tmp, ignore_errors=True)
        return None, "\n".join(_log)

    src_path = str(mp4_files[0])
    safe_name = _sanitize_filename(mp4_files[0].name)
    if mp4_files[0].name != safe_name:
        safe_path = str(mp4_files[0].parent / safe_name)
        os.rename(src_path, safe_path)
        src_path = safe_path
    orig_mb  = os.path.getsize(src_path) / (1024 * 1024)
    _log.append(f"[INFO] Downloaded: {orig_mb:.1f} MB")
    log.info("[cove] Downloaded: %s (%.1f MB)", safe_name, orig_mb)

    media_info = await get_media_info(src_path)
    timer.mark("ffprobe")
    duration = duration_from_media_info(media_info)
    if duration and duration > MAX_DURATION_SECONDS:
        mins = int(duration // 60)
        secs = int(duration % 60)
        log.info("[cove] Rejected after download: %dm%ds", mins, secs)
        shutil.rmtree(tmp, ignore_errors=True)
        _log.append(f"[TOOBIG] {mins}m{secs}s")
        return None, "\n".join(_log)

    is_compatible, media_summary = discord_mp4_compatibility(media_info, src_path)
    log.info("[cove] Download media info: %s", media_summary)

    if os.path.getsize(src_path) <= target_size:
        streamable = str(Path(tmp) / "streamable.mp4")
        if is_compatible:
            ok, remux_log = await remux_streamable_mp4(src_path, streamable)
            timer.mark("remux")
            if ok and os.path.getsize(streamable) <= target_size:
                _log.append(f"[INFO] Under {target_mb}MB — remuxed for Discord inline playback. ({timer.elapsed_str()})")
                return streamable, "\n".join(_log)
            log.warning("[ffmpeg] Faststart remux failed or exceeded target: %s", remux_log[-1000:])
        _log.append(f"[INFO] Under {target_mb}MB — uploading as-is. ({timer.elapsed_str()})")
        return src_path, "\n".join(_log)

    compressed = str(Path(tmp) / "compressed.mp4")
    _log.append(f"[INFO] Compressing to \u2264{target_mb}MB...")
    ok, result = await compress_to_target(src_path, compressed, target_mb, duration=duration)
    timer.mark("compress")

    if ok:
        final_info = await get_media_info(compressed)
        timer.mark("final_ffprobe")
        _, final_summary = discord_mp4_compatibility(final_info, compressed)
        log.info("[cove] Compressed media info: %s", final_summary)
        _log.append(f"[OK] Final size: {result} ({timer.elapsed_str()})")
        return compressed, "\n".join(_log)
    else:
        if orig_mb > target_mb:
            _log.append(f"[ERROR] Compression failed, and original ({orig_mb:.1f}MB) is too big for Discord.")
            shutil.rmtree(tmp, ignore_errors=True)
            return None, "\n".join(_log)

        _log.append(f"[WARN] Compression failed, but original fits. Using original. ({timer.elapsed_str()})")
        return src_path, "\n".join(_log)


async def download_and_clip(
    url: str, guild: discord.Guild | None, start: float, end: float,
) -> tuple:
    _log = []
    timer = PipelineTimer("clip")
    target_mb   = get_target_mb(guild)
    target_size = int(target_mb * 1024 * 1024)
    clip_duration = end - start

    _log.append(f"[INFO] Clip: {start:.1f}s → {end:.1f}s ({clip_duration:.1f}s)")

    url = resolve_fixup_url(url)
    url = await resolve_arazu(url)
    url = await resolve_reddit_shortlink(url)
    _log.append(f"[INFO] URL: {url}")
    timer.mark("resolve")

    is_reddit  = host_matches(hostname_for(url), {"reddit.com", "redd.it"})
    is_twitter = host_matches(hostname_for(url), TWITTER_DOMAINS)

    if is_reddit:
        has_video = await reddit_has_video(url)
        timer.mark("reddit_probe")
        if not has_video:
            _log.append("[NOVIDEO]")
            return None, "\n".join(_log)

    tmp = tempfile.mkdtemp(prefix="cove_", dir=TMP_BASE)
    os.chmod(tmp, 0o700)
    output_template = str(Path(tmp) / "%(title)s.%(ext)s")

    if is_reddit:
        fmt = FORMAT_REDDIT_FAST if FAST_SOURCE_MODE else FORMAT_REDDIT
    else:
        fmt = FORMAT_DEFAULT
        yt_fmt = youtube_quality_format(url)
        if yt_fmt is not None:
            fmt = yt_fmt

    cmd = ["yt-dlp", "--no-config", "--user-agent", YT_DLP_UA]

    cmd += [
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-N", str(YT_DLP_FRAGMENTS),
        "--no-part",
        "--trim-filenames", "150",
        "--no-playlist",
        "--extractor-retries", "0",
        "--max-filesize", f"{MAX_FILESIZE_MB}M",
        "--download-sections", f"*{start}-{end}",
        "-o", output_template,
    ]

    if should_use_aria2c(url):
        cmd += ["--downloader", "aria2c", "--downloader-args", "aria2c:-x16 -s16 -k1M"]

    if COOKIES_EXIST:
        cmd.extend(["--cookies", COOKIES_FILE])

    if is_reddit:
        cmd.extend(["--impersonate", "chrome"])

    cmd.append(url)

    log.info("[yt-dlp clip] Running: %s", ' '.join(cmd))
    _log.append("[INFO] Downloading clip...")

    code, out = await run_subprocess(cmd)
    timer.mark("download")
    log.info("[yt-dlp clip] Exit code: %d", code)
    _log.append(out.strip())

    if code != 0:
        if any(phrase.lower() in out.lower() for phrase in NO_VIDEO_PHRASES):
            _log.append("[NOVIDEO]")
        elif "Unsupported URL" in out and is_reddit and any(p in out for p in REDDIT_SILENT_URL_PATTERNS):
            _log.append("[NOVIDEO]")
        elif "Unsupported URL" in out and is_twitter:
            _log.append("[NOVIDEO]")
        elif friendly := user_facing_download_error(out, media="video"):
            _log.append(f"[ERROR] {friendly}")
        elif "Unsupported URL" in out:
            _log.append("[ERROR] Unsupported or private URL.")
        elif "HTTP Error 403" in out:
            _log.append("[ERROR] Access denied (403). Cookies may be needed or expired.")
        elif "HTTP Error 404" in out:
            _log.append("[ERROR] Video not found (404).")
        else:
            raw_last = out.strip().splitlines()[-1] if out.strip() else ""
            _log.append(f"[ERROR] {_sanitize_error_line(raw_last)}")
        shutil.rmtree(tmp, ignore_errors=True)
        return None, "\n".join(_log)

    mp4_files = list(Path(tmp).glob("*.mp4"))
    if not mp4_files:
        _log.append("[ERROR] No MP4 file found after download.")
        shutil.rmtree(tmp, ignore_errors=True)
        return None, "\n".join(_log)

    src_path = str(mp4_files[0])
    safe_name = _sanitize_filename(mp4_files[0].name)
    if mp4_files[0].name != safe_name:
        safe_path = str(mp4_files[0].parent / safe_name)
        os.rename(src_path, safe_path)
        src_path = safe_path
    orig_mb = os.path.getsize(src_path) / (1024 * 1024)
    _log.append(f"[INFO] Clip downloaded: {orig_mb:.1f} MB")

    if os.path.getsize(src_path) <= target_size:
        _log.append(f"[INFO] Under {target_mb:.0f}MB — skipping compression. ({timer.elapsed_str()})")
        return src_path, "\n".join(_log)

    compressed = str(Path(tmp) / "compressed.mp4")
    _log.append(f"[INFO] Compressing to ≤{target_mb:.0f}MB...")
    ok, result = await compress_to_target(src_path, compressed, target_mb, duration=clip_duration)
    timer.mark("compress")

    if ok:
        _log.append(f"[OK] Final size: {result} ({timer.elapsed_str()})")
        return compressed, "\n".join(_log)

    if orig_mb > target_mb:
        _log.append(f"[ERROR] Clip ({orig_mb:.1f}MB) exceeds the {target_mb:.0f}MB limit even after compression.")
        shutil.rmtree(tmp, ignore_errors=True)
        return None, "\n".join(_log)

    _log.append(f"[WARN] Compression failed, but original fits. Using original. ({timer.elapsed_str()})")
    return src_path, "\n".join(_log)


async def download_and_gif(url: str, guild: discord.Guild | None) -> tuple:
    _log = []
    timer = PipelineTimer("gif")
    target_mb = get_target_mb(guild)

    url = resolve_fixup_url(url)
    url = await resolve_arazu(url)
    url = await resolve_reddit_shortlink(url)
    _log.append(f"[INFO] URL: {url}")
    timer.mark("resolve")

    is_reddit  = host_matches(hostname_for(url), {"reddit.com", "redd.it"})
    is_twitter = host_matches(hostname_for(url), TWITTER_DOMAINS)

    if is_reddit:
        has_video = await reddit_has_video(url)
        timer.mark("reddit_probe")
        if not has_video:
            _log.append("[NOVIDEO]")
            return None, "\n".join(_log)

    tmp = tempfile.mkdtemp(prefix="cove_", dir=TMP_BASE)
    os.chmod(tmp, 0o700)
    output_template = str(Path(tmp) / "%(title)s.%(ext)s")

    if is_reddit:
        fmt = FORMAT_REDDIT_FAST if FAST_SOURCE_MODE else FORMAT_REDDIT
    else:
        fmt = FORMAT_DEFAULT
        yt_fmt = youtube_quality_format(url)
        if yt_fmt is not None:
            fmt = yt_fmt

    cmd = ["yt-dlp", "--no-config", "--user-agent", YT_DLP_UA]

    cmd += [
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-N", str(YT_DLP_FRAGMENTS),
        "--no-part",
        "--trim-filenames", "150",
        "--no-playlist",
        "--extractor-retries", "0",
        "--max-filesize", f"{MAX_FILESIZE_MB}M",
        "--match-filter", "!duration",
        "--match-filter", f"duration <= {MAX_DURATION_SECONDS}",
        "-o", output_template,
    ]

    if should_use_aria2c(url):
        cmd += ["--downloader", "aria2c", "--downloader-args", "aria2c:-x16 -s16 -k1M"]

    if COOKIES_EXIST:
        cmd.extend(["--cookies", COOKIES_FILE])

    if is_reddit:
        cmd.extend(["--impersonate", "chrome"])

    cmd.append(url)

    log.info("[yt-dlp gif] Running: %s", ' '.join(cmd))
    _log.append("[INFO] Downloading for GIF...")

    code, out = await run_subprocess(cmd)
    timer.mark("download")
    log.info("[yt-dlp gif] Exit code: %d", code)
    _log.append(out.strip())

    if "does not pass filter" in out:
        _log.append(f"[TOOBIG] >{MAX_DURATION_SECONDS // 60}min")
        shutil.rmtree(tmp, ignore_errors=True)
        return None, "\n".join(_log)

    if code != 0:
        if any(phrase.lower() in out.lower() for phrase in NO_VIDEO_PHRASES):
            _log.append("[NOVIDEO]")
        elif "Unsupported URL" in out and is_reddit and any(p in out for p in REDDIT_SILENT_URL_PATTERNS):
            _log.append("[NOVIDEO]")
        elif "Unsupported URL" in out and is_twitter:
            _log.append("[NOVIDEO]")
        elif friendly := user_facing_download_error(out, media="video"):
            _log.append(f"[ERROR] {friendly}")
        elif "Unsupported URL" in out:
            _log.append("[ERROR] Unsupported or private URL.")
        elif "HTTP Error 403" in out:
            _log.append("[ERROR] Access denied (403). Cookies may be needed or expired.")
        elif "HTTP Error 404" in out:
            _log.append("[ERROR] Video not found (404).")
        else:
            raw_last = out.strip().splitlines()[-1] if out.strip() else ""
            _log.append(f"[ERROR] {_sanitize_error_line(raw_last)}")
        shutil.rmtree(tmp, ignore_errors=True)
        return None, "\n".join(_log)

    mp4_files = list(Path(tmp).glob("*.mp4"))
    if not mp4_files:
        _log.append("[ERROR] No MP4 file found after download.")
        shutil.rmtree(tmp, ignore_errors=True)
        return None, "\n".join(_log)

    src_path = str(mp4_files[0])
    safe_name = _sanitize_filename(mp4_files[0].name)
    if mp4_files[0].name != safe_name:
        safe_path = str(mp4_files[0].parent / safe_name)
        os.rename(src_path, safe_path)
        src_path = safe_path

    gif_name = Path(safe_name).stem + ".gif"
    gif_path = str(Path(tmp) / gif_name)

    _log.append("[INFO] Converting to GIF...")
    ok, result = await convert_to_gif(src_path, gif_path, target_mb)
    timer.mark("convert")

    if ok:
        _log.append(f"[OK] GIF: {result} ({timer.elapsed_str()})")
        return gif_path, "\n".join(_log)

    _log.append(f"[ERROR] {result}")
    shutil.rmtree(tmp, ignore_errors=True)
    return None, "\n".join(_log)


async def download_audio(url: str, guild: discord.Guild | None) -> tuple:
    _log = []
    timer = PipelineTimer("audio")
    target_mb = get_target_mb(guild)
    target_size = int(target_mb * 1024 * 1024)

    _log.append(f"[INFO] Boost tier: {guild.premium_tier if guild else 0} — limit: {target_mb}MB")

    url = resolve_fixup_url(url)
    url = await resolve_arazu(url)
    url = await resolve_reddit_shortlink(url)
    _log.append(f"[INFO] URL: {url}")
    timer.mark("resolve")

    is_reddit  = host_matches(hostname_for(url), {"reddit.com", "redd.it"})
    is_twitter = host_matches(hostname_for(url), TWITTER_DOMAINS)
    is_youtube = host_matches(hostname_for(url), {"youtube.com", "youtu.be"})
    is_reddit_short = REDDIT_SHORT_RE.search(url) is not None

    if is_reddit:
        has_video = await reddit_has_video(url)
        timer.mark("reddit_probe")
        if not has_video:
            _log.append("[NOVIDEO]")
            return None, "\n".join(_log)

    tmp = tempfile.mkdtemp(prefix="cove_", dir=TMP_BASE)
    os.chmod(tmp, 0o700)
    keep_tmp = False
    try:
        output_template = str(Path(tmp) / "%(title)s.%(ext)s")

        cmd = ["yt-dlp", "--no-config", "--user-agent", YT_DLP_UA]

        cmd += [
            "-f", "bestaudio/best",
            "-x",
            "--audio-format", "mp3",
            "-N", str(YT_DLP_FRAGMENTS),
            "--no-part",
            "--trim-filenames", "150",
            "--no-playlist",
            "--extractor-retries", "0",
            "--max-filesize", f"{MAX_FILESIZE_MB}M",
            "--match-filter", "!duration",
            "--match-filter", f"duration <= {MAX_DURATION_SECONDS}",
            "-o", output_template,
        ]

        if should_use_aria2c(url):
            cmd += ["--downloader", "aria2c", "--downloader-args", "aria2c:-x16 -s16 -k1M"]

        if COOKIES_EXIST:
            cmd.extend(["--cookies", COOKIES_FILE])
            _log.append("[INFO] Using cookies.")
        else:
            _log.append("[WARN] No cookies.txt — some sites may fail.")

        if is_reddit:
            cmd.extend(["--impersonate", "chrome"])

        cmd.append(url)

        log.info("[yt-dlp audio] Running: %s", ' '.join(cmd))
        _log.append("[INFO] Downloading audio...")

        code, out = await run_subprocess(cmd)
        timer.mark("download")

        log.info("[yt-dlp audio] Exit code: %d", code)
        log.debug("[yt-dlp audio] Output:\n%s", out)
        _log.append(out.strip())

        if "does not pass filter" in out:
            log.info("[cove] Audio rejected by match-filter (>%dmin)", MAX_DURATION_SECONDS // 60)
            _log.append(f"[TOOBIG] >{MAX_DURATION_SECONDS // 60}min")
            return None, "\n".join(_log)
        if "larger than max-filesize" in out.lower() or "file is larger than max" in out.lower():
            log.info("[cove] Audio rejected by max-filesize (>%dMB)", MAX_FILESIZE_MB)
            _log.append(f"[TOOBIG] >{MAX_FILESIZE_MB}MB")
            return None, "\n".join(_log)

        if code != 0:
            if any(phrase.lower() in out.lower() for phrase in NO_VIDEO_PHRASES):
                log.info("[cove] No audio / network issue — ignoring silently.")
                _log.append("[NOVIDEO]")
            elif is_reddit and "[generic]" in out and not is_reddit_short:
                log.info("[cove] Reddit link-post (external, no audio) — ignoring silently.")
                _log.append("[NOVIDEO]")
            elif "Unsupported URL" in out and is_reddit and any(p in out for p in REDDIT_SILENT_URL_PATTERNS):
                log.info("[cove] Reddit GIF/image URL — ignoring silently.")
                _log.append("[NOVIDEO]")
            elif "Unsupported URL" in out and is_twitter:
                log.info("[cove] X/Twitter post has no downloadable audio — ignoring silently.")
                _log.append("[NOVIDEO]")
            elif friendly := user_facing_download_error(out, media="audio"):
                _log.append(f"[ERROR] {friendly}")
            elif is_youtube and (
                "Sign in to confirm" in out
                or "confirm you're not a bot" in out.lower()
            ):
                _log.append("[ERROR] YouTube bot detection triggered.")
            elif "empty media response" in out.lower():
                log.info("[cove] Instagram empty media response — account banned or post deleted.")
                _log.append("[ERROR] Instagram post is unavailable (private, restricted, deleted, or the account may be banned).")
            elif "Unsupported URL" in out:
                _log.append("[ERROR] Unsupported or private URL.")
            elif "HTTP Error 403" in out:
                _log.append("[ERROR] Access denied (403). Cookies may be needed or expired.")
            elif "HTTP Error 404" in out:
                _log.append("[ERROR] Video not found (404).")
            else:
                raw_last = out.strip().splitlines()[-1] if out.strip() else ""
                _log.append(f"[ERROR] {_sanitize_error_line(raw_last)}")
            return None, "\n".join(_log)

        mp3_files = list(Path(tmp).glob("*.mp3"))
        if not mp3_files:
            log.warning("[cove] Temp dir contents: %s", [f.name for f in Path(tmp).glob("*")])
            _log.append("[ERROR] No MP3 file found after download.")
            return None, "\n".join(_log)

        audio_path = str(mp3_files[0])
        safe_name = _sanitize_filename(mp3_files[0].name)
        if mp3_files[0].name != safe_name:
            safe_path = str(mp3_files[0].parent / safe_name)
            os.rename(audio_path, safe_path)
            audio_path = safe_path
        audio_mb = os.path.getsize(audio_path) / (1024 * 1024)
        _log.append(f"[INFO] Downloaded audio: {audio_mb:.1f} MB ({timer.elapsed_str()})")
        log.info("[cove] Downloaded audio: %s (%.1f MB)", safe_name, audio_mb)

        if os.path.getsize(audio_path) > target_size:
            _log.append(f"[TOOBIG] {audio_mb:.1f}MB")
            return None, "\n".join(_log)

        keep_tmp = True
        return audio_path, "\n".join(_log)
    finally:
        if not keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _cleanup_tmp_sync(filepath: str):
    try:
        parent = str(Path(filepath).parent)
        if "cove_" in parent:
            shutil.rmtree(parent, ignore_errors=True)
        else:
            os.remove(filepath)
    except Exception:
        pass


async def cleanup_tmp(filepath: str):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _cleanup_tmp_sync, filepath)


BUSY_MESSAGE = "Cove is busy processing other downloads. Try again in a moment."


def busy_message() -> str:
    running, waiting = _job_queue_status()
    return (
        f"{BUSY_MESSAGE} "
        f"Queue: {running}/{MAX_CONCURRENT_JOBS} running, {waiting}/{MAX_QUEUED_JOBS} waiting."
    )


async def process_url(
    url: str,
    guild: discord.Guild | None,
    on_success,
    on_error,
    on_too_big=None,
    on_no_video=None,
    youtube_quality: str | None = None,
):
    canonical = _inflight_key("video", url)
    if canonical in _inflight_urls:
        log.info("[dedup] Skipping already-in-flight URL: %s", url)
        if on_no_video:
            await on_no_video("")
        return
    _inflight_urls.add(canonical)
    reserved_slot = False
    try:
        if not _try_reserve_job_slot():
            await on_error(busy_message())
            return
        reserved_slot = True
        running, waiting = _job_queue_status()
        log.info("[queue] Accepted video job running=%d waiting=%d", running, waiting)
        async with JOB_SEMAPHORE:
            try:
                filepath, log_text = await download_and_compress(url, guild, youtube_quality)
            except Exception as e:
                log.exception("Unhandled exception during process_url")
                await on_error(f"Unexpected error: {e}")
                return

            novideo, toobig_str, error_str = _parse_log_markers(log_text)

            if novideo:
                if on_no_video:
                    await on_no_video(log_text)
                return

            if toobig_str:
                if on_too_big:
                    await on_too_big(toobig_str)
                else:
                    await on_error(f"Video too big {NYO_EMOJI} ({toobig_str}, max {MAX_DURATION_SECONDS // 60}min)")
                return

            if not filepath or not os.path.exists(filepath):
                msg = error_str or "Download failed."
                log.error("Download failed. Full log:\n%s", log_text)
                if error_str and "cookies" in error_str.lower():
                    spawn_tracked(_maybe_send_cookie_warning(client))
                await on_error(msg)
                return

            try:
                await on_success(filepath)
            except Exception as e:
                log.exception("Unhandled exception in on_success")
                await on_error(user_facing_upload_error(e))
            finally:
                await cleanup_tmp(filepath)
    finally:
        if reserved_slot:
            _release_job_slot()
        _inflight_urls.discard(canonical)


async def process_audio_url(
    url: str,
    guild: discord.Guild | None,
    on_success,
    on_error,
    on_too_big=None,
    on_no_video=None,
):
    canonical = _inflight_key("audio", url)
    if canonical in _inflight_urls:
        log.info("[dedup] Skipping already-in-flight audio URL: %s", url)
        if on_no_video:
            await on_no_video()
        return
    _inflight_urls.add(canonical)
    reserved_slot = False
    try:
        if not _try_reserve_job_slot():
            await on_error(BUSY_MESSAGE)
            return
        reserved_slot = True
        running, waiting = _job_queue_status()
        log.info("[queue] Accepted audio job running=%d waiting=%d", running, waiting)
        async with JOB_SEMAPHORE:
            try:
                filepath, log_text = await download_audio(url, guild)
            except Exception:
                log.exception("Unhandled exception during process_audio_url")
                await on_error("Audio download failed.")
                return

            novideo, toobig_str, error_str = _parse_log_markers(log_text)

            if novideo:
                if on_no_video:
                    await on_no_video()
                return

            if toobig_str:
                if on_too_big:
                    await on_too_big(toobig_str)
                else:
                    await on_error(f"Audio too big {NYO_EMOJI} ({toobig_str})")
                return

            if not filepath or not os.path.exists(filepath):
                msg = error_str or "Audio download failed."
                log.error("Audio download failed. Full log:\n%s", log_text)
                if error_str and "cookies" in error_str.lower():
                    spawn_tracked(_maybe_send_cookie_warning(client))
                await on_error(msg)
                return

            try:
                await on_success(filepath)
            except Exception as e:
                log.exception("Unhandled exception in audio on_success")
                await on_error(user_facing_upload_error(e))
            finally:
                await cleanup_tmp(filepath)
    finally:
        if reserved_slot:
            _release_job_slot()
        _inflight_urls.discard(canonical)


async def process_clip_url(
    url: str,
    guild: discord.Guild | None,
    start: float,
    end: float,
    on_success,
    on_error,
    on_no_video=None,
):
    canonical = _inflight_key("clip", f"{url}:{start}:{end}")
    if canonical in _inflight_urls:
        log.info("[dedup] Skipping already-in-flight clip URL: %s", url)
        if on_no_video:
            await on_no_video()
        return
    _inflight_urls.add(canonical)
    reserved_slot = False
    try:
        if not _try_reserve_job_slot():
            await on_error(BUSY_MESSAGE)
            return
        reserved_slot = True
        running, waiting = _job_queue_status()
        log.info("[queue] Accepted clip job running=%d waiting=%d", running, waiting)
        async with JOB_SEMAPHORE:
            try:
                filepath, log_text = await download_and_clip(url, guild, start, end)
            except Exception as e:
                log.exception("Unhandled exception during process_clip_url")
                await on_error(f"Unexpected error: {e}")
                return

            novideo, _, error_str = _parse_log_markers(log_text)

            if novideo:
                if on_no_video:
                    await on_no_video()
                return

            if not filepath or not os.path.exists(filepath):
                msg = error_str or "Clip failed."
                log.error("Clip failed. Full log:\n%s", log_text)
                if error_str and "cookies" in error_str.lower():
                    spawn_tracked(_maybe_send_cookie_warning(client))
                await on_error(msg)
                return

            try:
                await on_success(filepath)
            except Exception as e:
                log.exception("Unhandled exception in clip on_success")
                await on_error(user_facing_upload_error(e))
            finally:
                await cleanup_tmp(filepath)
    finally:
        if reserved_slot:
            _release_job_slot()
        _inflight_urls.discard(canonical)


async def process_gif_url(
    url: str,
    guild: discord.Guild | None,
    on_success,
    on_error,
    on_no_video=None,
):
    canonical = _inflight_key("gif", url)
    if canonical in _inflight_urls:
        log.info("[dedup] Skipping already-in-flight GIF URL: %s", url)
        if on_no_video:
            await on_no_video()
        return
    _inflight_urls.add(canonical)
    reserved_slot = False
    try:
        if not _try_reserve_job_slot():
            await on_error(BUSY_MESSAGE)
            return
        reserved_slot = True
        running, waiting = _job_queue_status()
        log.info("[queue] Accepted gif job running=%d waiting=%d", running, waiting)
        async with JOB_SEMAPHORE:
            try:
                filepath, log_text = await download_and_gif(url, guild)
            except Exception as e:
                log.exception("Unhandled exception during process_gif_url")
                await on_error(f"Unexpected error: {e}")
                return

            novideo, toobig_str, error_str = _parse_log_markers(log_text)

            if novideo:
                if on_no_video:
                    await on_no_video()
                return

            if toobig_str:
                await on_error(f"Source video too long (max {MAX_DURATION_SECONDS // 60}min)")
                return

            if not filepath or not os.path.exists(filepath):
                msg = error_str or "GIF conversion failed."
                log.error("GIF failed. Full log:\n%s", log_text)
                if error_str and "cookies" in error_str.lower():
                    spawn_tracked(_maybe_send_cookie_warning(client))
                await on_error(msg)
                return

            try:
                await on_success(filepath)
            except Exception as e:
                log.exception("Unhandled exception in gif on_success")
                await on_error(user_facing_upload_error(e))
            finally:
                await cleanup_tmp(filepath)
    finally:
        if reserved_slot:
            _release_job_slot()
        _inflight_urls.discard(canonical)


async def _maybe_send_cookie_warning(bot_client: discord.Client) -> None:
    global _cookie_warning_sent_at
    now = monotonic()
    if now - _cookie_warning_sent_at < COOKIE_WARNING_COOLDOWN:
        return
    _cookie_warning_sent_at = now
    guild = bot_client.get_guild(GUILD_ID)
    if not guild:
        return
    owner = guild.owner if guild.owner else None
    if owner is None and guild.owner_id:
        try:
            owner = await guild.fetch_member(guild.owner_id)
        except discord.HTTPException:
            owner = None
    if owner is None:
        return
    try:
        await owner.send(
            "Cove warning: cookies.txt appears expired or invalid. "
            "Downloads requiring authentication (Instagram, Reddit, etc.) "
            "will fail until cookies are refreshed."
        )
        log.info("[Cove] Sent cookie expiry warning DM to guild owner.")
    except discord.HTTPException as e:
        log.warning("[Cove] Could not DM cookie warning to guild owner: %s", e)


# ── Bot ───────────────────────────────────────────────────────────────────────

class CoveBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def _sync_tree_with_timeout(self, guild: discord.Object, label: str) -> None:
        try:
            await asyncio.wait_for(self.tree.sync(guild=guild), timeout=15)
            log.info("[Cove] %s slash commands synced to guild %d", label, guild.id)
        except TimeoutError:
            log.warning("[Cove] %s slash command sync timed out; continuing startup.", label)
        except Exception as e:
            log.warning("[Cove] %s slash command sync failed; continuing startup: %s", label, e)

    async def _periodic_temp_sweep(self):
        while not self.is_closed():
            await asyncio.sleep(300)
            try:
                await asyncio.get_running_loop().run_in_executor(None, _sweep_orphaned_tmpdirs)
            except Exception as e:
                log.warning("[Cove] Periodic temp sweep failed: %s", e)

    async def setup_hook(self):
        await asyncio.get_running_loop().run_in_executor(None, _sweep_orphaned_tmpdirs)
        asyncio.create_task(self._periodic_temp_sweep())
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self._sync_tree_with_timeout(guild, "Primary")
        if FRIEND_GUILD_ID != 0:
            friend_guild = discord.Object(id=FRIEND_GUILD_ID)
            await self._sync_tree_with_timeout(friend_guild, "Friend")

    async def on_ready(self):
        global _ytdlp_admin_warning_sent
        log.info("[Cove] Logged in as %s (ID: %d)", self.user, self.user.id)
        _get_http_session()
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="for links & /download",
            )
        )
        ytdlp_ok, ytdlp_message = _ytdlp_version_status
        if not ytdlp_ok and not _ytdlp_admin_warning_sent:
            guild = self.get_guild(GUILD_ID)
            owner = guild.owner if guild and guild.owner else None
            if owner is None and guild and guild.owner_id:
                try:
                    owner = await guild.fetch_member(guild.owner_id)
                except discord.HTTPException:
                    owner = None
            if owner is not None:
                try:
                    await owner.send(f"Cove warning: {ytdlp_message}")
                    _ytdlp_admin_warning_sent = True
                except discord.HTTPException as e:
                    log.warning("[Cove] Could not DM yt-dlp warning to guild owner: %s", e)

    async def close(self):
        await _close_http_session()
        await super().close()

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if is_friend_server(message.guild):
            prune_neet_skips()
        if is_friend_server(message.guild) and message.author.id in _friend_neet_skip_users:
            _friend_neet_skip_users.pop(message.author.id, None)
            log.info("[Cove] /neet skipped next friend message from %s", message.author)
            return

        if is_friend_server(message.guild) and message.reference and self.user:
            try:
                referenced = message.reference.resolved
                if referenced is None and message.reference.message_id:
                    referenced = await message.channel.fetch_message(message.reference.message_id)
                if isinstance(referenced, discord.Message) and referenced.author.id == self.user.id:
                    content = message.content.strip()
                    if content:
                        prune_friend_posts()
                        entry = _friend_posts.get(referenced.id)
                        if entry is None:
                            pass
                        else:
                            target_user_id, _ = entry
                            replier_name = message.author.display_name
                            await message.channel.send(
                                content=f"<@{target_user_id}> **{replier_name}**: {content}",
                                reference=referenced,
                                allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False),
                            )
                            try:
                                await message.delete()
                            except discord.HTTPException:
                                pass
                            return
            except discord.HTTPException:
                pass

        url = extract_supported_url(message.content)
        if not url:
            return

        if not _check_user_rate_limit(message.author.id):
            log.warning("[security] Rate limit hit for user %d in #%s", message.author.id, message.channel)
            return

        if message.guild and message.guild.me:
            perms_ok, missing = _check_bot_permissions(message.channel, message.guild.me)
            if not perms_ok:
                log.warning("[security] Missing permissions in #%s: %s", message.channel, missing)
                return

        if not mark_source_message_processing(message.id):
            log.info("[dedup] Skipping already-processed Discord message: %s", message.id)
            return

        log.info("[Cove] Auto-triggered by %s in #%s: %s", message.author, message.channel, url)

        try:
            await message.add_reaction("\u23f3")
        except discord.HTTPException:
            pass

        display_name   = message.author.display_name
        author_id      = message.author.id
        friend_mode    = is_friend_server(message.guild)
        extra_mentions = extract_extra_mentions(message.content)
        mention_names  = {user.id: user.display_name for user in message.mentions}

        async def on_success(filepath: str):
            try:
                await message.remove_reaction("\u23f3", self.user)
            except discord.HTTPException:
                pass

            if friend_mode:
                content = friend_target_post_content(extra_mentions, mention_names)
                if not content:
                    content = friend_post_content(display_name, extra_mentions)
                sent = await message.channel.send(
                    content=content,
                    file=discord.File(filepath),
                    allowed_mentions=discord.AllowedMentions(users=False, everyone=False, roles=False),
                )
                prune_friend_posts()
                _friend_posts[sent.id] = (author_id, monotonic() + FRIEND_POST_TTL_SECONDS)
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass

            else:
                content = f"<@{author_id}> posted:"
                if extra_mentions:
                    content += f" {extra_mentions}"

                sent = await message.channel.send(
                    content=content,
                    file=discord.File(filepath),
                    allowed_mentions=discord.AllowedMentions(users=False),
                )

                prune_deletable()
                _deletable[sent.id] = (author_id, monotonic() + DELETE_TTL_SECONDS)

                try:
                    await sent.add_reaction("\u274c")
                except discord.HTTPException:
                    pass

                if author_id in WHITELIST_IDS:
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        pass

        async def on_no_video(log_text: str = ""):
            try:
                await message.remove_reaction("\u23f3", self.user)
            except discord.HTTPException:
                pass
            if await send_instagram_image_rewrite(message, url, log_text):
                return
            fx_url = twitter_fxtwitter_url_from_log(log_text)
            if fx_url:
                try:
                    await message.channel.send(fx_url)
                except discord.HTTPException as e:
                    log.warning("[cove] Failed to send fxtwitter rewrite: %s", e)
                else:
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        pass
                return
            vx_url = reddit_vxreddit_url_from_log(log_text)
            if vx_url:
                try:
                    await message.channel.send(vx_url)
                except discord.HTTPException as e:
                    log.warning("[cove] Failed to send vxreddit rewrite: %s", e)
                else:
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        pass
                return
            reddit_gif_url = reddit_gif_url_from_log(log_text)
            if reddit_gif_url:
                await send_reddit_gif_repost(message, reddit_gif_url)
                return
            reddit_image_url_from_output = reddit_image_url_from_log(log_text)
            if reddit_image_url_from_output:
                if friend_mode:
                    img_content = friend_target_post_content(extra_mentions, mention_names)
                    if not img_content:
                        img_content = friend_post_content(display_name, extra_mentions)
                else:
                    img_content = f"<@{author_id}> posted:"
                    if extra_mentions:
                        img_content += f" {extra_mentions}"
                sent = await send_reddit_image_repost(message, reddit_image_url_from_output, img_content)
                if sent is not None:
                    if friend_mode:
                        prune_friend_posts()
                        _friend_posts[sent.id] = (author_id, monotonic() + FRIEND_POST_TTL_SECONDS)
                    else:
                        prune_deletable()
                        _deletable[sent.id] = (author_id, monotonic() + DELETE_TTL_SECONDS)
                        try:
                            await sent.add_reaction("❌")
                        except discord.HTTPException:
                            pass
                        if author_id in WHITELIST_IDS:
                            try:
                                await message.delete()
                            except discord.HTTPException:
                                pass

        async def on_too_big(duration_str: str):
            try:
                await message.remove_reaction("\u23f3", self.user)
            except discord.HTTPException:
                pass
            msg = f"Video too big {NYO_EMOJI} ({duration_str}, max {MAX_DURATION_SECONDS // 60}min)"
            try:
                await message.reply(msg, mention_author=False)
            except discord.HTTPException:
                await message.channel.send(msg)

        async def on_error(msg: str):
            try:
                await message.remove_reaction("\u23f3", self.user)
            except discord.HTTPException:
                pass
            try:
                await message.reply(f"\u274c {msg}", mention_author=False)
            except discord.HTTPException:
                await message.channel.send(f"\u274c {msg}")

        spawn_tracked(
            process_url(url, message.guild, on_success, on_error, on_too_big, on_no_video)
        )

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Main server only: let the original poster delete the bot's video message via ❌."""
        if payload.user_id == self.user.id:
            return
        if str(payload.emoji) != "\u274c":
            return

        prune_deletable()
        entry = _deletable.get(payload.message_id)
        if entry is None:
            return

        original_poster_id, _ = entry

        if payload.user_id != original_poster_id:
            channel = self.get_channel(payload.channel_id)
            if channel:
                try:
                    msg = await channel.fetch_message(payload.message_id)
                    user = await self.fetch_user(payload.user_id)
                    await msg.remove_reaction("\u274c", user)
                except discord.HTTPException:
                    pass
            return

        channel = self.get_channel(payload.channel_id)
        if channel:
            try:
                msg = await channel.fetch_message(payload.message_id)
                await msg.delete()
                _deletable.pop(payload.message_id, None)
            except discord.HTTPException:
                pass


client = CoveBot()


@client.tree.command(
    name="download",
    description="Download and compress a video from any supported site",
)
@app_commands.describe(
    url="The video URL to download",
    resolution="YouTube resolution for this download only",
)
@app_commands.choices(resolution=[
    app_commands.Choice(name="360p", value="360"),
    app_commands.Choice(name="480p", value="480"),
    app_commands.Choice(name="720p", value="720"),
    app_commands.Choice(name="1080p", value="1080"),
    app_commands.Choice(name="1440p", value="1440"),
    app_commands.Choice(name="2160p", value="2160"),
])
async def download_cmd(
    interaction: discord.Interaction,
    url: str,
    resolution: app_commands.Choice[str] | None = None,
):
    ok, err = await validate_manual_url(url)
    if not ok:
        try:
            await interaction.response.send_message(f"\u274c {err}", ephemeral=True)
        except (discord.errors.NotFound, discord.errors.HTTPException):
            pass
        return

    if not _check_user_rate_limit(interaction.user.id):
        try:
            await interaction.response.send_message(
                "\u274c You're sending too many requests. Please wait a moment.",
                ephemeral=True,
            )
        except (discord.errors.NotFound, discord.errors.HTTPException):
            pass
        return

    try:
        await interaction.response.defer(thinking=True)
    except (discord.errors.NotFound, discord.errors.HTTPException):
        return

    friend_mode = is_friend_server(interaction.guild)
    poster_id   = interaction.user.id

    async def on_success(filepath: str):
        if friend_mode:
            sent = await interaction.followup.send(
                content=friend_post_content(interaction.user.display_name, ""),
                file=discord.File(filepath),
            )
            prune_friend_posts()
            _friend_posts[sent.id] = (poster_id, monotonic() + FRIEND_POST_TTL_SECONDS)
        else:
            await interaction.followup.send(file=discord.File(filepath))

    async def on_error(msg: str):
        await interaction.followup.send(f"\u274c {msg}")

    async def on_no_video(log_text: str = ""):
        fx_url = twitter_fxtwitter_url_from_log(log_text)
        if fx_url:
            await interaction.followup.send(fx_url)
            return
        vx_url = reddit_vxreddit_url_from_log(log_text)
        if vx_url:
            await interaction.followup.send(vx_url)
            return
        await interaction.followup.send("\u274c No video found at that link.")

    async def on_too_big(duration_str: str):
        await interaction.followup.send(
            f"Video too big {NYO_EMOJI} ({duration_str}, max {MAX_DURATION_SECONDS // 60}min)"
        )

    await process_url(
        url,
        interaction.guild,
        on_success,
        on_error,
        on_too_big=on_too_big,
        on_no_video=on_no_video,
        youtube_quality=resolution.value if resolution else None,
    )


@client.tree.command(
    name="audio",
    description="Download an MP3 audio file from any supported site",
)
@app_commands.describe(url="The video URL to extract audio from")
async def audio_cmd(interaction: discord.Interaction, url: str):
    ok, err = await validate_manual_url(url)
    if not ok:
        try:
            await interaction.response.send_message(f"\u274c {err}", ephemeral=True)
        except (discord.errors.NotFound, discord.errors.HTTPException):
            pass
        return

    if not _check_user_rate_limit(interaction.user.id):
        try:
            await interaction.response.send_message(
                "\u274c You're sending too many requests. Please wait a moment.",
                ephemeral=True,
            )
        except (discord.errors.NotFound, discord.errors.HTTPException):
            pass
        return

    try:
        await interaction.response.defer(thinking=True)
    except (discord.errors.NotFound, discord.errors.HTTPException):
        return

    friend_mode = is_friend_server(interaction.guild)
    poster_id   = interaction.user.id

    async def on_success(filepath: str):
        if friend_mode:
            sent = await interaction.followup.send(
                content=friend_post_content(interaction.user.display_name, ""),
                file=discord.File(filepath),
            )
            prune_friend_posts()
            _friend_posts[sent.id] = (poster_id, monotonic() + FRIEND_POST_TTL_SECONDS)
        else:
            await interaction.followup.send(file=discord.File(filepath))

    async def on_error(msg: str):
        await interaction.followup.send(f"\u274c {msg}")

    async def on_no_video():
        await interaction.followup.send("\u274c No audio found at that link.")

    async def on_too_big(size_str: str):
        await interaction.followup.send(
            f"Audio too big {NYO_EMOJI} ({size_str}, max {get_target_mb(interaction.guild)}MB)"
        )

    await process_audio_url(
        url,
        interaction.guild,
        on_success,
        on_error,
        on_too_big=on_too_big,
        on_no_video=on_no_video,
    )


@client.tree.command(
    name="clip",
    description="Download a specific time range from a video",
)
@app_commands.describe(
    url="The video URL to clip",
    start="Start time (e.g. 1:30, 90, 0:05)",
    end="End time (e.g. 2:00, 120, 0:30)",
)
async def clip_cmd(interaction: discord.Interaction, url: str, start: str, end: str):
    ok, err = await validate_manual_url(url)
    if not ok:
        try:
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        except (discord.errors.NotFound, discord.errors.HTTPException):
            pass
        return

    start_sec = parse_timestamp(start)
    end_sec = parse_timestamp(end)
    if start_sec is None or end_sec is None:
        try:
            await interaction.response.send_message(
                "❌ Invalid timestamp format. Use e.g. `1:30` or `90`.",
                ephemeral=True,
            )
        except (discord.errors.NotFound, discord.errors.HTTPException):
            pass
        return

    if start_sec >= end_sec:
        try:
            await interaction.response.send_message(
                "❌ Start time must be before end time.", ephemeral=True,
            )
        except (discord.errors.NotFound, discord.errors.HTTPException):
            pass
        return

    clip_duration = end_sec - start_sec
    if clip_duration > MAX_DURATION_SECONDS:
        try:
            await interaction.response.send_message(
                f"❌ Clip is too long ({clip_duration:.0f}s, max {MAX_DURATION_SECONDS // 60}min).",
                ephemeral=True,
            )
        except (discord.errors.NotFound, discord.errors.HTTPException):
            pass
        return

    if not _check_user_rate_limit(interaction.user.id):
        try:
            await interaction.response.send_message(
                "❌ You're sending too many requests. Please wait a moment.",
                ephemeral=True,
            )
        except (discord.errors.NotFound, discord.errors.HTTPException):
            pass
        return

    try:
        await interaction.response.defer(thinking=True)
    except (discord.errors.NotFound, discord.errors.HTTPException):
        return

    friend_mode = is_friend_server(interaction.guild)
    poster_id   = interaction.user.id

    async def on_success(filepath: str):
        if friend_mode:
            sent = await interaction.followup.send(
                content=friend_post_content(interaction.user.display_name, ""),
                file=discord.File(filepath),
            )
            prune_friend_posts()
            _friend_posts[sent.id] = (poster_id, monotonic() + FRIEND_POST_TTL_SECONDS)
        else:
            await interaction.followup.send(file=discord.File(filepath))

    async def on_error(msg: str):
        await interaction.followup.send(f"❌ {msg}")

    async def on_no_video():
        await interaction.followup.send("❌ No video found at that link.")

    await process_clip_url(
        url,
        interaction.guild,
        start_sec,
        end_sec,
        on_success,
        on_error,
        on_no_video=on_no_video,
    )


@client.tree.command(
    name="gif",
    description="Convert a video to a high-quality GIF (max 10s)",
)
@app_commands.describe(url="The video URL to convert to GIF")
async def gif_cmd(interaction: discord.Interaction, url: str):
    ok, err = await validate_manual_url(url)
    if not ok:
        try:
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        except (discord.errors.NotFound, discord.errors.HTTPException):
            pass
        return

    if not _check_user_rate_limit(interaction.user.id):
        try:
            await interaction.response.send_message(
                "❌ You're sending too many requests. Please wait a moment.",
                ephemeral=True,
            )
        except (discord.errors.NotFound, discord.errors.HTTPException):
            pass
        return

    try:
        await interaction.response.defer(thinking=True)
    except (discord.errors.NotFound, discord.errors.HTTPException):
        return

    friend_mode = is_friend_server(interaction.guild)
    poster_id   = interaction.user.id

    async def on_success(filepath: str):
        if friend_mode:
            sent = await interaction.followup.send(
                content=friend_post_content(interaction.user.display_name, ""),
                file=discord.File(filepath),
            )
            prune_friend_posts()
            _friend_posts[sent.id] = (poster_id, monotonic() + FRIEND_POST_TTL_SECONDS)
        else:
            await interaction.followup.send(file=discord.File(filepath))

    async def on_error(msg: str):
        await interaction.followup.send(f"❌ {msg}")

    async def on_no_video():
        await interaction.followup.send("❌ No video found at that link.")

    await process_gif_url(
        url,
        interaction.guild,
        on_success,
        on_error,
        on_no_video=on_no_video,
    )


@client.tree.command(
    name="status",
    description="Show Cove queue status",
)
async def status_cmd(interaction: discord.Interaction):
    running, waiting = _job_queue_status()
    await interaction.response.send_message(
        (
            f"Running: **{running}/{MAX_CONCURRENT_JOBS}**\n"
            f"Waiting: **{waiting}/{MAX_QUEUED_JOBS}**"
        ),
        ephemeral=True,
    )


@client.tree.command(
    name="help",
    description="Show Cove commands",
)
async def help_cmd(interaction: discord.Interaction):
    commands = [
        "`/download url [resolution]` - download and compress a video",
        "`/audio url` - extract MP3 audio",
        "`/clip url start end` - download a clip",
        "`/gif url` - convert a short video to GIF",
        "`/status` - show queue status",
        "`/quality [resolution]` - admin YouTube default quality",
        "`/health` - admin runtime self-check",
    ]
    if FRIEND_GUILD_ID != 0 and is_friend_server(interaction.guild):
        commands.append("`/neet` - friend-server cooldown")
    await interaction.response.send_message("\n".join(commands), ephemeral=True)


@client.tree.command(
    name="health",
    description="Run an admin-only self-check for Cove dependencies and runtime state",
)
@app_commands.default_permissions(administrator=True)
@app_commands.check(is_admin_interaction)
async def health_cmd(interaction: discord.Interaction):
    if not ADMIN_HEALTH_COMMAND:
        await interaction.response.send_message("\u274c Health checks are disabled.", ephemeral=True)
        return
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except (discord.errors.NotFound, discord.errors.HTTPException):
        return
    report = await asyncio.to_thread(build_health_report)
    await interaction.followup.send(f"```text\n{report[:1800]}\n```", ephemeral=True)


@health_cmd.error
async def health_cmd_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        try:
            await interaction.response.send_message("\u274c Admins only.", ephemeral=True)
        except (discord.errors.NotFound, discord.errors.HTTPException):
            pass
        return
    raise error


@client.tree.command(
    name="quality",
    description="Set the YouTube download quality (360p-1080p)",
)
@app_commands.default_permissions(administrator=True)
@app_commands.check(is_admin_interaction)
@app_commands.describe(resolution="Resolution to use, or leave empty to see the current setting")
@app_commands.choices(resolution=[
    app_commands.Choice(name="360p", value="360"),
    app_commands.Choice(name="480p", value="480"),
    app_commands.Choice(name="720p", value="720"),
    app_commands.Choice(name="1080p", value="1080"),
])
async def quality_cmd(
    interaction: discord.Interaction,
    resolution: app_commands.Choice[str] | None = None,
):
    if resolution is None:
        await interaction.response.send_message(
            f"\U0001f3ac Current YouTube quality: **{get_youtube_quality()}p**.", ephemeral=True
        )
        return
    await asyncio.to_thread(set_youtube_quality, resolution.value)
    await interaction.response.send_message(
        f"\u2705 YouTube quality set to **{resolution.name}**.", ephemeral=True
    )


@quality_cmd.error
async def quality_cmd_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        try:
            await interaction.response.send_message("\u274c Admins only.", ephemeral=True)
        except (discord.errors.NotFound, discord.errors.HTTPException):
            pass
        return
    raise error


if FRIEND_GUILD_ID != 0:
    @client.tree.command(
        name="neet",
        description="Ignore your next message in the friend server",
        guild=discord.Object(id=FRIEND_GUILD_ID),
    )
    async def neet_cmd(interaction: discord.Interaction):
        if not is_friend_server(interaction.guild):
            await interaction.response.send_message(
                "\u274c This command is only available in the friend server.",
                ephemeral=True,
            )
            return

        prune_neet_skips()
        _friend_neet_skip_users[interaction.user.id] = monotonic() + NEET_TTL_SECONDS
        await interaction.response.send_message(
            "Got it. I will ignore your next message.",
            ephemeral=True,
        )


if __name__ == "__main__":
    client.run(TOKEN)
