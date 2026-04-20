# DollOS Protocol v1 + Memory Distillation Implementation Plan

> ⚠️ **SUPERSEDED 2026-04-20** — This plan implements the deprecated "server = brain, phone = client" design. The corresponding spec (`2026-04-02-dollos-protocol-memory-distillation-design.md`) has been superseded by `2026-04-20-doll-repositioning-design.md`. **Do not execute this plan.** A new plan for Bridge/Drone architecture will be written from the 4/20 spec.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable phone to push conversation logs to server via WebSocket, and server to run nightly memory distillation on those logs.

**Architecture:** Android client sends completed conversation rounds over WebSocket to server. Server stores raw JSONL logs. A nightly cron job reads the day's logs, calls LLM to summarize/extract facts, and writes results into memsearch memory system.

**Tech Stack:** Python (websockets, asyncio, APScheduler), Kotlin (OkHttp WebSocket), NATS (LLM routing), YAML config

**Repos:**
- Server: `~/Projects/DollOS-Server/` (branch: `dev`)
- Android: `~/Projects/DollOSAIService/`
- Settings: `~/Projects/DollOS-build/packages/apps/Settings/`

---

### Task 1: Server — WebSocket Protocol Server

**Files:**
- Create: `dollos_server/protocol/__init__.py`
- Create: `dollos_server/protocol/server.py`
- Create: `dollos_server/protocol/handler.py`
- Create: `dollos_server/protocol/storage.py`
- Modify: `dollos_server/config.py` (add protocol section)
- Modify: `dollos_server/guraos.py` (start protocol server)

- [ ] **Step 1: Create protocol package with storage module**

`dollos_server/protocol/__init__.py`:
```python
```

`dollos_server/protocol/storage.py`:
```python
"""Raw conversation log storage (JSONL files)."""

import json
import os
from datetime import datetime
from pathlib import Path

import logging

log = logging.getLogger(__name__)


class ConversationStorage:
    """Stores raw conversation sync messages as JSONL files."""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir) / "conversations"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def store(self, character_id: str, session_date: str, payload: dict) -> Path:
        """Append a conversation round to the daily JSONL file."""
        char_dir = self.base_dir / character_id
        char_dir.mkdir(parents=True, exist_ok=True)
        filepath = char_dir / f"{session_date}.jsonl"
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        log.info(f"Stored conversation: {filepath.name} ({len(payload.get('messages', []))} messages)")
        return filepath

    def read_day(self, character_id: str, date: str) -> list[dict]:
        """Read all conversation rounds for a given day."""
        filepath = self.base_dir / character_id / f"{date}.jsonl"
        if not filepath.exists():
            return []
        rounds = []
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rounds.append(json.loads(line))
        return rounds

    def list_dates(self, character_id: str) -> list[str]:
        """List all dates with conversation logs for a character."""
        char_dir = self.base_dir / character_id
        if not char_dir.exists():
            return []
        return sorted(
            f.stem for f in char_dir.glob("*.jsonl")
        )

    def has_data(self, character_id: str, date: str) -> bool:
        filepath = self.base_dir / character_id / f"{date}.jsonl"
        return filepath.exists() and filepath.stat().st_size > 0
```

- [ ] **Step 2: Create handler module**

`dollos_server/protocol/handler.py`:
```python
"""WebSocket message routing and handling."""

import json
import logging
import uuid

from .storage import ConversationStorage

log = logging.getLogger(__name__)


class ProtocolHandler:
    """Routes incoming WebSocket messages to appropriate handlers."""

    def __init__(self, storage: ConversationStorage):
        self.storage = storage

    async def handle(self, raw: str) -> str:
        """Handle a raw JSON message. Returns JSON response string."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return self._error("Invalid JSON")

        msg_type = msg.get("type")
        msg_id = msg.get("id", str(uuid.uuid4()))

        if msg_type == "conversation.sync":
            return await self._handle_sync(msg_id, msg.get("payload", {}))
        elif msg_type == "ping":
            return self._pong(msg_id)
        else:
            return self._error(f"Unknown message type: {msg_type}", msg_id)

    async def _handle_sync(self, msg_id: str, payload: dict) -> str:
        character_id = payload.get("characterId")
        session_date = payload.get("sessionDate")
        messages = payload.get("messages", [])

        if not character_id or not session_date or not messages:
            return self._error("Missing characterId, sessionDate, or messages", msg_id)

        self.storage.store(character_id, session_date, payload)
        return json.dumps({"type": "ack", "id": msg_id})

    def _pong(self, msg_id: str) -> str:
        return json.dumps({"type": "pong", "id": msg_id})

    def _error(self, message: str, msg_id: str = "") -> str:
        return json.dumps({"type": "error", "id": msg_id, "message": message})
```

