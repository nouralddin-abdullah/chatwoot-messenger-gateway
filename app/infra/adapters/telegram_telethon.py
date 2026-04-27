import logging
import os
import re
from typing import Optional

from pyee.asyncio import AsyncIOEventEmitter
from telethon import TelegramClient, errors, events, functions, types

from app.config import TelegramAccountConfig
from app.domain.message import TextContent

logger = logging.getLogger(__name__)

# Recipient-id formats supported in send_text:
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{5,}$")
PHONE_RE = re.compile(r"^\+?\d{7,15}$")

SESSIONS_DIR = os.environ.get("TG_SESSIONS_DIR", "/app/sessions")

# Whether to surface group/supergroup messages into Chatwoot. Off by default;
# CRMs almost always want only DMs. Set TG_INCLUDE_GROUPS=true to opt in.
INCLUDE_GROUPS = os.environ.get("TG_INCLUDE_GROUPS", "false").lower() in (
    "1", "true", "yes", "on"
)


class TelegramAdapter:
    """
    Personal Telegram account bridge (non-bot) backed by Telethon.

    One instance per Telegram account. Emits per-account bus events so multi-
    account setups stay unambiguous (telegram.incoming.<inbox_id>).
    """

    def __init__(
        self,
        bus: AsyncIOEventEmitter,
        api_id: int,
        api_hash: str,
        account: TelegramAccountConfig,
    ):
        self.bus = bus
        self._api_id = api_id
        self._api_hash = api_hash
        self._account = account
        self.inbox_id = account.inbox_id
        self.session_name = account.session_name
        self.client: Optional[TelegramClient] = None

    @property
    def incoming_event(self) -> str:
        return f"telegram.incoming.{self.inbox_id}"

    async def start(self) -> None:
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        session_path = os.path.join(SESSIONS_DIR, self.session_name)

        self.client = TelegramClient(
            session_path,
            self._api_id,
            self._api_hash,
            device_model="iPhone 14",
            system_version="16.5",
            app_version="8.4.1",
            lang_code="en",
            system_lang_code="en-US",
        )
        await self.client.connect()

        if not await self.client.is_user_authorized():
            logger.error(
                "[telegram:%s] session is NOT authorized — run auth.py inside "
                "the container to log in once before starting the service.",
                self.session_name,
            )
            return

        @self.client.on(events.NewMessage(incoming=True))
        async def handle_incoming(event):
            # Skip broadcast channels — they're not relevant for CRM use
            if event.is_channel and not event.is_group:
                return

            if event.is_group:
                if not INCLUDE_GROUPS:
                    return
                await self._dispatch_group_message(event)
            else:
                await self._dispatch_dm(event)

        me = await self.client.get_me()
        logger.info(
            "[telegram:%s] logged in as @%s (inbox=%s, groups=%s)",
            self.session_name,
            getattr(me, "username", None),
            self.inbox_id,
            "on" if INCLUDE_GROUPS else "off",
        )

    async def _dispatch_dm(self, event) -> None:
        sender = await event.get_sender()
        from_id = str(getattr(sender, "id", "") or "")
        username = getattr(sender, "username", None)
        first_name = getattr(sender, "first_name", None)

        payload = {
            "kind": "dm",
            "text": event.text or "",
            "from_id": from_id,
            "username": username,
            "name": first_name or username or from_id,
            "inbox_id": self.inbox_id,
            "session_name": self.session_name,
        }
        self.bus.emit(self.incoming_event, payload)

    async def _dispatch_group_message(self, event) -> None:
        chat = await event.get_chat()
        sender = await event.get_sender()
        chat_id = str(getattr(chat, "id", ""))
        chat_title = getattr(chat, "title", "") or f"Group {chat_id}"
        sender_name = (
            getattr(sender, "first_name", None)
            or getattr(sender, "username", None)
            or str(getattr(sender, "id", ""))
        )

        payload = {
            "kind": "group",
            "text": event.text or "",
            "chat_id": chat_id,
            "chat_title": chat_title,
            "sender_name": sender_name,
            "sender_id": str(getattr(sender, "id", "")),
            "sender_username": getattr(sender, "username", None),
            "inbox_id": self.inbox_id,
            "session_name": self.session_name,
        }
        self.bus.emit(self.incoming_event, payload)

    async def stop(self) -> None:
        if self.client and self.client.is_connected():
            await self.client.disconnect()
        logger.info("[telegram:%s] adapter stopped", self.session_name)

    async def _resolve_entity(self, raw: str):
        """
        Resolve Telethon 'entity' from a recipient string.
        Supported formats:
          - telegram:<user_id>       -> direct user lookup by id (preferred)
          - telegram_group:<chat_id> -> group/supergroup lookup by id
          - @username or username    -> public handle
          - +phone (E.164-ish)       -> imported into contacts on first send
          - id:<int> or bare integer -> user_id (legacy)
        """
        rid = (raw or "").strip()
        if not rid:
            raise ValueError("recipient_id is empty")

        if rid.startswith("telegram:"):
            try:
                user_id = int(rid[len("telegram:"):])
                return await self.client.get_entity(user_id)
            except (ValueError, errors.rpcerrorlist.PeerIdInvalidError):
                raise RuntimeError(
                    "Cannot resolve telegram:<user_id>. The session needs prior "
                    "interaction with this user before sending by id is allowed."
                )

        if rid.startswith("telegram_group:"):
            chat_id = int(rid[len("telegram_group:"):])
            return await self.client.get_entity(chat_id)

        if USERNAME_RE.match(rid):
            return rid.lstrip("@")

        if PHONE_RE.match(rid):
            await self.client(
                functions.contacts.ImportContactsRequest(
                    contacts=[
                        types.InputPhoneContact(
                            client_id=0, phone=rid, first_name="", last_name=""
                        )
                    ]
                )
            )
            return rid

        if rid.startswith("id:"):
            rid = rid[3:].strip()

        if rid.isdigit():
            user_id = int(rid)
            try:
                return await self.client.get_entity(user_id)
            except (ValueError, errors.rpcerrorlist.PeerIdInvalidError):
                raise RuntimeError(
                    "Cannot resolve user by user_id. "
                    "Use @username or phone number (the phone will be imported)."
                )

        raise ValueError(
            "recipient_id must be telegram:<id>, telegram_group:<id>, "
            "@username, phone number, or id:<int>"
        )

    async def send_text(self, recipient_id: str, content: TextContent) -> None:
        if not self.client or not self.client.is_connected():
            logger.warning(
                "[telegram:%s] client not connected; skipping send",
                self.session_name,
            )
            return

        try:
            entity = await self._resolve_entity(recipient_id)
            await self.client.send_message(entity, content.text)
            logger.info(
                "[telegram:%s] SENT %s -> %s",
                self.session_name,
                recipient_id,
                content.text,
            )
        except errors.rpcerrorlist.FloodWaitError as e:
            logger.error(
                "[telegram:%s] FloodWait: wait %s seconds",
                self.session_name,
                e.seconds,
            )
        except errors.rpcerrorlist.PeerFloodError:
            logger.error(
                "[telegram:%s] PeerFloodError: too many first messages",
                self.session_name,
            )
        except Exception as e:
            logger.exception(
                "[telegram:%s] failed to send text: %s", self.session_name, e
            )
