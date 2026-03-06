"""
utils/media.py
──────────────
Handles downloading media from Telegram messages and preparing them as
discord.File objects for upload.

Strategy:
- Files <= MAX_FILE_SIZE_MB  → downloaded to memory → uploaded directly to Discord
- Files >  MAX_FILE_SIZE_MB  → streamed to a temp file on disk → uploaded to
                               catbox.moe → link posted to Discord
                               (avoids loading large files into RAM on low-memory VPS)

Discord free tier limit: 25 MB per file.
Catbox.moe: free, anonymous, permanent links, no account needed.
"""

import asyncio
import asyncio
import io
import os
import tempfile
from typing import Optional

import aiohttp
import discord
from telethon import TelegramClient
from telethon.tl.types import (
    Message,
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageMediaWebPage,
)
from utils.logger import logger

# Max file size for direct Discord upload (bytes)
_env_mb = os.getenv("MAX_FILE_SIZE_MB", "24")
MAX_FILE_BYTES = int(float(_env_mb) * 1024 * 1024)

# Catbox upload endpoint — free, anonymous, permanent
CATBOX_URL = "https://catbox.moe/user/api.php"

# MIME → extension lookup for common types
_MIME_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "application/pdf": "pdf",
}


async def upload_to_catbox(filepath: str, filename: str) -> Optional[str]:
    """
    Upload a file from disk to catbox.moe and return the direct URL.
    Reads from disk so RAM usage stays flat regardless of file size.
    Returns None if the upload fails.
    """
    file_size = os.path.getsize(filepath)
    logger.info("Starting catbox upload: %s (%.1f MB)", filename, file_size / 1_048_576)

    try:
        # Use a TCPConnector with a longer keepalive for large uploads
        connector = aiohttp.TCPConnector(force_close=True)
        timeout = aiohttp.ClientTimeout(
            total=600,        # 10 minutes total
            connect=30,       # 30 seconds to connect
            sock_read=300,    # 5 minutes to read response
        )
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            with open(filepath, "rb") as f:
                form = aiohttp.FormData()
                form.add_field("reqtype", "fileupload")
                form.add_field("userhash", "")  # anonymous upload
                form.add_field(
                    "fileToUpload",
                    f,
                    filename=filename,
                    content_type="application/octet-stream",
                )
                logger.info("Sending file to catbox.moe…")
                async with session.post(CATBOX_URL, data=form) as resp:
                    logger.info("Catbox responded with HTTP %d", resp.status)
                    if resp.status == 200:
                        url = (await resp.text()).strip()
                        if url.startswith("https://"):
                            logger.info("Uploaded to catbox: %s", url)
                            return url
                        logger.warning("Catbox returned unexpected response: %s", url[:200])
                    else:
                        body = await resp.text()
                        logger.warning("Catbox upload failed — HTTP %d: %s", resp.status, body[:200])
    except aiohttp.ClientConnectorError as exc:
        logger.error("Catbox connection error (check internet/firewall): %s", exc)
    except asyncio.TimeoutError:
        logger.error("Catbox upload timed out after 10 minutes for file: %s", filename)
    except Exception as exc:
        logger.error("Catbox upload error: %s", exc)
    return None


async def download_media(
    client: TelegramClient, message: Message
) -> tuple[Optional[discord.File], Optional[str]]:
    """
    Download media from a Telegram message.

    Returns:
        (discord.File, None)      — small file, ready for Discord upload
        (None, url_or_notice)     — large file uploaded to catbox, or error notice
        (None, None)              — no downloadable media
    """
    media = message.media

    # Web page preview — Discord will auto-embed the URL in the text
    if isinstance(media, MessageMediaWebPage):
        return None, None

    if isinstance(media, MessageMediaPhoto):
        return await _handle_photo(client, message)

    if isinstance(media, MessageMediaDocument):
        return await _handle_document(client, message)

    if media is not None:
        logger.debug("Unsupported media type %s in msg %d", type(media).__name__, message.id)

    return None, None


