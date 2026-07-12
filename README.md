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
- **Gallery support** — Reddit and Instagram multi-image galleries are posted as native image attachments
- **Image fallbacks** — image-only Reddit posts are reposted directly; image-only Instagram posts are rewritten through an embed-friendly mirror instead of erroring
- **Silent ignore** — if a link has no video (image posts, text posts, rate limits), the bot removes the ⏳ and does nothing
- **Slash commands** — `/download`, `/audio` (MP3), `/clip` (time range), `/gif` (max 10s), `/status`, `/help`, plus admin-only `/health` and `/quality`
- **Cookie support** — place a `cookies.txt` next to `bot.py` for sites that require authentication
- **Friend server mode** — optional second server where the bot deletes the original message and posts plain-text attribution (with a `/neet` command to exempt your next message)

---

## Supported Sites

Anything yt-dlp supports, with auto-download triggered for:

- Twitter / X (and fixup mirrors: fxtwitter, vxtwitter, fixupx, twittpr - resolved back to x.com before download)
- Reddit (videos, image posts, gifs, and galleries; arazu.io links get a direct retry when Reddit 403s)
- TikTok
- Instagram (videos, reels, image posts, and galleries; profile links are ignored)
- Threads
- Twitch (incl. clips)
- Streamable
- Vimeo
- arazu.io

YouTube is intentionally **not** auto-triggered (Google's bot-detection makes it
unreliable in unattended runs). It's still available via `/download <youtube-url>` and `/audio <youtube-url>`.

---

## YouTube download quality

YouTube throttles its separate audio stream to about 30 KB/s for downloaders, so a high-resolution YouTube `/download` can take ~30s+ even for a short clip — the video stream is fine, the audio track is the bottleneck. This is a YouTube-side limit; more parallel connections cannot bypass it.

Admins can pick the YouTube resolution at runtime with the **`/quality`** slash command (choices: 360p, 480p, 720p, 1080p, 1440p, 2160p). The choice is saved and survives restarts — no need to edit any file. Run `/quality` with no option to see the current setting.

**360p is the fast one:** it uses a single progressive stream with no separate (throttled) audio track, so it downloads in a couple of seconds. Higher resolutions are sharper but slower, because they pull the throttled audio stream. Pick whichever tradeoff you prefer.

If a high-resolution download would blow past your server's upload limit, the bot probes the estimated size first and automatically steps down (2160 → 1440 → 1080 → 720) to a resolution that fits, instead of downloading and compressing a huge file.

The starting quality on a fresh install is **1080p**; set the `YOUTUBE_QUALITY` env var to change that default. It only affects YouTube video downloads; other sites and `/audio` are unaffected. No extra dependencies or services are required.

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

## Recent Changes (July 2026)

**Features**

- `/clip <url> <start> <end>` — download a specific time range from a video (times like `1:30`, `90`, `0:05`)
- `/gif <url>` — convert a video to a high-quality GIF (max 10 seconds)
- `/status` — show the current download queue
- `/help` — list Cove commands
- `/health` — admin-only self-check of dependencies and runtime state
- `/quality` now goes up to 2160p, with automatic step-down to a resolution that fits the server's upload limit
- `/neet` (friend server) — exempt your next message from auto-download and deletion
- `/download` now handles Reddit image and gif posts (reposted as media), not just videos
- Instagram and Reddit galleries are posted as native multi-image attachments
- Image-only Instagram posts and reels are rewritten through a mirror chain (instagram7 / vxinstagram / zzinstagram) so they embed instead of erroring; Instagram profile links are silently ignored
- Non-video Reddit posts rewrite to an embed-friendly link instead of failing; arazu.io links are retried directly when Reddit returns 403
- Streamable, Vimeo, and Threads links now auto-download

**Fixes and hardening**

- Reddit API calls use browser TLS impersonation (curl_cffi), fixing widespread Reddit 403s
- YouTube downloads work again after the July 2026 JS-challenge change (requires yt-dlp >= 2026.7.4 with the yt-dlp-ejs package)
- @everyone / role-mention injection is blocked in reposted content
- Mirror hosts are rejected in manual URLs, while fixup mirrors (fxtwitter etc.) still resolve to the real tweet
- Subprocess output and memory are strictly bounded; timed-out downloads kill the whole helper process group (ffmpeg / aria2c included)
- Partial YouTube downloads that end in a hidden HTTP 403 are reliably detected and rejected instead of being uploaded
- Active download temp directories are protected from the orphan sweeper; shutdown waits for in-flight jobs before closing sessions
- Dozens of smaller correctness fixes from six external bug-scan audit rounds

---

## License

MIT
