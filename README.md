# TeleCord — Telegram → Discord Bridge Bot

A production-ready Python bot that mirrors a Telegram channel to a Discord announcement channel in real time. Supports text, images, videos, and large files via catbox.moe fallback.

---

## Features

- Forwards text messages with full Markdown support
- Forwards images, videos, audio, and documents
- Large files (over 24 MB) automatically uploaded to catbox.moe and linked
- Filters out replies and comments — only original channel posts are forwarded
- Deduplication cache persisted to disk — no duplicate posts across restarts
- Time-windowed catch-up on restart — only forwards messages missed while offline
- DM alerts to bot owner on session expiry or unexpected disconnection
- Rotating log files
- Graceful shutdown on Ctrl+C / SIGTERM
- Docker + docker-compose support
- Raspberry Pi compatible

---

## Project Structure

```
tg-discord-bot/
├── main.py                  # Entry point
├── generate_session.py      # One-time Telegram session generator
├── requirements.txt
├── .env.example             # Environment variable template
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── data/                    # Auto-created — stores dedup cache
├── logs/                    # Auto-created — rotating log files
└── utils/
    ├── logger.py            # Logging setup
    ├── dedup.py             # Persistent deduplication cache
    ├── formatter.py         # Telegram → Discord Markdown converter
    └── media.py             # Media download + catbox upload
```

---

## Requirements

- Python 3.13+
- A personal Telegram account
- A Discord server where you can add bots

---

## Setup

### 1. Get Telegram API Credentials

1. Go to https://my.telegram.org and log in
2. Click **API development tools**
3. Create an application (name and description can be anything)
4. Copy your `api_id` and `api_hash`

### 2. Generate a Telegram Session String

Run this once locally — it requires interactive login:

```bash
python generate_session.py
```

Enter your api_id, api_hash, phone number, and the verification code Telegram sends you. Copy the session string it prints.

> Keep this secret — it grants full access to your Telegram account.

### 3. Create a Discord Bot

1. Go to https://discord.com/developers/applications
2. Click **New Application** → give it a name
3. Go to **Bot** → **Add Bot** → copy the **Token**
4. Scroll down → enable **Message Content Intent**
5. Go to **OAuth2 → URL Generator** → tick **bot** under Scopes
6. Tick **Send Messages** and **Manage Messages** under Bot Permissions
7. Open the generated URL in your browser and invite the bot to your server

### 4. Configure Discord Channel

1. In your server, right-click the channel → **Edit Channel**
2. Set Channel Type to **Announcement**
3. In channel Permissions, ensure the bot has **View Channel**, **Send Messages**, and **Manage Messages** enabled

### 5. Get Your Discord IDs

- **Channel ID** — right-click the announcement channel → Copy Channel ID
- **Owner ID** (optional, for DM alerts) — Settings → Advanced → enable Developer Mode, then right-click your username → Copy User ID

### 6. Configure Environment Variables

```bash
cp .env.example .env
```

Fill in all values:

```dotenv
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
TELEGRAM_SESSION=1BVtsOKABu...
TELEGRAM_CHANNEL_USERNAME=channelname

DISCORD_BOT_TOKEN=MTM...
DISCORD_CHANNEL_ID=1234567890123456789
DISCORD_OWNER_ID=9876543210123456789
```

### 7. Install Dependencies

```bash
pip install -r requirements.txt
```

### 8. Run

```bash
python main.py
```

---

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `TELEGRAM_API_ID` | From https://my.telegram.org |
| `TELEGRAM_API_HASH` | From https://my.telegram.org |
| `TELEGRAM_SESSION` | Session string from `generate_session.py` |
| `TELEGRAM_CHANNEL_USERNAME` | Channel username without `@`, or numeric ID |
| `DISCORD_BOT_TOKEN` | From Discord Developer Portal |
| `DISCORD_CHANNEL_ID` | ID of the Discord announcement channel |

### Optional

