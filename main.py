#!/usr/bin/env python3
"""
main.py
───────
Entry point for the Telegram → Discord bridge bot.

Architecture overview:
  1. Telethon (user session) connects to Telegram and listens for new
     messages on a specified channel.
  2. When a message arrives, text is formatted and any media is downloaded.
  3. A discord.py bot posts the content to a configured announcement channel.
  4. The message is automatically crossposts via Discord's publish API so
     all servers following the announcement channel receive it.
  5. A deduplication cache prevents the same message from being posted twice.

Run:
    python main.py

Environment variables (see .env.example):
    TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION,
    TELEGRAM_CHANNEL_USERNAME, DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID
"""

import asyncio
import os
import signal
import sys
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel

from utils.dedup import DeduplicationCache
from utils.formatter import format_message, build_discord_content
from utils.logger import logger
from utils.media import download_media

# ── Load environment variables from .env (ignored in production if env is set) ──
load_dotenv()

# ── Validate required environment variables ───────────────────────────────────

REQUIRED_VARS = [
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_SESSION",
    "TELEGRAM_CHANNEL_USERNAME",
    "DISCORD_BOT_TOKEN",
    "DISCORD_CHANNEL_ID",
]

missing = [v for v in REQUIRED_VARS if not os.getenv(v)]
if missing:
    logger.critical("Missing required environment variables: %s", ", ".join(missing))
    logger.critical("Copy .env.example → .env and fill in all values.")
    sys.exit(1)

# ── Read configuration ────────────────────────────────────────────────────────

TELEGRAM_API_ID: int = int(os.environ["TELEGRAM_API_ID"])
TELEGRAM_API_HASH: str = os.environ["TELEGRAM_API_HASH"]
TELEGRAM_SESSION: str = os.environ["TELEGRAM_SESSION"]

DISCORD_BOT_TOKEN: str = os.environ["DISCORD_BOT_TOKEN"]

# Optional: your Discord user ID to receive DM alerts (e.g. session expiry)
# Right-click your name in Discord → Copy User ID
DISCORD_OWNER_ID: int | None = int(os.environ["DISCORD_OWNER_ID"]) if os.getenv("DISCORD_OWNER_ID") else None

# ── Multi-channel configuration ───────────────────────────────────────────────
#
# CHANNELS is a JSON list in your .env, each entry having:
#   tg_channel   — Telegram channel username or numeric ID
#   discord_id   — Discord channel ID to post to
#   footer       — Footer label shown on every forwarded message
#
# Example .env value (single line):
# CHANNELS=[{"tg_channel":"-1001234567890","discord_id":"111222333444555666","footer":"📢 HXG's TG Channel"}]
#
# Multiple channels example:
# CHANNELS=[{"tg_channel":"channel1","discord_id":"111222333444555666","footer":"📢 Channel 1"},{"tg_channel":"-1009876543210","discord_id":"777888999000111222","footer":"📢 Channel 2"}]

import json as _json

_raw_channels = os.getenv("CHANNELS")
if not _raw_channels:
    # Fallback: support old single-channel env vars for backwards compatibility
    _tg = os.getenv("TELEGRAM_CHANNEL_USERNAME")
    _dc = os.getenv("DISCORD_CHANNEL_ID")
    _footer = os.getenv("CHANNEL_FOOTER_LABEL", "📢 TG Channel")
    if not _tg or not _dc:
        logger.critical(
            "No channel configuration found. Set CHANNELS in your .env — "
            "see .env.example for the format."
        )
        sys.exit(1)
    CHANNELS: list[dict] = [{"tg_channel": _tg, "discord_id": int(_dc), "footer": _footer}]
else:
    try:
        CHANNELS = _json.loads(_raw_channels)
        for ch in CHANNELS:
            ch["discord_id"] = int(ch["discord_id"])
    except Exception as exc:
        logger.critical("Failed to parse CHANNELS env variable: %s", exc)
        sys.exit(1)

# ── Shared state ──────────────────────────────────────────────────────────────

dedup_cache = DeduplicationCache()

# Python 3.10+ no longer auto-creates an event loop at module level.
# We create one explicitly here so asyncio.Event() and commands.Bot()
# can be safely instantiated before asyncio.run() is called.
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# This event signals the Discord bot is ready before Telegram starts listening
discord_ready_event = asyncio.Event()

