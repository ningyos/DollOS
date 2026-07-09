"""Kernel <-> PulseObserver wiring (system-pulse-proactive-trigger Task 6).

Unit-only: never starts a real event loop / never calls DollOS.start().
Mirrors tests/test_kernel_bridge_wiring.py / test_kernel_mcp_wiring.py style
(construct DollOS(settings) directly, assert on kernel attributes).

Core assertions:
1. ``kernel._pulse_observer`` exists, shares the kernel's real
   ``_perception_queue``, and its throttle/window come from
   ``settings.system_pulse``.
2. ``kernel._pulse_task`` is initialized to None at construction time,
   regardless of ``alerts_enabled`` — task creation itself only happens
   inside the async ``start()``, gated on ``alerts_enabled``. That gate is
   NOT exercised here (it needs a live event loop); coverage is deferred to
   the live-smoke checklist, consistent with the sibling observers
   (agenda / reflection / consolidation / evolution), which defer their own
   start()-gate coverage to the same tier boundary.
"""
from pathlib import Path

from dollos.config import (
    CharacterConfig,
    DataConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemsearchConfig,
    Settings,
    SystemPulseConfig,
)
from dollos.kernel import DollOS
from dollos.mind.pulse_observer import PulseObserver


def _make_settings(tmp_path: Path, *, system_pulse: SystemPulseConfig | None = None) -> Settings:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir(exist_ok=True)
    (pack_dir / "doll.toml").write_text(
        '[meta]\nid = "doll"\nname = "Doll"\n\n'
        '[identity]\nself = "You are Doll."\n'
        'personality = "- chill"\ntaboos = "- no LARP"\n'
    )
    return Settings(
        llm=LLMConfig(provider="llamacpp", template="qwen3-thinking",
                      base_url="http://test.local:8001", model_alias="big"),
        ipc=IPCConfig(host="127.0.0.1", port=9876),
        log=LogConfig(level="WARNING"),
        data=DataConfig(root=tmp_path / "data"),
        memsearch=MemsearchConfig(top_k=7),
        character=CharacterConfig(pack=pack_dir),
        system_pulse=system_pulse if system_pulse is not None else SystemPulseConfig(),
    )


def test_pulse_observer_constructed_sharing_queue_and_config(tmp_path: Path) -> None:
    settings = _make_settings(
        tmp_path,
        system_pulse=SystemPulseConfig(
            alerts_enabled=True, alert_throttle_s=123.0, window_stuck_s=456.0,
        ),
    )
    kernel = DollOS(settings)

    assert isinstance(kernel._pulse_observer, PulseObserver)
    assert kernel._pulse_observer._queue is kernel._perception_queue
    assert kernel._pulse_observer._throttle_s == 123.0
    assert kernel._pulse_observer._window_stuck_s == 456.0


def test_pulse_task_field_initializes_none(tmp_path: Path) -> None:
    """``_pulse_task`` initializes to None at construction regardless of
    ``alerts_enabled`` — __init__ unconditionally sets it to None; the
    real task-creation happens inside the async start(), gated on
    ``settings.system_pulse.alerts_enabled``. That start()-level gate is
    NOT exercised here (it needs a live event loop); it is covered by the
    live-smoke checklist (see the plan's "Live Smoke" section), consistent
    with how the sibling observers (agenda / reflection / consolidation /
    evolution) defer their own start()-gate coverage to the same tier
    boundary. This test only proves the field is honestly None pre-start
    in both the enabled and disabled configurations — it cannot and does
    not distinguish "the gate works" from "the gate is broken/inverted."
    """
    enabled_dir = tmp_path / "enabled"
    disabled_dir = tmp_path / "disabled"
    enabled_dir.mkdir()
    disabled_dir.mkdir()
    enabled_settings = _make_settings(enabled_dir, system_pulse=SystemPulseConfig(alerts_enabled=True))
    disabled_settings = _make_settings(disabled_dir, system_pulse=SystemPulseConfig(alerts_enabled=False))

    enabled_kernel = DollOS(enabled_settings)
    disabled_kernel = DollOS(disabled_settings)

    assert enabled_kernel._pulse_task is None
    assert disabled_kernel._pulse_observer is not None  # still constructed, just never scheduled
    assert disabled_kernel._pulse_task is None
