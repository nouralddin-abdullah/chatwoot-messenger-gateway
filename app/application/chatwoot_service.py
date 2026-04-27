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
        search_key: str,
        name: Optional[str],
        phone: Optional[str],
        email: Optional[str],
        custom_attributes: Dict[str, Any],
        additional_attributes: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Upsert contact and return {'id', 'source_id'}.
        Strategy:
          1) If custom_attributes contains telegram_user_id, look up via /contacts/filter
             so we match on the platform id even when the contact has no name yet.
          2) Otherwise fall back to /contacts/search with the search_key.
          3) If found -> update attributes (best effort).
          4) If not found -> create with inbox_id + attributes.
        """
        contacts = []

        attr_lookup_keys = [
            k for k in ("telegram_user_id",) if k in (custom_attributes or {})
        ]
        if attr_lookup_keys:
            try:
                res = await self._client.filter_contacts(
                    {k: custom_attributes[k] for k in attr_lookup_keys}
                )
                contacts = (res or {}).get("payload") or []
            except Exception as e:
                logger.warning("[chatwoot] filter_contacts failed: %s", e)

        if not contacts:
            try:
                res = await self._client.search_contacts(q=search_key)
                contacts = (res or {}).get("payload") or []
            except Exception as e:
                logger.warning("[chatwoot] search_contacts failed: %s", e)

        if contacts:
            contact = contacts[0]
            contact_id = int(contact.get("id"))
            if custom_attributes or additional_attributes is not None:
                try:
                    await self._client.update_contact(
                        contact_id=contact_id,
                        custom_attributes=custom_attributes,
                        additional_attributes=additional_attributes,
                    )
                except Exception as e:
                    logger.warning("[chatwoot] update_contact skipped: %s", e)
            if name and not (contact.get("name") or "").strip():
                try:
                    await self._client.update_contact(
                        contact_id=contact_id, name=name
                    )
                except Exception as e:
                    logger.warning("[chatwoot] update name skipped: %s", e)
        else:
            created = await self._client.create_contact(
                inbox_id=inbox_id,
                name=name or search_key,
                phone_number=phone,
                email=email,
                custom_attributes=custom_attributes or {},
                additional_attributes=additional_attributes,
            )
            payload = (created or {}).get("payload") or {}
            contact = payload.get("contact") or created.get("contact") or {}
            if not contact and "id" in (created or {}):
                contact = created

        source_id = self._extract_source_id_for_inbox(contact, inbox_id) or search_key
        logger.info(
            "[chatwoot] ensure_contact ok id=%s inbox=%s source_id=%r",
            contact.get("id"),
            inbox_id,
            source_id,
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

        for conv in conversations:
            if conv.get("status") not in ("open", "pending"):
                continue
            nested_source_id = (
                (conv.get("last_non_activity_message") or {})
                .get("conversation", {})
                .get("contact_inbox", {})
                .get("source_id")
            )
            if nested_source_id == source_id:
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
