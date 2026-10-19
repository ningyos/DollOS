"""ChatCompletionsAdapter against SSE recorded from the Strata server.

Fixtures in tests/fixtures/strata/ were recorded with small requests (the
request body sits next to each .sse file). The error chunk could not be
provoked from the server, so that one is hand-written here.
"""

import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest
import respx

from dollos.llm.chat_completions import ChatCompletionsAdapter
from dollos.llm.model import (
    End,
    ModelError,
    SpeechDelta,
    ThinkDelta,
    ToolCalls,
    tool_message,
)
from dollos.telemetry.llm_calls import LLMCallRecord, TelemetryRecorder

BASE = "http://strata.test:8080"
MODEL = "qwen3.8-flash-next-iq3_s"
FIXTURES = Path(__file__).parent / "fixtures" / "strata"
SSE = {"content-type": "text/event-stream"}
MESSAGES = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
CAP_LINE = "I have thought about this long enough; time to give my answer."
TOOLS = json.loads((FIXTURES / "think_speech_two_tools.request.json").read_text())["tools"]


def _sse(name: str) -> bytes:
    return (FIXTURES / f"{name}.sse").read_bytes()


def _lines(name: str) -> list[bytes]:
    return [ln for ln in _sse(name).split(b"\n") if ln.strip()]


def _join(lines: list[bytes]) -> bytes:
    return b"".join(ln + b"\n\n" for ln in lines)


def _data(line: bytes) -> dict:
    return json.loads(line.removeprefix(b"data: "))


def _is_tool_chunk(line: bytes) -> bool:
    if not line.startswith(b"data: {"):
        return False
    choices = _data(line).get("choices") or [{}]
    return bool(choices[0].get("delta", {}).get("tool_calls"))


def _tool_fragments(name: str) -> dict[int, int]:
    counts: dict[int, int] = {}
    for ln in filter(_is_tool_chunk, _lines(name)):
        for tc in _data(ln)["choices"][0]["delta"]["tool_calls"]:
            counts[tc["index"]] = counts.get(tc["index"], 0) + 1
    return counts


def _adapter(**kw) -> ChatCompletionsAdapter:
    kw.setdefault("timeout_s", 5.0)
    return ChatCompletionsAdapter(base_url=BASE, model=MODEL, **kw)


def _ok(body: bytes) -> httpx.Response:
    return httpx.Response(200, headers=SSE, content=body)


def _streamed(stream: httpx.AsyncByteStream) -> httpx.Response:
    return httpx.Response(200, headers=SSE, stream=stream)


async def _collect(adapter: ChatCompletionsAdapter, **kw) -> list:
    args = dict(
        messages=MESSAGES, tools=TOOLS, max_tokens=200, think_budget=64,
        purpose="test", on_usage=None,
    )
    args.update(kw)
    return [ev async for ev in adapter.stream(**args)]


class _GatedStream(httpx.AsyncByteStream):
    """Sends `head`, then hangs until `gate` is set, then sends `tail`."""

    def __init__(self, head: bytes, tail: bytes = b"") -> None:
        self.head = head
        self.tail = tail
        self.gate = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield self.head
        await self.gate.wait()
        yield self.tail

    async def aclose(self) -> None:
        self.closed = True


# --- parsing recorded streams -------------------------------------------------


async def test_think_speech_then_two_tool_calls():
    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(return_value=_ok(_sse("think_speech_two_tools")))
        events = await _collect(_adapter())

    kinds = [type(e).__name__ for e in events]
    first_speech = kinds.index("SpeechDelta")
    assert kinds[0] == "ThinkDelta"
    assert set(kinds[:first_speech]) == {"ThinkDelta"}
    assert set(kinds[first_speech:-2]) == {"SpeechDelta"}
    assert kinds[-2:] == ["ToolCalls", "End"]
    assert all(e.text for e in events if isinstance(e, (ThinkDelta, SpeechDelta)))

    # Arguments arrive in many pieces cut anywhere; joined by index, untouched.
    assert _tool_fragments("think_speech_two_tools") == {0: 12, 1: 19}
    calls = events[-2].calls
    assert [c.name for c in calls] == ["NoteMemory", "MoodTool"]
    assert calls[0].arguments == '{"text":"Owner eats breakfast at 8"}'
    assert json.loads(calls[1].arguments)["emotion"] == "happy"
    assert all(c.id for c in calls) and calls[0].id != calls[1].id

    end = events[-1]
    assert end.finish_reason == "tool_calls"
    assert end.think_capped is False
    assert end.dropped_tool_calls == 0
    u = end.usage
    for field in ("prompt_tokens", "completion_tokens", "cache_n", "prompt_n",
                  "prompt_ms", "predicted_per_second"):
        assert getattr(u, field) is not None, field
    assert u.cache_n + u.prompt_n == u.prompt_tokens