# ── Discord bot setup ─────────────────────────────────────────────────────────

# We need the `message_content` intent for any future commands, but the bot
# itself only needs to *send* messages, so intents are minimal.
intents = discord.Intents.default()
discord_bot = commands.Bot(command_prefix="!", intents=intents)


@discord_bot.event
async def on_ready():
    """Called once the Discord bot has successfully connected."""
    logger.info("Discord bot logged in as %s (ID: %s)", discord_bot.user, discord_bot.user.id)
    discord_ready_event.set()  # Unblock the Telegram listener setup


async def alert_owner(message: str) -> None:
    """
    Send a DM alert to the bot owner (DISCORD_OWNER_ID).
    Used to notify about critical issues like session expiry.
    Silently skips if DISCORD_OWNER_ID is not configured.
    """
    if not DISCORD_OWNER_ID:
        return
    try:
        user = await discord_bot.fetch_user(DISCORD_OWNER_ID)
        await user.send(f"⚠️ **TeleCord Alert**\n{message}")
        logger.info("Alert sent to owner (user_id=%d)", DISCORD_OWNER_ID)
    except Exception as exc:
        logger.warning("Could not send alert DM to owner: %s", exc)


async def send_to_discord(
    content: str,
    discord_channel_id: int,
    file: discord.File | None = None,
    tg_message_id: int | None = None,
) -> discord.Message | None:
    """
    Post a message (and optional file) to a Discord channel.

    Args:
        content:            The formatted text to post.
        discord_channel_id: The Discord channel to post to.
        file:               Optional discord.File attachment.
        tg_message_id:      Used only for logging.

    Returns:
        The sent discord.Message, or None on failure.
    """
    channel = discord_bot.get_channel(discord_channel_id)

    if channel is None:
        try:
            channel = await discord_bot.fetch_channel(discord_channel_id)
        except discord.NotFound:
            logger.error("Discord channel %d not found.", discord_channel_id)
            return None
        except discord.Forbidden:
            logger.error("Bot lacks permission to view channel %d.", discord_channel_id)
            return None

    sent = None
    try:
        if file:
            sent = await channel.send(content=content, file=file)
        else:
            sent = await channel.send(content=content)

        logger.info(
            "Posted to Discord | discord_msg_id=%d | tg_msg_id=%s | channel=%d",
            sent.id,
            tg_message_id or "N/A",
            discord_channel_id,
        )
    except discord.Forbidden:
        logger.error("Bot lacks Send Messages permission in channel %d.", discord_channel_id)
        return None
    except discord.HTTPException as exc:
        logger.error("Discord HTTP error while sending message: %s", exc)
        return None
    except Exception as exc:
        logger.exception("Unexpected error sending to Discord: %s", exc)
        return None

    return sent


# ── Telegram client setup ─────────────────────────────────────────────────────

tg_client = TelegramClient(
    StringSession(TELEGRAM_SESSION),
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH,
)


async def resolve_channel(username: str):
    """
    Resolve a channel username or numeric ID to a Telethon entity.
    Supports both '@username' and '-100xxxxxxxxx' numeric formats.
    For private channels, uses get_dialogs() to find the channel
    since get_entity() may fail without a prior interaction.
    """
    try:
        # Strip -100 prefix for numeric IDs and convert to int
        if username.lstrip("-").isdigit():
            peer_id = int(username)
            # For private channels, iterate dialogs to find the entity
            # since get_entity() requires a prior cache entry
            async for dialog in tg_client.iter_dialogs():
                if dialog.entity.id == abs(peer_id) or dialog.entity.id == peer_id:
                    logger.info(
                        "Resolved private channel: %s (ID: %s)",
                        getattr(dialog.entity, "title", username),
                        dialog.entity.id,
                    )
                    return dialog.entity
            # Fallback to get_entity if not found in dialogs
            entity = await tg_client.get_entity(peer_id)
        else:
            entity = await tg_client.get_entity(username)

        logger.info(
            "Resolved Telegram channel: %s (ID: %s)",
            getattr(entity, "title", username),
            getattr(entity, "id", "?"),
        )
        return entity
    except Exception as exc:
        logger.error("Could not resolve Telegram channel '%s': %s", username, exc)
        return None


