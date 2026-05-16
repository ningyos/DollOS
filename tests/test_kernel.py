"""Integration tests for DollOS._handle_message via EventDispatcher."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

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
from dollos.ipc.messages import ErrorMsg, TextChunk, TextInput, TurnEnd
from dollos.kernel import DollOS
from dollos.llm.adapter import LLMAdapter, StreamChunk


def _make_settings(tmp_path: Path) -> Settings:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "doll.toml").write_text(
        '[meta]\n'
        'id = "doll"\n'
        'name = "Doll"\n'
        '\n'
        '[identity]\n'
        'self = "You are Doll."\n'
        'personality = "- chill"\n'
        'taboos = "- no LARP"\n'
    )
    return Settings(
        llm=LLMConfig(
            provider="llamacpp",
            template="qwen3-thinking",
            base_url="http://test.local:8001",
            model_alias="big",
        ),
        ipc=IPCConfig(host="127.0.0.1", port=0),
        log=LogConfig(level="WARNING"),
        data=DataConfig(root=tmp_path / "data"),
        memsearch=MemsearchConfig(top_k=7),
        character=CharacterConfig(pack=pack_dir),
        inner_voice=InnerVoiceConfig(
            base_url="http://test.local:8003",
            timeout_s=15.0,
        ),
    )


@dataclass
class _FakeAdapter(LLMAdapter):
    chunks: list[StreamChunk] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    async def stream_completion(
        self,
        *,
        system: str,
        user: str,
        prefill: str = "",
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        tools: list[type] | None = None,
        grammar: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append({"system": system, "user": user, "prefill": prefill})
        for c in self.chunks:
            yield c

    async def stream_messages(
        self,
        *,
        system: str,
        messages: list[dict],
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        tools: list[type] | None = None,
        grammar: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append({"system": system, "messages": list(messages)})
        for c in self.chunks:
            yield c


class _FakeMemSearch:
    def __init__(self) -> None:
        self.indexed: list = []

    async def index_file(self, path):
        self.indexed.append(path)


def _install_fake_inner_voice(monkeypatch, recall_text: str | None = None, raises=None):
    captured: list[str] = []

    async def _stub_recall(self, query, **kwargs):
        captured.append(query)
        if raises is not None:
            raise raises
        return recall_text if recall_text is not None else "- foo"

    async def _stub_instinct_process(self, event):
        return ""

    async def _stub_compact_cascade(self, *, perception, cascade_messages):
        return "test summary"

    monkeypatch.setattr("dollos.inner_voice.InnerVoice.recall", _stub_recall)
    monkeypatch.setattr("dollos.instinct.SmallModelInstinct.process", _stub_instinct_process)
    monkeypatch.setattr(
        "dollos.instinct.SmallModelInstinct.compact_cascade", _stub_compact_cascade
    )
    return captured


@pytest.fixture
def dollos_with_fakes(tmp_path, monkeypatch):
    """Build a DollOS with fake adapter swapped in. Returns (dollos, adapter,
    iv_calls)."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)

    fake_adapter = _FakeAdapter()
    dollos.adapter = fake_adapter
    # Re-build dispatcher to point at the fake adapter.
    from dollos.conversation_history import ConversationHistory
    from dollos.dispatcher import EventDispatcher
    from dollos.scratchpad import Scratchpad

    dollos.dispatcher = EventDispatcher(
        adapter=fake_adapter,
        inner_voice=dollos.inner_voice,
        instinct=dollos.instinct,
        renderer=dollos.renderer,
        identity=dollos._doll_pack.identity,
        memory_root=tmp_path,
        memsearch=_FakeMemSearch(),
        transcripts_root=tmp_path / "transcripts",
        cascade_logger=dollos._cascade_logger,
        tool_output_store=dollos._tool_output_store,
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
    )
    return dollos, fake_adapter


