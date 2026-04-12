# Telegram Integration Implementation Plan

## Phase 1: Message Receive Webhook (Week 1)

### Files to Create
1. `src/secretary/telegram_gateway.py` — FastAPI webhook + routing
2. `tests/test_telegram_webhook.py` — Webhook tests

### Files to Modify
1. `src/secretary/config.py` — Add TelegramConfig dataclass
2. `config.yaml` — Add telegram.bot_token, telegram.allowed_chats

### Code Skeleton

**src/secretary/telegram_gateway.py:**
```python
from fastapi import FastAPI, Request, HTTPException
from typing import Optional
import logging
import time

log = logging.getLogger(__name__)

class TelegramGateway:
    def __init__(self, bot_token: str, allowed_chats: list[int], memory: MemoryStore):
        self.bot_token = bot_token
        self.allowed_chats = set(allowed_chats)
        self.memory = memory
        self.app = FastAPI()
        self._setup_routes()
    
    def _setup_routes(self):
        @self.app.post("/webhook/telegram")
        async def handle_telegram_update(request: Request):
            """Parse Telegram Update, validate, store."""
            # 1. Validate X-Telegram-Bot-API-Secret-Token header
            secret = request.headers.get("X-Telegram-Bot-API-Secret-Token", "")
            if secret != self.bot_token:
                raise HTTPException(status_code=401, detail="Invalid bot token")
            
            # 2. Parse update
            update = await request.json()
            if "message" not in update:
                return {"ok": True}  # Ignore non-message updates
            
            msg = update["message"]
            chat_id = msg["chat"]["id"]
            user_id = msg["from"]["id"]
            text = msg.get("text", "")
            
            # 3. Whitelist check
            if chat_id not in self.allowed_chats:
                log.warning(f"Telegram message from unauthorized chat {chat_id}")
                return {"ok": True}
            
            # 4. Store in memory
            await self.memory.store_telegram_message({
                "chat_id": chat_id,
                "user_id": user_id,
                "text": text,
                "timestamp": time.time(),
                "message_id": msg.get("message_id"),
            })
            
            log.info(f"Telegram {chat_id}: {text}")
            return {"ok": True}
```

**config.py addition:**
```python
@dataclass
class TelegramConfig:
    bot_token: str = ""
    allowed_chats: list[int] = field(default_factory=list)
    webhook_url: Optional[str] = None
```

**config.yaml addition:**
```yaml
telegram:
  bot_token: ${TELEGRAM_BOT_TOKEN}
  allowed_chats: [123456789, 987654321]  # Whitelist
  webhook_url: https://your-domain.com/webhook/telegram
```

### Tests
```python
# tests/test_telegram_webhook.py
import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_telegram_webhook_valid_message(telegram_gateway, memory):
    payload = {
        "message": {
            "chat": {"id": 123},
            "from": {"id": 456},
            "text": "hello",
            "message_id": 1
        }
    }
    response = await telegram_gateway.app.post("/webhook/telegram", json=payload)
    assert response.status_code == 200
    stored = await memory.get_last_telegram_message()
    assert stored["text"] == "hello"

@pytest.mark.asyncio
async def test_telegram_webhook_unauthorized_chat(telegram_gateway):
    payload = {
        "message": {
            "chat": {"id": 999},  # Not in whitelist
            "from": {"id": 456},
            "text": "hack"
        }
    }
    response = await telegram_gateway.app.post("/webhook/telegram", json=payload)
    assert response.status_code == 200  # Still 200 (silent ignore)
```

## Phase 2: Send Messages + Session Management (Week 2)

### TelegramGateway additions
```python
async def send_message(self, chat_id: int, text: str) -> dict:
    """Send message via Telegram Bot API."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text}
        )
        return resp.json()

async def link_session(self, chat_id: int, campaign_id: str) -> None:
    """Map Telegram chat to campaign context."""
    self.memory.sessions[chat_id] = {
        "campaign_id": campaign_id,
        "created": time.time()
    }
```

### Agent Integration
```python
# In direct_agent.py, add telegram message handler:
async def handle_telegram_task(chat_id: int, text: str):
    """Process Telegram message through agent loop."""
    session = memory.sessions.get(chat_id)
    if not session:
        await telegram.send_message(chat_id, "Not registered. /start first.")
        return
    
    # Run agent with telegram context
    result = await run_agent(
        task=text,
        user_context={"source": "telegram", "chat_id": chat_id}
    )
    
    await telegram.send_message(chat_id, result.summary)
```

## Deployment Checklist

- [ ] Add telegram_gateway.py + tests
- [ ] Modify config.py + config.yaml
- [ ] Update watcher.py to mount TelegramGateway
- [ ] Set environment variable: TELEGRAM_BOT_TOKEN
- [ ] Register webhook: POST /setWebhook to Telegram API
- [ ] Test end-to-end with test chat