async def test_assistant_message_is_the_reply_verbatim():
    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(return_value=_ok(_sse("think_speech_two_tools")))
        events = await _collect(_adapter())

    think = "".join(e.text for e in events if isinstance(e, ThinkDelta))
    speech = "".join(e.text for e in events if isinstance(e, SpeechDelta))
    calls = events[-2].calls
    msg = events[-1].assistant_message
    assert msg == {
        "role": "assistant",
        "content": speech,
        "reasoning_content": think,
        "tool_calls": [
            {"id": c.id, "type": "function",
             "function": {"name": c.name, "arguments": c.arguments}}
            for c in calls
        ],
    }
    assert tool_message(calls[0].id, "ok") == {
        "role": "tool", "tool_call_id": calls[0].id, "content": "ok",
    }


async def test_keepalive_and_role_chunk_do_not_count_as_first_token():
    lines = _lines("think_speech_two_tools")
    role, rest = lines[0], lines[1:]
    assert _data(role)["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    stream = _GatedStream(
        head=role + b"\n\n: keep-alive\n\n",
        tail=b": keep-alive\n\n" + _join(rest),
    )

    async def open_gate():
        await asyncio.sleep(0.3)
        stream.gate.set()

    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(return_value=_streamed(stream))
        opener = asyncio.create_task(open_gate())
        events = await _collect(_adapter())
        await opener

    assert all(e.text for e in events if isinstance(e, (ThinkDelta, SpeechDelta)))
    assert events[-1].ttft_ms >= 300


async def test_length_mid_tool_call_drops_every_call(caplog):
    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(return_value=_ok(_sse("length_in_tool_call")))
        with caplog.at_level(logging.WARNING, logger="dollos.llm.chat_completions"):
            events = await _collect(_adapter())

    assert not any(isinstance(e, ToolCalls) for e in events)
    end = events[-1]
    assert end.finish_reason == "length"
    assert end.dropped_tool_calls == 1
    assert "tool_calls" not in end.assistant_message
    assert any("dropped 1 tool call" in r.getMessage() for r in caplog.records)


async def test_length_in_thinking_has_no_speech():
    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(return_value=_ok(_sse("length_in_think")))
        events = await _collect(_adapter(), tools=[])

    assert any(isinstance(e, ThinkDelta) for e in events)
    assert not any(isinstance(e, (SpeechDelta, ToolCalls)) for e in events)
    end = events[-1]
    assert end.finish_reason == "length"
    assert end.assistant_message["content"] == ""


async def test_think_cap_hides_the_second_read_timings(tmp_path):
    """Recorded with reasoning_budget_tokens=16: the server inserted its cap
    line, re-read the prompt, and reported that read (cache_n 412, prompt_n 0
    for a 396-token prompt)."""
    recorder = TelemetryRecorder(tmp_path)
    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(return_value=_ok(_sse("length_in_tool_call")))
        events = await _collect(_adapter(recorder=recorder))

    think = "".join(e.text for e in events if isinstance(e, ThinkDelta))
    assert CAP_LINE in think
    end = events[-1]
    assert end.think_capped is True
    assert end.usage.prompt_tokens == 396
    assert end.usage.cache_n is None
    assert end.usage.prompt_n is None
    assert end.usage.prompt_ms is None
    assert end.usage.predicted_per_second is not None
    [rec] = recorder.read_today()
    assert rec.think_capped is True
    assert rec.cache_n is None and rec.prompt_n is None and rec.prompt_ms is None


async def test_think_cap_with_timings_that_add_up_is_not_flagged():
    """think_capped follows the timings check (cache_n + prompt_n vs
    prompt_tokens). In this recording the cap line was inserted, yet the
    timings add up (47 + 5 == 52), so it is not flagged and they are kept."""
    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(return_value=_ok(_sse("think_cap_timings_add_up")))
        events = await _collect(_adapter(), tools=[])

    think = "".join(e.text for e in events if isinstance(e, ThinkDelta))
    assert CAP_LINE in think
    end = events[-1]
    assert end.finish_reason == "stop"
    assert end.think_capped is False
    assert (end.usage.cache_n, end.usage.prompt_n, end.usage.prompt_tokens) == (47, 5, 52)
    assert any(isinstance(e, SpeechDelta) for e in events)


