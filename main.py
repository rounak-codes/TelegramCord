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
TELEGRAM_CHANNEL_USERNAME: str = os.environ["TELEGRAM_CHANNEL_USERNAME"]

DISCORD_BOT_TOKEN: str = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID: int = int(os.environ["DISCORD_CHANNEL_ID"])

# Optional: your Discord user ID to receive DM alerts (e.g. session expiry)
# Right-click your name in Discord → Copy User ID
DISCORD_OWNER_ID: int | None = int(os.environ["DISCORD_OWNER_ID"]) if os.getenv("DISCORD_OWNER_ID") else None

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
    file: discord.File | None = None,
    tg_message_id: int | None = None,
) -> discord.Message | None:
    """
    Post a message (and optional file) to the configured Discord channel,
    then crosspost it so follower servers receive the announcement.

    Args:
        content:        The formatted text to post.
        file:           Optional discord.File attachment.
        tg_message_id:  Used only for logging.

    Returns:
        The sent discord.Message, or None on failure.
    """
    channel = discord_bot.get_channel(DISCORD_CHANNEL_ID)

    if channel is None:
        # Bot may not have cached it yet — fetch directly
        try:
            channel = await discord_bot.fetch_channel(DISCORD_CHANNEL_ID)
        except discord.NotFound:
            logger.error("Discord channel %d not found. Check DISCORD_CHANNEL_ID.", DISCORD_CHANNEL_ID)
            return None
        except discord.Forbidden:
            logger.error(
                "Bot lacks permission to view channel %d. "
                "Ensure it has View Channel + Send Messages.",
                DISCORD_CHANNEL_ID,
            )
            return None

    sent = None
    try:
        # ── Send the message ──────────────────────────────────────────────────
        if file:
            sent = await channel.send(content=content, file=file)
        else:
            sent = await channel.send(content=content)

        logger.info(
            "Posted to Discord | discord_msg_id=%d | tg_msg_id=%s",
            sent.id,
            tg_message_id or "N/A",
        )
    except discord.Forbidden:
        logger.error("Bot lacks Send Messages permission in channel %d.", DISCORD_CHANNEL_ID)
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


async def resolve_channel(username: str) -> Channel | None:
    """
    Resolve a channel username or numeric ID to a Telethon entity.
    Supports both '@username' and '-100xxxxxxxxx' numeric formats.
    """
    try:
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


async def handle_new_message(event):
    """
    Event handler called by Telethon for every new message in the monitored
    Telegram channel.

    Flow:
        1. Deduplication check.
        2. Skip service messages (channel created, pinned, etc.)
        3. Format text via utils/formatter.py.
        4. Download media via utils/media.py.
        5. Guard: skip if there's genuinely nothing to post.
        6. Post to Discord.
        7. Mark as seen in dedup cache.
    """
    message = event.message
    msg_id: int = message.id

    # ── Deduplication ─────────────────────────────────────────────────────────
    if dedup_cache.seen(msg_id):
        logger.debug("Duplicate message detected (id=%d) — skipping", msg_id)
        return

    # ── Skip service messages (channel created, photo changed, pinned…) ───────
    # These have no text and no real media — only an `action` field set.
    if getattr(message, "action", None) is not None:
        logger.debug("Skipping service message id=%d (action: %s)", msg_id, type(message.action).__name__)
        dedup_cache.add(msg_id)
        return

    # ── Skip if truly empty (no text AND no media) ────────────────────────────
    raw_text = (message.text or message.message or "").strip()
    if not raw_text and not message.media:
        logger.debug("Skipping empty message id=%d", msg_id)
        dedup_cache.add(msg_id)
        return

    logger.info("New Telegram message received | id=%d", msg_id)

    # ── Format text ───────────────────────────────────────────────────────────
    formatted_text = format_message(message)

    # Build source label for footer (e.g. "📢 @channelname")
    channel_label = f"📢 @{TELEGRAM_CHANNEL_USERNAME.lstrip('@')}"
    discord_content = build_discord_content(formatted_text, source_label=channel_label)

    # ── Download media ────────────────────────────────────────────────────────
    discord_file = None
    media_notice: str | None = None

    if message.media:
        discord_file, media_notice = await download_media(tg_client, message)

        # If we couldn't download (too large etc.) and got a notice, append it
        if discord_file is None and media_notice:
            discord_content = (discord_content + "\n" + media_notice).strip()

    # ── Guard: footer-only content means nothing real to post ─────────────────
    # If formatted_text is None and there's no file/notice, don't post just the footer
    if not formatted_text and discord_file is None and not media_notice:
        logger.debug("Message %d has no postable content — skipping", msg_id)
        dedup_cache.add(msg_id)
        return

    # ── Post to Discord ───────────────────────────────────────────────────────
    sent = await send_to_discord(
        content=discord_content,
        file=discord_file,
        tg_message_id=msg_id,
    )

    if sent is not None:
        # Only mark as seen after a *successful* Discord post
        logger.info("Marking tg_msg_id=%d as seen in dedup cache", msg_id)
        dedup_cache.add(msg_id)
    elif sent is None:
        # Fallback: if sent is None but we got here, log it clearly
        logger.warning(
            "send_to_discord returned None for tg_msg_id=%d — NOT adding to dedup cache",
            msg_id,
        )


