"""Integration tests for DollOS kernel — MindLoop-based architecture."""

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path

import pytest

from dollos.config import (
    CharacterConfig,
    DataConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemsearchConfig,
    Settings,
)
from dollos.ipc.messages import TextInput
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
    )


# ----- Basic wiring tests -----


def test_kernel_has_mind_loop(tmp_path: Path) -> None:
    """DollOS constructs a MindLoop."""
    from dollos.mind.mind_loop import MindLoop

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert isinstance(dollos._mind_loop, MindLoop)


def test_kernel_has_mind_state(tmp_path: Path) -> None:
    """DollOS loads a MindState."""
    from dollos.mind.mind_state import MindState

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert isinstance(dollos._mind_state, MindState)


def test_kernel_has_perception_queue(tmp_path: Path) -> None:
    """DollOS creates a PerceptionQueue."""
    from dollos.mind.perception_queue import PerceptionQueue

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert isinstance(dollos._perception_queue, PerceptionQueue)


def test_kernel_has_sink_resolver(tmp_path: Path) -> None:
    """DollOS creates a SinkResolver."""
    from dollos.mind.sink_resolver import SinkResolver

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert isinstance(dollos._sink_resolver, SinkResolver)


def test_kernel_has_mind_ctx(tmp_path: Path) -> None:
    """DollOS creates a MindCtx wired to MindState."""
    from dollos.mind.mind_ctx import MindCtx

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert isinstance(dollos._mind_ctx, MindCtx)
    assert dollos._mind_ctx.mind_state is dollos._mind_state


def test_kernel_mind_loop_uses_same_state(tmp_path: Path) -> None:
    """MindLoop shares the same MindState as the kernel."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert dollos._mind_loop._state is dollos._mind_state


def test_kernel_mind_loop_uses_same_queue(tmp_path: Path) -> None:
    """MindLoop shares the same PerceptionQueue as the kernel."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert dollos._mind_loop._queue is dollos._perception_queue


def test_kernel_has_tool_output_store(tmp_path: Path) -> None:
    from dollos.tool_outputs import ToolOutputStore

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert isinstance(dollos._tool_output_store, ToolOutputStore)
    assert dollos._tool_output_dir.exists()
    dollos._tool_output_store.cleanup()
    assert not dollos._tool_output_dir.exists()


# ----- Runner wiring tests -----


@pytest.mark.asyncio
async def test_kernel_creates_shell_runner(tmp_path: Path):
    """DollOS exposes a ShellRunner wired to the PerceptionQueue."""
    from dollos.shell_runner import ShellRunner

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert isinstance(dollos.shell_runner, ShellRunner)
    # Shell runner is wired to the perception queue
    assert dollos.shell_runner._perception_queue is dollos._perception_queue
    # MindCtx references the same shell_runner
    assert dollos._mind_ctx.shell_runner is dollos.shell_runner


@pytest.mark.asyncio
async def test_kernel_creates_monitor_runner(tmp_path: Path):
    """DollOS exposes a MonitorRunner wired to the PerceptionQueue."""
    from dollos.monitor_runner import MonitorRunner

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert isinstance(dollos.monitor_runner, MonitorRunner)
    assert dollos.monitor_runner._perception_queue is dollos._perception_queue
    assert dollos._mind_ctx.monitor_runner is dollos.monitor_runner


@pytest.mark.asyncio
async def test_kernel_subagent_runner_wired(tmp_path: Path):
    """SubagentRunner is wired to PerceptionQueue and MindCtx."""
    from dollos.subagent import SubagentRunner

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert isinstance(dollos.subagent_runner, SubagentRunner)
    assert dollos.subagent_runner._perception_queue is dollos._perception_queue
    assert dollos._mind_ctx.subagent_runner is dollos.subagent_runner


# ----- IPC wiring tests -----


@pytest.mark.asyncio
async def test_handle_message_text_input_enqueues_perception(tmp_path: Path):
    """TextInput → Perception(kind='UserSpoke') pushed to PerceptionQueue."""
    from dollos.mind.mind_state import Perception

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    sink: asyncio.Queue = asyncio.Queue()

    await dollos._handle_message(TextInput(text="hello"), sink)

    # Drain the queue
    perceptions = await dollos._perception_queue.drain(timeout_s=0.1)
    assert any(p.kind == "UserSpoke" and p.data["text"] == "hello" for p in perceptions)


