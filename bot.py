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

# Cookies file for yt-dlp (export from browser via "Get cookies.txt LOCALLY")
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")

AUDIO_KBPS = 128  # reserved for audio track

# Discord upload limits by guild boost tier (leave headroom below hard cap)
BOOST_TIER_LIMITS_MB = {
    0: 9.5,   # No boosts    — 10 MB hard cap
    1: 9.5,   # Tier 1       — 10 MB hard cap
    2: 49.0,  # Tier 2       — 50 MB hard cap
    3: 99.0,  # Tier 3       — 100 MB hard cap
}

# Domains that trigger auto-download from messages
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

# Regex to extract URLs from message content
URL_RE = re.compile(r"https?://[^\s]+")


def extract_supported_url(content: str) -> str | None:
    """Return the first URL in content that matches a supported domain, or None."""
    for match in URL_RE.finditer(content):
        url = match.group(0).rstrip(").,>")
        if any(domain in url for domain in AUTO_DOWNLOAD_DOMAINS):
            return url
    return None


def get_target_mb(guild: discord.Guild | None) -> float:
    """Return the safe upload target in MB for the given guild's boost tier."""
    if guild is None:
        return BOOST_TIER_LIMITS_MB[0]
    return BOOST_TIER_LIMITS_MB.get(guild.premium_tier, 9.5)


def clean_env():
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


async def run_subprocess(cmd: list, env: dict) -> tuple:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace")