async def test_content_after_tool_fragments_is_still_speech():
    lines = _lines("think_speech_two_tools")
    last_tool = max(i for i, ln in enumerate(lines) if _is_tool_chunk(ln))
    late = {"choices": [{"index": 0, "delta": {"content": " Late words."}}]}
    lines.insert(last_tool + 1, b"data: " + json.dumps(late).encode())

    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(return_value=_ok(_join(lines)))
        events = await _collect(_adapter())

    speech = [e.text for e in events if isinstance(e, SpeechDelta)]
    assert speech[-1] == " Late words."
    assert [type(e).__name__ for e in events[-2:]] == ["ToolCalls", "End"]
    assert len(events[-2].calls) == 2
    assert events[-1].assistant_message["content"].endswith(" Late words.")


async def test_usage_only_chunk_without_choices_is_accepted():
    lines = _lines("think_speech_two_tools")
    final = _data(lines[-2])
    usage = {"choices": [], "usage": final.pop("usage"), "timings": final.pop("timings")}
    lines[-2:-1] = [b"data: " + json.dumps(final).encode(), b"data: " + json.dumps(usage).encode()]

    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(return_value=_ok(_join(lines)))
        events = await _collect(_adapter())

    assert events[-1].finish_reason == "tool_calls"
    assert events[-1].usage.prompt_tokens is not None


# --- errors -------------------------------------------------------------------


async def test_400_before_stream_is_a_server_error_with_the_server_text():
    recorded = json.loads((FIXTURES / "error_400.response.json").read_text())
    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(
            return_value=httpx.Response(recorded["status"], json=recorded["body"])
        )
        with pytest.raises(ModelError) as ei:
            await _collect(_adapter())
    assert ei.value.kind == "server"
    assert recorded["body"]["error"]["message"] in str(ei.value)


async def test_503_before_stream_is_transport():
    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(
            return_value=httpx.Response(503, json={"error": {"message": "Loading model"}})
        )
        with pytest.raises(ModelError) as ei:
            await _collect(_adapter())
    assert ei.value.kind == "transport"
    assert "503" in str(ei.value)


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
async def test_unreachable_or_timeout_is_transport(exc):
    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(side_effect=exc)
        with pytest.raises(ModelError) as ei:
            await _collect(_adapter())
    assert ei.value.kind == "transport"


async def test_error_chunk_mid_stream_is_a_server_error():
    lines = _lines("think_speech_two_tools")[:4]
    err = {"error": {"code": 500, "message": "decode failed", "type": "server_error"}}
    lines.append(b"data: " + json.dumps(err).encode())
    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(return_value=_ok(_join(lines)))
        with pytest.raises(ModelError) as ei:
            await _collect(_adapter())
    assert ei.value.kind == "server"
    assert "decode failed" in str(ei.value)


@pytest.mark.parametrize("keep", ["mid_reply", "after_finish"])
async def test_stream_cut_before_done_is_transport(keep):
    lines = _lines("think_speech_two_tools")
    assert lines[-1] == b"data: [DONE]"
    cut = lines[:6] if keep == "mid_reply" else lines[:-1]
    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(return_value=_ok(_join(cut)))
        with pytest.raises(ModelError) as ei:
            await _collect(_adapter())
    assert ei.value.kind == "transport"


async def test_tool_fragments_with_finish_stop_is_a_server_error():
    lines = _lines("think_speech_two_tools")
    final = _data(lines[-2])
    assert final["choices"][0]["finish_reason"] == "tool_calls"
    final["choices"][0]["finish_reason"] = "stop"
    lines[-2] = b"data: " + json.dumps(final).encode()
    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(return_value=_ok(_join(lines)))
        with pytest.raises(ModelError) as ei:
            await _collect(_adapter())
    assert ei.value.kind == "server"


