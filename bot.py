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
import sys
import tempfile
import json
from pathlib import Path
from time import monotonic, time
from urllib.parse import urlparse, urlunparse
from dotenv import load_dotenv

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

AUDIO_KBPS           = 128
MAX_DURATION_SECONDS = 600
NYO_EMOJI            = "<:NYO:1312902725750624316>"

MAX_CONCURRENT_JOBS    = _require_int_env("MAX_CONCURRENT_JOBS", default="3")
SUBPROCESS_TIMEOUT     = _require_int_env("SUBPROCESS_TIMEOUT", default="900")
DELETE_TTL_SECONDS     = _require_int_env("DELETE_TTL_SECONDS", default="21600")
FRIEND_POST_TTL_SECONDS = _require_int_env("FRIEND_POST_TTL_SECONDS", default="86400")
YT_DLP_FRAGMENTS       = _require_int_env("YT_DLP_FRAGMENTS", default="4")
MAX_FILESIZE_MB        = _require_int_env("MAX_FILESIZE_MB", default="500")
MAX_URL_LENGTH         = _require_int_env("MAX_URL_LENGTH", allow_zero=False, default="2048")
NEET_TTL_SECONDS       = _require_int_env("NEET_TTL_SECONDS", allow_zero=False, default="600")
FAST_SOURCE_MODE       = _env_bool("FAST_SOURCE_MODE", "0")
USE_NVENC              = _env_bool("USE_NVENC", "0")

JOB_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

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