- [ ] **Step 3: Create WebSocket server**

`dollos_server/protocol/server.py`:
```python
"""WebSocket server for DollOS Protocol."""

import asyncio
import logging

import websockets
from websockets.asyncio.server import ServerConnection

from .handler import ProtocolHandler
from .storage import ConversationStorage

log = logging.getLogger(__name__)


class ProtocolServer:
    """DollOS Protocol WebSocket server."""

    def __init__(self, port: int, auth_token: str, data_dir: str):
        self.port = port
        self.auth_token = auth_token
        self.storage = ConversationStorage(data_dir)
        self.handler = ProtocolHandler(self.storage)
        self._server = None
        self._clients: set[ServerConnection] = set()

    async def start(self):
        self._server = await websockets.serve(
            self._on_connect,
            "0.0.0.0",
            self.port,
            ping_interval=30,
            ping_timeout=10,
        )
        log.info(f"Protocol server listening on ws://0.0.0.0:{self.port}/dollos")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            log.info("Protocol server stopped")

    async def _on_connect(self, ws: ServerConnection):
        # Auth check via query parameter
        path = ws.request.path if ws.request else ""
        params = {}
        if "?" in path:
            query = path.split("?", 1)[1]
            for pair in query.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    params[k] = v

        if params.get("token") != self.auth_token:
            log.warning(f"Auth failed from {ws.remote_address}")
            await ws.close(4001, "Unauthorized")
            return

        self._clients.add(ws)
        log.info(f"Client connected: {ws.remote_address}")
        try:
            async for message in ws:
                response = await self.handler.handle(message)
                await ws.send(response)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)
            log.info(f"Client disconnected: {ws.remote_address}")
```

- [ ] **Step 4: Add protocol config and integrate with GuraOS**

Add to `dollos_server/config.py` — in the `ServiceConfig` class, add:
```python
# In the config dataclass/model, add:
protocol_port: int = 8765
protocol_auth_token: str = ""
```

Find where config is loaded from YAML and ensure `protocol_port` and `protocol_auth_token` are read from:
```yaml
protocol:
  port: 8765
  auth_token: "your-secret-token"
```

Modify `dollos_server/guraos.py` — in the `start()` method, after existing services start:
```python
# Start protocol server
from dollos_server.protocol.server import ProtocolServer
self.protocol_server = ProtocolServer(
    port=self.config.protocol_port,
    auth_token=self.config.protocol_auth_token,
    data_dir=str(self.data_dir / "memory"),
)
await self.protocol_server.start()
```

And in `stop()`:
```python
if self.protocol_server:
    await self.protocol_server.stop()
```

- [ ] **Step 5: Test server manually**

```bash
cd ~/Projects/DollOS-Server
git checkout dev
uv run dollos-server start -f config.yaml
```

In another terminal:
```python
import asyncio, websockets, json

async def test():
    uri = "ws://localhost:8765/dollos?token=your-secret-token"
    async with websockets.connect(uri) as ws:
        msg = json.dumps({
            "type": "conversation.sync",
            "id": "test-1",
            "timestamp": 1712000000000,
            "payload": {
                "sessionDate": "2026-04-02",
                "characterId": "test-char",
                "characterName": "Test",
                "messages": [
                    {"role": "user", "content": "hello", "timestamp": 1712000000000},
                    {"role": "assistant", "content": "hi!", "timestamp": 1712000001000}
                ],
                "metadata": {"model": "grok-3", "provider": "GROK"}
            }
        })
        await ws.send(msg)
        resp = json.loads(await ws.recv())
        print(f"Response: {resp}")
        assert resp["type"] == "ack"

asyncio.run(test())
```

