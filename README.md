# Cove Video Downloader — Discord Bot

A self-hosted Discord bot that automatically detects video links in chat and downloads, compresses, and re-uploads them directly — no embeds, no external services.

![Python](https://img.shields.io/badge/python-3.10%2B-orange?style=flat-square&logo=python)
![Platform](https://img.shields.io/badge/platform-Linux%20%28Arch%2FEOS%29-blue?style=flat-square&logo=archlinux)
![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)

---

## Features

- **Auto-download** — detects supported links posted in chat and downloads them automatically
- **Attribution line** — shows mentions first, then the poster's display name as plain text
- **Smart compression** — uses ffmpeg to compress videos to fit within Discord's upload limit for your server's boost tier
- **Silent ignore** — if a link has no video (image posts, text posts, rate limits), the bot removes the ⏳ and does nothing
- **Slash commands** — `/download <url>` for manual video downloads and `/audio <url>` for MP3 audio extraction
- **Cookie support** — place a `cookies.txt` next to `bot.py` for sites that require authentication
- **Friend server mode** — optional second server where the bot deletes the original message and posts plain-text attribution

---

## Supported Sites

Anything yt-dlp supports, with auto-download triggered for:

- Twitter / X (and fixup mirrors: fxtwitter, vxtwitter, fixupx, twittpr)
- Reddit
- TikTok
- Instagram
- Twitch (incl. clips)
- arazu.io

YouTube is intentionally **not** auto-triggered (Google's bot-detection makes it
unreliable in unattended runs). It's still available via `/download <youtube-url>` and `/audio <youtube-url>`.

---

## YouTube download quality

YouTube throttles its separate audio stream to about 30 KB/s for downloaders, so a high-resolution YouTube `/download` can take ~30s+ even for a short clip — the video stream is fine, the audio track is the bottleneck. This is a YouTube-side limit; more parallel connections cannot bypass it.

Admins can pick the YouTube resolution at runtime with the **`/quality`** slash command (choices: 360p, 480p, 720p, 1080p). The choice is saved and survives restarts — no need to edit any file. Run `/quality` with no option to see the current setting.

**360p is the fast one:** it uses a single progressive stream with no separate (throttled) audio track, so it downloads in a couple of seconds. 480p/720p/1080p are sharper but slower, because they pull the throttled audio stream. Pick whichever tradeoff you prefer.

The starting quality on a fresh install is **1080p**; set the `YOUTUBE_QUALITY` env var (`360`, `480`, `720`, or `1080`) to change that default. It only affects YouTube video downloads; other sites and `/audio` are unaffected. No extra dependencies or services are required.

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
sudo pacman -S python ffmpeg
pip install discord.py python-dotenv yt-dlp yt-dlp-ejs curl_cffi
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
| Post video | ✅ attribution + file | ✅ attribution + file |
| Delete original message | ❌ | ✅ |
| Ping tagged users | ❌ | ✅ |
| Show poster name | ✅ silent mention | ✅ plaintext name |

Friend server mode pings users who were tagged in the original message, adds the original poster's display name as plain text, then deletes the original message. The delete step requires the bot to have the **Manage Messages** permission in the friend server. If it's missing, the delete step is silently skipped.

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
