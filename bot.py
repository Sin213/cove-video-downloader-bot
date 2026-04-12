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
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TOKEN    = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
COOKIES_EXIST = os.path.exists(COOKIES_FILE)  # check once at startup, not per-download

AUDIO_KBPS = 128

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
)

URL_RE = re.compile(r"https?://[^\s]+")


def extract_supported_url(content: str) -> str | None:
    for match in URL_RE.finditer(content):
        url = match.group(0).rstrip(").,>")
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


ENV = clean_env()  # build once at startup


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
    """
    Single-pass CBR encode with ultrafast preset + all threads.
    0.92 safety factor compensates for 1-pass size variance.
    """
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
        return False, out

    final_mb = os.path.getsize(dest) / (1024 * 1024)
    print(f"[ffmpeg] Output: {final_mb:.2f} MB")
    return True, f"{final_mb:.2f} MB"


async def download_and_compress(url: str, guild: discord.Guild | None) -> tuple:
    """
    Returns (filepath, log). filepath is None on failure.
    The returned file is inside a temp dir managed by this function —
    caller must delete it after use.
    """
    log = []
    target_mb   = get_target_mb(guild)
    target_size = int(target_mb * 1024 * 1024)

    log.append(f"[INFO] Boost tier: {guild.premium_tier if guild else 0} — limit: {target_mb}MB")

    # Use a persistent temp dir so we can return a path without copy2
    tmp = tempfile.mkdtemp(prefix="cove_")
    try:
        output_template = str(Path(tmp) / "%(title)s.%(ext)s")

        # ── Download ─────────────────────────────────────────────
        cmd = [
            "yt-dlp",
            "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            "--merge-output-format", "mp4",
            "-N", "16",                   # 16 concurrent fragment downloads
            "--http-chunk-size", "10M",   # force-split single-file URLs into 10MB chunks (IDM-style)
            "--no-part",                  # write directly, no .part rename
            "--no-check-certificates",    # skip TLS verify overhead
            "-o", output_template,
        ]

        if COOKIES_EXIST:
            cmd.extend(["--cookies", COOKIES_FILE])
            log.append("[INFO] Using cookies.")
        else:
            log.append("[WARN] No cookies.txt — some sites may fail.")

        cmd.append(url)

        log.append("[INFO] Downloading...")
        code, out = await run_subprocess(cmd)
        log.append(out.strip())

        if code != 0:
            if "Sign in to confirm" in out or "bot" in out.lower():
                log.append("[ERROR] YouTube bot detection triggered.")
            else:
                log.append("[ERROR] Download failed.")
            return None, "\n".join(log)

        mp4_files = list(Path(tmp).glob("*.mp4"))
        if not mp4_files:
            log.append("[ERROR] No MP4 file found after download.")
            return None, "\n".join(log)

        src_path = str(mp4_files[0])
        orig_mb  = os.path.getsize(src_path) / (1024 * 1024)
        log.append(f"[INFO] Downloaded: {orig_mb:.1f} MB")

        # ── Skip compression if already within limit ────────────────
        if os.path.getsize(src_path) <= target_size:
            log.append(f"[INFO] Under {target_mb}MB — skipping compression.")
            return src_path, "\n".join(log)  # return in-place, no copy

        # ── Compress (1-pass) ──────────────────────────────────
        compressed = str(Path(tmp) / "compressed.mp4")
        log.append(f"[INFO] Compressing to ≤{target_mb}MB...")
        ok, result = await compress_to_target(src_path, compressed, target_mb)

        if ok:
            log.append(f"[OK] Final size: {result}")
            return compressed, "\n".join(log)
        else:
            log.append(f"[WARN] Compression failed: {result}. Using original.")
            return src_path, "\n".join(log)

    except Exception as e:
        log.append(f"[ERROR] Unexpected error: {e}")
        shutil.rmtree(tmp, ignore_errors=True)
        return None, "\n".join(log)


def cleanup_tmp(filepath: str):
    """Remove the entire temp dir that filepath lives in."""
    try:
        parent = str(Path(filepath).parent)
        if "cove_" in parent:
            shutil.rmtree(parent, ignore_errors=True)
        else:
            os.remove(filepath)
    except Exception:
        pass


# ── Core send logic (shared by auto and slash) ───────────────────────
async def process_url(url: str, guild: discord.Guild | None,
                      on_success, on_error):
    """
    Download + compress url, then call on_success(filepath) or on_error(msg).
    Cleans up temp files after either callback.
    """
    filepath, log = await download_and_compress(url, guild)

    if not filepath or not os.path.exists(filepath):
        error_lines = [l for l in log.splitlines() if l.startswith("[ERROR]")]
        msg = error_lines[-1].replace("[ERROR] ", "") if error_lines else "Download failed."
        await on_error(msg)
        return

    try:
        await on_success(filepath)
    finally:
        cleanup_tmp(filepath)


# ── Bot ───────────────────────────────────────────────────────────────
class CoveBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
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

        # Suppress embed immediately and add reaction — do both concurrently
        await asyncio.gather(
            message.edit(suppress=True),
            message.add_reaction("⏳"),
            return_exceptions=True
        )

        async def on_success(filepath: str):
            await message.channel.send(
                content=f"📥 {message.author.mention}",
                file=discord.File(filepath)
            )
            await message.delete()

        async def on_error(msg: str):
            await asyncio.gather(
                message.edit(suppress=False),
                message.remove_reaction("⏳", self.user),
                return_exceptions=True
            )
            try:
                await message.reply(f"❌ {msg}", mention_author=False)
            except discord.HTTPException:
                await message.channel.send(f"❌ {msg}")

        # Fire and forget — each message is processed concurrently
        asyncio.create_task(process_url(url, message.guild, on_success, on_error))


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
        await interaction.followup.send(f"❌ {msg}")

    await process_url(url, interaction.guild, on_success, on_error)


client.run(TOKEN)