- [ ] **Step 6: Commit**

```bash
cd ~/Projects/DollOS-Server
git add dollos_server/protocol/
git commit -m "feat: DollOS Protocol v1 — WebSocket server + conversation storage"
```

---

### Task 2: Server — Memory Distillation

**Files:**
- Create: `dollos_server/distillation/__init__.py`
- Create: `dollos_server/distillation/distiller.py`
- Create: `dollos_server/distillation/scheduler.py`
- Create: `dollos_server/distillation/prompts.py`
- Modify: `dollos_server/guraos.py` (start scheduler)
- Modify: `dollos_server/config.py` (add distillation config)

- [ ] **Step 1: Create distillation prompts**

`dollos_server/distillation/__init__.py`:
```python
```

`dollos_server/distillation/prompts.py`:
```python
"""Prompt templates for memory distillation."""

DAILY_DISTILLATION_PROMPT = """You are a memory assistant for an AI companion named {character_name}.
Review today's conversations with the owner and produce a JSON response with these fields:

1. "summary" (string, < 200 words): A narrative of what happened today — what the owner talked about, what they asked for, notable moments.

2. "facts" (array of strings): Concrete facts learned about the owner — preferences, habits, opinions, plans, relationships. Only include new information not already in existing memory.

3. "habits" (array of strings): Patterns noticed — when they usually talk, what topics they bring up, communication style. Only include if there's enough data to suggest a pattern.

4. "action_items" (array of strings): Things the owner mentioned wanting to do, reminders they asked for, follow-ups needed. Empty array if none.

Existing memory for context (do not repeat what's already known):
{existing_memory}

Today's conversations ({round_count} rounds, {message_count} messages):
{conversations}

Respond with ONLY valid JSON, no markdown fences."""

WEEKLY_ROLLUP_PROMPT = """You are a memory assistant for an AI companion named {character_name}.
Summarize this week's daily summaries into a weekly digest.

Identify:
1. "summary" (string, < 300 words): Week-level narrative
2. "patterns" (array of strings): Patterns visible across the week but not in any single day
3. "key_facts" (array of strings): Most important new facts from the week

Daily summaries:
{daily_summaries}

Respond with ONLY valid JSON, no markdown fences."""
```

- [ ] **Step 2: Create distiller**

