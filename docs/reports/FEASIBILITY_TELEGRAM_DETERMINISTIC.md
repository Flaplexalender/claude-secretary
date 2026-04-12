# 15-Min Feasibility: [telegram-integration] & [deterministic-pipelines]

## 1. TELEGRAM-INTEGRATION: Minimum Viable Features

### Core MVF (Phase 1 — Week 1)
- **Message receive**: Webhook listener (FastAPI) → parse Telegram updates → store in memory
- **Message send**: Telegram Bot API client wrapper (send_message, send_photo, send_document)
- **Session management**: Link Telegram chat_id to internal user/campaign context
- **Basic auth**: Bot token validation, chat whitelist

### Nice-to-Have (Phase 2)
- Inline keyboards (task confirmation UI)
- Media upload support (photos, docs)
- Markdown formatting

### Dependencies
- **NO dependency on web-dashboard**: Standalone webhook service. Dashboard can consume `/api/telegram/history` later.
- **Depends on**: SecretaryConfig (bot token), MemoryStore (session persistence), direct_agent (task execution)
- **External**: python-telegram-bot library OR raw httpx calls

---

## 2. DETERMINISTIC-PIPELINES: Minimum Viable Features

### Core MVF (Phase 1 — Week 1)
- **Deterministic execution**: Given input X, always produce output Y (seed-based RNG, fixed model routing)
- **Pipeline DSL**: Simple YAML schema (steps, branching, retry logic)
- **Reproducibility log**: Store seed + routing decisions for replay
- **Test coverage**: Snapshot-based assertions (golden outputs)

### Nice-to-Have (Phase 2)
- DAG visualization
- Distributed execution (task queues)
- Performance benchmarking

### Dependencies
- **NO dependency on telegram-integration**
- **Depends on**: router.py (model selection is deterministic), direct_agent.py (tool execution reproducibility)
- **Internal**: config.py (seed management), memory.py (state snapshots)

---

## 3. SEQUENCING & PARALLELISM

| Aspect | Recommendation |
|--------|-----------------|
| **Can run in parallel?** | ✅ **YES** — They touch different subsystems |
| **Shared dependencies?** | ❌ **NO** — Both depend on config/agent but not each other |
| **Interaction risk?** | Low — Telegram is I/O-bound; deterministic is compute-bound |
| **Suggested start** | **Parallel Phase 1s** (Week 1) with shared code review checkpoint (Day 4) |

---

## 4. EASIEST SUB-GOAL TO START WITH

**Winner: [telegram-integration] — Message Receive Webhook**

**Why?**
1. **Lowest barrier to test**: POST a JSON, check logs → instant feedback
2. **No domain complexity**: Just parse & store (no branching, no snapshot logic)
3. **Unblocks other tasks**: Once webhook works, can build send/session on top
4. **Single dependency**: Only needs SecretaryConfig + basic httpx

---

## 5. INITIAL STEP PLAN: Telegram Message Receive Webhook

### Step 1: Skeleton (30 min)
```python
# src/secretary/telegram_gateway.py
from fastapi import FastAPI, Request
import httpx

app = FastAPI()
BOT_TOKEN = config.telegram.bot_token

@app.post("/webhook/telegram")
async def handle_update(request: Request):
    """Receive Telegram Update JSON, parse, log."""
    update = await request.json()
    chat_id = update["message"]["chat"]["id"]
    text = update["message"]["text"]
    log.info(f"Telegram {chat_id}: {text}")
    return {"ok": True}
```

### Step 2: Storage Integration (30 min)
```python
# Add to MemoryStore
def store_telegram_message(chat_id, user_id, text, timestamp):
    self.messages_log.append({
        "source": "telegram", "chat_id": chat_id, 
        "text": text, "ts": timestamp
    })

# Webhook calls:
await memory.store_telegram_message(chat_id, user_id, text, time.time())
```

### Step 3: Test (15 min)
```python
# tests/test_telegram_webhook.py
@pytest.mark.asyncio
async def test_webhook_receive():
    payload = {"message": {"chat": {"id": 123}, "text": "hello"}}
    resp = await client.post("/webhook/telegram", json=payload)
    assert resp.status_code == 200
    assert memory.messages_log[-1]["text"] == "hello"
```

### Step 4: Auth + Whitelist (15 min)
```python
# Validate X-Telegram-Bot-API-Secret-Token header
# Check chat_id against ALLOWED_CHATS
```

**Total: 90 min (1.5 hrs) → Working webhook ready for Phase 2 (send, session, agent integration)**

---

## 6. PARALLEL: Deterministic-Pipelines Quick Start

While Telegram webhook is tested, start **router determinism audit**:
```bash
# Step 1: Grep for non-deterministic calls
grep -r "random\|uuid\|time\.time\|datetime\.now" src/secretary/router.py

# Step 2: Freeze seed in router.select_model()
# Step 3: Write golden test cases
```

**This runs in parallel, no blocking on Telegram.**

---

## Recommendation

✅ **Start [telegram-integration] webhook immediately** (lowest risk, instant feedback loop)
✅ **Parallel: Audit router.py for determinism** (research task, can happen async)
✅ **Week 1 Goal**: Webhook + deterministic routing + snapshot tests passing
✅ **Week 2**: Telegram send/session, pipeline DSL, end-to-end integration test
