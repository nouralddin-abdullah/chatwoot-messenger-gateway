from typing import Optional

from pydantic import BaseModel


class ChatwootConversationMeta(BaseModel):
    channel: Optional[str] = None  # "telegram" (single channel in this fork)
    recipient_id: Optional[str] = None  # unified recipient id


class ChatwootConversation(BaseModel):
    meta: ChatwootConversationMeta = ChatwootConversationMeta()


class ChatwootMessageCreatedWebhook(BaseModel):
    """Minimal model for Chatwoot event=message_created."""

    event: str
    message_type: Optional[str] = None  # "incoming" | "outgoing"
    private: Optional[bool] = None
    content: Optional[str] = None
    conversation: ChatwootConversation = ChatwootConversation()
