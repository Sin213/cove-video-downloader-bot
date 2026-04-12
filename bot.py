#!/usr/bin/env python3
import discord
from discord import app_commands
import subprocess
import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TOKEN    = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# Discord free tier file size limit (bytes)
FILE_LIMIT = 10 * 1024 * 1024  # 10 MB


def clean_env():
    """
    Strip PyInstaller env vars so yt-dlp and HandBrakeCLI
    use the real system Python and libs.
    """
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


def detect_browser():
    """Return first installed browser for cookie extraction, or None."""
    for b in ["firefox", "chrome", "brave"]:
        if shutil.which(b):
            return b
    return None


async def run_subprocess(cmd: list[str], env: dict) -> tuple[int, str]:
    """Run a subprocess asynchronously and return (returncode, output)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace")


async def download_and_compress(url: str) -> tuple[str | None, str]:
    """
    Download and compress a video.
    Returns (filepath, log) on success, (None, log) on failure.
    """
    env     = clean_env()
    browser = detect_browser()
    log     = []

    with tempfile.TemporaryDirectory(prefix="cove_") as tmp:
        output_template = str(Path(tmp) / "%(title)s.%(ext)s")

        # ── Download ──────────────────────────────────────────────────
        cmd = [
            "yt-dlp",
            "-f", "bv*+ba/b",
            "--merge-output-format", "mp4",
            "-o", output_template,
        ]
        if browser:
            cmd.extend(["--cookies-from-browser", browser])
            log.append(f"[INFO] Using {browser} cookies.")
        cmd.append(url)

        log.append("[INFO] Downloading...")
        code, out = await run_subprocess(cmd, env)
        log.append(out.strip())

        if code != 0:
            return None, "\n".join(log)

        # Find the downloaded file
        mp4_files = list(Path(tmp).glob("*.mp4"))
        if not mp4_files:
            log.append("[ERROR] No MP4 file found after download.")
            return None, "\n".join(log)

        src = str(mp4_files[0])
        orig_mb = os.path.getsize(src) / (1024 * 1024)
        log.append(f"[INFO] Downloaded: {orig_mb:.1f} MB")

        # ── Compress ──────────────────────────────────────────────────
        compressed = src + ".compressed.mp4"
        hb_cmd = [
            "HandBrakeCLI",
            "-i", src,
            "-o", compressed,
            "-e", "x265",
            "-q", "31.5",
            "--encoder-preset", "fast",
            "-E", "aac",
            "-B", "192",
        ]

        log.append("[INFO] Compressing (H.265)...")
        hb_code, hb_out = await run_subprocess(hb_cmd, env)
        log.append(hb_out.strip())

        if hb_code == 0 and os.path.exists(compressed):
            new_sz  = os.path.getsize(compressed)
            orig_sz = os.path.getsize(src)
            new_mb  = new_sz / (1024 * 1024)
            if new_sz < orig_sz:
                final = compressed
                log.append(f"[OK] Compressed: {orig_mb:.1f}MB → {new_mb:.1f}MB")
            else:
                final = src
                log.append(f"[SKIP] Compression made it larger. Using original ({orig_mb:.1f}MB).")
        else:
            final = src
            log.append("[WARN] Compression failed. Using original.")

        # Check file size against Discord limit
        final_sz = os.path.getsize(final)
        if final_sz > FILE_LIMIT:
            final_mb = final_sz / (1024 * 1024)
            log.append(f"[ERROR] Final file is {final_mb:.1f}MB — over Discord's 10MB limit.")
            return None, "\n".join(log)

        # Move to a persistent temp path before the TemporaryDirectory is deleted
        dest = tempfile.mktemp(suffix=".mp4", prefix="cove_upload_")
        shutil.copy2(final, dest)
        return dest, "\n".join(log)


# ── Bot setup ─────────────────────────────────────────────────────────────
class CoveBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
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
                name="for /download"
            )
        )


client = CoveBot()


@client.tree.command(
    name="download",
    description="Download and compress a video from any supported site"
)
@app_commands.describe(url="The video URL to download")
async def download_cmd(interaction: discord.Interaction, url: str):
    await interaction.response.defer(thinking=True)

    filepath, log = await download_and_compress(url)

    if filepath and os.path.exists(filepath):
        try:
            await interaction.followup.send(
                content="✅ Here's your video:",
                file=discord.File(filepath),
            )
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"❌ Upload failed: {e}\n"
                f"The file may still be too large for this server."
            )
        finally:
            # Clean up the temp upload file
            try:
                os.remove(filepath)
            except Exception:
                pass
    else:
        # Pull just the last meaningful error line for the reply
        error_lines = [l for l in log.splitlines() if l.startswith("[ERROR]")]
        error_msg   = error_lines[-1] if error_lines else "Download failed."
        await interaction.followup.send(f"❌ {error_msg}")


client.run(TOKEN)