async def get_duration(filepath: str, env: dict) -> float:
    """Return video duration in seconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        filepath,
    ]
    code, out = await run_subprocess(cmd, env)
    if code != 0:
        return None
    try:
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return None


async def compress_to_target(src: str, dest: str, env: dict, target_mb: float) -> tuple:
    """
    Encode src to dest targeting just under the guild's upload limit using ffmpeg.
    Calculates exact video bitrate from duration so the result is
    always uploadable to Discord regardless of original quality.
    """
    duration = await get_duration(src, env)
    if not duration or duration <= 0:
        return False, "Could not read video duration."

    # Total budget in kilobits, minus audio
    total_kbits = target_mb * 8 * 1024
    audio_kbits = AUDIO_KBPS * duration
    video_kbits = total_kbits - audio_kbits
    video_kbps  = max(100, int(video_kbits / duration))  # floor at 100kbps

    print(f"[ffmpeg] Target: {target_mb}MB | Duration: {duration:.1f}s | Video bitrate: {video_kbps}kbps | Audio: {AUDIO_KBPS}kbps")

    cmd = [
        "ffmpeg", "-y",
        "-i", src,
        "-c:v", "libx264",
        "-b:v", f"{video_kbps}k",
        "-pass", "1",
        "-an",
        "-f", "null", "/dev/null",
    ]
    await run_subprocess(cmd, env)

    cmd2 = [
        "ffmpeg", "-y",
        "-i", src,
        "-c:v", "libx264",
        "-b:v", f"{video_kbps}k",
        "-pass", "2",
        "-c:a", "aac",
        "-b:a", f"{AUDIO_KBPS}k",
        "-movflags", "+faststart",
        dest,
    ]
    code2, out2 = await run_subprocess(cmd2, env)

    # Clean up 2-pass log files
    for f in Path(".").glob("ffmpeg2pass*"):
        try:
            f.unlink()
        except Exception:
            pass

    if code2 != 0:
        return False, out2

    final_mb = os.path.getsize(dest) / (1024 * 1024)
    print(f"[ffmpeg] Output: {final_mb:.2f} MB")
    return True, f"{final_mb:.2f} MB"


async def download_and_compress(url: str, guild: discord.Guild | None) -> tuple:
    env = clean_env()
    log = []
    target_mb   = get_target_mb(guild)
    target_size = int(target_mb * 1024 * 1024)

    log.append(f"[INFO] Guild boost tier: {guild.premium_tier if guild else 0} — upload limit: {target_mb}MB")

    with tempfile.TemporaryDirectory(prefix="cove_") as tmp:
        output_template = str(Path(tmp) / "%(title)s.%(ext)s")

        # ── Download ──────────────────────────────────────────────────
        cmd = [
            "yt-dlp",
            "-f", "bv*+ba/b",
            "--merge-output-format", "mp4",
            "-o", output_template,
        ]

        if os.path.exists(COOKIES_FILE):
            cmd.extend(["--cookies", COOKIES_FILE])
            log.append("[INFO] Using cookies file.")
        else:
            log.append("[WARN] No cookies.txt found — some sites may fail.")

        cmd.append(url)

        log.append("[INFO] Downloading...")
        code, out = await run_subprocess(cmd, env)
        log.append(out.strip())

        if code != 0:
            if "Sign in to confirm" in out or "bot" in out.lower():
                log.append("[ERROR] YouTube bot detection triggered. YouTube downloads from this server are currently blocked.")
            return None, "\n".join(log)

        mp4_files = list(Path(tmp).glob("*.mp4"))
        if not mp4_files:
            log.append("[ERROR] No MP4 file found after download.")
            return None, "\n".join(log)

        src     = str(mp4_files[0])
        orig_mb = os.path.getsize(src) / (1024 * 1024)
        log.append(f"[INFO] Downloaded: {orig_mb:.1f} MB")

        # ── Skip compression if already within limit ──────────────────
        if os.path.getsize(src) <= target_size:
            log.append(f"[INFO] File is under {target_mb}MB — skipping compression.")
            dest = tempfile.mktemp(suffix=".mp4", prefix="cove_upload_")
            shutil.copy2(src, dest)
            return dest, "\n".join(log)

        # ── Compress to target size ───────────────────────────────────
        compressed = str(Path(tmp) / "compressed.mp4")
        log.append(f"[INFO] Compressing to ≤{target_mb}MB...")
        ok, result = await compress_to_target(src, compressed, env, target_mb)

        if ok:
            log.append(f"[OK] Final size: {result}")
            final = compressed
        else:
            log.append(f"[WARN] Compression failed: {result}. Using original.")
            final = src

        dest = tempfile.mktemp(suffix=".mp4", prefix="cove_upload_")
        shutil.copy2(final, dest)
        return dest, "\n".join(log)


# ── Bot setup ─────────────────────────────────────────────────────────────
class CoveBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # required to read message content
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
        # Ignore messages from bots (including ourselves)
        if message.author.bot:
            return

        url = extract_supported_url(message.content)
        if not url:
            return

        print(f"[Cove] Auto-triggered by {message.author} in #{message.channel}: {url}")

        # Suppress Discord's native embed immediately so it doesn't appear
        # while the bot is downloading — prevents the duplicate embed flash
        try:
            await message.edit(suppress=True)
        except discord.HTTPException:
            pass

        # Add a loading reaction so the user knows it's working
        try:
            await message.add_reaction("⏳")
        except discord.HTTPException:
            pass

        filepath, log = await download_and_compress(url, message.guild)

        if not filepath or not os.path.exists(filepath):
            # Re-enable embed on the original message since we're keeping it
            try:
                await message.edit(suppress=False)
            except discord.HTTPException:
                pass
            try:
                await message.remove_reaction("⏳", self.user)
            except discord.HTTPException:
                pass
            error_lines = [l for l in log.splitlines() if l.startswith("[ERROR]")]
            error_msg   = error_lines[-1].replace("[ERROR] ", "") if error_lines else "Download failed."
            try:
                await message.reply(f"❌ {error_msg}", mention_author=False)
            except discord.HTTPException:
                await message.channel.send(f"❌ {error_msg}")
            return

        try:
            await message.delete()
            await message.channel.send(
                content=f"📥 {message.author.mention}",
                file=discord.File(filepath)
            )
        except discord.HTTPException as e:
            try:
                await message.remove_reaction("⏳", self.user)
            except discord.HTTPException:
                pass
            try:
                await message.reply(f"❌ Upload failed: {e}", mention_author=False)
            except discord.HTTPException:
                await message.channel.send(f"❌ Upload failed: {e}")
        finally:
            try:
                os.remove(filepath)
            except Exception:
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

    filepath, log = await download_and_compress(url, interaction.guild)

    if not filepath or not os.path.exists(filepath):
        error_lines = [l for l in log.splitlines() if l.startswith("[ERROR]")]
        error_msg   = error_lines[-1].replace("[ERROR] ", "") if error_lines else "Download failed."
        await interaction.followup.send(f"❌ {error_msg}")
        return

    try:
        await interaction.followup.send(file=discord.File(filepath))
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Upload failed: {e}")
    finally:
        try:
            os.remove(filepath)
        except Exception:
            pass


client.run(TOKEN)
