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
async def test_kernel_workflow_runner_wired(tmp_path: Path):
    """WorkflowRunner is wired to PerceptionQueue and MindCtx."""
    from dollos.workflow import WorkflowRunner

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert isinstance(dollos.workflow_runner, WorkflowRunner)
    assert dollos.workflow_runner._perception_queue is dollos._perception_queue
    assert dollos._mind_ctx.workflow_runner is dollos.workflow_runner


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


import pytest


@pytest.mark.asyncio
async def test_text_input_preempts_active_cascade():
    """Marker: kernel-level preempt covered by Task 7 E2E smoke."""
    pytest.skip("integration covered in Task 7 E2E smoke")


@pytest.mark.asyncio
async def test_kernel_replays_pending_wal_on_startup():
    """WAL replay flow — covered by Task 7 E2E smoke."""
    pytest.skip("integration covered in Task 7 smoke (scripts/smoke_crash_recovery.py)")


# ----- Regression: I5 — bad schedule file must NOT kill _schedule_runner -----


@pytest.mark.asyncio
async def test_schedule_runner_survives_malformed_schedule_file(tmp_path, monkeypatch, caplog):
    """A TOML-decode error (or any exception) from load_schedule must NOT kill
    _schedule_runner. The task should log a WARNING and continue its loop.

    Regression test for I5: ``load_schedule(path)`` previously ran outside the
    try/except, so any parse error permanently killed the task silently.
    """
    import logging
    import dollos.kernel as kernel_mod

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)

    today_str = f"{_date.today():%Y-%m-%d}"
    sched_path = tmp_path / "data" / "memory" / "schedule" / f"{today_str}.toml"
    sched_path.parent.mkdir(parents=True, exist_ok=True)
    # Write deliberately malformed TOML (will raise TOMLDecodeError).
    sched_path.write_bytes(b"[[entry]\ntime = not valid toml!!!!\n")

    real_wait_for = asyncio.wait_for
    tick_count = {"n": 0}

    async def _fast_wait_for(awaitable, timeout):
        tick_count["n"] += 1
        if tick_count["n"] <= 2:
            # Simulate the 30s timeout expiring so the loop body runs.
            awaitable.close()
            raise TimeoutError
        # On tick 3+, let shutdown through.
        dollos._shutdown.set()
        return await real_wait_for(awaitable, timeout=0.5)

    monkeypatch.setattr(kernel_mod.asyncio, "wait_for", _fast_wait_for)

    with caplog.at_level(logging.WARNING, logger="dollos.kernel"):
        # Must complete without raising — if I5 bug is present, _schedule_runner
        # would propagate the TOMLDecodeError and this await would re-raise it.
        await real_wait_for(dollos._schedule_runner(), timeout=2.0)

    # The task survived at least 2 ticks.
    assert tick_count["n"] >= 2, "schedule_runner did not iterate as expected"
    # A WARNING was logged mentioning the schedule error.
    warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("_schedule_runner" in m for m in warning_msgs), (
        f"expected a _schedule_runner WARNING in logs; got: {warning_msgs}"
    )


# ----- Shutdown sequence -----


