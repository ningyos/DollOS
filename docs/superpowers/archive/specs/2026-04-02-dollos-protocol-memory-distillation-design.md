# DollOS Protocol v1 + Memory Distillation Design

> ⚠️ **SUPERSEDED 2026-04-20** — This design was based on the "server = brain, phone = client" mental model, which was discarded during the Doll product repositioning. See `2026-04-20-doll-repositioning-design.md` for the current architecture (phone-as-body, Bridge/Drone as optional extensions). The WebSocket protocol and server-side memory distillation described below are **not being implemented**.

## Overview

DollOS Protocol v1 enables the phone to push conversation logs to the server over WebSocket. The server uses these logs for nightly memory distillation — an automated process that summarizes daily conversations, extracts facts/preferences, and writes them into long-term memory.

## Scope

**In scope (v1):**
- WebSocket transport (LAN direct connect)
- Conversation sync (phone → server)
- Nightly memory distillation (server-side, local LLM)

**Out of scope (future versions):**
- Server TTS streaming
- Server LLM proxy
- Memory query from phone
- Vision (screenshot/camera → server)
- WAN/external network access

---

## Part 1: DollOS Protocol v1

### Transport

- **Protocol:** WebSocket (`ws://`)
- **Network:** LAN direct connect (phone and server on same network)
- **Server endpoint:** `ws://<LAN_IP>:8765/dollos`
- **Heartbeat:** Ping/pong every 30 seconds
- **Reconnect:** Exponential backoff (1s, 2s, 4s, 8s, max 30s)

### Authentication

- Pre-shared token configured in Android Settings (AI Settings → Server)
- Sent in WebSocket handshake as query parameter: `ws://192.168.1.100:8765/dollos?token=<token>`
- Server rejects connection if token doesn't match

### Message Format

All messages are JSON with a `type` field:

```json
{
  "type": "string",
  "id": "uuid",
  "timestamp": 1712000000000,
  "payload": {}
}
```

**Client → Server messages:**

| Type | Description |
|------|-------------|
| `conversation.sync` | Push conversation round to server |
| `ping` | Heartbeat |

**Server → Client messages:**

| Type | Description |
|------|-------------|
| `ack` | Acknowledge received message (with original `id`) |
| `pong` | Heartbeat response |
| `error` | Error response |

### Conversation Sync Message

Sent after each conversation round completes (state returns to IDLE):

```json
{
  "type": "conversation.sync",
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "timestamp": 1712000000000,
  "payload": {
    "sessionDate": "2026-04-02",
    "characterId": "gura-1775062625",
    "characterName": "Gura",
    "messages": [
      {
        "role": "user",
        "content": "今天天氣怎麼樣",
        "timestamp": 1712000000000
      },
      {
        "role": "assistant",
        "content": "今天天氣很好喔！是個適合出門的日子～",
        "timestamp": 1712000002000
      }
    ],
    "metadata": {
      "inputTokens": 15,
      "outputTokens": 30,
      "model": "grok-3",
      "provider": "GROK",
      "asrText": "今天天氣怎麼樣",
      "triggeredBy": "wake_word"
    }
  }
}
```

### Server Storage

Server receives conversation sync and:
1. Appends to `memory/conversations/<characterId>/YYYY-MM-DD.jsonl` (one JSON per line, raw log, under memsearch data dir)
2. Sends `ack` back to phone

Only sync complete conversation rounds (LLM responded successfully, state reached IDLE naturally). Do not sync interrupted/cancelled conversations.

No processing at sync time — raw storage only. Distillation happens later.

---

## Part 2: Memory Distillation

### Trigger

- **Schedule:** Daily cron at 03:00 local time
- **Condition:** Only runs if there are new conversation logs for the day
- **Fallback:** If server was off, process missed days on next startup

### Input

Read all conversation logs from `memory/conversations/<characterId>/YYYY-MM-DD.jsonl` for the target date.

### Distillation Pipeline

**Step 1: Aggregate**
- Concatenate all conversation rounds for the day
- Calculate total: rounds, user messages, assistant messages, tokens used

**Step 2: Summarize (LLM call)**

Prompt the local LLM with the day's conversations:

