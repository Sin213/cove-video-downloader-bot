#!/usr/bin/env python3
import discord
from discord import app_commands
import asyncio
import aiohttp
import os
import shutil
import tempfile
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TOKEN    = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

FILE_LIMIT   = 10 * 1024 * 1024   # 10 MB — Discord free tier
CATBOX_API   = "https://catbox.moe/user/api.php"
CATBOX_LIMIT = 200 * 1024 * 1024  # 200 MB — Catbox max


def clean_env():
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


def detect_browser():
    for b in ["firefox", "chrome", "brave"]:
        if shutil.which(b):
            return b
    return None


async def run_subprocess(cmd: list[str], env: dict) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace")


async def upload_to_catbox(filepath: str) -> str | None:
    """Upload a file to Catbox.moe anonymously. Returns the URL or None."""
    try:
        file_sz = os.path.getsize(filepath)
        print(f"[Catbox] Uploading {Path(filepath).name} ({file_sz / 1024 / 1024:.1f} MB)...")
        async with aiohttp.ClientSession() as session:
            with open(filepath, "rb") as f:
                form = aiohttp.FormData()
                form.add_field("reqtype", "fileupload")
                form.add_field(
                    "fileToUpload",
                    f,
                    filename=Path(filepath).name,
                    content_type="video/mp4",
                )
                async with session.post(CATBOX_API, data=form, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                    status = resp.status
                    body   = (await resp.text()).strip()
                    print(f"[Catbox] Response {status}: {body!r}")
                    if status == 200 and body.startswith("https://"):
                        return body
    except Exception as e:
        print(f"[Catbox] Exception: {e}")
    return None


async def download_and_compress(url: str) -> tuple[str | None, str]:
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

        mp4_files = list(Path(tmp).glob("*.mp4"))
        if not mp4_files:
            log.append("[ERROR] No MP4 file found after download.")
            return None, "\n".join(log)

        src     = str(mp4_files[0])
        orig_sz = os.path.getsize(src)
        orig_mb = orig_sz / (1024 * 1024)
        log.append(f"[INFO] Downloaded: {orig_mb:.1f} MB")

        # ── Compress (H.264) ────────────────────────────────────────────
        compressed = src + ".compressed.mp4"
        hb_cmd = [
            "HandBrakeCLI",
            "-i", src,
            "-o", compressed,
            "-e", "x264",
            "-q", "28",
            "--encoder-preset", "fast",
            "-E", "aac",
            "-B", "192",
        ]

        log.append("[INFO] Compressing (H.264)...")
        hb_code, hb_out = await run_subprocess(hb_cmd, env)
        log.append(hb_out.strip())

        if hb_code == 0 and os.path.exists(compressed):
            new_sz = os.path.getsize(compressed)
            new_mb = new_sz / (1024 * 1024)
            if new_sz < orig_sz:
                final = compressed
                log.append(f"[OK] Compressed: {orig_mb:.1f}MB → {new_mb:.1f}MB")
            else:
                final = src
                log.append(f"[SKIP] Compression larger than original. Keeping original ({orig_mb:.1f}MB).")
        else:
            final = src
            log.append("[WARN] Compression failed. Using original.")

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
    try:
        await interaction.response.defer(thinking=True)
    except (discord.errors.NotFound, discord.errors.HTTPException):
        return

    filepath, log = await download_and_compress(url)

    if not filepath or not os.path.exists(filepath):
        error_lines = [l for l in log.splitlines() if l.startswith("[ERROR]")]
        error_msg   = error_lines[-1] if error_lines else "Download failed."
        await interaction.followup.send(f"❌ {error_msg}")
        return

    try:
        file_sz = os.path.getsize(filepath)

        if file_sz <= FILE_LIMIT:
            await interaction.followup.send(
                file=discord.File(filepath)
            )
        elif file_sz <= CATBOX_LIMIT:
            await interaction.followup.send(
                "⏳ File is over 10MB — uploading to Catbox..."
            )
            catbox_url = await upload_to_catbox(filepath)
            if catbox_url:
                await interaction.followup.send(catbox_url)
            else:
                await interaction.followup.send(
                    "❌ Catbox upload failed. Try again or use a shorter video."
                )
        else:
            file_mb = file_sz / (1024 * 1024)
            await interaction.followup.send(
                f"❌ File is {file_mb:.1f}MB — over Catbox's 200MB limit."
            )
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Upload failed: {e}")
    finally:
        try:
            os.remove(filepath)
        except Exception:
            pass


client.run(TOKEN)