async def handle_new_message(event, channel_config: dict):
    """
    Event handler for new messages.

    Args:
        event:          Telethon event object.
        channel_config: Dict with keys tg_channel, discord_id, footer.
    """
    message = event.message
    msg_id: int = message.id
    discord_channel_id: int = channel_config["discord_id"]
    footer: str = channel_config["footer"]

    # ── Deduplication — scoped per channel to avoid cross-channel conflicts ────
    cache_key = f"{channel_config['tg_channel']}:{msg_id}"
    if dedup_cache.seen(cache_key):
        logger.debug("Duplicate message detected (id=%d) — skipping", msg_id)
        return

    # ── Skip service messages ─────────────────────────────────────────────────
    if getattr(message, "action", None) is not None:
        logger.debug("Skipping service message id=%d", msg_id)
        dedup_cache.add(cache_key)
        return

    # ── Skip empty messages ───────────────────────────────────────────────────
    raw_text = (message.text or message.message or "").strip()
    if not raw_text and not message.media:
        logger.debug("Skipping empty message id=%d", msg_id)
        dedup_cache.add(cache_key)
        return

    logger.info("New message | id=%d | channel=%s", msg_id, channel_config["tg_channel"])

    # ── Format text ───────────────────────────────────────────────────────────
    formatted_text = format_message(message)
    discord_content = build_discord_content(formatted_text, source_label=footer)

    # ── Download media ────────────────────────────────────────────────────────
    discord_file = None
    media_notice: str | None = None

    if message.media:
        discord_file, media_notice = await download_media(tg_client, message)
        if discord_file is None and media_notice:
            discord_content = (discord_content + "\n" + media_notice).strip()

    # ── Guard: nothing real to post ───────────────────────────────────────────
    if not formatted_text and discord_file is None and not media_notice:
        logger.debug("Message %d has no postable content — skipping", msg_id)
        dedup_cache.add(cache_key)
        return

    # ── Post to Discord ───────────────────────────────────────────────────────
    sent = await send_to_discord(
        content=discord_content,
        file=discord_file,
        tg_message_id=msg_id,
        discord_channel_id=discord_channel_id,
    )

    if sent is not None:
        logger.info("Marking tg_msg_id=%d as seen in dedup cache", msg_id)
        dedup_cache.add(cache_key)
    else:
        logger.warning(
            "Discord post failed for tg_msg_id=%d — NOT adding to dedup cache",
            msg_id,
        )


async def catchup_missed_messages(channel_entity, channel_config: dict, limit: int = 50):
    """
    On startup, forward messages missed during downtime within the catchup window.
    """
    catchup_minutes = int(os.getenv("CATCHUP_WINDOW_MINUTES", "30"))
    cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=catchup_minutes)

    logger.info(
        "Catching up on '%s' — last %d minutes (since %s)…",
        channel_config["tg_channel"],
        catchup_minutes,
        cutoff_time.strftime("%H:%M:%S UTC"),
    )
    caught_up = 0

    try:
        async for message in tg_client.iter_messages(channel_entity, limit=limit):
            msg_time = message.date
            if msg_time.tzinfo is None:
                from datetime import timezone as tz
                msg_time = msg_time.replace(tzinfo=tz.utc)

            if msg_time < cutoff_time:
                logger.debug(
                    "Message id=%d is older than %d min window — stopping catch-up",
                    message.id, catchup_minutes,
                )
                break

            if message.reply_to is not None:
                continue
            if message.from_id is not None:
                continue
            if getattr(message, "action", None) is not None:
                continue

            cache_key = f"{channel_config['tg_channel']}:{message.id}"
            if dedup_cache.seen(cache_key):
                continue

            logger.info("Catch-up: forwarding missed message id=%d", message.id)
            await handle_new_message(type("Event", (), {"message": message})(), channel_config)
            caught_up += 1
            await asyncio.sleep(1.5)

    except Exception as exc:
        logger.error("Catch-up failed for '%s': %s", channel_config["tg_channel"], exc)

    if caught_up == 0:
        logger.info("Catch-up complete for '%s' — no missed messages.", channel_config["tg_channel"])
    else:
        logger.info("Catch-up complete for '%s' — forwarded %d message(s).", channel_config["tg_channel"], caught_up)