async def _handle_photo(
    client: TelegramClient, message: Message
) -> tuple[Optional[discord.File], Optional[str]]:
    """Photos are almost always under 5 MB — download to memory directly."""
    try:
        buf = io.BytesIO()
        await client.download_media(message.media, file=buf)
        size = buf.tell()

        if size > MAX_FILE_BYTES:
            # Rare but handle it — save to disk and upload to catbox
            return await _large_file_to_catbox(
                client, message, f"photo_{message.id}.jpg", size
            )

        buf.seek(0)
        return discord.File(buf, filename=f"photo_{message.id}.jpg"), None

    except Exception as exc:
        logger.error("Failed to download photo from msg %d: %s", message.id, exc)
        return None, "📎 *Could not download image*"


async def _handle_document(
    client: TelegramClient, message: Message
) -> tuple[Optional[discord.File], Optional[str]]:
    """
    For documents/videos:
    - If size is known and exceeds limit → stream straight to disk, skip RAM
    - If size fits limit → download to memory for Discord upload
    """
    try:
        doc = message.media.document
        mime: str = getattr(doc, "mime_type", "application/octet-stream") or ""
        size: int = getattr(doc, "size", 0) or 0
        filename = _extract_filename(doc) or f"file_{message.id}.{_MIME_EXT.get(mime, 'bin')}"

        if size > MAX_FILE_BYTES:
            # Stream directly to disk — never loads full file into RAM
            logger.info(
                "Large file detected: '%s' (%.1f MB) — streaming to disk",
                filename, size / 1_048_576,
            )
            return await _large_file_to_catbox(client, message, filename, size)

        # Small enough for Discord — download to memory
        buf = io.BytesIO()
        await client.download_media(message.media, file=buf)
        buf.seek(0)
        return discord.File(buf, filename=filename), None

    except Exception as exc:
        logger.error("Failed to download document from msg %d: %s", message.id, exc)
        return None, "📎 *Could not download attachment*"


async def _large_file_to_catbox(
    client: TelegramClient,
    message: Message,
    filename: str,
    size: int,
) -> tuple[Optional[discord.File], Optional[str]]:
    """
    Stream a large file from Telegram directly to a temp file on disk,
    then upload it to catbox.moe from disk.

    Peak RAM usage: only Telethon's internal download buffer (~512 KB chunks),
    NOT the entire file. Safe on a 100 MB VPS.
    """
    tmp_path = None
    try:
        # Create temp file first, then pass the path to Telethon
        with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{filename}") as tmp:
            tmp_path = tmp.name

        logger.info("Streaming %.1f MB to temp file: %s", size / 1_048_576, tmp_path)

        # Wrap download in a timeout — 3 minutes max for any file size
        try:
            await asyncio.wait_for(
                client.download_media(message.media, file=tmp_path),
                timeout=180,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Download timed out for '%s' (%.1f MB) after 180s — skipping",
                filename, size / 1_048_576,
            )
            return None, f"📎 *Download timed out for {filename} ({size / 1_048_576:.1f} MB)*"

        # Confirm the file actually downloaded
        actual_size = os.path.getsize(tmp_path)
        logger.info(
            "Download complete: %.1f MB written to disk — uploading to catbox…",
            actual_size / 1_048_576,
        )

        # Upload from disk to catbox — aiohttp reads in chunks, no RAM spike
        url = await upload_to_catbox(tmp_path, filename)
        if url:
            return None, f"📎 {url}"

        return None, (
            f"📎 *File too large for Discord ({size / 1_048_576:.1f} MB) "
            f"and catbox upload failed — try again later*"
        )

    except Exception as exc:
        logger.error("Large file handling failed for msg %d: %s", message.id, exc)
        return None, "📎 *Could not process large attachment*"

    finally:
        # Always clean up the temp file from disk
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
                logger.debug("Deleted temp file: %s", tmp_path)
            except Exception as exc:
                logger.warning("Could not delete temp file %s: %s", tmp_path, exc)


def _extract_filename(doc) -> Optional[str]:
    """Pull the original filename from document attributes if available."""
    try:
        from telethon.tl.types import DocumentAttributeFilename
        for attr in doc.attributes or []:
            if isinstance(attr, DocumentAttributeFilename):
                return attr.file_name
    except Exception:
        pass
    return None