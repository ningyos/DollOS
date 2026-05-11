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
from unittest.mock import AsyncMock, MagicMock

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
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "doll.toml").write_text(
        '[meta]\n'
        'id = "gura"\n'
        'name = "Gura"\n'
        '\n'
        '[identity]\n'
        'self = "You are Gura, a 9000-year-old shark."\n'
        'personality = "- chill"\n'
        'taboos = "- no LARP"\n'
    )

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
        character=CharacterConfig(pack=pack_dir),
        inner_voice=InnerVoiceConfig(
            base_url="http://test.local:8003",
            timeout_s=5.0,
        ),
    )

    # Stub InnerVoice.recall — recall behavior is covered by test_inner_voice.py.
    async def _stub_recall(self, query, **kwargs):
        return "- user likes coffee"

    monkeypatch.setattr("dollos.inner_voice.InnerVoice.recall", _stub_recall)

    # Stub SmallModelInstinct.process — legacy path (not called by current
    # dispatcher, but kept for symmetry).
    async def _stub_instinct_process(self, event):
        return ""

    monkeypatch.setattr("dollos.instinct.SmallModelInstinct.process", _stub_instinct_process)

    # Stub SmallModelInstinct.compact_cascade — avoids real small-LLM call.
    async def _stub_compact_cascade(self, *, perception, cascade_messages):
        return "test summary"

    monkeypatch.setattr(
        "dollos.instinct.SmallModelInstinct.compact_cascade", _stub_compact_cascade
    )

    # No-op memsearch.index() to avoid downloading the ONNX model in tests.
    async def _noop_index(self):
        return None

    monkeypatch.setattr("memsearch.MemSearch.index", _noop_index)

    dollos = DollOS(settings)
    # Phase 1 schedule (2026-05-10): on first WS connect, kernel fires a
    # DailyPlanEvent if today has no schedule. Mark today as already
    # bootstrapped so this test exercises only the user turn.
    from datetime import date as _date
    dollos._bootstrapped_dates.add(_date.today())

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
            # Wire format (2026-05-08): IV.recall result lives in a
            # [Memory context] block in the user message, not in prefill.
            # The literal "RECALL:" label is gone for good.
            assert "[Memory context]" in prompt
            assert "user likes coffee" in prompt
            assert "RECALL:" not in prompt
            # The ChatML assistant turn opens exactly one <think> block.
            # (Scaffolding may contain `<think>` in inline code — check the
            # actual turn opener pattern, not raw count.) Multi-message
            # render (2026-05-08): with no cascade, messages list has only
            # the original framed user — exactly 1 user block + 1 open
            # assistant block.
            assert prompt.count("<|im_start|>assistant\n<think>") == 1
            assert prompt.count("<|im_start|>user\n") == 1
        finally:
            await dollos.server.stop()


@pytest.mark.asyncio
async def test_monitor_round_trip_with_mocked_llamacpp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """SpawnMonitor → 2 MonitorTriggeredEvents (per line) → MonitorExitedEvent.

    Mocked LLM emits SpawnMonitor on call 1, Say('ok') on every subsequent
    call. Real MonitorRunner spawns a `printf 'a\\nb\\n'` proc — it exits
    immediately producing 2 lines + EOF. We expect 4 turn_ends:
      T1 — SpawnMonitor cascade (2 LLM iters: tool + Say)
      T2/T3 — MonitorTriggered for 'a' / 'b'
      T4 — MonitorExited
    """
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "doll.toml").write_text(
        '[meta]\n'
        'id = "gura"\n'
        'name = "Gura"\n'
        '\n'
        '[identity]\n'
        'self = "You are Gura."\n'
        'personality = "- chill"\n'
        'taboos = "- no LARP"\n'
    )

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
        character=CharacterConfig(pack=pack_dir),
        inner_voice=InnerVoiceConfig(
            base_url="http://test.local:8003",
            timeout_s=5.0,
        ),
    )

    async def _stub_recall(self, query, **kwargs):
        return ""

    monkeypatch.setattr("dollos.inner_voice.InnerVoice.recall", _stub_recall)

    async def _stub_instinct_process(self, event):
        return ""

    monkeypatch.setattr(
        "dollos.instinct.SmallModelInstinct.process", _stub_instinct_process
    )

    async def _stub_compact_cascade(self, *, perception, cascade_messages):
        return "test summary"

    monkeypatch.setattr(
        "dollos.instinct.SmallModelInstinct.compact_cascade", _stub_compact_cascade
    )

    async def _noop_index(self):
        return None

    monkeypatch.setattr("memsearch.MemSearch.index", _noop_index)

    dollos = DollOS(settings)
    from datetime import date as _date
    dollos._bootstrapped_dates.add(_date.today())

    spawn_payload = {
        "name": "SpawnMonitor",
        "arguments": {
            # Raw r-string so the JSON value carries literal \n (two chars)
            # — bash + printf interprets them as newlines at run time.
            "command": r"printf 'a\nb\n'",
            "rate_limit_s": 0,
        },
    }
    spawn_tc = f"<tool_call>{json.dumps(spawn_payload)}</tool_call>"
    say_tc = '<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>'

    def _sse(content: str) -> str:
        return (
            "data: " + json.dumps({"content": content, "stop": False}) + "\n\n"
            + "data: " + json.dumps({"content": "", "stop": True}) + "\n\n"
        )

    captured_prompts: list[str] = []
    call_count = 0

    def _capture_and_respond(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        body = json.loads(request.content)
        captured_prompts.append(body["prompt"])
        call_count += 1
        tc = spawn_tc if call_count == 1 else say_tc
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(tc),
        )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(side_effect=_capture_and_respond)

        await dollos.memsearch.index()
        await dollos.server.start()
        try:
            port = dollos.server.port
            assert port is not None
            uri = f"ws://127.0.0.1:{port}"
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({
                    "type": "text_input", "text": "monitor 兩行",
                }))

                turn_ends = 0
                received: list[dict] = []
                # Each turn: LLM call (mocked, instant) + minor scheduling
                # latency. 4 turns should land well under 10s even on slow CI.
                deadline = asyncio.get_event_loop().time() + 10.0
                while turn_ends < 4:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    msg = json.loads(raw)
                    received.append(msg)
                    if msg["type"] == "turn_end":
                        turn_ends += 1

            assert turn_ends == 4, (
                f"expected 4 turn_ends, got {turn_ends}; "
                f"received types={[m['type'] for m in received]}"
            )

            # Exited handler pops from _active; give it a beat to drain.
            for _ in range(20):
                if dollos.monitor_runner.active_state() == []:
                    break
                await asyncio.sleep(0.05)
            assert dollos.monitor_runner.active_state() == []

            joined = "\n".join(captured_prompts)
            # Triggered perceptions for both lines reached the LLM:
            assert "monitor mon-1 觸發" in joined
            assert "line: a" in joined and "line: b" in joined
            # Exited perception also reached the LLM:
            assert "monitor mon-1 結束" in joined
            # 5 LLM calls total: T1 cascade has 2 iters (SpawnMonitor + Say),
            # T2/T3/T4 each have 1 iter (Say).
            assert call_count == 5, f"expected 5 LLM calls, got {call_count}"
        finally:
            await dollos.server.stop()


