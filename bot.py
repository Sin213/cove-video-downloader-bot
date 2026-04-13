#!/usr/bin/env python3
from __future__ import annotations
import discord
from discord import app_commands
import asyncio
import os
import re
import shutil
import tempfile
import json
import traceback
import urllib.request
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TOKEN           = os.getenv("DISCORD_TOKEN")
GUILD_ID        = int(os.getenv("GUILD_ID", "0"))
FRIEND_GUILD_ID = int(os.getenv("FRIEND_GUILD_ID", "0"))

# Whitelist: comma-separated user IDs whose original message is deleted on the main server
_WHITELIST_RAW  = os.getenv("WHITELIST_USER_IDS", "")
WHITELIST_IDS  = {
    int(uid.strip())
    for uid in _WHITELIST_RAW.split(",")
    if uid.strip().isdigit()
}

COOKIES_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
COOKIES_EXIST = os.path.exists(COOKIES_FILE)

AUDIO_KBPS = 128
MAX_DURATION_SECONDS = 600  # 10 minutes — reject anything longer

NYO_EMOJI = "<:NYO:1312902725750624316>"

BOOST_TIER_LIMITS_MB = {
    0: 9.5,
    1: 9.5,
    2: 49.0,
    3: 99.0,
}

AUTO_DOWNLOAD_DOMAINS = (
    "twitter.com",
    "x.com",
    "reddit.com",
    "redd.it",
    "tiktok.com",
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "arazu.io",
    # Twitter/X embed-fixer proxies — rewritten to x.com before download
    "fixupx.com",
    "fxtwitter.com",
    "vxtwitter.com",
    "twittpr.com",
)

BLACKLISTED_DOMAINS = (
    "kkinstagram.com",
)

# Embed-fixer domains that should be silently rewritten to x.com
FIXUP_DOMAINS = (
    "fixupx.com",
    "fxtwitter.com",
    "vxtwitter.com",
    "twittpr.com",
)

# Phrases in yt-dlp output that mean there is simply no video — silently ignore
NO_VIDEO_PHRASES = (
    "No video could be found",
    "no video",
    "does not have a video",
    "no media",
    "HTTP Error 429",
    "Too Many Requests",
)

# yt-dlp "Unsupported URL" errors that are silently ignorable for Reddit
REDDIT_SILENT_URL_PATTERNS = (
    "i.redd.it",
    "reddit.com/media",
)

# Use RAM-backed tmpfs if available, otherwise fall back to /tmp
TMP_BASE = "/dev/shm" if os.path.isdir("/dev/shm") else None

URL_RE    = re.compile(r"https?://[^\s]+")
REDDIT_RE = re.compile(r'href="(https?://(?:old\.)?reddit\.com/r/[^/]+/comments/[^"]+)"')
REDDIT_POST_RE = re.compile(r'reddit\.com/r/[^/]+/comments/')

# VIDEO_DOMAINS: if a Reddit post links to one of these, treat it as downloadable
VIDEO_DOMAINS = (
    "v.redd.it",
    "youtube.com",
    "youtu.be",
    "streamable.com",
    "gfycat.com",
    "redgifs.com",
    "clips.twitch.tv",
    "twitch.tv",
    "vimeo.com",
)

# Realistic browser User-Agent for Reddit API requests
REDDIT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# In-memory map: bot_message_id -> original_poster_id
# Used for ❌ reaction-delete on the main server only
_deletable: dict[int, int] = {}


def is_friend_server(guild: discord.Guild | None) -> bool:
    """Returns True if the message is in the optional friend server."""
    return FRIEND_GUILD_ID != 0 and guild is not None and guild.id == FRIEND_GUILD_ID


def resolve_fixup_url(url: str) -> str:
    """Rewrite Twitter embed-fixer proxy domains to x.com so yt-dlp can handle them."""
    for domain in FIXUP_DOMAINS:
        if domain in url:
            rewritten = url.replace(domain, "x.com")
            print(f"[fixup] Rewrote {domain} -> x.com: {rewritten}")
            return rewritten
    return url


def extract_extra_mentions(content: str) -> str:
    """Strip the URL(s) from content and return whatever remains (e.g. extra @mentions)."""
    stripped = URL_RE.sub("", content).strip()
    return stripped