`dollos_server/distillation/distiller.py`:
```python
"""Memory distillation — summarize daily conversations into long-term memory."""

import json
import logging
from datetime import datetime, timedelta

from ..protocol.storage import ConversationStorage
from .prompts import DAILY_DISTILLATION_PROMPT, WEEKLY_ROLLUP_PROMPT

log = logging.getLogger(__name__)


class MemoryDistiller:
    """Reads conversation logs, calls LLM to distill, writes to memsearch."""

    def __init__(self, conversation_storage: ConversationStorage, memsearch, llm_call):
        """
        Args:
            conversation_storage: ConversationStorage instance
            memsearch: MemsearchService instance (has write_memory, load_context)
            llm_call: async callable(messages) -> str (LLM completion)
        """
        self.storage = conversation_storage
        self.memsearch = memsearch
        self.llm_call = llm_call

    async def distill_day(self, character_id: str, character_name: str, date: str) -> bool:
        """Distill a single day's conversations into memory. Returns True if successful."""
        rounds = self.storage.read_day(character_id, date)
        if not rounds:
            log.info(f"No conversations for {character_id} on {date}, skipping")
            return False

        # Aggregate
        all_messages = []
        total_messages = 0
        for r in rounds:
            msgs = r.get("messages", [])
            all_messages.extend(msgs)
            total_messages += len(msgs)

        conversations_text = self._format_conversations(rounds)
        existing_memory = await self._load_existing_memory()

        # Call LLM
        prompt = DAILY_DISTILLATION_PROMPT.format(
            character_name=character_name,
            existing_memory=existing_memory,
            round_count=len(rounds),
            message_count=total_messages,
            conversations=conversations_text,
        )

        for attempt in range(3):
            try:
                response = await self.llm_call([
                    {"role": "system", "content": "You are a precise memory assistant. Respond only in valid JSON."},
                    {"role": "user", "content": prompt},
                ])
                result = json.loads(response)
                break
            except (json.JSONDecodeError, Exception) as e:
                log.warning(f"Distillation attempt {attempt + 1} failed: {e}")
                if attempt == 2:
                    log.error(f"Distillation failed for {date} after 3 attempts")
                    return False

        # Write to memory
        await self._write_distillation(date, result)
        log.info(f"Distilled {date}: {len(result.get('facts', []))} facts, {len(result.get('habits', []))} habits")
        return True

    async def distill_missed_days(self, character_id: str, character_name: str):
        """Process any days that haven't been distilled yet."""
        dates = self.storage.list_dates(character_id)
        today = datetime.now().strftime("%Y-%m-%d")
        for date in dates:
            if date >= today:
                continue  # Don't distill today (still in progress)
            # Check if already distilled (daily markdown has distillation section)
            if await self._is_distilled(date):
                continue
            log.info(f"Processing missed day: {date}")
            await self.distill_day(character_id, character_name, date)

    async def weekly_rollup(self, character_id: str, character_name: str):
        """Create weekly summary from daily summaries."""
        today = datetime.now()
        week_start = today - timedelta(days=today.weekday())
        dates = [(week_start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

        daily_summaries = []
        for date in dates:
            summary = await self._read_daily_summary(date)
            if summary:
                daily_summaries.append(f"## {date}\n{summary}")

        if len(daily_summaries) < 3:
            log.info("Not enough daily summaries for weekly rollup")
            return

        prompt = WEEKLY_ROLLUP_PROMPT.format(
            character_name=character_name,
            daily_summaries="\n\n".join(daily_summaries),
        )

        try:
            response = await self.llm_call([
                {"role": "system", "content": "You are a precise memory assistant. Respond only in valid JSON."},
                {"role": "user", "content": prompt},
            ])
            result = json.loads(response)
            week_num = today.isocalendar()[1]
            year = today.year
            await self.memsearch.write_memory(
                content=f"# Week {week_num} Summary\n\n{result.get('summary', '')}\n\n## Patterns\n" +
                        "\n".join(f"- {p}" for p in result.get("patterns", [])) +
                        "\n\n## Key Facts\n" +
                        "\n".join(f"- {f}" for f in result.get("key_facts", [])),
                category="weekly",
                filename=f"{year}-W{week_num:02d}.md",
            )
            log.info(f"Weekly rollup written: {year}-W{week_num:02d}")
        except Exception as e:
            log.error(f"Weekly rollup failed: {e}")

    def _format_conversations(self, rounds: list[dict]) -> str:
        parts = []
        for i, r in enumerate(rounds):
            triggered_by = r.get("metadata", {}).get("triggeredBy", "text")
            parts.append(f"--- Round {i + 1} (triggered by: {triggered_by}) ---")
            for msg in r.get("messages", []):
                role = msg["role"]
                content = msg["content"]
                parts.append(f"{role}: {content}")
        return "\n".join(parts)

    async def _load_existing_memory(self) -> str:
        try:
            ctx = await self.memsearch.load_context("")
            return ctx.get("tier1", "")
        except Exception:
            return "(no existing memory)"

    async def _write_distillation(self, date: str, result: dict):
        # Write daily summary
        summary = result.get("summary", "")
        facts = result.get("facts", [])
        habits = result.get("habits", [])
        action_items = result.get("action_items", [])

        daily_content = f"\n\n## Distillation\n\n{summary}\n"
        if action_items:
            daily_content += "\n### Action Items\n" + "\n".join(f"- {a}" for a in action_items) + "\n"

        await self.memsearch.write_memory(
            content=daily_content,
            category="daily",
            filename=f"{date}.md",
            append=True,
        )

        # Write facts via consolidation
        for fact in facts:
            await self.memsearch.write_memory(
                content=fact,
                category="topics",
            )

        # Write habits
        if habits:
            habits_content = f"\n### {date}\n" + "\n".join(f"- {h}" for h in habits) + "\n"
            await self.memsearch.write_memory(
                content=habits_content,
                category="topics",
                filename="owner-habits.md",
                append=True,
            )

    async def _is_distilled(self, date: str) -> bool:
        try:
            content = await self.memsearch.read_daily(date)
            return "## Distillation" in (content or "")
        except Exception:
            return False

    async def _read_daily_summary(self, date: str) -> str | None:
        try:
            content = await self.memsearch.read_daily(date)
            if content and "## Distillation" in content:
                idx = content.index("## Distillation")
                return content[idx:]
        except Exception:
            pass
        return None
```