AUTO_DOWNLOAD_DOMAINS = {
    "twitter.com",
    "x.com",
    "reddit.com",
    "redd.it",
    "tiktok.com",
    "instagram.com",
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

_deletable: dict[int, tuple[int, float]] = {}
_friend_posts: dict[int, tuple[int, float]] = {}
_friend_neet_skip_users: dict[int, float] = {}
_active_tasks: set[asyncio.Task] = set()
_http_session: aiohttp.ClientSession | None = None

# (value, expires_at_monotonic) — TTL caches for Reddit pre-checks.
# Shortlinks resolve deterministically and never change; has_video can shift
# if a post is edited, so it gets a shorter TTL.
_reddit_shortlink_cache: dict[str, tuple[str, float]] = {}
_reddit_has_video_cache: dict[str, tuple[bool, float]] = {}
SHORTLINK_CACHE_TTL = 24 * 3600
HAS_VIDEO_CACHE_TTL = 3600
CACHE_MAX_ENTRIES = 512


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
        # Drop the oldest-expiring entries to keep the cache bounded.
        for stale_key in sorted(cache, key=lambda k: cache[k][1])[: CACHE_MAX_ENTRIES // 4]:
            cache.pop(stale_key, None)
    cache[key] = (value, monotonic() + ttl)


def _get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": REDDIT_UA},
        )
    return _http_session


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


def _sweep_orphaned_tmpdirs(min_age_seconds: float = 3600) -> None:
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


def extract_extra_mentions(content: str) -> str:
    return URL_RE.sub("", content).strip()


def extract_supported_url(content: str) -> str | None:
    for match in URL_RE.finditer(content):
        url = match.group(0).rstrip(").,>")
        if len(url) > MAX_URL_LENGTH:
            continue
        host = hostname_for(url)
        if host_matches(host, BLACKLISTED_DOMAINS):
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
        return False, "URL points to a non-public address."
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except socket.gaierror:
        return False, "Could not resolve URL host."
    for info in infos:
        if _is_internal_ip(info[4][0]):
            return False, "URL points to a non-public address."
    return True, ""


def is_friend_server(guild: discord.Guild | None) -> bool:
    return FRIEND_GUILD_ID != 0 and guild is not None and guild.id == FRIEND_GUILD_ID


def get_target_mb(guild: discord.Guild | None) -> float:
    if guild is None:
        return BOOST_TIER_LIMITS_MB[0]
    return BOOST_TIER_LIMITS_MB.get(guild.premium_tier, 9.5)


def clean_env():
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


ENV = clean_env()


# ── Subprocess ────────────────────────────────────────────────────────────────

async def run_subprocess(cmd: list[str], timeout: int = SUBPROCESS_TIMEOUT) -> tuple[int, str]:
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
        stdout, _ = await proc.communicate()
        output = stdout.decode(errors="replace")
        return 124, output + "\n[ERROR] Subprocess timed out."
    return proc.returncode, stdout.decode(errors="replace")


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
        return resolved

    log.warning("[reddit-short] Could not resolve %s — passing to yt-dlp as-is.", url)
    return url


async def reddit_has_video(url: str) -> bool:
    if not REDDIT_POST_RE.search(url):
        return True
    api_url = url.rstrip("/").split("?")[0] + ".json?limit=1"
    cached = _cache_get(_reddit_has_video_cache, api_url)
    if cached is not None:
        log.info("[reddit-check] Cache hit for %s", api_url)
        return cached
    try:
        session = _get_http_session()
        async with session.get(
            api_url,
            headers={"User-Agent": REDDIT_UA, "Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            raw = await resp.text(errors="replace")
        data = json.loads(raw)
        post = data[0]["data"]["children"][0]["data"]
        if post.get("is_video"):
            _cache_set(_reddit_has_video_cache, api_url, True, HAS_VIDEO_CACHE_TTL)
            return True
        post_url = post.get("url", "")
        if host_matches(hostname_for(post_url), VIDEO_DOMAINS):
            _cache_set(_reddit_has_video_cache, api_url, True, HAS_VIDEO_CACHE_TTL)
            return True
        if post.get("media") or post.get("secure_media"):
            _cache_set(_reddit_has_video_cache, api_url, True, HAS_VIDEO_CACHE_TTL)
            return True
        log.info("[reddit-check] No video in post (url=%s)", post_url)
        _cache_set(_reddit_has_video_cache, api_url, False, HAS_VIDEO_CACHE_TTL)
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
            html = await resp.text(errors="replace")
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


def ffmpeg_video_args(use_nvenc: bool) -> list[str]:
    if use_nvenc:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "hq"]
    return ["-c:v", "libx264", "-preset", "veryfast"]


async def compress_to_target(src: str, dest: str, target_mb: float) -> tuple[bool, str]:
    duration = await get_duration(src)
    if not duration or duration <= 0:
        return False, "Could not read video duration."

    target_kbits = target_mb * 8 * 1024 * 0.97
    audio_kbits  = AUDIO_KBPS * duration
    video_kbps   = max(250, int((target_kbits - audio_kbits) / duration))

    log.info(
        "[ffmpeg] Target=%sMB Duration=%.1fs Video=%sk Audio=%sk",
        target_mb, duration, video_kbps, AUDIO_KBPS,
    )

    def build_cmd(use_nvenc: bool) -> list[str]:
        return [
            "ffmpeg", "-y",
            "-i", src,
            *ffmpeg_video_args(use_nvenc),
            "-pix_fmt", "yuv420p",
            "-b:v", f"{video_kbps}k",
            "-maxrate", f"{int(video_kbps * 1.15)}k",
            "-bufsize", f"{int(video_kbps * 2)}k",
            "-c:a", "aac",
            "-b:a", f"{AUDIO_KBPS}k",
            "-movflags", "+faststart",
            dest,
        ]

    attempts = [USE_NVENC, False] if USE_NVENC else [False]
    encoder_used = "libx264"
    out = ""
    for use_nvenc in attempts:
        encoder_used = "h264_nvenc" if use_nvenc else "libx264"
        code, out = await run_subprocess(build_cmd(use_nvenc))
        if code == 0:
            break
        if use_nvenc:
            log.warning("[ffmpeg] NVENC failed; retrying with libx264.")
    else:
        log.error("[ffmpeg ERROR]\n%s", out)
        return False, out

    final_size  = os.path.getsize(dest)
    final_mb    = final_size / (1024 * 1024)
    target_size = int(target_mb * 1024 * 1024)
    log.info("[ffmpeg] Encoder=%s Output=%.2f MB", encoder_used, final_mb)

    if final_size > target_size:
        log.warning("[ffmpeg] Overshot target: %.2f MB > %.2f MB", final_mb, target_mb)
        return False, f"Compressed file ({final_mb:.2f} MB) still exceeds the {target_mb} MB limit."

    return True, f"{final_mb:.2f} MB"


# ── Download pipeline ─────────────────────────────────────────────────────────

async def download_and_compress(url: str, guild: discord.Guild | None) -> tuple:
    _log = []
    target_mb   = get_target_mb(guild)
    target_size = int(target_mb * 1024 * 1024)

    _log.append(f"[INFO] Boost tier: {guild.premium_tier if guild else 0} — limit: {target_mb}MB")

    url = resolve_fixup_url(url)
    url = await resolve_arazu(url)
    url = await resolve_reddit_shortlink(url)
    _log.append(f"[INFO] URL: {url}")

    is_reddit  = host_matches(hostname_for(url), {"reddit.com", "redd.it"})
    is_twitter = host_matches(hostname_for(url), TWITTER_DOMAINS)
    is_youtube = host_matches(hostname_for(url), {"youtube.com", "youtu.be"})
    is_reddit_short = REDDIT_SHORT_RE.search(url) is not None

    if is_reddit:
        has_video = await reddit_has_video(url)
        if not has_video:
            _log.append("[NOVIDEO]")
            return None, "\n".join(_log)

    tmp = tempfile.mkdtemp(prefix="cove_", dir=TMP_BASE)
    output_template = str(Path(tmp) / "%(title)s.%(ext)s")

    if FAST_SOURCE_MODE:
        log.info("[cove] Fast source mode enabled; preferring <=720p sources.")

    if is_reddit:
        fmt = FORMAT_REDDIT_FAST if FAST_SOURCE_MODE else FORMAT_REDDIT
    else:
        fmt = FORMAT_FAST if FAST_SOURCE_MODE else FORMAT_DEFAULT

    cmd = ["yt-dlp"]

    if not is_reddit:
        cmd += ["--user-agent", YT_DLP_UA]

    cmd += [
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-N", str(YT_DLP_FRAGMENTS),
        "--no-part",
        "--no-playlist",
        "--extractor-retries", "0",
        "--max-filesize", f"{MAX_FILESIZE_MB}M",
        "--match-filter", "!duration",
        "--match-filter", f"duration <= {MAX_DURATION_SECONDS}",
        "-o", output_template,
    ]

    if COOKIES_EXIST:
        cmd.extend(["--cookies", COOKIES_FILE])
        _log.append("[INFO] Using cookies.")
    else:
        _log.append("[WARN] No cookies.txt — some sites may fail.")

    cmd.append(url)

    log.info("[yt-dlp] Running: %s", ' '.join(cmd))
    _log.append("[INFO] Downloading...")

    code, out = await run_subprocess(cmd)

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

    if code != 0:
        if any(phrase.lower() in out.lower() for phrase in NO_VIDEO_PHRASES):
            log.info("[cove] No video / network issue — ignoring silently.")
            _log.append("[NOVIDEO]")
        elif is_reddit and "[generic]" in out and not is_reddit_short:
            log.info("[cove] Reddit link-post (external, no video) — ignoring silently.")
            _log.append("[NOVIDEO]")
        elif "Unsupported URL" in out and is_reddit and any(p in out for p in REDDIT_SILENT_URL_PATTERNS):
            log.info("[cove] Reddit GIF/image URL — ignoring silently.")
            _log.append("[NOVIDEO]")
        elif "Unsupported URL" in out and is_twitter:
            log.info("[cove] X/Twitter post has no downloadable video — ignoring silently.")
            _log.append("[NOVIDEO]")
        elif is_youtube and (
            "Sign in to confirm" in out
            or "confirm you're not a bot" in out.lower()
        ):
            _log.append("[ERROR] YouTube bot detection triggered.")
        elif "empty media response" in out.lower():
            log.info("[cove] Instagram empty media response — account banned or post deleted.")
            _log.append("[ERROR] Link is broken (the account may have been banned or the post was deleted).")
        elif "Unsupported URL" in out:
            _log.append("[ERROR] Unsupported or private URL.")
        elif "HTTP Error 403" in out:
            _log.append("[ERROR] Access denied (403). Cookies may be needed or expired.")
        elif "HTTP Error 404" in out:
            _log.append("[ERROR] Video not found (404).")
        else:
            last_error = out.strip().splitlines()[-1] if out.strip() else "Unknown error."
            _log.append(f"[ERROR] {last_error}")
        shutil.rmtree(tmp, ignore_errors=True)
        return None, "\n".join(_log)

    mp4_files = list(Path(tmp).glob("*.mp4"))
    if not mp4_files:
        log.warning("[cove] Temp dir contents: %s", [f.name for f in Path(tmp).glob("*")])
        _log.append("[ERROR] No MP4 file found after download.")
        shutil.rmtree(tmp, ignore_errors=True)
        return None, "\n".join(_log)

    src_path = str(mp4_files[0])
    orig_mb  = os.path.getsize(src_path) / (1024 * 1024)
    _log.append(f"[INFO] Downloaded: {orig_mb:.1f} MB")
    log.info("[cove] Downloaded: %s (%.1f MB)", mp4_files[0].name, orig_mb)

    duration = await get_duration(src_path)
    if duration and duration > MAX_DURATION_SECONDS:
        mins = int(duration // 60)
        secs = int(duration % 60)
        log.info("[cove] Rejected after download: %dm%ds", mins, secs)
        shutil.rmtree(tmp, ignore_errors=True)
        _log.append(f"[TOOBIG] {mins}m{secs}s")
        return None, "\n".join(_log)

    if os.path.getsize(src_path) <= target_size:
        _log.append(f"[INFO] Under {target_mb}MB — skipping compression.")
        return src_path, "\n".join(_log)

    compressed = str(Path(tmp) / "compressed.mp4")
    _log.append(f"[INFO] Compressing to \u2264{target_mb}MB...")
    ok, result = await compress_to_target(src_path, compressed, target_mb)

    if ok:
        _log.append(f"[OK] Final size: {result}")
        return compressed, "\n".join(_log)
    else:
        if orig_mb > target_mb:
            _log.append(f"[ERROR] Compression failed, and original ({orig_mb:.1f}MB) is too big for Discord.")
            shutil.rmtree(tmp, ignore_errors=True)
            return None, "\n".join(_log)

        _log.append("[WARN] Compression failed, but original fits. Using original.")
        return src_path, "\n".join(_log)


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


async def process_url(
    url: str,
    guild: discord.Guild | None,
    on_success,
    on_error,
    on_too_big=None,
    on_no_video=None,
):
    async with JOB_SEMAPHORE:
        try:
            filepath, log_text = await download_and_compress(url, guild)
        except Exception as e:
            log.exception("Unhandled exception during process_url")
            await on_error(f"Unexpected error: {e}")
            return

        if any(line.strip() == "[NOVIDEO]" for line in log_text.splitlines()):
            if on_no_video:
                await on_no_video()
            return

        toobig_lines = [line for line in log_text.splitlines() if line.startswith("[TOOBIG]")]
        if toobig_lines:
            duration_str = toobig_lines[0].replace("[TOOBIG] ", "")
            if on_too_big:
                await on_too_big(duration_str)
            else:
                await on_error(f"Video too big {NYO_EMOJI} ({duration_str}, max {MAX_DURATION_SECONDS // 60}min)")
            return

        if not filepath or not os.path.exists(filepath):
            error_lines = [line for line in log_text.splitlines() if line.startswith("[ERROR]")]
            msg = error_lines[0].replace("[ERROR] ", "") if error_lines else "Download failed."
            log.error("Download failed. Full log:\n%s", log_text)
            await on_error(msg)
            return

        try:
            await on_success(filepath)
        except Exception as e:
            log.exception("Unhandled exception in on_success")
            await on_error(f"Upload failed: {e}")
        finally:
            await cleanup_tmp(filepath)


# ── Bot ───────────────────────────────────────────────────────────────────────

class CoveBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await asyncio.get_running_loop().run_in_executor(None, _sweep_orphaned_tmpdirs)
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("[Cove] Slash commands synced to guild %d", GUILD_ID)
        if FRIEND_GUILD_ID != 0:
            friend_guild = discord.Object(id=FRIEND_GUILD_ID)
            await self.tree.sync(guild=friend_guild)
            log.info("[Cove] Friend slash commands synced to guild %d", FRIEND_GUILD_ID)

    async def on_ready(self):
        log.info("[Cove] Logged in as %s (ID: %d)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="for links & /download",
            )
        )

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

        log.info("[Cove] Auto-triggered by %s in #%s: %s", message.author, message.channel, url)

        try:
            await message.add_reaction("\u23f3")
        except discord.HTTPException:
            pass

        display_name   = message.author.display_name
        author_id      = message.author.id
        friend_mode    = is_friend_server(message.guild)
        extra_mentions = extract_extra_mentions(message.content)

        async def on_success(filepath: str):
            try:
                await message.remove_reaction("\u23f3", self.user)
            except discord.HTTPException:
                pass

            if friend_mode:
                embed = discord.Embed()
                embed.set_author(
                    name=f"{display_name} posted:",
                    icon_url=message.author.display_avatar.url,
                )
                sent = await message.channel.send(
                    content=extra_mentions if extra_mentions else None,
                    embed=embed,
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

        async def on_no_video():
            try:
                await message.remove_reaction("\u23f3", self.user)
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
@app_commands.describe(url="The video URL to download")
async def download_cmd(interaction: discord.Interaction, url: str):
    ok, err = await validate_manual_url(url)
    if not ok:
        try:
            await interaction.response.send_message(f"\u274c {err}", ephemeral=True)
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
            embed = discord.Embed()
            embed.set_author(
                name=f"{interaction.user.display_name} posted:",
                icon_url=interaction.user.display_avatar.url,
            )
            sent = await interaction.followup.send(
                embed=embed,
                file=discord.File(filepath),
            )
            prune_friend_posts()
            _friend_posts[sent.id] = (poster_id, monotonic() + FRIEND_POST_TTL_SECONDS)
        else:
            await interaction.followup.send(file=discord.File(filepath))

    async def on_error(msg: str):
        await interaction.followup.send(f"\u274c {msg}")

    async def on_no_video():
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
    )


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


client.run(TOKEN)