async def test_errors_are_recorded(tmp_path):
    recorder = TelemetryRecorder(tmp_path)
    usage: list = []
    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(return_value=httpx.Response(503, text="busy"))
        with pytest.raises(ModelError):
            await _collect(_adapter(recorder=recorder),
                           on_usage=lambda p, c: usage.append((p, c)))
    [rec] = recorder.read_today()
    assert rec.error.startswith("transport")
    assert usage == [(None, None)]


# --- request ------------------------------------------------------------------


async def test_request_body():
    seen: list[httpx.Request] = []

    def capture(request):
        seen.append(request)
        return _ok(_sse("think_cap_timings_add_up"))

    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(side_effect=capture)
        await _collect(_adapter(), max_tokens=1024, think_budget=512)
        await _collect(_adapter(), tools=[])
        await _collect(_adapter(api_key="sk-1"))

    body = json.loads(seen[0].content)
    assert body["model"] == MODEL
    assert body["messages"] == MESSAGES
    assert body["tools"] == TOOLS
    assert body["stream"] is True
    assert body["max_tokens"] == 1536
    assert body["reasoning_budget_tokens"] == 512
    assert body["presence_penalty"] == 1.3
    for absent in ("grammar", "response_format", "tool_choice", "reasoning_effort"):
        assert absent not in body
    assert "authorization" not in seen[0].headers
    assert "tools" not in json.loads(seen[1].content)
    assert seen[2].headers["authorization"] == "Bearer sk-1"


# --- accounting ---------------------------------------------------------------


async def test_on_usage_gets_new_prompt_tokens_and_record_has_conversation(tmp_path):
    recorder = TelemetryRecorder(tmp_path)
    final = _data(_lines("think_speech_two_tools")[-2])
    prompt, completion = final["usage"]["prompt_tokens"], final["usage"]["completion_tokens"]
    got: list = []
    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(
            side_effect=lambda r: _ok(_sse("think_speech_two_tools"))
        )
        adapter = _adapter(recorder=recorder)
        await _collect(adapter, on_usage=lambda p, c: got.append((p, c)),
                       conversation=("private", 3), prior_tokens=100, purpose="mind")
        await _collect(adapter, on_usage=lambda p, c: got.append((p, c)),
                       prior_tokens=prompt + 500)
        await _collect(adapter, on_usage=lambda p, c: got.append((p, c)))

    assert got == [(prompt - 100, completion), (0, completion), (prompt, completion)]
    r1, r2, r3 = recorder.read_today()
    assert (r1.conversation, r1.epoch, r1.new_prompt_tokens) == ("private", 3, prompt - 100)
    assert r1.call_purpose == "mind"
    assert r1.model == MODEL
    assert r1.prompt_tokens == prompt and r1.completion_tokens == completion
    assert r1.finish_reason == "tool_calls"
    assert r1.cache_n is not None and r1.prompt_ms is not None
    assert r1.predicted_per_second is not None
    assert r1.latency_ttft_ms is not None and r1.first_content_ms >= r1.latency_ttft_ms
    assert r1.think_capped is False
    assert r1.error is None
    assert r2.new_prompt_tokens == 0
    assert (r3.conversation, r3.epoch, r3.new_prompt_tokens) == (None, None, prompt)


def test_old_llm_call_lines_still_load():
    old = {
        "ts": 1.0, "model": "Qwen3.6", "prompt_tokens": 10, "completion_tokens": 2,
        "latency_ttft_ms": 5.0, "latency_total_ms": 9.0, "error": None,
        "context_pct": 0.1, "call_purpose": "cascade",
    }
    rec = LLMCallRecord.from_dict(old)
    assert rec.prompt_tokens == 10
    assert rec.new_prompt_tokens is None and rec.conversation is None
    assert rec.think_capped is None and rec.finish_reason is None


# --- closing and cancellation -------------------------------------------------


async def test_closing_the_iterator_early_closes_the_connection_and_still_accounts(tmp_path):
    recorder = TelemetryRecorder(tmp_path)
    lines = _lines("think_speech_two_tools")
    stream = _GatedStream(head=_join(lines[:4]), tail=_join(lines[4:]))
    got: list = []
    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(return_value=_streamed(stream))
        it = _adapter(recorder=recorder).stream(
            messages=MESSAGES, tools=TOOLS, max_tokens=200, think_budget=64,
            purpose="test", on_usage=lambda p, c: got.append((p, c)),
        )
        first = await anext(it)
        assert isinstance(first, ThinkDelta)
        await it.aclose()

    assert stream.closed
    assert got == [(None, None)]
    [rec] = recorder.read_today()
    assert rec.finish_reason is None
    assert rec.error is None  # an interrupted call is not a model failure