- [ ] **Step 3: Create scheduler**

`dollos_server/distillation/scheduler.py`:
```python
"""Cron scheduler for memory distillation."""

import asyncio
import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


class DistillationScheduler:
    """Simple asyncio-based scheduler for distillation tasks."""

    def __init__(self, distiller, character_id: str, character_name: str,
                 daily_hour: int = 3, weekly_enabled: bool = True):
        self.distiller = distiller
        self.character_id = character_id
        self.character_name = character_name
        self.daily_hour = daily_hour
        self.weekly_enabled = weekly_enabled
        self._task: asyncio.Task | None = None

    async def start(self):
        # Process missed days on startup
        await self.distiller.distill_missed_days(self.character_id, self.character_name)
        # Start cron loop
        self._task = asyncio.create_task(self._run_loop())
        log.info(f"Distillation scheduler started (daily at {self.daily_hour:02d}:00)")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Distillation scheduler stopped")

    async def _run_loop(self):
        while True:
            now = datetime.now()
            # Next daily run
            next_run = now.replace(hour=self.daily_hour, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)

            wait_seconds = (next_run - now).total_seconds()
            log.debug(f"Next distillation in {wait_seconds / 3600:.1f} hours")
            await asyncio.sleep(wait_seconds)

            # Run daily distillation (yesterday)
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            try:
                await self.distiller.distill_day(
                    self.character_id, self.character_name, yesterday
                )
            except Exception as e:
                log.error(f"Daily distillation failed: {e}")

            # Weekly rollup on Sunday
            if self.weekly_enabled and datetime.now().weekday() == 6:
                try:
                    await self.distiller.weekly_rollup(
                        self.character_id, self.character_name
                    )
                except Exception as e:
                    log.error(f"Weekly rollup failed: {e}")
```

- [ ] **Step 4: Integrate distillation with GuraOS**

Modify `dollos_server/guraos.py` — after protocol server starts:

```python
# Start distillation scheduler
from dollos_server.distillation.distiller import MemoryDistiller
from dollos_server.distillation.scheduler import DistillationScheduler

distiller = MemoryDistiller(
    conversation_storage=self.protocol_server.storage,
    memsearch=self.memsearch,
    llm_call=self.llm_router.chat,  # async callable that calls kmod LLM
)
self.distillation_scheduler = DistillationScheduler(
    distiller=distiller,
    character_id=self.config.default_character_id or "default",
    character_name=self.config.default_character_name or "AI",
    daily_hour=3,
    weekly_enabled=True,
)
await self.distillation_scheduler.start()
```

Add to `stop()`:
```python
if self.distillation_scheduler:
    await self.distillation_scheduler.stop()
```

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/DollOS-Server
git add dollos_server/distillation/
git commit -m "feat: memory distillation — nightly conversation summarization + fact extraction"
```

---

### Task 3: Android — DollOS Protocol Client

**Files:**
- Create: `app/src/main/java/org/dollos/ai/protocol/DollOSProtocolClient.kt`
- Modify: `app/src/main/java/org/dollos/ai/DollOSAIServiceImpl.kt` (add sync call)

- [ ] **Step 1: Add OkHttp dependency (if not present)**

Check `app/build.gradle` for OkHttp. DollOSAIService likely already has it (used by LLM providers). If not:
```gradle
implementation 'com.squareup.okhttp3:okhttp:4.12.0'
```

- [ ] **Step 2: Create Protocol Client**

`app/src/main/java/org/dollos/ai/protocol/DollOSProtocolClient.kt`:
```kotlin
package org.dollos.ai.protocol

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import okhttp3.*
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.TimeUnit

