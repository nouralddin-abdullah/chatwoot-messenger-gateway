import asyncio
import logging
import os
import re
from typing import Optional

from pyee.asyncio import AsyncIOEventEmitter
from telethon import TelegramClient, errors, events, functions, types

from app.config import TelegramAccountConfig
from app.domain.message import TextContent

logger = logging.getLogger(__name__)

# Accept @username or plain username (min length 5)
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{5,}$")
# E.164-like phone pattern: optional + and 7..15 digits
PHONE_RE = re.compile(r"^\+?\d{7,15}$")

# Where session files live inside the container (mount a named volume here).
SESSIONS_DIR = os.environ.get("TG_SESSIONS_DIR", "/app/sessions")


class TelegramAdapter:
    """
    Personal Telegram account bridge (non-bot) backed by Telethon.

    One instance per Telegram account. The adapter:
      - listens for incoming messages on its own session
      - emits a bus event keyed by inbox_id so multi-account setups don't collide
      - sends outbound text via Telethon when the router calls send_text(...)
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
        """Per-account bus event name to keep multi-account routing unambiguous."""
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
        # connect() rather than start() so we never block on stdin in a container
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
            sender = await event.get_sender()
            payload = {
                "text": event.text,
                "from_id": str(getattr(sender, "id", "") or ""),
                "username": getattr(sender, "username", None),
                "name": (
                    getattr(sender, "first_name", None)
                    or getattr(sender, "username", None)
                    or str(getattr(sender, "id", ""))
                ),
                "inbox_id": self.inbox_id,
                "session_name": self.session_name,
            }
            self.bus.emit(self.incoming_event, payload)

        me = await self.client.get_me()
        logger.info(
            "[telegram:%s] logged in as @%s (inbox=%s)",
            self.session_name,
            getattr(me, "username", None),
            self.inbox_id,
        )

    async def stop(self) -> None:
        if self.client and self.client.is_connected():
            await self.client.disconnect()
        logger.info("[telegram:%s] adapter stopped", self.session_name)

    async def _resolve_entity(self, raw: str):
        """
        Resolve Telethon 'entity' from a recipient string.
        Supported formats:
          - @username or username
          - phone number (+79991234567)  — imported into contacts on first send
          - id:<int> or a bare integer (user_id)  — only works if session has cached access_hash
        """
        rid = (raw or "").strip()
        if not rid:
            raise ValueError("recipient_id is empty")

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

        raise ValueError("recipient_id must be @username, phone number, or id:<int>")

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
