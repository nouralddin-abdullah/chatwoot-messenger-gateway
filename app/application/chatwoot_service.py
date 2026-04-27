import logging
from typing import Any, Dict, Literal, Optional

from app.infra.chatwoot_client import ChatwootClient

logger = logging.getLogger(__name__)


class ChatwootService:
    """Uses ChatwootClient to upsert contact, ensure conversation, and post messages."""

    def __init__(self, client: ChatwootClient):
        self._client = client

    async def ensure_contact(
        self,
        *,
        inbox_id: int,
        identifier: str,
        name: Optional[str],
        custom_attributes: Optional[Dict[str, Any]] = None,
        additional_attributes: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Upsert a contact keyed by Chatwoot's built-in `identifier` column.

        We use identifier as the single canonical key (e.g. "telegram:12345"
        for a user, "telegram_group:-100abc" for a group). identifier is a
        real indexed column on the contacts table, so lookup is reliable
        across versions — unlike /contacts/filter on custom_attributes which
        depends on schema definitions and has been buggy.

        Returns {'id': int, 'source_id': str}.
        """
        contacts = []

        # search_contacts hits identifier among other fields; we filter the
        # results to exact identifier match so we don't accidentally take a
        # partial name hit.
        try:
            res = await self._client.search_contacts(q=identifier)
            all_contacts = (res or {}).get("payload") or []
            contacts = [c for c in all_contacts if (c.get("identifier") or "") == identifier]
        except Exception as e:
            logger.warning("[chatwoot] search by identifier failed: %s", e)

        if contacts:
            contact = contacts[0]
            contact_id = int(contact.get("id"))
            try:
                await self._client.update_contact(
                    contact_id=contact_id,
                    identifier=identifier,
                    custom_attributes=custom_attributes,
                    additional_attributes=additional_attributes,
                )
            except Exception as e:
                logger.warning("[chatwoot] update_contact skipped: %s", e)
            if name and not (contact.get("name") or "").strip():
                try:
                    await self._client.update_contact(contact_id=contact_id, name=name)
                except Exception as e:
                    logger.warning("[chatwoot] update name skipped: %s", e)
        else:
            created = await self._client.create_contact(
                inbox_id=inbox_id,
                name=name or identifier,
                identifier=identifier,
                custom_attributes=custom_attributes or {},
                additional_attributes=additional_attributes,
            )
            payload = (created or {}).get("payload") or {}
            contact = payload.get("contact") or created.get("contact") or {}
            if not contact and "id" in (created or {}):
                contact = created

        source_id = self._extract_source_id_for_inbox(contact, inbox_id) or identifier
        logger.info(
            "[chatwoot] ensure_contact id=%s inbox=%s identifier=%s",
            contact.get("id"),
            inbox_id,
            identifier,
        )
        return {"id": int(contact.get("id")), "source_id": source_id}

    def _extract_source_id_for_inbox(
        self, contact: Dict[str, Any], inbox_id: int
    ) -> Optional[str]:
        """Find source_id for a specific inbox in contact_inboxes."""
        for ci in contact.get("contact_inboxes", []) or []:
            inbox = (ci or {}).get("inbox") or {}
            if int(inbox.get("id") or 0) == int(inbox_id):
                sid = ci.get("source_id")
                if sid:
                    return sid
        return None

    async def ensure_conversation(
        self,
        *,
        inbox_id: int,
        contact_id: int,
        source_id: str,
        custom_attributes: Optional[Dict[str, Any]] = None,
    ) -> int:
        res = await self._client.list_conversations(contact_id)
        conversations = (res or {}).get("payload") or []

        # Reuse any active conversation belonging to this contact + inbox.
        # A contact has at most one contact_inbox per inbox, so all conversations
        # for that pair share the same source_id; matching on inbox_id is enough.
        for conv in conversations:
            if conv.get("status") not in ("open", "pending"):
                continue
            if int(conv.get("inbox_id") or 0) != int(inbox_id):
                continue
            logger.info("[chatwoot] reuse conversation id=%s", conv.get("id"))
            return int(conv["id"])

        extra: Dict[str, Any] = {}
        if custom_attributes:
            extra["custom_attributes"] = custom_attributes

        created = await self._client.create_conversation(
            inbox_id=inbox_id,
            source_id=source_id,
            contact_id=contact_id,
            **extra,
        )
        conv_id = (created or {}).get("id") or (
            (created or {}).get("payload") or {}
        ).get("id")
        logger.info("[chatwoot] create conversation id=%s inbox=%s", conv_id, inbox_id)
        return int(conv_id)

    async def create_message(
        self,
        *,
        conversation_id: int,
        content: str,
        direction: Literal["incoming", "outgoing"],
    ) -> int:
        message_type = "incoming" if direction == "incoming" else "outgoing"
        res = await self._client.send_message(
            conversation_id=conversation_id,
            content=content or "",
            message_type=message_type,
        )
        msg_id = (res or {}).get("id") or ((res or {}).get("payload") or {}).get("id")
        logger.info("[chatwoot] create_message id=%s type=%s", msg_id, message_type)
        return int(msg_id)
