# DollOS Daemon Skeleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the DollOS Python daemon skeleton — a runnable process that accepts WebSocket connections, receives text input, calls a self-hosted llama.cpp `/completion` endpoint, and streams tokens back to the client.

**Architecture:** Async Python daemon using `asyncio`. Pluggable LLM adapter pattern (ABC + concrete impl). WebSocket IPC layer separates daemon core from any UI/App frontends. No memory, no Instinct, no character pack yet — those come in subsequent plans. This plan produces the foundation everything else hangs off.

**Tech Stack:**
- Python 3.12+
- `uv` for environment + dependency management
- `pydantic` v2 for config + IPC message schemas
- `httpx` for async HTTP to llama.cpp
- `websockets` library for WebSocket server
- `pytest` + `pytest-asyncio` for tests
- `respx` for mocking httpx in tests

**Spec reference:** `docs/superpowers/specs/2026-05-01-dollos-pivot-to-computer-design.md` §3 (architecture), §5 (Doll turn), §11.3 (monorepo layout)

---

## File Structure

```
DollOS/
├── daemon/                          # ← new sub-tree (this plan creates)
│   ├── pyproject.toml
│   ├── README.md
│   ├── config.example.toml
│   ├── src/
│   │   └── dollos/
│   │       ├── __init__.py
│   │       ├── __main__.py          # `python -m dollos` entry
│   │       ├── config.py            # TOML loader + pydantic Settings
│   │       ├── log.py               # logging setup
│   │       ├── llm/
│   │       │   ├── __init__.py
│   │       │   ├── adapter.py       # LLMAdapter abstract base
│   │       │   └── llamacpp.py      # LlamaCppAdapter concrete impl
│   │       ├── ipc/
│   │       │   ├── __init__.py
│   │       │   ├── messages.py      # pydantic message schemas
│   │       │   └── server.py        # WebSocket server
│   │       └── daemon.py            # wires everything together
│   └── tests/
│       ├── __init__.py
│       ├── conftest.py
│       ├── test_config.py
│       ├── test_llm_llamacpp.py
│       ├── test_ipc_messages.py
│       ├── test_ipc_server.py
│       └── test_e2e.py
```

Each file's responsibility:
- `config.py` — load TOML, validate via pydantic, expose `Settings` singleton
- `log.py` — configure stdlib logging (loguru-style format), used everywhere
- `llm/adapter.py` — `LLMAdapter` ABC defining `stream_completion(prompt, prefill, stop)` interface; backend-agnostic
- `llm/llamacpp.py` — `LlamaCppAdapter` calls `/completion` raw endpoint, supports prefill via prompt concatenation, yields tokens as async iterator
- `ipc/messages.py` — pydantic models: `TextInput`, `TextChunk`, `TurnEnd`, `ErrorMsg`. JSON-serializable
- `ipc/server.py` — `WebSocketServer` accepts client connection, parses incoming messages, dispatches to handler callback, sends JSON frames back
- `daemon.py` — assembles config + adapter + server, wires "incoming text → adapter → outgoing chunks" flow
- `__main__.py` — CLI entry: `python -m dollos --config <path>`

---

## Task 1: Project Scaffold

**Files:**
- Create: `daemon/pyproject.toml`
- Create: `daemon/src/dollos/__init__.py`
- Create: `daemon/src/dollos/__main__.py`
- Create: `daemon/tests/__init__.py`
- Create: `daemon/tests/conftest.py`
- Create: `daemon/README.md`
- Create: `daemon/.gitignore`

- [ ] **Step 1: Initialize uv project**

```bash
cd /home/progcat/Projects/DollOS
mkdir -p daemon/src/dollos/llm daemon/src/dollos/ipc daemon/tests
cd daemon
uv init --no-readme --no-pin-python --bare
```

- [ ] **Step 2: Write `daemon/pyproject.toml`**

```toml
[project]
name = "dollos"
version = "0.1.0"
description = "DollOS daemon — event loop, Instinct, Memory, Conversation Engine"
requires-python = ">=3.12"
dependencies = [
    "pydantic>=2.6",
    "httpx>=0.27",
    "websockets>=12.0",
    "tomli; python_version < '3.11'",
]

[project.scripts]
dollos = "dollos.__main__:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/dollos"]

[tool.uv]
dev-dependencies = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "respx>=0.21",
    "ruff>=0.4",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "ASYNC"]
```