async def reddit_has_video(url: str) -> bool:
    """Check Reddit's JSON API to see if a post contains video before attempting yt-dlp."""
    if not REDDIT_POST_RE.search(url):
        return True

    api_url = url.rstrip("/").split("?")[0] + ".json?limit=1"
    try:
        req = urllib.request.Request(
            api_url,
            headers={
                "User-Agent": REDDIT_UA,
                "Accept": "application/json",
            },
        )
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(
            None,
            lambda: urllib.request.urlopen(req, timeout=8).read().decode(errors="replace"),
        )
        data = json.loads(raw)
        post = data[0]["data"]["children"][0]["data"]

        if post.get("is_video"):
            print("[reddit-check] Native Reddit video detected.")
            return True

        post_url = post.get("url", "")
        if any(domain in post_url for domain in VIDEO_DOMAINS):
            print(f"[reddit-check] External video link detected: {post_url}")
            return True

        media = post.get("media") or post.get("secure_media")
        if media:
            print("[reddit-check] Media embed detected.")
            return True

        print(f"[reddit-check] No video found in post (is_video=False, url={post_url})")
        return False

    except Exception as e:
        print(f"[reddit-check] Pre-check failed ({e}) — letting yt-dlp try anyway.")
        return True


async def resolve_arazu(url: str) -> str:
    if "arazu.io" not in url:
        return url
    try:
        print(f"[arazu] Resolving: {url}")
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; CoveBot/1.0)"},
        )
        loop = asyncio.get_event_loop()
        html = await loop.run_in_executor(
            None,
            lambda: urllib.request.urlopen(req, timeout=10).read().decode(errors="replace"),
        )
        match = REDDIT_RE.search(html)
        if match:
            reddit_url = match.group(1).replace("old.reddit.com", "www.reddit.com")
            print(f"[arazu] Resolved to: {reddit_url}")
            return reddit_url
        print("[arazu] Could not find Reddit link in page.")
    except Exception as e:
        print(f"[arazu] Fetch error: {e}")
    return url


def extract_supported_url(content: str) -> str | None:
    for match in URL_RE.finditer(content):
        url = match.group(0).rstrip(").,>")
        if any(domain in url for domain in BLACKLISTED_DOMAINS):
            return None
        if any(domain in url for domain in AUTO_DOWNLOAD_DOMAINS):
            return url
    return None


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


async def run_subprocess(cmd: list) -> tuple:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=ENV,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace")


async def get_duration(filepath: str) -> float | None:
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        filepath,
    ]
    code, out = await run_subprocess(cmd)
    if code != 0:
        return None
    try:
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return None


async def compress_to_target(src: str, dest: str, target_mb: float) -> tuple:
    duration = await get_duration(src)
    if not duration or duration <= 0:
        return False, "Could not read video duration."

    total_kbits = target_mb * 8 * 1024
    audio_kbits = AUDIO_KBPS * duration
    video_kbps  = max(100, int(((total_kbits - audio_kbits) / duration) * 0.92))

    print(f"[ffmpeg] Target: {target_mb}MB | Duration: {duration:.1f}s | Video: {video_kbps}kbps | Audio: {AUDIO_KBPS}kbps")

    cmd = [
        "ffmpeg", "-y",
        "-i", src,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-threads", "0",
        "-b:v", f"{video_kbps}k",
        "-c:a", "aac",
        "-b:a", f"{AUDIO_KBPS}k",
        "-movflags", "+faststart",
        dest,
    ]
    code, out = await run_subprocess(cmd)
    if code != 0:
        print(f"[ffmpeg ERROR]\n{out}")
        return False, out

    final_mb = os.path.getsize(dest) / (1024 * 1024)
    print(f"[ffmpeg] Output: {final_mb:.2f} MB")
    return True, f"{final_mb:.2f} MB"