| Variable | Default | Description |
|---|---|---|
| `DISCORD_OWNER_ID` | — | Your Discord user ID for DM alerts |
| `CATCHUP_WINDOW_MINUTES` | `30` | How far back to look for missed messages on restart |
| `MAX_FILE_SIZE_MB` | `24` | Files above this are uploaded to catbox.moe instead |
| `DEDUP_CACHE_SIZE` | `500` | Number of message IDs to keep in the dedup cache |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |

---

## Docker

### Build and run with docker-compose

```bash
docker compose up -d
```

### View logs

```bash
docker compose logs -f
```

### Stop

```bash
docker compose down
```

The `data/` and `logs/` directories are mounted as volumes so the dedup cache and logs persist across container restarts.

### Raspberry Pi (ARM)

```bash
docker buildx build --platform linux/arm64 -t tg-discord-bot .
```

---

## Deploying on a VPS

### systemd service (recommended)

Create `/etc/systemd/system/telecord.service`:

```ini
[Unit]
Description=TeleCord Bridge Bot
After=network.target

[Service]
WorkingDirectory=/path/to/tg-discord-bot
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=10
EnvironmentFile=/path/to/tg-discord-bot/.env

[Install]
WantedBy=multi-user.target
```

Then:

```bash
systemctl enable telecord
systemctl start telecord
systemctl status telecord
```

### Simple background process

```bash
nohup python main.py > /dev/null 2>&1 &
```

### With screen

```bash
screen -S telecord
python main.py
# Ctrl+A then D to detach
# screen -r telecord to reattach
```

---

## How It Works

1. Telethon connects to Telegram using a user session string
2. Listens for new messages in the configured channel
3. Filters out replies, comments, and service messages
4. Formats text with Discord Markdown
5. Downloads media — files under `MAX_FILE_SIZE_MB` are uploaded directly to Discord, larger files are streamed to disk and uploaded to catbox.moe
6. Posts to the Discord announcement channel
7. Saves the message ID to disk so it is never posted again

On startup, the bot also catches up on any messages posted while it was offline, within the `CATCHUP_WINDOW_MINUTES` window.

---

## Troubleshooting

**Bot posts nothing after startup**
- Check that `TELEGRAM_CHANNEL_USERNAME` is correct — use the username without `@`
- Make sure your Telegram account is a member of the channel
- Check logs for errors

**`SessionPasswordNeededError` during session generation**
- Your account has 2-Step Verification enabled — `generate_session.py` will prompt for your cloud password automatically

**`ModuleNotFoundError: No module named 'audioop'`**
- You are on Python 3.13+ — run `pip install audioop-lts`

**Messages are duplicated after restart**
- Check that `data/seen_messages.json` exists and contains message IDs
- If the file is empty or missing, the dedup cache is not saving — check logs for `FAILED to save dedup cache`

**Discord `Forbidden` error**
- The bot is missing permissions on the channel
- Ensure it has View Channel, Send Messages, and Manage Messages

**Large files not uploading to catbox**
- catbox.moe may be temporarily down
- The bot will post an error notice in Discord and continue running
- Check logs for `Catbox upload failed`

**Telegram session expired**
- Re-run `generate_session.py` and update `TELEGRAM_SESSION` in your `.env` or hosting dashboard
- If `DISCORD_OWNER_ID` is set, the bot will DM you when this happens

---

## Security Notes

- Never commit `.env` to a public repository
- The session string is equivalent to your Telegram password — treat it as such
- Revoke and regenerate your Discord bot token immediately if it is ever exposed
- All secrets should be stored as environment variables, never hardcoded

---

## Hardware Recommendation

For self-hosting, a **Raspberry Pi Zero 2 W** (~$15) is ideal:
- 512 MB RAM — comfortable for the bot's ~65 MB idle usage
- Built-in WiFi
- ~1-3W power draw — negligible electricity cost
- Runs 24/7 with no monthly fees

---

## License

MIT