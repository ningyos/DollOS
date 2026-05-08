"""End-to-end test: WebSocket client → DollOS → mocked llama.cpp → response.

The full chain runs, but with two cheap stubs:
- InnerVoice.recall returns a fixed RECALL block (recall behavior is
  covered by tests/test_inner_voice.py).
- MemSearch.index is no-op'd (a real index() would download the
  ~558MB ONNX model on first run).
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx
import websockets

from dollos.config import (
    CharacterConfig,
    DataConfig,
    InnerVoiceConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemsearchConfig,
    Settings,
)
from dollos.kernel import DollOS


@pytest.mark.asyncio
async def test_full_round_trip_with_mocked_llamacpp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    character_path = tmp_path / "test_character.jinja"
    character_path.write_text("You are Gura, a 9000-year-old shark.")

    settings = Settings(
        llm=LLMConfig(
            provider="llamacpp",
            template="qwen3-thinking",
            base_url="http://test.local:8001",
            model_alias="mock",
        ),
        ipc=IPCConfig(host="127.0.0.1", port=0),
        log=LogConfig(level="WARNING"),
        data=DataConfig(root=tmp_path / "data"),
        memsearch=MemsearchConfig(top_k=10),
        character=CharacterConfig(profile_path=character_path),
        inner_voice=InnerVoiceConfig(
            base_url="http://test.local:8003",
            timeout_s=5.0,
        ),
    )

    # Stub InnerVoice.recall — recall behavior is covered by test_inner_voice.py.
    async def _stub_recall(self, query, **kwargs):
        return "RECALL:\n- user likes coffee\n"

    monkeypatch.setattr("dollos.inner_voice.InnerVoice.recall", _stub_recall)

    # Stub SmallModelInstinct.process — returns empty so no STATE block in prefill.
    async def _stub_instinct_process(self, event):
        return ""

    monkeypatch.setattr("dollos.instinct.SmallModelInstinct.process", _stub_instinct_process)

    # No-op memsearch.index() to avoid downloading the ONNX model in tests.
    async def _noop_index(self):
        return None

    monkeypatch.setattr("memsearch.MemSearch.index", _noop_index)

    dollos = DollOS(settings)

    _tc = '<tool_call>{"name":"Say","arguments":{"text":"Hi there"}}</tool_call>'
    sse_body = (
        "data: " + json.dumps({"content": _tc, "stop": False}) + "\n\n"
        + "data: " + json.dumps({"content": "", "stop": True}) + "\n\n"
    )

    captured_requests: list[dict] = []

    def _capture_and_respond(request: httpx.Request) -> httpx.Response:
        captured_requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_body,
        )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(side_effect=_capture_and_respond)

        # Use run-style lifecycle: index then start. We call them manually
        # because dollos.run() blocks on _shutdown.wait().
        await dollos.memsearch.index()  # no-op due to monkeypatch
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

            text_chunks = [msg for msg in received if msg["type"] == "text_chunk"]
            assert "".join(c["text"] for c in text_chunks) == "Hi there"
            assert received[-1]["type"] == "turn_end"
            assert len(captured_requests) == 1
            prompt = captured_requests[0]["prompt"]
            assert "You are Gura, a 9000-year-old shark." in prompt
            # Prefill removed 2026-05-07 — RECALL must NOT appear in prompt
            # (mimicry / infinite-transcript bug). IV.recall still runs but
            # its output stays in memsearch, not in <think>.
            assert "RECALL:" not in prompt
            # The ChatML assistant turn opens exactly one <think> block.
            # (Scaffolding may contain `<think>` in inline code — check the
            # actual turn opener pattern, not raw count.)
            assert prompt.count("<|im_start|>assistant\n<think>") == 1
        finally:
            await dollos.server.stop()