async def catchup_missed_messages(channel_entity, limit: int = 50):
    """
    On startup, fetch recent messages from the Telegram channel and forward
    any that were posted while the bot was offline — but ONLY within the
    catch-up window (default: last 30 minutes).

    Messages older than the window are ignored entirely, preventing the bot
    from re-posting old content every time it restarts.

    Args:
        channel_entity: The resolved Telegram channel entity.
        limit:          Max number of recent messages to inspect.
    """
    # How far back to look on startup (in minutes). Adjust via env var.
    catchup_minutes = int(os.getenv("CATCHUP_WINDOW_MINUTES", "30"))
    cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=catchup_minutes)

    logger.info(
        "Catching up on messages from the last %d minutes (since %s)…",
        catchup_minutes,
        cutoff_time.strftime("%H:%M:%S UTC"),
    )
    caught_up = 0

    try:
        async for message in tg_client.iter_messages(channel_entity, limit=limit):
            # Telethon returns messages newest-first; once we hit a message
            # older than the cutoff we can stop — everything after is older too
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

            # Apply the same filters as the live listener
            if message.reply_to is not None:
                continue
            if message.from_id is not None:
                continue
            if getattr(message, "action", None) is not None:
                continue

            # Skip already-seen messages
            if dedup_cache.seen(message.id):
                continue

            logger.info("Catch-up: forwarding missed message id=%d", message.id)
            await handle_new_message(type("Event", (), {"message": message})())
            caught_up += 1

            # Small delay to avoid hitting Discord rate limits during catch-up
            await asyncio.sleep(1.5)

    except Exception as exc:
        logger.error("Catch-up failed: %s", exc)

    if caught_up == 0:
        logger.info("Catch-up complete — no missed messages found.")
    else:
        logger.info("Catch-up complete — forwarded %d missed message(s).", caught_up)


async def start_telegram_listener(channel_entity):
    """
    Register the new-message event handler and keep Telethon running.
    """

    @tg_client.on(events.NewMessage(chats=channel_entity))
    async def _handler(event):
        # Skip replies — only forward original channel posts, not comments
        if event.message.reply_to is not None:
            logger.debug("Skipping reply/comment message id=%d", event.message.id)
            return

        # Skip messages posted by users (comments) — only forward posts
        # made by the channel itself (from_id is None for channel posts)
        if event.message.from_id is not None:
            logger.debug("Skipping user message id=%d (not a channel post)", event.message.id)
            return

        # Wrap in try/except so one bad message never kills the listener
        try:
            await handle_new_message(event)
        except Exception as exc:
            logger.exception("Unhandled error in message handler: %s", exc)

    logger.info(
        "Telegram listener active — watching for new messages in '%s'",
        TELEGRAM_CHANNEL_USERNAME,
    )
    await tg_client.run_until_disconnected()

    # If we reach here, Telethon disconnected unexpectedly
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

    # ── Resolve target channel ────────────────────────────────────────────────
    channel_entity = await resolve_channel(TELEGRAM_CHANNEL_USERNAME)
    if channel_entity is None:
        logger.critical("Cannot resolve Telegram channel — aborting.")
        await tg_client.disconnect()
        await discord_bot.close()
        sys.exit(1)

    # ── Catch up on any messages missed while the bot was offline ────────────
    await catchup_missed_messages(channel_entity)

    # ── Start listening ───────────────────────────────────────────────────────
    telegram_task = asyncio.create_task(
        start_telegram_listener(channel_entity),
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