class DollOSProtocolClient(private val context: Context) : WebSocketListener() {
    companion object {
        private const val TAG = "DollOSProtocol"
        private const val PREFS_NAME = "dollos_server_prefs"
        private const val MAX_QUEUE = 100
    }

    private val client = OkHttpClient.Builder()
        .pingInterval(30, TimeUnit.SECONDS)
        .build()

    private var ws: WebSocket? = null
    @Volatile
    private var connected = false
    private val queue = ConcurrentLinkedQueue<String>()
    private var reconnectDelay = 1000L

    fun connect() {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val ip = prefs.getString("dollos_server_ip", "") ?: ""
        val port = prefs.getInt("dollos_server_port", 8765)
        val token = prefs.getString("dollos_server_token", "") ?: ""

        if (ip.isBlank()) {
            Log.d(TAG, "No server IP configured, skipping connection")
            return
        }

        val url = "ws://$ip:$port/dollos?token=$token"
        Log.i(TAG, "Connecting to $ip:$port")
        val request = Request.Builder().url(url).build()
        ws = client.newWebSocket(request, this)
    }

    fun disconnect() {
        ws?.close(1000, "Client disconnect")
        ws = null
        connected = false
    }

    fun syncConversation(
        characterId: String,
        characterName: String,
        messages: List<Map<String, Any>>,
        metadata: Map<String, Any>
    ) {
        val msg = JSONObject().apply {
            put("type", "conversation.sync")
            put("id", UUID.randomUUID().toString())
            put("timestamp", System.currentTimeMillis())
            put("payload", JSONObject().apply {
                put("sessionDate", java.time.LocalDate.now().toString())
                put("characterId", characterId)
                put("characterName", characterName)
                put("messages", JSONArray(messages))
                put("metadata", JSONObject(metadata))
            })
        }.toString()

        if (connected) {
            ws?.send(msg)
        } else {
            if (queue.size < MAX_QUEUE) {
                queue.add(msg)
            }
        }
    }

    override fun onOpen(webSocket: WebSocket, response: Response) {
        connected = true
        reconnectDelay = 1000L
        Log.i(TAG, "Connected to server")
        // Flush queued messages
        while (queue.isNotEmpty()) {
            val msg = queue.poll() ?: break
            webSocket.send(msg)
        }
    }