```
You are a memory assistant for an AI companion named {characterName}.
Review today's conversations with the owner and produce:

1. **Daily Summary** (< 200 words): A narrative of what happened today — what the owner talked about, what they asked for, notable moments.

2. **Extracted Facts** (bullet list): Concrete facts learned about the owner — preferences, habits, opinions, plans, relationships. Only include new information not already in existing memory.

3. **Habit Observations** (bullet list): Patterns noticed — when they usually talk, what topics they bring up, communication style, emotional patterns. Only include if there's enough data to suggest a pattern.

4. **Action Items** (bullet list, optional): Things the owner mentioned wanting to do, reminders they asked for, follow-ups needed.

Existing memory for context (do not repeat what's already known):
{existing_core_memory}

Today's conversations:
{conversations}
```

**Step 3: Write to Memory**

- **Daily summary** → `daily/YYYY-MM-DD.md` (append distillation section)
- **Extracted facts** → Write via memsearch with consolidation:
  - Each fact checked against existing memory (similarity >= 0.85 → update, >= 0.95 → skip)
  - Route to appropriate category: `people/`, `topics/`, `decisions/`
- **Habit observations** → `topics/owner-habits.md` (append with date)
- **Action items** → `daily/YYYY-MM-DD.md` (action items section)

### Consolidation Rules

Same as existing memsearch write-time consolidation:
- Similarity >= 0.95 → skip (duplicate)
- Similarity >= 0.85 → update existing entry
- Below 0.85 → insert new

### Weekly Rollup (optional, triggered Sunday 03:30)

If 7+ daily summaries exist for the week:
- LLM summarizes the week's daily summaries into a weekly digest
- Written to `weekly/YYYY-WXX.md`
- Identifies week-level patterns not visible in daily view

---

## Part 3: Android Client Implementation

### Settings UI

Add to AI Settings → new "Server" sub-page:
- **Server IP** (EditTextPreference): e.g., `192.168.1.100`
- **Server Port** (EditTextPreference): default `8765`
- **Auth Token** (EditTextPreference): pre-shared secret
- **Connection Status** (Preference, read-only): Connected / Disconnected / Error

### DollOSProtocolClient

New class in DollOSAIService:
- Manages WebSocket lifecycle (connect, heartbeat, reconnect)
- Exposes `syncConversation(messages, metadata)` method
- Called by `DollOSAIServiceImpl` after each conversation round completes
- Runs on background thread
- Queues messages if disconnected, sends when reconnected (max 100 queued)

### Integration Point

In `DollOSAIServiceImpl`, after LLM response completes and state returns to IDLE:
```
protocolClient?.syncConversation(conversationRound, metadata)
```

---

## Part 4: Server Implementation

### New Module: `dollos_server/protocol/`

- `server.py` — WebSocket server (Python `websockets` library)
- `handler.py` — Message routing and handling
- `storage.py` — Raw conversation log storage (JSONL files)
- `auth.py` — Token validation

### New Module: `dollos_server/distillation/`

- `distiller.py` — Main distillation logic (read logs, call LLM, write memory)
- `scheduler.py` — Cron scheduling (APScheduler or simple asyncio loop)
- `prompts.py` — Distillation prompt templates

### Integration

- Protocol server starts alongside existing NATS/kmod services
- Distiller uses existing memsearch service for memory writes
- LLM calls go through existing kmod LLM service (NATS)

---

## Configuration

### Server config (`config.yaml` or environment):
```yaml
protocol:
  port: 8765
  auth_token: "your-secret-token"

distillation:
  schedule: "0 3 * * *"  # 03:00 daily
  llm_model: "grok-3"  # via kmod LLM service
  weekly_rollup: true
  weekly_schedule: "30 3 * * 0"  # Sunday 03:30
```

### Android config (SharedPreferences via Settings UI):
```
dollos_server_ip: "192.168.1.100"
dollos_server_port: 8765
dollos_server_token: "your-secret-token"
```

---

## Error Handling

- **WebSocket disconnect:** Phone queues messages (max 100), retries connection with exponential backoff
- **Server receives malformed message:** Responds with `error` type, logs warning, does not crash
- **Distillation LLM fails:** Retry 3 times with 1 minute interval, then skip day (log error)
- **Disk full:** Server checks disk space before writing, alerts if < 1GB free