- [ ] **Step 3: Write minimal `daemon/src/dollos/__init__.py`**

```python
"""DollOS daemon package."""

__version__ = "0.1.0"
```

- [ ] **Step 4: Write `daemon/src/dollos/__main__.py` placeholder**

```python
"""Entry point: python -m dollos."""

import sys


def main() -> int:
    print("dollos daemon — skeleton (not yet implemented)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Write `daemon/tests/__init__.py` (empty) and `daemon/tests/conftest.py`**

`daemon/tests/__init__.py`: empty file.

`daemon/tests/conftest.py`:

```python
"""Shared pytest fixtures."""

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"
```

- [ ] **Step 6: Write `daemon/.gitignore` and `daemon/README.md`**

`daemon/.gitignore`:

```
.venv/
__pycache__/
*.egg-info/
.pytest_cache/
.ruff_cache/
dist/
build/
.coverage
*.pyc
config.toml
```

`daemon/README.md`:

```markdown
# DollOS Daemon

Python daemon — event loop, Instinct, Memory, Conversation Engine, IPC server.

## Setup

```bash
cd daemon
uv sync
```

## Run

```bash
uv run python -m dollos --config config.toml
```

See `config.example.toml` for configuration template.

## Test

```bash
uv run pytest
```
```

- [ ] **Step 7: Sync and verify package installs**

```bash
cd daemon
uv sync
uv run python -m dollos
```

Expected output: `dollos daemon — skeleton (not yet implemented)`

- [ ] **Step 8: Commit**

```bash
cd /home/progcat/Projects/DollOS
git add daemon/
git commit -m "chore: scaffold dollos daemon Python project"
```

---

## Task 2: Config Module

**Files:**
- Create: `daemon/src/dollos/config.py`
- Create: `daemon/src/dollos/log.py`
- Create: `daemon/config.example.toml`
- Create: `daemon/tests/test_config.py`

- [ ] **Step 1: Write the failing test `daemon/tests/test_config.py`**

```python
"""Tests for config loading."""

from pathlib import Path

import pytest

from dollos.config import Settings, load_settings


def test_load_settings_from_toml(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
backend = "llamacpp"
base_url = "http://127.0.0.1:8001"
model_alias = "test-model"

[ipc]
host = "127.0.0.1"
port = 9876

[log]
level = "INFO"
"""
    )

    settings = load_settings(config_path)

    assert isinstance(settings, Settings)
    assert settings.llm.backend == "llamacpp"
    assert settings.llm.base_url == "http://127.0.0.1:8001"
    assert settings.llm.model_alias == "test-model"
    assert settings.ipc.host == "127.0.0.1"
    assert settings.ipc.port == 9876
    assert settings.log.level == "INFO"


def test_load_settings_missing_required_field(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
backend = "llamacpp"
# missing base_url
"""
    )

    with pytest.raises(ValueError):
        load_settings(config_path)