async def _drain_until_separator(sink: asyncio.Queue) -> list:
    """Drain a sink until the first None separator (turn boundary)."""
    out = []
    while True:
        item = await sink.get()
        if item is None:
            return out
        out.append(item)


@pytest.mark.asyncio
async def test_handle_message_text_input_yields_chunks_then_turnend(
    dollos_with_fakes, monkeypatch
):
    dollos, adapter = dollos_with_fakes
    adapter.chunks = [
        StreamChunk(
            text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
            done=False,
        ),
        StreamChunk(text="", done=True),
    ]
    _install_fake_inner_voice(monkeypatch, "- foo")

    sink: asyncio.Queue = asyncio.Queue()
    result = await dollos._handle_message(TextInput(text="hi"), sink)
    assert result is None  # void return; output flows via sink
    items = await asyncio.wait_for(_drain_until_separator(sink), timeout=2.0)
    assert any(isinstance(m, TextChunk) and m.text == "ok" for m in items)
    assert isinstance(items[-1], TurnEnd)


@pytest.mark.asyncio
async def test_handle_message_text_input_yields_errormsg_on_dispatch_failure(
    dollos_with_fakes, monkeypatch
):
    dollos, _adapter = dollos_with_fakes
    _install_fake_inner_voice(monkeypatch, raises=RuntimeError("boom"))

    sink: asyncio.Queue = asyncio.Queue()
    await dollos._handle_message(TextInput(text="x"), sink)
    items = await asyncio.wait_for(_drain_until_separator(sink), timeout=2.0)
    assert len(items) == 1
    assert isinstance(items[0], ErrorMsg)
    assert "boom" in items[0].message


@pytest.mark.asyncio
async def test_dispatch_user_text_uses_stream_messages(
    dollos_with_fakes, monkeypatch
):
    """Cascade uses multi-message API (2026-05-08); legacy stream_completion
    is reserved for small-model callers."""
    dollos, adapter = dollos_with_fakes
    adapter.chunks = [StreamChunk(text="", done=True)]
    _install_fake_inner_voice(monkeypatch, "- foo")

    sink: asyncio.Queue = asyncio.Queue()
    await dollos._handle_message(TextInput(text="hi"), sink)
    await asyncio.wait_for(_drain_until_separator(sink), timeout=2.0)
    assert len(adapter.calls) == 1
    assert "messages" in adapter.calls[0]
    assert "prefill" not in adapter.calls[0]


@pytest.mark.asyncio
async def test_dispatch_user_text_uses_text_as_user_role(
    dollos_with_fakes, monkeypatch
):
    dollos, adapter = dollos_with_fakes
    adapter.chunks = [StreamChunk(text="", done=True)]
    _install_fake_inner_voice(monkeypatch)

    sink: asyncio.Queue = asyncio.Queue()
    await dollos._handle_message(TextInput(text="hello world"), sink)
    await asyncio.wait_for(_drain_until_separator(sink), timeout=2.0)
    user = adapter.calls[0]["messages"][0]["content"]
    assert "[Memory context]" in user
    assert "[Message]" in user
    assert "hello world" in user


@pytest.mark.asyncio
async def test_drain_diary_sink_consumes_until_sentinel(tmp_path):
    """_drain_diary_sink eats messages and returns on None sentinel."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    sink: asyncio.Queue = asyncio.Queue()
    sink.put_nowait(TextChunk(text="ignored"))
    sink.put_nowait(ErrorMsg(message="logged"))
    sink.put_nowait(TurnEnd())
    sink.put_nowait(None)
    await asyncio.wait_for(dollos._drain_diary_sink(sink), timeout=1.0)


@pytest.mark.asyncio
async def test_diary_scheduler_returns_on_shutdown(tmp_path):
    """Scheduler returns when shutdown is set, even if next fire is far away."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)

    async def _quickshutdown():
        await asyncio.sleep(0.05)
        dollos._shutdown.set()

    asyncio.create_task(_quickshutdown())
    await asyncio.wait_for(dollos._diary_scheduler(), timeout=2.0)


