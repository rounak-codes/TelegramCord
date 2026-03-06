"""
utils/formatter.py
──────────────────
Converts a Telethon Message object into content that Discord can render
nicely — handling plain text, hyperlinks, and Telegram's MessageEntityUrl /
MessageEntityTextUrl entity types.

Discord supports Markdown links in the form [label](url) inside regular
messages but NOT inside embeds by default.  We keep things simple and post
as a regular message so that Discord auto-embeds URLs and the crosspost
button works correctly on announcement channels.
"""

import re
from typing import Optional

from telethon.tl.types import (
    Message,
    MessageEntityBold,
    MessageEntityItalic,
    MessageEntityCode,
    MessageEntityPre,
    MessageEntityUrl,
    MessageEntityTextUrl,
    MessageEntityStrike,
    MessageEntityUnderline,
)
from utils.logger import logger

# Discord has a 2000-character message limit.
DISCORD_MAX_CHARS = 1990  # leave 10 chars as safety buffer


def _apply_markdown(text: str, entities) -> str:
    """
    Walk through Telegram message entities and convert them to Discord
    Markdown where there is a sensible equivalent.

    Telegram offset/length values are in UTF-16 code units, but Python strings
    are Unicode so we handle them as character indices which is accurate for
    the vast majority of content.
    """
    if not entities:
        return text

    # Build a list of (start, end, prefix, suffix) transformations
    transforms: list[tuple[int, int, str, str]] = []

    for entity in entities:
        s = entity.offset
        e = entity.offset + entity.length

        if isinstance(entity, MessageEntityBold):
            transforms.append((s, e, "**", "**"))
        elif isinstance(entity, MessageEntityItalic):
            transforms.append((s, e, "_", "_"))
        elif isinstance(entity, MessageEntityCode):
            transforms.append((s, e, "`", "`"))
        elif isinstance(entity, MessageEntityPre):
            transforms.append((s, e, "```\n", "\n```"))
        elif isinstance(entity, MessageEntityStrike):
            transforms.append((s, e, "~~", "~~"))
        elif isinstance(entity, MessageEntityUnderline):
            # Discord doesn't support underline in standard Markdown; skip.
            pass
        elif isinstance(entity, MessageEntityTextUrl):
            # Hyperlink: render as [label](url)
            label = text[s:e]
            url = entity.url
            # We store the full replacement string as a special marker
            transforms.append((s, e, f"[{label}](", f") <!-- URL:{url} -->"))
        # MessageEntityUrl — the raw URL is already in the text; leave it as-is.

    if not transforms:
        return text

    # Apply transforms in reverse order so offsets stay valid
    transforms.sort(key=lambda x: x[0], reverse=True)
    chars = list(text)
    for start, end, prefix, suffix in transforms:
        # Handle TextUrl special case where prefix already contains the label
        if suffix.startswith(") <!-- URL:"):
            url = suffix.replace(") <!-- URL:", "").replace(" -->", "")
            replacement = list(f"[{''.join(chars[start:end])}]({url})")
            chars[start:end] = replacement
        else:
            chars.insert(end, suffix)
            chars.insert(start, prefix)

    return "".join(chars)


def format_message(message: Message) -> Optional[str]:
    """
    Convert a Telethon Message to a Discord-ready string.

    Returns None if there is nothing worth posting (e.g. a service message).
    """
    raw_text: str = message.text or message.message or ""

    if not raw_text and not message.media:
        logger.debug("Message %d has no text and no media — skipping", message.id)
        return None

    # Apply entity-based formatting if we have text
    if raw_text:
        try:
            formatted = _apply_markdown(raw_text, message.entities)
        except Exception as exc:
            logger.warning("Entity formatting failed for msg %d: %s", message.id, exc)
            formatted = raw_text
    else:
        formatted = ""

    # Truncate to Discord's limit, appending a notice if we cut content
    if len(formatted) > DISCORD_MAX_CHARS:
        formatted = formatted[:DISCORD_MAX_CHARS] + "\n…*(message truncated)*"

    return formatted if formatted else None


def build_discord_content(text: Optional[str], source_label: str = "") -> str:
    """
    Wrap formatted text with an optional source footer using Discord's
    -# subtext format (renders as small grey text).

    Args:
        text: The formatted message text (may be None for media-only posts).
        source_label: E.g. "📢 @MyChannel" appended as a footer.

    Returns:
        The final string to post to Discord.
    """
    parts: list[str] = []

    if text:
        parts.append(text)

    if source_label:
        parts.append(f"-# {source_label}")

    return "\n".join(parts).strip() or "\u200b"  # zero-width space fallback