async def download_and_compress(url: str, guild: discord.Guild | None) -> tuple:
    log = []
    target_mb   = get_target_mb(guild)
    target_size = int(target_mb * 1024 * 1024)

    log.append(f"[INFO] Boost tier: {guild.premium_tier if guild else 0} — limit: {target_mb}MB")

    # Rewrite embed-fixer proxy URLs before anything else
    url = resolve_fixup_url(url)
    url = await resolve_arazu(url)
    log.append(f"[INFO] URL: {url}")

    is_reddit = any(d in url for d in ("reddit.com", "redd.it"))
    if is_reddit:
        has_video = await reddit_has_video(url)
        if not has_video:
            log.append("[NOVIDEO]")
            return None, "\n".join(log)

    tmp = tempfile.mkdtemp(prefix="cove_", dir=TMP_BASE)
    output_template = str(Path(tmp) / "%(title)s.%(ext)s")

    cmd = [
        "yt-dlp",
        "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "--merge-output-format", "mp4",
        "-N", "16",
        "--no-part",
        "--no-check-certificates",
        "--no-playlist",
        "--extractor-retries", "0",
        "--sleep-requests", "2",
        "-o", output_template,
    ]

    if COOKIES_EXIST:
        cmd.extend(["--cookies", COOKIES_FILE])
        log.append("[INFO] Using cookies.")
    else:
        log.append("[WARN] No cookies.txt — some sites may fail.")

    cmd.append(url)

    print(f"[yt-dlp] Running: {' '.join(cmd)}")
    log.append("[INFO] Downloading...")

    code, out = await run_subprocess(cmd)

    print(f"[yt-dlp] Exit code: {code}")
    print(f"[yt-dlp] Output:\n{out}")
    log.append(out.strip())

    if code != 0:
        if any(phrase.lower() in out.lower() for phrase in NO_VIDEO_PHRASES):
            print(f"[cove] No video in post (or rate limited) — ignoring silently.")
            log.append("[NOVIDEO]")
        elif "Unsupported URL" in out and is_reddit and any(p in out for p in REDDIT_SILENT_URL_PATTERNS):
            print(f"[cove] Reddit GIF/image URL — ignoring silently.")
            log.append("[NOVIDEO]")
        elif "Sign in to confirm" in out or "bot" in out.lower():
            log.append("[ERROR] YouTube bot detection triggered.")
        elif "Unsupported URL" in out:
            log.append("[ERROR] Unsupported or private URL.")
        elif "HTTP Error 403" in out:
            log.append("[ERROR] Access denied (403). Cookies may be needed or expired.")
        elif "HTTP Error 404" in out:
            log.append("[ERROR] Video not found (404).")
        else:
            last_error = out.strip().splitlines()[-1] if out.strip() else "Unknown error."
            log.append(f"[ERROR] {last_error}")
        shutil.rmtree(tmp, ignore_errors=True)
        return None, "\n".join(log)

    mp4_files = list(Path(tmp).glob("*.mp4"))
    if not mp4_files:
        any_video = list(Path(tmp).glob("*"))
        print(f"[cove] Temp dir contents: {[f.name for f in any_video]}")
        log.append("[ERROR] No MP4 file found after download.")
        shutil.rmtree(tmp, ignore_errors=True)
        return None, "\n".join(log)

    src_path = str(mp4_files[0])
    orig_mb  = os.path.getsize(src_path) / (1024 * 1024)
    log.append(f"[INFO] Downloaded: {orig_mb:.1f} MB")
    print(f"[cove] Downloaded: {mp4_files[0].name} ({orig_mb:.1f} MB)")

    duration = await get_duration(src_path)
    if duration and duration > MAX_DURATION_SECONDS:
        mins = int(duration // 60)
        secs = int(duration % 60)
        print(f"[cove] Rejected after download: {mins}m{secs}s")
        shutil.rmtree(tmp, ignore_errors=True)
        log.append(f"[TOOBIG] {mins}m{secs}s")
        return None, "\n".join(log)

    if os.path.getsize(src_path) <= target_size:
        log.append(f"[INFO] Under {target_mb}MB — skipping compression.")
        return src_path, "\n".join(log)

    compressed = str(Path(tmp) / "compressed.mp4")
    log.append(f"[INFO] Compressing to \u2264{target_mb}MB...")
    ok, result = await compress_to_target(src_path, compressed, target_mb)

    if ok:
        log.append(f"[OK] Final size: {result}")
        return compressed, "\n".join(log)
    else:
        log.append(f"[WARN] Compression failed. Using original.")
        return src_path, "\n".join(log)


def cleanup_tmp(filepath: str):
    try:
        parent = str(Path(filepath).parent)
        if "cove_" in parent:
            shutil.rmtree(parent, ignore_errors=True)
        else:
            os.remove(filepath)
    except Exception:
        pass


async def process_url(url: str, guild: discord.Guild | None,
                      on_success, on_error, on_too_big=None, on_no_video=None):
    try:
        filepath, log = await download_and_compress(url, guild)
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[cove] UNHANDLED EXCEPTION:\n{tb}")
        await on_error(f"Unexpected error: {e}")
        return

    if any(l.strip() == "[NOVIDEO]" for l in log.splitlines()):
        if on_no_video:
            await on_no_video()
        return

    toobig_lines = [l for l in log.splitlines() if l.startswith("[TOOBIG]")]
    if toobig_lines:
        duration_str = toobig_lines[0].replace("[TOOBIG] ", "")
        if on_too_big:
            await on_too_big(duration_str)
        else:
            await on_error(f"Video too big {NYO_EMOJI} ({duration_str}, max {MAX_DURATION_SECONDS//60}min)")
        return

    if not filepath or not os.path.exists(filepath):
        error_lines = [l for l in log.splitlines() if l.startswith("[ERROR]")]
        msg = error_lines[-1].replace("[ERROR] ", "") if error_lines else "Download failed."
        print(f"[cove] Download failed. Full log:\n{log}")
        await on_error(msg)
        return

    try:
        await on_success(filepath)
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[cove] UNHANDLED EXCEPTION in on_success:\n{tb}")
        await on_error(f"Upload failed: {e}")
    finally:
        cleanup_tmp(filepath)


# ── Bot ────────────────────────────────────────────────────────────
class CoveBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print(f"[Cove] Slash commands synced to guild {GUILD_ID}")

    async def on_ready(self):
        print(f"[Cove] Logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="for links & /download"
            )
        )

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        url = extract_supported_url(message.content)
        if not url:
            return

        print(f"[Cove] Auto-triggered by {message.author} in #{message.channel}: {url}")

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
                # ── Friend server ──────────────────────────────────────────────
                content = f"<@{author_id}> posted:"
                if extra_mentions:
                    content += f" {extra_mentions}"

                await message.channel.send(
                    content=content,
                    file=discord.File(filepath),
                    allowed_mentions=discord.AllowedMentions(
                        users=True,
                        everyone=False,
                        roles=False,
                    ),
                )
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass

            else:
                # ── Main server ────────────────────────────────────────────────
                content = f"<@{author_id}> posted:"

                embed = discord.Embed()
                embed.set_author(
                    name=f"{display_name} posted:",
                    icon_url=message.author.display_avatar.url,
                )

                sent = await message.channel.send(
                    content=content,
                    embed=embed,
                    file=discord.File(filepath),
                    allowed_mentions=discord.AllowedMentions(users=False),
                )

                _deletable[sent.id] = author_id
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
            msg = f"Video too big {NYO_EMOJI} ({duration_str}, max {MAX_DURATION_SECONDS//60}min)"
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

        asyncio.create_task(
            process_url(url, message.guild, on_success, on_error, on_too_big, on_no_video)
        )

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Main server only: let the original poster delete the bot's video message via ❌."""
        if payload.user_id == self.user.id:
            return

        if str(payload.emoji) != "\u274c":
            return

        original_poster_id = _deletable.get(payload.message_id)
        if original_poster_id is None:
            return

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
    description="Download and compress a video from any supported site"
)
@app_commands.describe(url="The video URL to download")
async def download_cmd(interaction: discord.Interaction, url: str):
    try:
        await interaction.response.defer(thinking=True)
    except (discord.errors.NotFound, discord.errors.HTTPException):
        return

    async def on_success(filepath: str):
        await interaction.followup.send(file=discord.File(filepath))

    async def on_error(msg: str):
        await interaction.followup.send(f"\u274c {msg}")

    await process_url(url, interaction.guild, on_success, on_error)


client.run(TOKEN)
