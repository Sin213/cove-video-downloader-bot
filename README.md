# Cove Video Downloader — Bot

Discord bot that downloads and compresses videos via **yt-dlp** and **HandBrakeCLI**.

![Python](https://img.shields.io/badge/python-3.10%2B-orange?style=flat-square&logo=python)
![Platform](https://img.shields.io/badge/platform-Linux%20%28Arch%2FEOS%29-blue?style=flat-square&logo=archlinux)
![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)

---

## Usage

```
/download <url>
```

The bot downloads the video, compresses it with H.265, and uploads it directly to the channel. If the file is still over Discord's limit after compression, it replies with an error message.

---

## Setup

### 1. Install dependencies

```bash
sudo pacman -S python yt-dlp handbrake-cli ffmpeg
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

- `yt-dlp` and `HandBrakeCLI` must be on your system PATH
- Bot needs **Send Messages**, **Attach Files**, and **Use Slash Commands** permissions
- File size limit: 10MB (Discord free tier). Videos over this limit after compression will not be uploaded.

---

## License

MIT
