# Cove Video Downloader — Discord Bot

A self-hosted Discord bot that automatically detects video links in chat and downloads, compresses, and re-uploads them directly — no embeds, no external services.

![Python](https://img.shields.io/badge/python-3.10%2B-orange?style=flat-square&logo=python)
![Platform](https://img.shields.io/badge/platform-Linux%20%28Arch%2FEOS%29-blue?style=flat-square&logo=archlinux)
![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)

---

## Features

- **Auto-download** — detects supported links posted in chat and downloads them automatically
- **Attribution embed** — shows who posted the original link (e.g. "DrunkenSquirrel posted:") without pinging them
- **Smart compression** — uses ffmpeg to compress videos to fit within Discord's upload limit for your server's boost tier
- **Silent ignore** — if a link has no video (image posts, text posts, rate limits), the bot removes the ⏳ and does nothing
- **Slash command** — `/download <url>` for manual downloads
- **Cookie support** — place a `cookies.txt` next to `bot.py` for sites that require authentication
- **Friend server mode** — optional second server where the bot deletes the original message and silently tags the poster

---

## Supported Sites

Anything yt-dlp supports, with auto-download triggered for:

- Twitter / X
- Reddit
- TikTok
- Instagram
- YouTube
- arazu.io

---

## Upload Limits

Automatically adjusts based on your server's Nitro boost tier:

| Boost Tier | Upload Limit |
|---|---|
| Tier 0 / 1 | 9.5 MB |
| Tier 2 (7 boosts) | 49 MB |
| Tier 3 (14 boosts) | 99 MB |

If the file is already under the limit, compression is skipped. Videos over 10 minutes are rejected outright.

---

## Setup

### 1. Install dependencies

```bash
sudo pacman -S python yt-dlp ffmpeg
pip install discord.py python-dotenv
```

### 2. Configure your environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your values:

```
DISCORD_TOKEN=your_token_here
GUILD_ID=your_main_guild_id_here
FRIEND_GUILD_ID=your_friend_guild_id_here  # optional
```

To get a Guild ID: enable Developer Mode in Discord → right-click your server → **Copy Server ID**.

`FRIEND_GUILD_ID` is optional. If omitted or set to `0`, the bot behaves identically on all servers.

### 3. (Optional) Add cookies

For sites that require a logged-in session (Reddit, Instagram, etc.), export your browser cookies using a tool like [cookies.txt](https://github.com/kairi003/Get-cookies.txt-LOCALLY) and place the file at:

```
cookies.txt  ← same directory as bot.py
```

### 4. Run the bot

```bash
python bot.py
```

#### Running as a systemd service (recommended for always-on)

```bash
mkdir -p ~/.config/systemd/user
```

Create `~/.config/systemd/user/cove-bot.service`:

```ini
[Unit]
Description=Cove Video Downloader Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/cove-video-downloader-bot
ExecStart=/path/to/cove-video-downloader-bot/.venv/bin/python -u /path/to/cove-video-downloader-bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Then enable and start it:

```bash
systemctl --user enable --now cove-bot.service
```

---

## Friend Server Mode

When `FRIEND_GUILD_ID` is set, the bot activates a special mode in that server only:

| Behavior | Main Server | Friend Server |
|---|---|---|
| Post video | ✅ embed + file | ✅ embed + file |
| Delete original message | ❌ | ✅ |
| @mention poster | ❌ | ✅ silent (no ping) |

The silent mention renders the poster's name as a clickable tag in the bot's message but sends **zero notification**. This requires the bot to have the **Manage Messages** permission in the friend server. If it's missing, the delete step is silently skipped.

---

## Bot Permissions Required

- Send Messages
- Attach Files
- Add Reactions
- Read Message History
- Use Slash Commands
- **Manage Messages** *(friend server only — for deleting the original message)*

---

## License

MIT