async def start_telegram_listener(channel_entities: list[tuple]):
    """
    Register new-message event handlers for all channels and keep Telethon running.

    Args:
        channel_entities: List of (entity, channel_config) tuples.
    """
    for entity, config in channel_entities:
        # Use a closure to capture config per channel
        def make_handler(channel_config):
            async def _handler(event):
                if event.message.reply_to is not None:
                    return
                if event.message.from_id is not None:
                    return
                try:
                    await handle_new_message(event, channel_config)
                except Exception as exc:
                    logger.exception("Unhandled error in message handler: %s", exc)
            return _handler

        tg_client.add_event_handler(
            make_handler(config),
            events.NewMessage(chats=entity),
        )
        logger.info("Listener registered for channel: %s", config["tg_channel"])

    logger.info("Telegram listener active — watching %d channel(s)", len(channel_entities))
    await tg_client.run_until_disconnected()

    if not _shutting_down:
        msg = (
            "Telegram client disconnected unexpectedly. "
            "This may mean your session expired. "
            "Check logs and re-run `generate_session.py` if needed."
        )
        logger.error(msg)
        await alert_owner(msg)


# ── Graceful shutdown ─────────────────────────────────────────────────────────

_shutting_down = False


def _shutdown_signal_handler(sig, frame):
    """Handle SIGINT/SIGTERM for clean shutdown — runs only once."""
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    logger.info("Shutdown signal received (%s) — stopping bot…", signal.Signals(sig).name)
    loop.create_task(_shutdown())


async def _shutdown():
    logger.info("Disconnecting Telegram client…")
    await tg_client.disconnect()
    logger.info("Closing Discord bot…")
    await discord_bot.close()


# ── Main entry point ──────────────────────────────────────────────────────────

async def main():
    logger.info("=" * 60)
    logger.info("  Telegram → Discord Bridge Bot  |  Starting up…")
    logger.info("=" * 60)

    # ── Start Discord bot in background ──────────────────────────────────────
    discord_task = asyncio.create_task(
        discord_bot.start(DISCORD_BOT_TOKEN),
        name="discord_bot",
    )

    # Wait until Discord bot is connected and ready
    logger.info("Waiting for Discord bot to be ready…")
    await discord_ready_event.wait()
    logger.info("Discord bot is ready.")

    # ── Connect Telegram client ───────────────────────────────────────────────
    logger.info("Connecting Telegram client…")
    await tg_client.connect()

    if not await tg_client.is_user_authorized():
        msg = (
            "Telegram session is invalid or expired. "
            "Re-run `generate_session.py` and update `TELEGRAM_SESSION`."
        )
        logger.critical(msg)
        await alert_owner(msg)
        await discord_bot.close()
        sys.exit(1)

    me = await tg_client.get_me()
    logger.info(
        "Telegram connected as: %s %s (@%s)",
        getattr(me, "first_name", ""),
        getattr(me, "last_name", "") or "",
        getattr(me, "username", "N/A"),
    )

    # ── Resolve all configured channels ──────────────────────────────────────
    channel_entities: list[tuple] = []
    for config in CHANNELS:
        entity = await resolve_channel(config["tg_channel"])
        if entity is None:
            logger.error("Cannot resolve channel '%s' — skipping.", config["tg_channel"])
            continue
        channel_entities.append((entity, config))

    if not channel_entities:
        logger.critical("No channels could be resolved — aborting.")
        await tg_client.disconnect()
        await discord_bot.close()
        sys.exit(1)

    # ── Catch up on missed messages for all channels ──────────────────────────
    for entity, config in channel_entities:
        await catchup_missed_messages(entity, config)

    # ── Start listening ───────────────────────────────────────────────────────
    telegram_task = asyncio.create_task(
        start_telegram_listener(channel_entities),
        name="telegram_listener",
    )

    # Run both tasks concurrently; if either dies, cancel the other
    done, pending = await asyncio.wait(
        [discord_task, telegram_task],
        return_when=asyncio.FIRST_EXCEPTION,
    )

    for task in done:
        exc = task.exception()
        if exc:
            logger.error("Task '%s' raised an exception: %s", task.get_name(), exc)

    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # ── end of main ───────────────────────────────────────────────────────────


if __name__ == "__main__":
    # Register OS-level signal handlers for clean shutdown
    signal.signal(signal.SIGINT, _shutdown_signal_handler)
    signal.signal(signal.SIGTERM, _shutdown_signal_handler)

    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass  # Already handled by _shutdown_signal_handler
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            loop.close()
            sys.exit(0)