    override fun onMessage(webSocket: WebSocket, text: String) {
        try {
            val msg = JSONObject(text)
            when (msg.optString("type")) {
                "ack" -> Log.d(TAG, "Ack: ${msg.optString("id")}")
                "error" -> Log.w(TAG, "Server error: ${msg.optString("message")}")
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to parse server message", e)
        }
    }

    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
        connected = false
        Log.w(TAG, "Connection failed: ${t.message}, reconnecting in ${reconnectDelay}ms")
        Thread {
            Thread.sleep(reconnectDelay)
            reconnectDelay = (reconnectDelay * 2).coerceAtMost(30000)
            connect()
        }.start()
    }

    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
        connected = false
        Log.i(TAG, "Connection closed: $reason")
    }
}
```

- [ ] **Step 3: Integrate with DollOSAIServiceImpl**

Modify `app/src/main/java/org/dollos/ai/DollOSAIServiceImpl.kt`:

Add field:
```kotlin
private var protocolClient: DollOSProtocolClient? = null
```

In init/onCreate:
```kotlin
protocolClient = DollOSProtocolClient(context)
protocolClient?.connect()
```

After LLM response completes successfully (in the `onComplete` callback where usage is recorded), add:
```kotlin
// Sync conversation to server
val msgs = conversationManager.getLastRoundMessages().map { msg ->
    mapOf("role" to msg.role, "content" to msg.content, "timestamp" to msg.timestamp)
}
val meta = mapOf(
    "inputTokens" to response.inputTokens,
    "outputTokens" to response.outputTokens,
    "model" to response.model,
    "provider" to client.providerType.name,
)
protocolClient?.syncConversation(
    characterId = characterManager.activeCharacterId ?: "unknown",
    characterName = characterManager.activeCharacterName ?: "AI",
    messages = msgs,
    metadata = meta,
)
```

- [ ] **Step 4: Commit**

```bash
cd ~/Projects/DollOSAIService
git add app/src/main/java/org/dollos/ai/protocol/
git commit -m "feat: DollOS Protocol client — WebSocket conversation sync to server"
```

---

### Task 4: Android — Server Settings UI

**Files:**
- Create: `Settings/res/xml/dollos_server_settings.xml`
- Create: `Settings/src/com/android/settings/dollos/DollOSServerSettingsFragment.java`
- Modify: `Settings/res/xml/dollos_ai_settings.xml` (add Server sub-page link)

- [ ] **Step 1: Create server settings XML**

`~/Projects/DollOS-build/packages/apps/Settings/res/xml/dollos_server_settings.xml`:
```xml
<?xml version="1.0" encoding="utf-8"?>
<PreferenceScreen
    xmlns:android="http://schemas.android.com/apk/res/android"
    android:title="Server Settings">

    <PreferenceCategory android:title="Connection">
        <EditTextPreference
            android:key="dollos_server_ip"
            android:title="Server IP"
            android:summary="Not configured"
            android:defaultValue="" />
        <EditTextPreference
            android:key="dollos_server_port"
            android:title="Server Port"
            android:summary="8765"
            android:defaultValue="8765" />
        <EditTextPreference
            android:key="dollos_server_token"
            android:title="Auth Token"
            android:summary="Not set"
            android:defaultValue="" />
        <Preference
            android:key="dollos_server_status"
            android:title="Connection Status"
            android:summary="Not connected"
            android:selectable="false" />
    </PreferenceCategory>

</PreferenceScreen>
```

- [ ] **Step 2: Create server settings fragment**

`DollOSServerSettingsFragment.java` — follows same pattern as `DollOSLLMSettingsFragment.java`:
- Bind to AI service
- Load/save server IP, port, token to SharedPreferences `dollos_server_prefs`
- Use `setText()` before `save()` (same fix as LLM settings)
- Show connection status from service

- [ ] **Step 3: Add Server link to main AI settings page**

Add to `dollos_ai_settings.xml`:
```xml
<Preference
    android:key="dollos_server_settings"
    android:title="Server"
    android:summary="DollOS Protocol connection"
    android:fragment="com.android.settings.dollos.DollOSServerSettingsFragment" />
```

- [ ] **Step 4: Build and deploy Settings**

```bash
cd ~/Projects/DollOS-build
source build/envsetup.sh && lunch dollos_bluejay-bp2a-userdebug
m Settings -j$(nproc)
adb push out/target/product/bluejay/system_ext/priv-app/Settings/Settings.apk /system_ext/priv-app/Settings/
```

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/DollOS-build
git add packages/apps/Settings/
git commit -m "feat: Server settings UI for DollOS Protocol"
```

---

### Task 5: End-to-End Test

- [ ] **Step 1: Configure server**

Add to `~/Projects/DollOS-Server/config.yaml`:
```yaml
protocol:
  port: 8765
  auth_token: "dollos-test-token"
```

- [ ] **Step 2: Start server**

```bash
cd ~/Projects/DollOS-Server
uv run dollos-server start -f config.yaml
```

- [ ] **Step 3: Configure phone**

In AI Settings → Server:
- Server IP: (your PC's LAN IP, e.g., `192.168.1.100`)
- Port: `8765`
- Token: `dollos-test-token`

- [ ] **Step 4: Test conversation sync**

1. Call "Gura" on the phone
2. Say something, wait for AI response
3. Check server logs for "Stored conversation"
4. Check `data/memory/conversations/` for JSONL file

- [ ] **Step 5: Test distillation manually**

```python
# In server Python REPL
from dollos_server.distillation.distiller import MemoryDistiller
# ... trigger distill_day for today's date
```

Verify:
- `data/memory/daily/YYYY-MM-DD.md` has Distillation section
- `data/memory/topics/` has new fact files