@pytest.mark.asyncio
async def test_handle_connect_registers_sink_with_resolver(tmp_path: Path):
    """On WS connect, sink is registered with SinkResolver."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    # Suppress bootstrap
    dollos._bootstrapped_dates.add(_date.today())

    sink: asyncio.Queue = asyncio.Queue()
    await dollos._handle_connect(sink)
    # Sink is now in the resolver stack
    assert dollos._sink_resolver() is sink


@pytest.mark.asyncio
async def test_handle_disconnect_unregisters_sink(tmp_path: Path):
    """On WS disconnect, sink is unregistered from SinkResolver."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    dollos._bootstrapped_dates.add(_date.today())

    sink: asyncio.Queue = asyncio.Queue()
    await dollos._handle_connect(sink)
    # Sanity: registered
    assert dollos._sink_resolver() is sink
    await dollos._handle_disconnect(sink)
    # After disconnect, resolver returns DummySink (stack empty)
    from dollos.mind.sink_resolver import DummySink
    assert isinstance(dollos._sink_resolver(), DummySink)


# ----- Bootstrap perception tests -----


@pytest.mark.asyncio
async def test_kernel_bootstrap_pushes_perception_on_first_connect_if_no_schedule(tmp_path):
    """On first connect with no schedule.toml, a ScheduledMoment bootstrap
    perception is pushed to the queue."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)

    sink: asyncio.Queue = asyncio.Queue()
    await dollos._handle_connect(sink)

    perceptions = await dollos._perception_queue.drain(timeout_s=0.1)
    assert any(p.kind == "ScheduledMoment" for p in perceptions)


@pytest.mark.asyncio
async def test_kernel_bootstrap_skips_when_schedule_exists(tmp_path):
    """If a schedule.toml exists for today, no bootstrap perception is pushed."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    today_str = f"{_date.today():%Y-%m-%d}"
    sched_path = tmp_path / "data" / "memory" / "schedule" / f"{today_str}.toml"
    sched_path.parent.mkdir(parents=True, exist_ok=True)
    sched_path.write_text('[[entry]]\ntime = "07:00:00"\nintent = "x"\n')

    sink: asyncio.Queue = asyncio.Queue()
    await dollos._handle_connect(sink)

    perceptions = await dollos._perception_queue.drain(timeout_s=0.1)
    assert not any(p.kind == "ScheduledMoment" for p in perceptions)


# ----- Scheduler tests -----


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


@pytest.mark.asyncio
async def test_kernel_scheduler_pushes_perception_for_due_entry(tmp_path, monkeypatch):
    """The schedule runner pushes a ScheduledMoment perception for due entries."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    today_str = f"{_date.today():%Y-%m-%d}"
    sched_path = tmp_path / "data" / "memory" / "schedule" / f"{today_str}.toml"
    sched_path.parent.mkdir(parents=True, exist_ok=True)
    sched_path.write_text('[[entry]]\ntime = "07:30:00"\nintent = "morning"\n')

    from dollos.schedule import ScheduleEntry
    from datetime import time as _time
    import dollos.kernel as kernel_mod

    poll_count = {"n": 0}

    def _fake_due_entries(schedule, now, fired):
        poll_count["n"] += 1
        if poll_count["n"] == 1:
            return [ScheduleEntry(time=_time(7, 30), intent="morning")]
        return []

    monkeypatch.setattr(kernel_mod, "due_entries", _fake_due_entries)

    real_wait_for = asyncio.wait_for
    tick_count = {"n": 0}

    async def _fast_wait_for(awaitable, timeout):
        tick_count["n"] += 1
        if tick_count["n"] == 1:
            awaitable.close()
            raise TimeoutError
        dollos._shutdown.set()
        return await real_wait_for(awaitable, timeout=0.5)

    monkeypatch.setattr(kernel_mod.asyncio, "wait_for", _fast_wait_for)

    await real_wait_for(dollos._schedule_runner(), timeout=2.0)
    perceptions = await dollos._perception_queue.drain(timeout_s=0.1)
    assert any(
        p.kind == "ScheduledMoment" and p.data.get("intent") == "morning"
        for p in perceptions
    )


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