# ----- Phase 1 schedule: bootstrap + sink lifecycle -----


@pytest.mark.asyncio
async def test_kernel_bootstrap_fires_daily_plan_on_first_connect_if_no_schedule(tmp_path):
    """gap #5: DailyPlanEvent fires when first connection sees no
    schedule.toml for today."""
    from dollos.events import DailyPlanEvent

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    dispatched: list = []
    dollos.dispatcher.dispatch = lambda raw: dispatched.append(raw)

    sink: asyncio.Queue = asyncio.Queue()
    await dollos._handle_connect(sink)
    plan_events = [d for d in dispatched if isinstance(d, DailyPlanEvent)]
    assert plan_events
    # Bootstrap is daemon-internal: the dispatched event must NOT carry the
    # active connection's sink. Its Say tool output goes to a throwaway queue.
    assert plan_events[0].response_sink is not dollos._active_sink
    assert plan_events[0].response_sink is not sink


@pytest.mark.asyncio
async def test_kernel_bootstrap_uses_dummy_sink_not_active(tmp_path):
    """Bootstrap dispatches with a fresh dummy sink, never the active sink.

    Architectural guarantee: daemon-internal planning (bootstrap) must not
    emit Say output to the connected client; the day's first user-facing
    greeting comes from a scheduled entry instead.
    """
    from dollos.events import DailyPlanEvent

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    dispatched: list = []
    dollos.dispatcher.dispatch = lambda raw: dispatched.append(raw)

    sink: asyncio.Queue = asyncio.Queue()
    await dollos._handle_connect(sink)

    plan_events = [d for d in dispatched if isinstance(d, DailyPlanEvent)]
    assert len(plan_events) == 1
    bootstrap_sink = plan_events[0].response_sink
    assert bootstrap_sink is not sink
    assert bootstrap_sink is not dollos._active_sink
    assert isinstance(bootstrap_sink, asyncio.Queue)


@pytest.mark.asyncio
async def test_kernel_bootstrap_skips_daily_plan_when_schedule_exists(tmp_path):
    """If a schedule.toml already exists for today, no DailyPlanEvent is fired."""
    from datetime import date as _date

    from dollos.events import DailyPlanEvent

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    today_str = f"{_date.today():%Y-%m-%d}"
    sched_path = tmp_path / "data" / "memory" / "schedule" / f"{today_str}.toml"
    sched_path.parent.mkdir(parents=True, exist_ok=True)
    sched_path.write_text('[[entry]]\ntime = "07:00:00"\nintent = "x"\n')

    dispatched: list = []
    dollos.dispatcher.dispatch = lambda raw: dispatched.append(raw)

    sink: asyncio.Queue = asyncio.Queue()
    await dollos._handle_connect(sink)
    assert not any(isinstance(d, DailyPlanEvent) for d in dispatched)