def test_load_settings_default_log_level(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
backend = "llamacpp"
base_url = "http://127.0.0.1:8001"
model_alias = "test-model"

[ipc]
host = "127.0.0.1"
port = 9876
"""
    )

    settings = load_settings(config_path)

    assert settings.log.level == "INFO"
```

- [ ] **Step 2: Run test, verify it fails**

```bash
cd daemon
uv run pytest tests/test_config.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'dollos.config'`

- [ ] **Step 3: Write `daemon/src/dollos/config.py`**

```python
"""Configuration: TOML loading + pydantic validation."""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    backend: Literal["llamacpp"] = "llamacpp"
    base_url: str
    model_alias: str
    timeout_s: float = 60.0


class IPCConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 9876


class LogConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class Settings(BaseModel):
    llm: LLMConfig
    ipc: IPCConfig = Field(default_factory=lambda: IPCConfig())
    log: LogConfig = Field(default_factory=lambda: LogConfig())


def load_settings(path: Path) -> Settings:
    """Load and validate a TOML config file."""
    with path.open("rb") as f:
        data = tomllib.load(f)
    return Settings.model_validate(data)
```

- [ ] **Step 4: Write `daemon/src/dollos/log.py`**

```python
"""Logging configuration."""

import logging
import sys


def setup_logging(level: str) -> None:
    """Configure root logger. Idempotent."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.setLevel(level)
```

- [ ] **Step 5: Write `daemon/config.example.toml`**

```toml
# DollOS daemon configuration template.
# Copy to config.toml and edit.

[llm]
backend = "llamacpp"
base_url = "http://127.0.0.1:8001"
model_alias = "unsloth/Qwen3.6"
timeout_s = 60.0

[ipc]
host = "127.0.0.1"
port = 9876

[log]
level = "INFO"
```

- [ ] **Step 6: Run tests, verify they pass**

```bash
uv run pytest tests/test_config.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 7: Commit**

```bash
cd /home/progcat/Projects/DollOS
git add daemon/
git commit -m "feat(daemon): config module + logging setup"
```

---

## Task 3: LLM Adapter Abstract Interface

**Files:**
- Create: `daemon/src/dollos/llm/__init__.py`
- Create: `daemon/src/dollos/llm/adapter.py`

- [ ] **Step 1: Write `daemon/src/dollos/llm/__init__.py`**

```python
"""LLM backend adapters."""

from dollos.llm.adapter import LLMAdapter, StreamChunk

__all__ = ["LLMAdapter", "StreamChunk"]
```

- [ ] **Step 2: Write `daemon/src/dollos/llm/adapter.py`**

```python
"""Abstract LLM adapter interface."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass
class StreamChunk:
    """A single streamed chunk from the LLM."""

    text: str
    """The new text added by this chunk."""

    done: bool = False
    """True iff this is the final chunk for the turn."""


class LLMAdapter(ABC):
    """Abstract interface for LLM backends.

    All concrete adapters MUST support prefill — assistant-side text that the
    model continues from. This is critical for VoM (see grammar_injection_techreport.md).
    """

    @abstractmethod
    async def stream_completion(
        self,
        *,
        system: str,
        user: str,
        prefill: str = "",
        stop: list[str] | None = None,
        max_tokens: int = 1024,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a completion.

        Args:
            system: system prompt
            user: user message
            prefill: assistant prefix tokens (already attributed to assistant role)
            stop: optional stop sequences
            max_tokens: hard cap on generated tokens

        Yields:
            StreamChunk objects until done=True is yielded.
        """
        ...
```

Note: ABC + async generator is intentional — concrete impl uses `async def` with `yield`.

- [ ] **Step 3: Verify import works**

```bash
cd daemon
uv run python -c "from dollos.llm import LLMAdapter, StreamChunk; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
cd /home/progcat/Projects/DollOS
git add daemon/src/dollos/llm/
git commit -m "feat(daemon): LLM adapter abstract interface"
```

---

## Task 4: LlamaCppAdapter Implementation

**Files:**
- Create: `daemon/src/dollos/llm/llamacpp.py`
- Create: `daemon/tests/test_llm_llamacpp.py`

The `/completion` endpoint of llama-server takes a raw `prompt` field. To support prefill we concatenate `system + user-rendered + assistant-prefix + prefill` into a single prompt string. For Qwen-style chat templates we use the ChatML format directly (simpler than calling `/apply-template` for v1 — that becomes a refinement in a later plan).

- [ ] **Step 1: Write the failing test `daemon/tests/test_llm_llamacpp.py`**

```python
"""Tests for LlamaCppAdapter."""

import json

import httpx
import pytest
import respx

from dollos.llm.llamacpp import LlamaCppAdapter


@pytest.mark.asyncio
async def test_stream_completion_basic():
    """Adapter streams chunks until done."""
    adapter = LlamaCppAdapter(base_url="http://test.local:8001", timeout_s=5.0)

    sse_body = (
        'data: {"content": "Hello", "stop": false}\n\n'
        'data: {"content": " world", "stop": false}\n\n'
        'data: {"content": "", "stop": true}\n\n'
    )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_body,
            )
        )

        chunks = []
        async for chunk in adapter.stream_completion(
            system="You are helpful.",
            user="Hi",
            prefill="",
        ):
            chunks.append(chunk)

    assert len(chunks) == 3
    assert chunks[0].text == "Hello"
    assert chunks[0].done is False
    assert chunks[1].text == " world"
    assert chunks[1].done is False
    assert chunks[2].done is True


@pytest.mark.asyncio
async def test_stream_completion_includes_prefill_in_prompt():
    """Adapter sends prefill as part of the prompt field."""
    adapter = LlamaCppAdapter(base_url="http://test.local:8001", timeout_s=5.0)

    captured: dict = {}

    def capture_request(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"content": "", "stop": true}\n\n',
        )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(side_effect=capture_request)

        async for _ in adapter.stream_completion(
            system="SYS",
            user="USR",
            prefill="<think>\nRECALL: x\nGOAL: ",
        ):
            pass

    prompt = captured["body"]["prompt"]
    assert "SYS" in prompt
    assert "USR" in prompt
    assert "<think>\nRECALL: x\nGOAL: " in prompt
    # Prefill must come AFTER the assistant role marker
    assert prompt.endswith("<think>\nRECALL: x\nGOAL: ")


@pytest.mark.asyncio
async def test_stream_completion_passes_stop_sequences():
    """Stop sequences are forwarded to the backend."""
    adapter = LlamaCppAdapter(base_url="http://test.local:8001", timeout_s=5.0)

    captured: dict = {}

    def capture_request(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"content": "", "stop": true}\n\n',
        )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(side_effect=capture_request)

        async for _ in adapter.stream_completion(
            system="s",
            user="u",
            prefill="",
            stop=["<|im_end|>"],
            max_tokens=512,
        ):
            pass

    assert captured["body"]["stop"] == ["<|im_end|>"]
    assert captured["body"]["n_predict"] == 512
    assert captured["body"]["stream"] is True
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd daemon
uv run pytest tests/test_llm_llamacpp.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'dollos.llm.llamacpp'`

- [ ] **Step 3: Write `daemon/src/dollos/llm/llamacpp.py`**

```python
"""llama.cpp /completion endpoint adapter.

Supports prefill via prompt concatenation: the adapter renders a ChatML-formatted
prompt where the assistant role is opened and the prefill text is appended,
letting the model continue from there. This is the prefill mechanism described
in grammar_injection_techreport.md §2.3.
"""

import json
import logging
from collections.abc import AsyncIterator

import httpx

from dollos.llm.adapter import LLMAdapter, StreamChunk

logger = logging.getLogger(__name__)


def _render_chatml(system: str, user: str, prefill: str) -> str:
    """Render Qwen-style ChatML prompt with assistant role opened for prefill."""
    parts = [
        "<|im_start|>system",
        system,
        "<|im_end|>",
        "<|im_start|>user",
        user,
        "<|im_end|>",
        "<|im_start|>assistant",
        "",
    ]
    rendered = "\n".join(parts)
    if prefill:
        rendered += prefill
    return rendered


class LlamaCppAdapter(LLMAdapter):
    """Adapter for self-hosted llama.cpp `/completion` endpoint."""

    def __init__(self, base_url: str, timeout_s: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    async def stream_completion(
        self,
        *,
        system: str,
        user: str,
        prefill: str = "",
        stop: list[str] | None = None,
        max_tokens: int = 1024,
    ) -> AsyncIterator[StreamChunk]:
        prompt = _render_chatml(system=system, user=user, prefill=prefill)
        body = {
            "prompt": prompt,
            "stream": True,
            "n_predict": max_tokens,
            "stop": stop or ["<|im_end|>"],
            "cache_prompt": True,
        }
        url = f"{self.base_url}/completion"
        timeout = httpx.Timeout(self.timeout_s, connect=5.0)

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=body) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line.removeprefix("data:").strip()
                    if not payload:
                        continue
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        logger.warning("non-JSON SSE line: %r", payload)
                        continue
                    yield StreamChunk(
                        text=data.get("content", ""),
                        done=bool(data.get("stop", False)),
                    )
                    if data.get("stop"):
                        return
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
uv run pytest tests/test_llm_llamacpp.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/progcat/Projects/DollOS
git add daemon/
git commit -m "feat(daemon): llama.cpp /completion adapter with prefill support"
```

---

## Task 5: IPC Message Schemas

**Files:**
- Create: `daemon/src/dollos/ipc/__init__.py`
- Create: `daemon/src/dollos/ipc/messages.py`
- Create: `daemon/tests/test_ipc_messages.py`

Message types for v1 plan-1 are minimal — just text input and streaming text output. Audio / Cubism / proactive_speak come in later plans.

- [ ] **Step 1: Write the failing test `daemon/tests/test_ipc_messages.py`**

```python
"""Tests for IPC message schemas."""

import json

import pytest
from pydantic import ValidationError

from dollos.ipc.messages import (
    ClientMessage,
    ServerMessage,
    TextChunk,
    TextInput,
    TurnEnd,
    decode_client_message,
    encode_server_message,
)


def test_text_input_round_trip():
    msg = TextInput(text="hello")
    raw = msg.model_dump_json()
    parsed = json.loads(raw)
    assert parsed == {"type": "text_input", "text": "hello"}


def test_decode_client_message_text_input():
    raw = '{"type": "text_input", "text": "hi"}'
    msg = decode_client_message(raw)
    assert isinstance(msg, TextInput)
    assert msg.text == "hi"


def test_decode_client_message_unknown_type_raises():
    raw = '{"type": "unknown_type"}'
    with pytest.raises(ValidationError):
        decode_client_message(raw)


def test_decode_client_message_malformed_json_raises():
    with pytest.raises(ValueError):
        decode_client_message("not json")


def test_encode_text_chunk():
    msg = TextChunk(text="world")
    raw = encode_server_message(msg)
    parsed = json.loads(raw)
    assert parsed == {"type": "text_chunk", "text": "world"}


def test_encode_turn_end():
    msg = TurnEnd()
    raw = encode_server_message(msg)
    parsed = json.loads(raw)
    assert parsed == {"type": "turn_end"}
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd daemon
uv run pytest tests/test_ipc_messages.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write `daemon/src/dollos/ipc/__init__.py`**

```python
"""IPC layer — WebSocket server and message schemas."""
```

- [ ] **Step 4: Write `daemon/src/dollos/ipc/messages.py`**

```python
"""IPC message schemas (pydantic).

Wire format: JSON for control messages (`type` field discriminator).
Binary frames (audio etc.) come in later plans.
"""

import json
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter


# ===== Client → Server =====

class TextInput(BaseModel):
    type: Literal["text_input"] = "text_input"
    text: str


ClientMessage = Annotated[Union[TextInput], Field(discriminator="type")]
_client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


def decode_client_message(raw: str) -> ClientMessage:
    """Parse a raw JSON string into a typed client message.

    Raises ValueError on malformed JSON or unknown message type.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"malformed JSON: {e}") from e
    return _client_adapter.validate_python(data)


# ===== Server → Client =====

class TextChunk(BaseModel):
    type: Literal["text_chunk"] = "text_chunk"
    text: str


class TurnEnd(BaseModel):
    type: Literal["turn_end"] = "turn_end"


class ErrorMsg(BaseModel):
    type: Literal["error"] = "error"
    message: str


ServerMessage = Annotated[
    Union[TextChunk, TurnEnd, ErrorMsg],
    Field(discriminator="type"),
]


def encode_server_message(msg: ServerMessage) -> str:
    """Serialize a server message to JSON."""
    return msg.model_dump_json()
```

- [ ] **Step 5: Run tests, verify they pass**

```bash
uv run pytest tests/test_ipc_messages.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 6: Commit**

```bash
cd /home/progcat/Projects/DollOS
git add daemon/
git commit -m "feat(daemon): IPC message schemas"
```

---

## Task 6: WebSocket Server

**Files:**
- Create: `daemon/src/dollos/ipc/server.py`
- Create: `daemon/tests/test_ipc_server.py`

The server accepts WebSocket connections and delegates each `text_input` message to a handler callback, which is expected to yield `ServerMessage` objects (typically a stream of `TextChunk` followed by `TurnEnd`). The handler is injected — keeps the server testable without an LLM backend.

- [ ] **Step 1: Write the failing test `daemon/tests/test_ipc_server.py`**

```python
"""Tests for WebSocket server."""

import asyncio
from collections.abc import AsyncIterator

import pytest
import websockets

from dollos.ipc.messages import ServerMessage, TextChunk, TextInput, TurnEnd
from dollos.ipc.server import WebSocketServer


async def _echo_handler(msg: TextInput) -> AsyncIterator[ServerMessage]:
    """Test handler — yields the input text chunked, then turn_end."""
    for ch in msg.text:
        yield TextChunk(text=ch)
    yield TurnEnd()


@pytest.mark.asyncio
async def test_server_accepts_text_input_and_streams_back():
    server = WebSocketServer(host="127.0.0.1", port=0, handler=_echo_handler)
    await server.start()
    try:
        port = server.port
        assert port is not None

        uri = f"ws://127.0.0.1:{port}"
        async with websockets.connect(uri) as ws:
            await ws.send('{"type": "text_input", "text": "hi"}')
            msgs = []
            for _ in range(3):  # "h", "i", turn_end
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                msgs.append(raw)

        assert '"text_chunk"' in msgs[0] and '"h"' in msgs[0]
        assert '"text_chunk"' in msgs[1] and '"i"' in msgs[1]
        assert '"turn_end"' in msgs[2]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_server_sends_error_on_malformed_message():
    server = WebSocketServer(host="127.0.0.1", port=0, handler=_echo_handler)
    await server.start()
    try:
        port = server.port
        assert port is not None

        uri = f"ws://127.0.0.1:{port}"
        async with websockets.connect(uri) as ws:
            await ws.send("not json")
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)

        assert '"error"' in raw
    finally:
        await server.stop()
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd daemon
uv run pytest tests/test_ipc_server.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write `daemon/src/dollos/ipc/server.py`**

```python
"""WebSocket IPC server."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable

import websockets
from websockets.asyncio.server import ServerConnection, serve

from dollos.ipc.messages import (
    ErrorMsg,
    ServerMessage,
    TextInput,
    decode_client_message,
    encode_server_message,
)

logger = logging.getLogger(__name__)


Handler = Callable[[TextInput], AsyncIterator[ServerMessage]]
"""A handler takes a typed client message and yields server messages."""


class WebSocketServer:
    """Async WebSocket server.

    Each incoming client message is dispatched to the handler callback. The
    handler is expected to yield a stream of ServerMessage objects.
    """

    def __init__(self, host: str, port: int, handler: Handler):
        self._host = host
        self._port_requested = port
        self._handler = handler
        self._server: websockets.asyncio.server.Server | None = None

    @property
    def port(self) -> int | None:
        if self._server is None:
            return None
        for sock in self._server.sockets:
            return sock.getsockname()[1]
        return None

    async def start(self) -> None:
        self._server = await serve(self._on_connect, self._host, self._port_requested)
        logger.info("WebSocket server listening on %s:%d", self._host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _on_connect(self, ws: ServerConnection) -> None:
        logger.info("client connected: %s", ws.remote_address)
        try:
            async for raw in ws:
                if not isinstance(raw, str):
                    await self._send_error(ws, "binary frames not supported in v1")
                    continue
                try:
                    msg = decode_client_message(raw)
                except ValueError as e:
                    await self._send_error(ws, f"decode error: {e}")
                    continue

                async for out in self._handler(msg):
                    await ws.send(encode_server_message(out))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            logger.info("client disconnected: %s", ws.remote_address)

    async def _send_error(self, ws: ServerConnection, message: str) -> None:
        await ws.send(encode_server_message(ErrorMsg(message=message)))
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
uv run pytest tests/test_ipc_server.py -v
```

Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/progcat/Projects/DollOS
git add daemon/
git commit -m "feat(daemon): WebSocket IPC server with handler dispatch"
```

---

## Task 7: Daemon Entry — Wire Adapter and Server Together

**Files:**
- Create: `daemon/src/dollos/daemon.py`
- Modify: `daemon/src/dollos/__main__.py`

This task wires `LLMAdapter` and `WebSocketServer` together: when a `TextInput` arrives, call `adapter.stream_completion()` and wrap each `StreamChunk` as a `TextChunk` server message, finishing with `TurnEnd`.

For this skeleton, the system prompt is hardcoded ("You are Doll, a helpful AI companion."). Character pack loading and personality come in a later plan.

- [ ] **Step 1: Write `daemon/src/dollos/daemon.py`**

```python
"""Daemon: wires LLM adapter and IPC server together."""

import asyncio
import logging
import signal
from collections.abc import AsyncIterator

from dollos.config import Settings
from dollos.ipc.messages import ServerMessage, TextChunk, TextInput, TurnEnd
from dollos.ipc.server import WebSocketServer
from dollos.llm.adapter import LLMAdapter
from dollos.llm.llamacpp import LlamaCppAdapter

logger = logging.getLogger(__name__)


PLACEHOLDER_SYSTEM_PROMPT = "You are Doll, a helpful AI companion."
"""Placeholder until character pack loading lands in a later plan."""


def build_adapter(settings: Settings) -> LLMAdapter:
    if settings.llm.backend == "llamacpp":
        return LlamaCppAdapter(
            base_url=settings.llm.base_url,
            timeout_s=settings.llm.timeout_s,
        )
    raise ValueError(f"unknown LLM backend: {settings.llm.backend}")


class Daemon:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.adapter = build_adapter(settings)
        self.server = WebSocketServer(
            host=settings.ipc.host,
            port=settings.ipc.port,
            handler=self._handle_text_input,
        )
        self._shutdown = asyncio.Event()

    async def _handle_text_input(self, msg: TextInput) -> AsyncIterator[ServerMessage]:
        try:
            async for chunk in self.adapter.stream_completion(
                system=PLACEHOLDER_SYSTEM_PROMPT,
                user=msg.text,
                prefill="",
            ):
                if chunk.text:
                    yield TextChunk(text=chunk.text)
                if chunk.done:
                    break
            yield TurnEnd()
        except Exception as e:
            logger.exception("handler error")
            from dollos.ipc.messages import ErrorMsg
            yield ErrorMsg(message=f"handler error: {e}")

    async def run(self) -> None:
        await self.server.start()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._shutdown.set)
        try:
            await self._shutdown.wait()
        finally:
            await self.server.stop()
```

- [ ] **Step 2: Update `daemon/src/dollos/__main__.py`**

```python
"""Entry point: python -m dollos --config <path>."""

import argparse
import asyncio
import sys
from pathlib import Path

from dollos.config import load_settings
from dollos.daemon import Daemon
from dollos.log import setup_logging


def main() -> int:
    parser = argparse.ArgumentParser(prog="dollos")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="path to TOML config file",
    )
    args = parser.parse_args()

    settings = load_settings(args.config)
    setup_logging(settings.log.level)

    daemon = Daemon(settings)
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Verify daemon starts (manual sanity check)**

```bash
cd daemon
cp config.example.toml config.toml
# Run in background and kill after a second
timeout 2 uv run python -m dollos --config config.toml || true
```

Expected: log line `WebSocket server listening on 127.0.0.1:9876`, then exits cleanly on timeout.

- [ ] **Step 4: Commit**

```bash
cd /home/progcat/Projects/DollOS
git add daemon/
git commit -m "feat(daemon): wire adapter to IPC server, runnable entry point"
```

---

## Task 8: End-to-End Integration Test

**Files:**
- Create: `daemon/tests/test_e2e.py`

This test stands up the full Daemon (real WebSocketServer + LlamaCppAdapter) but mocks the llama.cpp HTTP endpoint via `respx`. It validates the full path: WebSocket text → adapter → mocked LLM → text chunks back over WebSocket.

- [ ] **Step 1: Write the failing test `daemon/tests/test_e2e.py`**

```python
"""End-to-end test: WebSocket client → daemon → mocked llama.cpp → response."""

import asyncio
import json

import httpx
import pytest
import respx
import websockets

from dollos.config import IPCConfig, LLMConfig, LogConfig, Settings
from dollos.daemon import Daemon


@pytest.mark.asyncio
async def test_full_round_trip_with_mocked_llamacpp():
    settings = Settings(
        llm=LLMConfig(
            backend="llamacpp",
            base_url="http://test.local:8001",
            model_alias="mock",
        ),
        ipc=IPCConfig(host="127.0.0.1", port=0),
        log=LogConfig(level="WARNING"),
    )

    daemon = Daemon(settings)

    sse_body = (
        'data: {"content": "Hi", "stop": false}\n\n'
        'data: {"content": " there", "stop": false}\n\n'
        'data: {"content": "", "stop": true}\n\n'
    )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_body,
            )
        )

        await daemon.server.start()
        try:
            port = daemon.server.port
            assert port is not None

            uri = f"ws://127.0.0.1:{port}"
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({"type": "text_input", "text": "Hello"}))

                received: list[dict] = []
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                    parsed = json.loads(raw)
                    received.append(parsed)
                    if parsed["type"] == "turn_end":
                        break

            text_chunks = [m for m in received if m["type"] == "text_chunk"]
            assert "".join(c["text"] for c in text_chunks) == "Hi there"
            assert received[-1]["type"] == "turn_end"
        finally:
            await daemon.server.stop()
