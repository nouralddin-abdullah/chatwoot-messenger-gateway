import logging

from fastapi import APIRouter, HTTPException, Request
from pyee.asyncio import AsyncIOEventEmitter

from app.config import AppConfig

logger = logging.getLogger(__name__)


def create_router(bus: AsyncIOEventEmitter, config: AppConfig) -> APIRouter:
    """HTTP routes: health probe + Chatwoot webhook receiver."""
    router = APIRouter(tags=["webhooks"])

    @router.get("/health")
    async def health():
        return {
            "ok": True,
            "chatwoot": {
                "account_id": config.chatwoot.account_id,
                "base_url": str(config.chatwoot.base_url),
            },
            "telegram": {
                "accounts": [
                    {
                        "session_name": a.session_name,
                        "inbox_id": a.inbox_id,
                    }
                    for a in config.telegram.accounts
                ],
            },
        }

    @router.post("/chatwoot/webhook/{webhook_id}", response_model=dict)
    async def chatwoot_webhook(webhook_id: str, request: Request):
        inbox_id = config.chatwoot.inbox_by_webhook_id.get(webhook_id)
        if inbox_id is None:
            raise HTTPException(status_code=403, detail="Unknown webhook ID")

        payload = await request.json()
        event = payload.get("event")
        msg_type = payload.get("message_type")

        # Stash the resolved inbox_id on the payload so the router knows which
        # Telegram account to dispatch outbound to.
        payload["_inbox_id"] = inbox_id

        logger.info(
            "[http] chatwoot webhook accepted: event=%s type=%s inbox=%s",
            event,
            msg_type,
            inbox_id,
        )

        if event == "message_created" and msg_type == "outgoing":
            bus.emit("chatwoot.outgoing", payload)

        return {"status": "received"}

    return router
