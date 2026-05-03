"""End-to-end test: WebSocket client → daemon → mocked llama.cpp → response."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx
import websockets

from dollos.config import (
    CharacterConfig,
    EmbedderConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemoryConfig,
    Settings,
)
from dollos.kernel import DollOS


@pytest.mark.asyncio
async def test_full_round_trip_with_mocked_llamacpp():
    settings = Settings(
        llm=LLMConfig(
            provider="llamacpp",
            template="qwen3-thinking",
            base_url="http://test.local:8001",
            model_alias="mock",
        ),
        ipc=IPCConfig(host="127.0.0.1", port=0),
        log=LogConfig(level="WARNING"),
        memory=MemoryConfig(db_path=Path("/tmp/dollos-test.db")),
        embedder=EmbedderConfig(
            backend="llamacpp",
            base_url="http://test.local:8002",
            model_id="test-emb",
        ),
        character=CharacterConfig(
            profile_path=Path("experiments/test_character.jinja"),
        ),
    )

    dollos = DollOS(settings)

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

        await dollos.server.start()
        try:
            port = dollos.server.port
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
            await dollos.server.stop()
