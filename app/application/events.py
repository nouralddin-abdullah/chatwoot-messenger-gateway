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
    Wire bus event handlers.

    For each Telegram adapter we listen on its per-account incoming event
    (`telegram.incoming.<inbox_id>`) and forward the message to Chatwoot.
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
                text = (payload.get("text") or "").strip()
                from_id = str(payload.get("from_id") or "")
                username = payload.get("username")
                name = payload.get("name") or username or from_id

                custom_attributes: Dict[str, Any] = {}
                if from_id:
                    custom_attributes["telegram_user_id"] = from_id
                if username:
                    custom_attributes["telegram_username"] = username

                search_key = username or from_id

                contact = await cw.ensure_contact(
                    inbox_id=inbox_id,
                    search_key=search_key,
                    name=name,
                    phone=None,
                    email=None,
                    custom_attributes=custom_attributes,
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
                    "[events] telegram -> chatwoot OK inbox=%s conv=%s",
                    inbox_id,
                    conv_id,
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