```

- [ ] **Step 2: Run test, verify it passes**

```bash
cd daemon
uv run pytest tests/test_e2e.py -v
```

Expected: PASS.

- [ ] **Step 3: Run the full test suite**

```bash
uv run pytest -v
```

Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
cd /home/progcat/Projects/DollOS
git add daemon/
git commit -m "test(daemon): end-to-end integration test with mocked llama.cpp"
```

---

## Task 9: Live Smoke Test (Manual)

**Files:**
- Modify: `daemon/README.md` — add manual smoke test instructions

This is a one-time manual verification that the daemon talks to a real llama.cpp server. Not run in CI.

- [ ] **Step 1: Append to `daemon/README.md`**

````markdown

## Manual Smoke Test

Prereq: a running llama-server (e.g. Qwen3.6-35B-A3B per project CLAUDE.md).

1. Start daemon:
   ```bash
   cp config.example.toml config.toml
   # edit config.toml to point at your llama-server URL
   uv run python -m dollos --config config.toml
   ```

2. In another terminal, send a test message via Python:
   ```bash
   uv run python -c "
   import asyncio, json, websockets

   async def main():
       async with websockets.connect('ws://127.0.0.1:9876') as ws:
           await ws.send(json.dumps({'type': 'text_input', 'text': 'Say hi.'}))
           while True:
               raw = await ws.recv()
               msg = json.loads(raw)
               print(msg)
               if msg['type'] == 'turn_end':
                   break

   asyncio.run(main())
   "
   ```

