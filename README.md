# Chatwoot ↔ Telegram Bridge

Bridge **personal Telegram accounts** (non-bot) to Chatwoot. Multi-account from
day one — one Telethon session per Chatwoot inbox.

Forked from [feel90d/chatwoot-messenger-gateway][upstream], stripped down to
just the Telegram path, refactored for multi-account, and packaged for Docker.

[upstream]: https://github.com/feel90d/chatwoot-messenger-gateway

## What this is

Chatwoot's native Telegram integration only supports **bots** (BotFather token).
Bots can't read your DMs, can't see your groups, and can only message users who
messaged the bot first. Useless for using Chatwoot as a CRM over your real
Telegram identity.

This service plugs that gap. It logs into Telegram **as you** via Telethon
(MTProto user-API), watches your incoming messages, and forwards them to a
Chatwoot **API channel** inbox. When an agent replies in Chatwoot, the reply
goes back out through your Telegram account.

## How it fits together

```
                       ┌─────────────────────────────┐
                       │  Telegram servers (MTProto) │
                       └──────────────┬──────────────┘
                                      │  one TCP/TLS session per account
                       ┌──────────────┴──────────────┐
                       │  this service               │
                       │   - Telethon clients (N)    │
                       │   - FastAPI webhook server  │
                       └──────────────┬──────────────┘
                  Telegram→Chatwoot   │   Chatwoot→Telegram
              (REST: contacts, msgs)  │   (webhook from Chatwoot per inbox)
                       ┌──────────────┴──────────────┐
                       │  Chatwoot                   │
                       └─────────────────────────────┘
```

Per Telegram account, one Channel::Api inbox in Chatwoot. Outbound replies
arrive at `/chatwoot/webhook/<webhook_id>`; the bridge maps `webhook_id →
inbox_id → adapter` and Telethon sends as the right account.

## Risks worth knowing

Telegram allows user-API access but flags accounts that look like bots.
Reading via the bridge and replying manually through Chatwoot is fine. The
following will get an account banned:

- auto-replies, mass DMs, scheduled broadcasts
- replying to people who never wrote to you (cold outreach)
- replies that arrive faster than a human could realistically type

Use it like a CRM, not like a marketing tool.

## Quick start (Docker, recommended)

The repo ships a `Dockerfile`, a `docker-compose.example.yml`, and an
`auth.py` helper. Recommended layout: clone next to your existing
Chatwoot/Evolution stack.

### 1. Clone and configure

```bash
git clone https://github.com/nouralddin-abdullah/chatwoot-messenger-gateway.git
cd chatwoot-messenger-gateway
cp .env_template .env
```

Fill in `.env`:

- **Chatwoot section** — copy your admin API token (Profile → API Access
  Token), account id (`1` for single-tenant), and `CHATWOOT_BASE_URL`
  (e.g. `https://chat.example.com`).
- **TG_API_ID / TG_API_HASH** — create a Telegram dev app at
  [my.telegram.org → API development tools](https://my.telegram.org). One app
  is enough for many sessions.
- **One block per account**: `TG_1_SESSION_NAME`, `TG_1_INBOX_ID`,
  `TG_1_WEBHOOK_ID`. Generate webhook IDs with:
  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```
  Repeat for `TG_2_*`, `TG_3_*`, ... as you add more accounts.

### 2. Create the Chatwoot inboxes

For each Telegram account, create a Chatwoot inbox of type **API** (not
Telegram, not Email). Note the inbox id from the URL (`.../inboxes/8` ⇒
`INBOX_ID=8`) and put it in `.env`.

In each inbox's **Configuration → Webhook URL**, paste:
```
https://<your-bridge-host>/chatwoot/webhook/<the-TG_N_WEBHOOK_ID-for-this-inbox>
```

If you only run the bridge on the internal Docker network, set Chatwoot's
inbox webhook URL to `http://telegram-bridge:8000/chatwoot/webhook/<id>` —
Chatwoot's Sidekiq workers can reach internal hostnames.

### 3. Add the service to your compose stack

Copy the snippet from `docker-compose.example.yml` into your existing
`docker-compose.yml` (alongside chatwoot-rails, chatwoot-sidekiq, etc.) and add
the named volume.

### 4. Authenticate each Telegram account (one-time)

This is interactive — you'll enter your phone number, the SMS code Telegram
sends, and your 2FA password if set:

```bash
docker compose run --rm telegram-bridge python auth.py <session_name>
```

Run once per account, using the `TG_N_SESSION_NAME` value as
`<session_name>`. The session file is written to the
`telegram_sessions` named volume and persists across container restarts.

### 5. Start the service

```bash
docker compose up -d telegram-bridge
docker compose logs -f telegram-bridge
```

You should see one `[telegram:<session>] logged in as @username (inbox=N)`
line per configured account, then `Application startup complete`.

### 6. Test

Send a Telegram DM to one of the bridged accounts from another phone. The
conversation should appear in the matching Chatwoot inbox within ~1s. Reply
from Chatwoot and watch the message arrive on the original sender's phone.

## Health endpoint

```bash
curl http://localhost:8000/health
```

Returns the configured Chatwoot account, base URL, and the list of Telegram
accounts (session names + bound inbox ids). No secrets exposed.

## Adding another account later

1. Append a new `TG_N_*` block to `.env`
2. Create a Chatwoot API inbox for it; put the `inbox_id` and a fresh
   `webhook_id` in `.env`
3. `docker compose run --rm telegram-bridge python auth.py <new-session-name>`
4. `docker compose up -d --force-recreate telegram-bridge`

## Troubleshooting

**Service crashes with "session is NOT authorized"** — you started before
running `auth.py` for that session. Run the auth helper, then restart.

**Outbound message logged but never delivered** — Telethon couldn't resolve
the recipient. Check the contact in Chatwoot has either
`telegram_username`, `telegram_user_id`, or a `phone_number` set. New contacts
created by the bridge get these populated automatically; manually-added
contacts may not.

**Account got "limited" by Telegram** — slow down. Spam patterns from a
user-API session get the account flagged. Wait it out, then use the account
manually for a few days.

**`FloodWaitError`** — Telegram's rate limit kicked in. The error includes a
seconds count; don't retry until it elapses.

## Architecture notes

- **One adapter per Telegram account**, keyed by Chatwoot `inbox_id`. The
  `MessageRouter` looks up the adapter by inbox to dispatch outbound.
- **Bus event names are per-account** (`telegram.incoming.<inbox_id>`) so
  multiple Telethon clients don't share an event channel.
- **Sessions are persisted** to `/app/sessions` (mount a named volume in
  production). Losing them forces re-auth.
- **Text only** for now. Media support is scaffolded
  (`MediaContent`/`StickerContent`/`LocationContent` in `app/domain/message.py`)
  but `send_text` is the only outbound path wired up.

## Acknowledgments

Original code by [@feel90d](https://github.com/feel90d) and
[@lukyan0v_a](https://github.com/lukyan0v_a). This fork drops the WhatsApp
(Wasender) and VK adapters, refactors for multi-account Telegram, and adds
Docker packaging.

## License

MIT (see upstream).
