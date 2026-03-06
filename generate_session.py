#!/usr/bin/env python3
"""
generate_session.py
───────────────────
Run this ONCE locally to authenticate with Telegram and get a session string.
The session string is then stored in your .env / hosting environment variable
so the main bot never needs interactive login again.

Usage:
    python generate_session.py
"""

import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    print("=" * 60)
    print("  Telegram Session Generator")
    print("=" * 60)
    print()
    print("You can find your API credentials at: https://my.telegram.org/apps")
    print()

    api_id = int(input("Enter your TELEGRAM_API_ID: ").strip())
    api_hash = input("Enter your TELEGRAM_API_HASH: ").strip()

    print()
    print("Starting Telegram authentication...")
    print("You will receive a code via the Telegram app or SMS.")
    print()

    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        session_string = client.session.save()

    print()
    print("=" * 60)
    print("  ✅ SUCCESS — Your session string:")
    print("=" * 60)
    print()
    print(session_string)
    print()
    print("Copy the string above and set it as TELEGRAM_SESSION in your .env file.")
    print("⚠️  Keep this secret — it grants full access to your Telegram account.")


if __name__ == "__main__":
    asyncio.run(main())