async def test_cancel_mid_stream_releases_the_slot_and_closes_the_connection(tmp_path):
    recorder = TelemetryRecorder(tmp_path)
    lines = _lines("think_speech_two_tools")
    hung = _GatedStream(head=_join(lines[:4]))
    responses = iter([
        _streamed(hung),
        _ok(_sse("think_speech_two_tools")),
    ])
    adapter = _adapter(recorder=recorder, max_concurrency=1)
    started = asyncio.Event()

    async def consume():
        async for _ in adapter.stream(
            messages=MESSAGES, tools=TOOLS, max_tokens=200, think_budget=64,
            purpose="consolidation", on_usage=None,
        ):
            started.set()

    with respx.mock(base_url=BASE) as m:
        m.post("/v1/chat/completions").mock(side_effect=lambda r: next(responses))
        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert hung.closed
        events = await asyncio.wait_for(_collect(adapter), timeout=2)

    assert isinstance(events[-1], End)
    cancelled, done = recorder.read_today()
    assert cancelled.call_purpose == "consolidation" and cancelled.finish_reason is None
    assert done.finish_reason == "tool_calls"


async def test_cancel_while_waiting_for_the_slot_does_not_leak_it():
    lines = _lines("think_speech_two_tools")
    holders = [_GatedStream(head=_join(lines[:4]), tail=_join(lines[4:])) for _ in range(3)]
    responses = iter(_streamed(s) for s in holders)
    adapter = _adapter(max_concurrency=1)

    with respx.mock(base_url=BASE) as m:
        route = m.post("/v1/chat/completions").mock(side_effect=lambda r: next(responses))
        first = asyncio.create_task(_collect(adapter))
        waiter = asyncio.create_task(_collect(adapter))
        await asyncio.sleep(0.05)
        assert route.call_count == 1  # waiter is queued on the slot
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        holders[0].gate.set()
        await asyncio.wait_for(first, timeout=2)

        # Still exactly one slot: of two new calls only one reaches the server.
        a = asyncio.create_task(_collect(adapter))
        b = asyncio.create_task(_collect(adapter))
        await asyncio.sleep(0.05)
        assert route.call_count == 2
        holders[1].gate.set()
        holders[2].gate.set()
        await asyncio.wait_for(asyncio.gather(a, b), timeout=2)
        assert route.call_count == 3


# --- boot check ---------------------------------------------------------------

HEALTH = {"status": "ok", "model": MODEL, "service": "strata"}
MODELS = {"object": "list", "data": [{"id": MODEL, "object": "model"}]}


async def test_check_passes_when_healthy_and_model_listed():
    with respx.mock(base_url=BASE, assert_all_called=False) as m:
        m.get("/health").mock(return_value=httpx.Response(200, json=HEALTH))
        m.get("/v1/models").mock(return_value=httpx.Response(200, json=MODELS))
        chat = m.post("/v1/chat/completions")
        await _adapter().check()
    assert not chat.called


async def test_check_rejects_a_model_the_server_does_not_have():
    with respx.mock(base_url=BASE) as m:
        m.get("/health").mock(return_value=httpx.Response(200, json=HEALTH))
        m.get("/v1/models").mock(return_value=httpx.Response(200, json=MODELS))
        with pytest.raises(RuntimeError) as ei:
            await ChatCompletionsAdapter(base_url=BASE, model="qwen3.6").check()
    msg = str(ei.value)
    assert BASE in msg and "qwen3.6" in msg and MODEL in msg


async def test_check_rejects_health_that_is_not_ok():
    with respx.mock(base_url=BASE, assert_all_called=False) as m:
        m.get("/health").mock(
            return_value=httpx.Response(503, json={"status": "loading model"})
        )
        m.get("/v1/models").mock(return_value=httpx.Response(200, json=MODELS))
        with pytest.raises(RuntimeError) as ei:
            await _adapter().check()
    assert "loading model" in str(ei.value) and BASE in str(ei.value)


async def test_check_reports_an_unreachable_server():
    with respx.mock(base_url=BASE) as m:
        m.get("/health").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(RuntimeError) as ei:
            await _adapter().check()
    assert BASE in str(ei.value) and MODEL in str(ei.value)