@pytest.mark.asyncio
async def test_kernel_shutdown_awaits_workflow_runner_stop(tmp_path, monkeypatch):
    """DollOS.run() teardown awaits workflow_runner.stop() before the mind-loop exits.

    Guards against accidental removal of the stop() call from the finally block.
    """
    from dollos.wal.pidfile import RestartKind

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)

    stop_called = asyncio.Event()

    async def _spy_stop() -> None:
        stop_called.set()

    async def _noop() -> None:
        pass

    monkeypatch.setattr(dollos.workflow_runner, "stop", _spy_stop)
    monkeypatch.setattr(dollos.shell_runner, "stop", lambda: _noop())
    monkeypatch.setattr(dollos.monitor_runner, "stop", lambda: _noop())
    monkeypatch.setattr(dollos.system_pulse, "stop", lambda: _noop())
    monkeypatch.setattr(dollos.system_pulse, "start", lambda: None)
    monkeypatch.setattr(dollos.memsearch, "index", lambda: _noop())
    monkeypatch.setattr(dollos.memsearch, "close", lambda: None)
    monkeypatch.setattr(dollos.server, "start", lambda: _noop())
    monkeypatch.setattr(dollos.server, "stop", lambda: _noop())
    monkeypatch.setattr(dollos, "_replay_wal", lambda: _noop())
    monkeypatch.setattr(dollos._mind_loop, "run", lambda: _noop())
    monkeypatch.setattr(dollos._mind_loop, "shutdown", lambda: None)
    monkeypatch.setattr(dollos._reflection_observer, "run", lambda: _noop())
    monkeypatch.setattr(dollos._tool_output_store, "cleanup", lambda: None)
    monkeypatch.setattr(dollos._pidfile, "acquire", lambda: RestartKind.COLD)
    monkeypatch.setattr(dollos._pidfile, "release", lambda: None)
    # Suppress signal-handler registration (only valid on main thread in production).
    monkeypatch.setattr(asyncio.get_event_loop(), "add_signal_handler", lambda *a, **kw: None)

    dollos._shutdown.set()  # trigger immediate teardown
    await asyncio.wait_for(dollos.run(), timeout=3.0)

    assert stop_called.is_set(), (
        "workflow_runner.stop() was not awaited during kernel teardown"
    )


# ----- B2 Task 6: ConsolidationTrigger kernel wiring -----


def test_consolidation_trigger_wired_in_kernel(tmp_path: Path) -> None:
    """DollOS constructs and wires a ConsolidationTrigger sharing the same MindState."""
    from dollos.mind.consolidation import ConsolidationTrigger

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    assert isinstance(dollos._consolidation_trigger, ConsolidationTrigger)
    assert dollos._consolidation_trigger._state is dollos._mind_state


def test_cancel_consolidation_calls_trigger_cancel_current(tmp_path: Path) -> None:
    """_cancel_consolidation() delegates to trigger.cancel_current()."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)

    class _StubTrigger:
        def __init__(self) -> None:
            self.cancel_count = 0

        def cancel_current(self) -> None:
            self.cancel_count += 1

    stub = _StubTrigger()
    dollos._consolidation_trigger = stub
    dollos._cancel_consolidation()
    assert stub.cancel_count == 1


def test_cancel_consolidation_noop_when_no_attribute(tmp_path: Path) -> None:
    """_cancel_consolidation() does not raise when _consolidation_trigger is absent."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    # Simulate the attribute being absent (e.g. before full __init__)
    del dollos._consolidation_trigger
    dollos._cancel_consolidation()  # must not raise


