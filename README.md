# Cove Video Downloader — Bot

Discord bot that downloads and compresses videos via **yt-dlp** and **ffmpeg**.

![Python](https://img.shields.io/badge/python-3.10%2B-orange?style=flat-square&logo=python)
![Platform](https://img.shields.io/badge/platform-Linux%20%28Arch%2FEOS%29-blue?style=flat-square&logo=archlinux)
![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)

---

## Usage

```
/download <url>
```

The bot downloads the video, compresses it with H.264 (2-pass CBR via ffmpeg), and uploads it directly to the channel. The upload size limit is automatically determined by the server's boost tier:

| Boost Tier | Upload Limit |
|---|---|
| Tier 0 / 1 (no boosts) | 9.5 MB |
| Tier 2 (7 boosts) | 49 MB |
| Tier 3 (14 boosts) | 99 MB |

If the downloaded file is already within the limit, compression is skipped entirely. If compression still can't bring it under the limit, the bot replies with an error.

---

## Setup

### 1. Install dependencies

```bash
sudo pacman -S python yt-dlp ffmpeg
pip install discord.py python-dotenv
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Then open `.env` and fill in your values:

```
DISCORD_TOKEN=your_token_here
GUILD_ID=963140546191302656
```

### 3. Run the bot

```bash
python bot.py
```

---

## Requirements

- `yt-dlp` and `ffmpeg` (with `ffprobe`) must be on your system PATH
- Bot needs **Send Messages**, **Attach Files**, and **Use Slash Commands** permissions
- Upload size limit is automatically adjusted based on the server's Nitro boost tier

---

## License

MIT
