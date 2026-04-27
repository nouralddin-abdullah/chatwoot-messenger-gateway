import logging
from typing import Any, Dict, List

from pyee.asyncio import AsyncIOEventEmitter

from app.application.chatwoot_service import ChatwootService
from app.application.router import MessageRouter
from app.config import AppConfig
from app.infra.adapters.telegram_telethon import TelegramAdapter
from app.infra.chatwoot_client import ChatwootClient

logger = logging.getLogger(__name__)


def wire_events(
    bus: AsyncIOEventEmitter,
    config: AppConfig,
    adapters: List[TelegramAdapter],
    router: MessageRouter,
) -> None:
    """
    Forward incoming Telegram events to Chatwoot. Each adapter listens on its
    own per-account bus event so multi-account setups don't collide.

    A contact in Chatwoot is keyed by its `identifier`:
      - "telegram:<user_id>"        for a direct DM partner
      - "telegram_group:<chat_id>"  for a group / supergroup
    """
    cw_client = ChatwootClient(
        api_access_token=config.chatwoot.api_access_token,
        account_id=config.chatwoot.account_id,
        base_url=str(config.chatwoot.base_url),
    )
    cw = ChatwootService(client=cw_client)

    def make_handler(inbox_id: int):
        async def _handler(payload: Dict[str, Any]) -> None:
            try:
                kind = payload.get("kind", "dm")
                text = (payload.get("text") or "").strip()

                if kind == "group":
                    chat_id = payload.get("chat_id") or ""
                    chat_title = payload.get("chat_title") or f"Group {chat_id}"
                    sender_name = payload.get("sender_name") or "?"
                    identifier = f"telegram_group:{chat_id}"
                    contact_name = chat_title
                    custom_attrs: Dict[str, Any] = {
                        "telegram_chat_id": chat_id,
                        "telegram_chat_type": "group",
                    }
                    # prefix the per-message body with the actual sender so
                    # one Chatwoot conversation can host many participants
                    if text:
                        text = f"[{sender_name}] {text}"
                    else:
                        text = f"[{sender_name}]"
                else:  # dm
                    from_id = str(payload.get("from_id") or "")
                    username = payload.get("username")
                    identifier = f"telegram:{from_id}"
                    contact_name = (
                        payload.get("name") or username or from_id
                    )
                    custom_attrs = {"telegram_user_id": from_id}
                    if username:
                        custom_attrs["telegram_username"] = username

                contact = await cw.ensure_contact(
                    inbox_id=inbox_id,
                    identifier=identifier,
                    name=contact_name,
                    custom_attributes=custom_attrs,
                )

                conv_id = await cw.ensure_conversation(
                    inbox_id=inbox_id,
                    contact_id=contact["id"],
                    source_id=contact["source_id"],
                )

                await cw.create_message(
                    conversation_id=conv_id,
                    content=text,
                    direction="incoming",
                )
                logger.info(
                    "[events] telegram %s -> chatwoot OK inbox=%s conv=%s id=%s",
                    kind,
                    inbox_id,
                    conv_id,
                    identifier,
                )
            except Exception as e:
                logger.exception(
                    "[events] telegram inbox=%s handling failed: %s", inbox_id, e
                )

        return _handler

    for adapter in adapters:
        bus.on(adapter.incoming_event, make_handler(adapter.inbox_id))

    @bus.on("chatwoot.outgoing")
    async def _chatwoot_outgoing(payload: Dict[str, Any]) -> None:
        await router.handle_outgoing(payload)
