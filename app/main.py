import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Dict

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from pyee.asyncio import AsyncIOEventEmitter

from app.application.events import wire_events
from app.application.router import MessageRouter
from app.config import load_config
from app.delivery.http import create_router
from app.infra.adapters.telegram_telethon import TelegramAdapter

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)

load_dotenv()
config = load_config()

bus = AsyncIOEventEmitter()

# Build one TelegramAdapter per configured account, keyed by inbox_id for routing.
adapters_by_inbox: Dict[int, TelegramAdapter] = {}
for account in config.telegram.accounts:
    adapters_by_inbox[account.inbox_id] = TelegramAdapter(
        bus=bus,
        api_id=config.telegram.api_id,
        api_hash=config.telegram.api_hash,
        account=account,
    )

router = MessageRouter(adapters_by_inbox=adapters_by_inbox)

# Wire incoming Telegram events -> Chatwoot, and outgoing Chatwoot webhooks -> Telegram.
wire_events(
    bus=bus,
    config=config,
    adapters=list(adapters_by_inbox.values()),
    router=router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info(
        "starting %s telegram account(s): %s",
        len(adapters_by_inbox),
        ", ".join(
            f"{a.session_name}->inbox{a.inbox_id}"
            for a in adapters_by_inbox.values()
        ),
    )
    await asyncio.gather(
        *(a.start() for a in adapters_by_inbox.values()), return_exceptions=True
    )
    try:
        yield
    finally:
        await asyncio.gather(
            *(a.stop() for a in adapters_by_inbox.values()), return_exceptions=True
        )


app = FastAPI(title="Chatwoot ↔ Telegram Bridge", version="1.0.0", lifespan=lifespan)
app.include_router(create_router(bus=bus, config=config))


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")