Expected: a stream of `text_chunk` messages followed by `turn_end`.
````

- [ ] **Step 2: Run the manual smoke test against your real llama-server**

Follow the README steps. Confirm tokens stream back.

- [ ] **Step 3: Commit**

```bash
cd /home/progcat/Projects/DollOS
git add daemon/README.md
git commit -m "docs(daemon): manual smoke test instructions"
```

---

## Done — What This Plan Produced

After all tasks complete you have:

- A runnable Python daemon (`uv run python -m dollos --config config.toml`)
- WebSocket server listening on configured host:port
- Working llama.cpp `/completion` adapter with prefill support (the foundation for VoM in plan 3)
- Type-safe IPC message schemas (extensible — audio / Cubism messages added in later plans)
- 14 passing automated tests across config, adapter, IPC, and end-to-end
- Manual smoke test verified against real llama-server

**What is NOT in this plan (deferred to later plans):**
- Memory SoT — Plan 2
- Inner Voice + Instinct + VoM recall — Plan 3
- Streaming TTS / audio frames — Plan 4 (Voice Pipeline)
- Character Pack loading — Plan 5
- Subagent / Drone — Plan 6
- Self-state / Self-First Design — Plan 7
- Phone-side network WS authentication / pairing — Plan 8 (App MVP)

Next plan: **Memory SoT storage layer** (sqlite-vec or LanceDB; schema for facts vs self-memory; basic CRUD + hybrid retrieval).