@pytest.mark.asyncio
async def test_text_input_calls_cancel_consolidation(tmp_path: Path) -> None:
    """TextInput path calls _cancel_consolidation() when a UserSpoke is enqueued."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)

    cancelled: list[int] = []

    class _StubTrigger:
        def cancel_current(self) -> None:
            cancelled.append(1)

    dollos._consolidation_trigger = _StubTrigger()
    sink: asyncio.Queue = asyncio.Queue()
    await dollos._handle_message(TextInput(text="hello"), sink)

    assert len(cancelled) == 1, "_cancel_consolidation was not called on TextInput"


@pytest.mark.asyncio
async def test_shutdown_consolidation_before_memsearch_close(tmp_path, monkeypatch):
    """trigger.shutdown() must be called before memsearch.close() on teardown."""
    from dollos.wal.pidfile import RestartKind

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)

    order: list[str] = []

    # Spy on trigger.shutdown
    real_trigger_shutdown = dollos._consolidation_trigger.shutdown

    def _spy_trigger_shutdown() -> None:
        order.append("trigger_shutdown")
        real_trigger_shutdown()

    monkeypatch.setattr(dollos._consolidation_trigger, "shutdown", _spy_trigger_shutdown)

    # Spy on memsearch.close
    def _spy_memsearch_close() -> None:
        order.append("memsearch_close")

    monkeypatch.setattr(dollos.memsearch, "close", _spy_memsearch_close)

    async def _noop() -> None:
        pass

    monkeypatch.setattr(dollos.workflow_runner, "stop", lambda: _noop())
    monkeypatch.setattr(dollos.shell_runner, "stop", lambda: _noop())
    monkeypatch.setattr(dollos.monitor_runner, "stop", lambda: _noop())
    monkeypatch.setattr(dollos.system_pulse, "stop", lambda: _noop())
    monkeypatch.setattr(dollos.system_pulse, "start", lambda: None)
    monkeypatch.setattr(dollos.memsearch, "index", lambda: _noop())
    monkeypatch.setattr(dollos.server, "start", lambda: _noop())
    monkeypatch.setattr(dollos.server, "stop", lambda: _noop())
    monkeypatch.setattr(dollos, "_replay_wal", lambda: _noop())
    monkeypatch.setattr(dollos._mind_loop, "run", lambda: _noop())
    monkeypatch.setattr(dollos._mind_loop, "shutdown", lambda: None)
    monkeypatch.setattr(dollos._reflection_observer, "run", lambda: _noop())
    # Patch consolidation trigger run to a noop so it completes immediately
    monkeypatch.setattr(dollos._consolidation_trigger, "run", lambda: _noop())
    monkeypatch.setattr(dollos._tool_output_store, "cleanup", lambda: None)
    monkeypatch.setattr(dollos._pidfile, "acquire", lambda: RestartKind.COLD)
    monkeypatch.setattr(dollos._pidfile, "release", lambda: None)
    monkeypatch.setattr(asyncio.get_event_loop(), "add_signal_handler", lambda *a, **kw: None)

    dollos._shutdown.set()
    await asyncio.wait_for(dollos.run(), timeout=3.0)

    assert "trigger_shutdown" in order, "trigger.shutdown() was not called during teardown"
    assert "memsearch_close" in order, "memsearch.close() was not called during teardown"
    trigger_idx = order.index("trigger_shutdown")
    memsearch_idx = order.index("memsearch_close")
    assert trigger_idx < memsearch_idx, (
        f"trigger.shutdown() must precede memsearch.close(); got order={order}"
    )


# ----- B2 Batch 3: voice cancel + in-flight keeper gather regression tests -----


@pytest.mark.asyncio
async def test_voice_ingress_calls_cancel_consolidation(
    tmp_path: Path, monkeypatch
) -> None:
    """Voice ingress (_on_user_text closure in _handle_offer) calls _cancel_consolidation().

    M1 regression: if _cancel_consolidation() is dropped from the voice path,
    voice speech will no longer preempt an in-flight keeper agent.
    """
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)

    cancelled: list[int] = []

    class _StubTrigger:
        def cancel_current(self) -> None:
            cancelled.append(1)

    dollos._consolidation_trigger = _StubTrigger()

    # Capture the on_user_text callback by intercepting VoiceSession construction.
    captured_callbacks: list = []

    class _FakeSession:
        def __init__(self, asr, tts, on_user_text) -> None:
            captured_callbacks.append(on_user_text)

        async def handle_offer(self, sdp: str) -> str:
            return "fake_answer_sdp"

    # Stub build_voice_engines so _handle_offer doesn't try to load real ASR/TTS.
    monkeypatch.setattr(
        "dollos.kernel.build_voice_engines",
        lambda *a, **kw: (object(), object()),
    )
    monkeypatch.setattr("dollos.kernel.VoiceSession", _FakeSession)

    # Drive _handle_offer to wire the closure, then invoke it as ASR would.
    await dollos._handle_offer("fake_offer_sdp", asyncio.Queue())

    assert captured_callbacks, "VoiceSession was not constructed; on_user_text not captured"
    on_user_text = captured_callbacks[0]

    # Simulate ASR firing the callback (user spoke via voice).
    await on_user_text("hello from voice")

    assert len(cancelled) == 1, (
        "_cancel_consolidation was not called on voice ingress; "
        "voice path does not cancel in-flight keeper"
    )


@pytest.mark.asyncio
async def test_shutdown_gathers_inflight_keeper(tmp_path, monkeypatch) -> None:
    """shutdown must await the in-flight keeper task before calling memsearch.close().

    M2 regression: if the asyncio.gather(*_consolidation_tasks, ...) line is removed,
    memsearch.close() races against a still-running keeper agent that uses memsearch,
    which would crash on sqlite access.  This test creates a real in-flight asyncio.Task
    and asserts it is done by the time memsearch.close() is called.
    """
    from dollos.wal.pidfile import RestartKind

    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)

    # Create a real in-flight keeper task (long sleep so it's still running at shutdown).
    keeper_task: asyncio.Task = asyncio.create_task(asyncio.sleep(100))
    # Inject it as if the consolidation trigger is mid-keeper.
    dollos._consolidation_trigger.current_task = keeper_task

    # Spy: record keeper_task.done() at the moment memsearch.close() is called.
    # If gather is skipped, done() will be False (cancellation pending but not processed).
    keeper_done_at_close: list[bool] = []

    def _spy_memsearch_close() -> None:
        keeper_done_at_close.append(keeper_task.done())

    monkeypatch.setattr(dollos.memsearch, "close", _spy_memsearch_close)

    async def _noop() -> None:
        pass

    monkeypatch.setattr(dollos.workflow_runner, "stop", lambda: _noop())
    monkeypatch.setattr(dollos.shell_runner, "stop", lambda: _noop())
    monkeypatch.setattr(dollos.monitor_runner, "stop", lambda: _noop())
    monkeypatch.setattr(dollos.system_pulse, "stop", lambda: _noop())
    monkeypatch.setattr(dollos.system_pulse, "start", lambda: None)
    monkeypatch.setattr(dollos.memsearch, "index", lambda: _noop())
    monkeypatch.setattr(dollos.server, "start", lambda: _noop())
    monkeypatch.setattr(dollos.server, "stop", lambda: _noop())
    monkeypatch.setattr(dollos, "_replay_wal", lambda: _noop())
    monkeypatch.setattr(dollos._mind_loop, "run", lambda: _noop())
    monkeypatch.setattr(dollos._mind_loop, "shutdown", lambda: None)
    monkeypatch.setattr(dollos._reflection_observer, "run", lambda: _noop())
    # Patch consolidation trigger poll loop to a noop so _consolidation_trigger_task
    # completes immediately; our fake current_task is the one being tested.
    monkeypatch.setattr(dollos._consolidation_trigger, "run", lambda: _noop())
    monkeypatch.setattr(dollos._tool_output_store, "cleanup", lambda: None)
    monkeypatch.setattr(dollos._pidfile, "acquire", lambda: RestartKind.COLD)
    monkeypatch.setattr(dollos._pidfile, "release", lambda: None)
    monkeypatch.setattr(asyncio.get_event_loop(), "add_signal_handler", lambda *a, **kw: None)

    dollos._shutdown.set()
    await asyncio.wait_for(dollos.run(), timeout=3.0)

    # (a) Keeper task must be done (cancelled) after shutdown completes.
    assert keeper_task.done(), (
        "in-flight keeper task was not awaited during shutdown; "
        "removing asyncio.gather(*_consolidation_tasks) would trigger this"
    )

    # (b) memsearch.close() must have been called after keeper task was done.
    assert keeper_done_at_close == [True], (
        f"memsearch.close() called before keeper task was done; "
        f"keeper.done() at close time = {keeper_done_at_close}. "
        "This means the gather is missing or bypassed."
    )