@pytest.mark.asyncio
async def test_kernel_active_sink_set_on_connect_cleared_on_disconnect(tmp_path):
    """gap #1: _active_sink lifecycle tracks the live connection."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    # Pretend we already bootstrapped so connect doesn't dispatch.
    from datetime import date as _date
    dollos._bootstrapped_dates.add(_date.today())

    assert dollos._active_sink is None
    sink: asyncio.Queue = asyncio.Queue()
    await dollos._handle_connect(sink)
    assert dollos._active_sink is sink
    await dollos._handle_disconnect(sink)
    assert dollos._active_sink is None


@pytest.mark.asyncio
async def test_kernel_scheduler_fires_due_event(tmp_path, monkeypatch):
    """The scheduler runner reads schedule.toml, calls due_entries, and
    dispatches a ScheduledEvent for each entry whose time arrived."""
    from datetime import date as _date
    from datetime import time as _time

    from dollos.events import ScheduledEvent

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    today_str = f"{_date.today():%Y-%m-%d}"
    sched_path = tmp_path / "data" / "memory" / "schedule" / f"{today_str}.toml"
    sched_path.parent.mkdir(parents=True, exist_ok=True)
    sched_path.write_text(
        '[[entry]]\ntime = "07:30:00"\nintent = "morning"\n'
    )

    dispatched: list = []
    dollos.dispatcher.dispatch = lambda raw: dispatched.append(raw)

    # Patch due_entries to yield the entry on the first poll, then nothing.
    from dollos.schedule import ScheduleEntry
    import dollos.kernel as kernel_mod

    poll_count = {"n": 0}

    def _fake_due_entries(schedule, now, fired):
        poll_count["n"] += 1
        if poll_count["n"] == 1:
            return [ScheduleEntry(time=_time(7, 30), intent="morning")]
        return []

    monkeypatch.setattr(kernel_mod, "due_entries", _fake_due_entries)

    # Short-circuit the 30s sleep inside the runner so it polls quickly.
    # We replace the bound asyncio module reference inside dollos.kernel
    # via a tiny stub that only handles the runner's two call sites
    # (shutdown.wait + a sentinel awaitable).
    real_wait_for = asyncio.wait_for
    tick_count = {"n": 0}

    async def _fast_wait_for(awaitable, timeout):
        tick_count["n"] += 1
        # Always close the awaitable so it doesn't leak.
        if tick_count["n"] == 1:
            awaitable.close()
            raise TimeoutError
        # Second tick: signal shutdown then let the awaitable resolve.
        dollos._shutdown.set()
        return await real_wait_for(awaitable, timeout=0.5)

    # Patch on the kernel module's asyncio binding so only the runner sees it.
    monkeypatch.setattr(kernel_mod.asyncio, "wait_for", _fast_wait_for)

    await real_wait_for(dollos._schedule_runner(), timeout=2.0)
    assert any(
        isinstance(d, ScheduledEvent) and d.intent == "morning"
        for d in dispatched
    )


# ----- ToolOutputStore wiring -----


def test_kernel_has_tool_output_store(tmp_path: Path) -> None:
    from dollos.tool_outputs import ToolOutputStore

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert isinstance(dollos._tool_output_store, ToolOutputStore)
    assert dollos._tool_output_dir.exists()
    dollos._tool_output_store.cleanup()
    assert not dollos._tool_output_dir.exists()


# ----- Scratchpad wiring -----


def test_kernel_has_scratchpad(tmp_path: Path) -> None:
    from dollos.scratchpad import Scratchpad

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert isinstance(dollos._scratchpad, Scratchpad)
    assert dollos._scratchpad.read() == ""


# ----- ConversationHistory wiring -----


def test_kernel_has_conversation_history(tmp_path: Path) -> None:
    from dollos.conversation_history import ConversationHistory

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert isinstance(dollos._conversation_history, ConversationHistory)
    assert dollos._conversation_history.turn_count() == 0


def test_kernel_uses_configured_max_turns(tmp_path: Path) -> None:
    from dollos.config import ConversationHistoryConfig
    from dollos.conversation_history import ConversationHistory

    settings = _make_settings(tmp_path)
    settings = settings.model_copy(
        update={"conversation_history": ConversationHistoryConfig(max_turns=10)}
    )
    dollos = DollOS(settings)
    assert isinstance(dollos._conversation_history, ConversationHistory)
    assert dollos._conversation_history._max_turns == 10


def test_kernel_dispatcher_has_conversation_history(tmp_path: Path) -> None:
    from dollos.conversation_history import ConversationHistory

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert isinstance(dollos.dispatcher._conversation_history, ConversationHistory)
    assert dollos.dispatcher._conversation_history is dollos._conversation_history


# ----- ShellRunner wiring -----


@pytest.mark.asyncio
async def test_kernel_creates_shell_runner(tmp_path: Path):
    """DollOS exposes a ShellRunner on construction; the dispatcher and
    SubagentRunner share that same instance."""
    from dollos.shell_runner import ShellRunner

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert isinstance(dollos.shell_runner, ShellRunner)
    assert dollos.dispatcher._shell_runner is dollos.shell_runner
    assert dollos.subagent_runner._shell_runner is dollos.shell_runner


@pytest.mark.asyncio
async def test_kernel_shell_runner_dispatch_fn_wired(tmp_path: Path):
    """ShellRunner's dispatch_fn is set to dispatcher.dispatch after
    both are constructed."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    # Bound methods compare equal (==) but are not identical (is) on each access.
    assert dollos.shell_runner._dispatch_fn == dollos.dispatcher.dispatch