@pytest.mark.asyncio
async def test_voice_session_offer_answer_with_mocked_aiortc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A WS client sends webrtc_offer → daemon answers; aiortc is fully mocked.

    Verifies: signaling messages are routed, VoiceSession is created with
    the correct engines, answer SDP comes back over WS.
    """
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "doll.toml").write_text(
        '[meta]\nid="gura"\nname="Gura"\n[identity]\nself="x"\npersonality="x"\ntaboos="x"\n'
    )
    voice_dir = pack_dir / "voice"
    voice_dir.mkdir()
    (voice_dir / "engine.toml").write_text(
        '[asr]\nengine="e2e-asr"\n\n[tts]\nengine="e2e-tts"\n'
    )

    settings = Settings(
        llm=LLMConfig(
            provider="llamacpp", template="qwen3-thinking",
            base_url="http://test.local:8001", model_alias="mock",
        ),
        ipc=IPCConfig(host="127.0.0.1", port=0),
        log=LogConfig(level="ERROR"),
        data=DataConfig(root=tmp_path / "data"),
        memsearch=MemsearchConfig(top_k=10),
        character=CharacterConfig(pack=pack_dir),
        inner_voice=InnerVoiceConfig(
            base_url="http://test.local:8003", timeout_s=5.0,
        ),
    )

    async def _stub_recall(self, q, **kw): return ""
    async def _stub_process(self, e): return ""
    async def _stub_compact(self, *, perception, cascade_messages): return ""
    async def _noop_index(self): return None

    monkeypatch.setattr("dollos.inner_voice.InnerVoice.recall", _stub_recall)
    monkeypatch.setattr("dollos.instinct.SmallModelInstinct.process", _stub_process)
    monkeypatch.setattr("dollos.instinct.SmallModelInstinct.compact_cascade", _stub_compact)
    monkeypatch.setattr("memsearch.MemSearch.index", _noop_index)

    from dollos.voice.engines import ASR_REGISTRY, TTS_REGISTRY, ASREngine, TTSEngine

    class _E2EASR(ASREngine):
        def __init__(self, **kw): pass
        async def transcribe(self, audio_pcm, sample_rate): return ""
        async def aclose(self): pass

    class _E2ETTS(TTSEngine):
        sample_rate = 48000
        def __init__(self, **kw): pass
        async def synthesize(self, text):
            if False: yield b""
        async def aclose(self): pass

    ASR_REGISTRY["e2e-asr"] = _E2EASR
    TTS_REGISTRY["e2e-tts"] = _E2ETTS

    fake_peer = MagicMock()
    fake_peer.setRemoteDescription = AsyncMock()
    fake_peer.createAnswer = AsyncMock(return_value=MagicMock(sdp="answer-sdp"))
    fake_peer.setLocalDescription = AsyncMock()
    fake_peer.localDescription = MagicMock(sdp="answer-sdp")
    fake_peer.addIceCandidate = AsyncMock()
    fake_peer.close = AsyncMock()
    fake_peer.addTrack = MagicMock()
    fake_peer.on = MagicMock()
    monkeypatch.setattr("dollos.voice.session.RTCPeerConnection", lambda: fake_peer)

    dollos = DollOS(settings)
    from datetime import date as _date
    dollos._bootstrapped_dates.add(_date.today())

    try:
        await dollos.memsearch.index()
        await dollos.server.start()
        uri = f"ws://127.0.0.1:{dollos.server.port}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"type": "webrtc_offer", "sdp": "offer-sdp"}))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert msg["type"] == "webrtc_answer"
            assert msg["sdp"] == "answer-sdp"
    finally:
        await dollos.server.stop()
        del ASR_REGISTRY["e2e-asr"]
        del TTS_REGISTRY["e2e-tts"]
