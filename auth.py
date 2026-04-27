#!/usr/bin/env python3
"""
Interactive Telethon login. Run once per Telegram account before starting the
service so a session file is created in TG_SESSIONS_DIR.

Usage (inside the container):
    docker compose run --rm telegram-bridge python auth.py <session_name>

Reads TG_API_ID / TG_API_HASH from the environment (same .env as the service).
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient


def _env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise SystemExit(f"missing env var: {name}")
    return v


async def _main(session_name: str) -> None:
    load_dotenv()
    api_id = int(_env("TG_API_ID"))
    api_hash = _env("TG_API_HASH")

    sessions_dir = os.environ.get("TG_SESSIONS_DIR", "./sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    session_path = os.path.join(sessions_dir, session_name)

    client = TelegramClient(
        session_path, api_id, api_hash,
        device_model="iPhone 14",
        system_version="16.5",
        app_version="8.4.1",
        lang_code="en",
        system_lang_code="en-US",
    )

    print(f"[auth] starting interactive login for session '{session_name}'")
    print(f"[auth] session file will be written under {sessions_dir}/")

    # Telethon's start() reads phone, code, and 2FA password from stdin.
    await client.start()

    me = await client.get_me()
    print(f"[auth] OK — logged in as @{me.username} (id={me.id})")
    print(f"[auth] you can now start the service; this account is ready.")

    await client.disconnect()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    asyncio.run(_main(sys.argv[1]))