# ----- MonitorRunner wiring -----


@pytest.mark.asyncio
async def test_kernel_creates_monitor_runner(tmp_path: Path):
    """DollOS exposes a MonitorRunner on construction; the dispatcher and
    SubagentRunner share that same instance."""
    from dollos.monitor_runner import MonitorRunner

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert isinstance(dollos.monitor_runner, MonitorRunner)
    assert dollos.dispatcher._monitor_runner is dollos.monitor_runner
    assert dollos.subagent_runner._monitor_runner is dollos.monitor_runner


@pytest.mark.asyncio
async def test_kernel_monitor_runner_dispatch_fn_wired(tmp_path: Path):
    """MonitorRunner's dispatch_fn is set to dispatcher.dispatch after
    both are constructed."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    # Bound methods compare equal (==) but are not identical (is) on each access.
    assert dollos.monitor_runner._dispatch_fn == dollos.dispatcher.dispatch


# ----- Voice engine builder -----


@pytest.mark.asyncio
async def test_kernel_builds_voice_engines_from_pack(tmp_path: Path, monkeypatch):
    """When character pack has voice/engine.toml, kernel loads engines."""
    from dollos.voice import engines as eng_mod
    from dollos.voice.engines import ASREngine, TTSEngine

    # Fake engines registered for this test.
    class _FakeASR(ASREngine):
        def __init__(self, **kw): pass
        async def transcribe(self, audio_pcm, sample_rate): return ""
        async def aclose(self): pass

    class _FakeTTS(TTSEngine):
        sample_rate = 48000
        def __init__(self, **kw): pass
        async def synthesize(self, text):
            yield b""
        async def aclose(self): pass

    eng_mod.ASR_REGISTRY["fake-asr"] = _FakeASR
    eng_mod.TTS_REGISTRY["fake-tts"] = _FakeTTS

    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "doll.toml").write_text(
        '[meta]\nid="t"\nname="T"\n[identity]\nself="x"\npersonality="x"\ntaboos="x"\n'
    )
    voice_dir = pack_dir / "voice"
    voice_dir.mkdir()
    (voice_dir / "engine.toml").write_text(
        '[tts.fake-tts]\n'
    )

    from dollos.kernel import build_voice_engines
    asr, tts = build_voice_engines(
        pack_dir,
        data_root=tmp_path / "data",
        voice_asr={"engine": "fake-asr"},
        voice_tts={"engine": "fake-tts"},
    )
    assert isinstance(asr, _FakeASR)
    assert isinstance(tts, _FakeTTS)

    # Cleanup the test registrations.
    del eng_mod.ASR_REGISTRY["fake-asr"]
    del eng_mod.TTS_REGISTRY["fake-tts"]


@pytest.mark.asyncio
async def test_kernel_no_voice_when_dollos_config_absent(tmp_path: Path):
    from dollos.kernel import build_voice_engines

    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "doll.toml").write_text(
        '[meta]\nid="t"\nname="T"\n[identity]\nself="x"\npersonality="x"\ntaboos="x"\n'
    )

    out = build_voice_engines(
        pack_dir,
        data_root=tmp_path / "data",
        voice_asr=None,
        voice_tts=None,
    )
    assert out is None
