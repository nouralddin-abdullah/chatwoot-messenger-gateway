import logging
from typing import Any, Dict

from app.domain.message import TextContent
from app.domain.webhooks.chatwoot import ChatwootMessageCreatedWebhook
from app.infra.adapters.telegram_telethon import TelegramAdapter

logger = logging.getLogger(__name__)


def _dig(src: dict, *path, default=None):
    """Safe dict traversal: _dig(d, 'a','b','c') -> d['a']['b']['c'] or default."""
    cur: Any = src
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


class MessageRouter:
    """
    Routes Chatwoot outbound webhook payloads to the right Telegram adapter.

    Adapters are keyed by Chatwoot inbox_id, so multi-account setups stay
    unambiguous: each Telegram session has its own inbox.
    """

    def __init__(self, adapters_by_inbox: Dict[int, TelegramAdapter]):
        self.adapters_by_inbox = adapters_by_inbox

    def _derive_recipient_id(self, payload: dict) -> str | None:
        """
        Build a Telethon-compatible recipient id from the Chatwoot sender:
          1) sender.custom_attributes.telegram_username      -> '@username'
          2) sender.additional_attributes.social_telegram_user_name (Chatwoot TG bot) -> '@username'
          3) sender.phone_number                             -> '+7999...'
          4) sender.custom_attributes.telegram_user_id       -> 'id:<int>'
          5) sender.additional_attributes.social_telegram_user_id (Chatwoot TG bot) -> 'id:<int>'
        """
        sender = _dig(payload, "conversation", "meta", "sender", default={}) or {}

        username = (sender.get("custom_attributes", {}) or {}).get("telegram_username") or ""
        username = username.strip()
        if username:
            return username

        social_username = (sender.get("additional_attributes", {}) or {}).get(
            "social_telegram_user_name", ""
        )
        social_username = (social_username or "").strip()
        if social_username:
            return social_username

        phone = (sender.get("phone_number") or "").strip()
        if phone:
            return phone

        tg_uid = (sender.get("custom_attributes", {}) or {}).get("telegram_user_id")
        if tg_uid is not None and str(tg_uid).strip():
            return f"id:{tg_uid}"

        social_tg_uid = (sender.get("additional_attributes", {}) or {}).get(
            "social_telegram_user_id"
        )
        if social_tg_uid is not None and str(social_tg_uid).strip():
            return f"id:{social_tg_uid}"

        return None

    async def handle_outgoing(self, payload: dict) -> None:
        """Process a Chatwoot 'message_created' outbound webhook and dispatch."""
        try:
            cw = ChatwootMessageCreatedWebhook.model_validate(payload)
        except Exception as e:
            logger.warning("[router] invalid Chatwoot payload: %s", e)
            return

        if cw.event != "message_created":
            return
        if cw.private:
            return
        if cw.message_type != "outgoing":
            return

        # The HTTP layer injected the resolved inbox_id into payload['_inbox_id']
        inbox_id = payload.get("_inbox_id")
        if not isinstance(inbox_id, int):
            logger.warning("[router] no inbox_id resolved on payload — dropping")
            return

        adapter = self.adapters_by_inbox.get(inbox_id)
        if not adapter:
            logger.warning("[router] no adapter for inbox_id=%s", inbox_id)
            return

        text = (cw.content or "").strip()
        recipient_id = self._derive_recipient_id(payload)

        if not recipient_id or not text:
            logger.warning(
                "[router] missing fields: inbox=%s recipient_id=%r text=%r",
                inbox_id,
                recipient_id,
                text,
            )
            return

        await adapter.send_text(recipient_id, TextContent(type="text", text=text))
        logger.info(
            "[router] OUTBOUND inbox=%s recipient=%s text=%r",
            inbox_id,
            recipient_id,
            text,
        )
