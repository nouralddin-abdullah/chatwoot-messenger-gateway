import os
import re
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, HttpUrl, ValidationError


class TelegramAccountConfig(BaseModel):
    """One personal Telegram account bridged to one Chatwoot inbox."""

    session_name: str        # filename for the persisted Telethon session
    inbox_id: int            # Chatwoot inbox bound to this account
    webhook_id: str          # path token Chatwoot uses to reach this account


class TelegramConfig(BaseModel):
    """Shared Telegram dev-app credentials + the list of accounts."""

    api_id: int
    api_hash: str
    accounts: List[TelegramAccountConfig] = Field(default_factory=list)


class ChatwootWebhookConfig(BaseModel):
    api_access_token: str
    account_id: int
    base_url: HttpUrl
    # Map webhook_id -> inbox_id (used to route Chatwoot outbound to the right adapter)
    inbox_by_webhook_id: Dict[str, int] = Field(default_factory=dict)


class AppConfig(BaseModel):
    telegram: TelegramConfig
    chatwoot: ChatwootWebhookConfig


def _getenv(name: str) -> str:
    """Get required environment variable or raise RuntimeError."""
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return v


_INDEXED_ACCOUNT_RE = re.compile(r"^TG_(\d+)_SESSION_NAME$")


def _discover_accounts() -> List[TelegramAccountConfig]:
    """
    Find all Telegram accounts defined via env vars.

    Indexed form (preferred for multi-account):
        TG_1_SESSION_NAME, TG_1_INBOX_ID, TG_1_WEBHOOK_ID
        TG_2_SESSION_NAME, TG_2_INBOX_ID, TG_2_WEBHOOK_ID
        ...

    Single-account legacy form (auto-mapped to index 1 if no indexed vars exist):
        TG_SESSION_NAME, TG_INBOX_ID, TG_WEBHOOK_ID
    """
    indexes: List[int] = sorted(
        int(m.group(1))
        for k in os.environ
        if (m := _INDEXED_ACCOUNT_RE.match(k))
    )

    accounts: List[TelegramAccountConfig] = []

    if indexes:
        for i in indexes:
            accounts.append(
                TelegramAccountConfig(
                    session_name=_getenv(f"TG_{i}_SESSION_NAME"),
                    inbox_id=int(_getenv(f"TG_{i}_INBOX_ID")),
                    webhook_id=_getenv(f"TG_{i}_WEBHOOK_ID"),
                )
            )
        return accounts

    # Legacy single-account fallback
    if os.getenv("TG_SESSION_NAME"):
        accounts.append(
            TelegramAccountConfig(
                session_name=_getenv("TG_SESSION_NAME"),
                inbox_id=int(_getenv("TG_INBOX_ID")),
                webhook_id=_getenv("TG_WEBHOOK_ID"),
            )
        )

    return accounts


def load_config() -> AppConfig:
    try:
        accounts = _discover_accounts()
        if not accounts:
            raise RuntimeError(
                "No Telegram accounts configured. Set TG_1_SESSION_NAME, "
                "TG_1_INBOX_ID, TG_1_WEBHOOK_ID (and so on for additional "
                "accounts), or the legacy TG_SESSION_NAME/TG_INBOX_ID/"
                "TG_WEBHOOK_ID for a single account."
            )

        # Detect duplicate inbox_id or webhook_id which would break routing
        seen_inbox: set = set()
        seen_hook: set = set()
        for a in accounts:
            if a.inbox_id in seen_inbox:
                raise RuntimeError(f"Duplicate TG_*_INBOX_ID: {a.inbox_id}")
            if a.webhook_id in seen_hook:
                raise RuntimeError(f"Duplicate TG_*_WEBHOOK_ID: {a.webhook_id}")
            seen_inbox.add(a.inbox_id)
            seen_hook.add(a.webhook_id)

        telegram_cfg = TelegramConfig(
            api_id=int(_getenv("TG_API_ID")),
            api_hash=_getenv("TG_API_HASH"),
            accounts=accounts,
        )

        inbox_by_webhook_id = {a.webhook_id: a.inbox_id for a in accounts}

        return AppConfig(
            telegram=telegram_cfg,
            chatwoot=ChatwootWebhookConfig(
                api_access_token=_getenv("CHATWOOT_API_ACCESS_TOKEN"),
                account_id=int(_getenv("CHATWOOT_ACCOUNT_ID")),
                base_url=_getenv("CHATWOOT_BASE_URL"),
                inbox_by_webhook_id=inbox_by_webhook_id,
            ),
        )
    except ValidationError as e:
        raise RuntimeError(f"Invalid configuration: {e}") from e
