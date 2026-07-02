"""turn/external_ctx threading: MindLoop batch → MindCtx → PinSelf → self_history."""
import json
import time
import types

import pytest

from dollos.mind.mind_loop import batch_external
from dollos.mind.mind_state import Perception
from dollos.tools import PinSelf


def _p(kind):
    return Perception(kind=kind, t=time.time(), data={})


def test_batch_external_true_for_tool_results():
    assert batch_external([_p("UserSpoke"), _p("ToolResultArrived")]) is True
    assert batch_external([_p("MonitorFired")]) is True
    assert batch_external([_p("MonitorEnded")]) is True


def test_batch_external_false_for_internal_kinds():
    assert batch_external([_p("UserSpoke"), _p("ReflectionMoment")]) is False
    assert batch_external([]) is False


@pytest.mark.asyncio
async def test_pinself_threads_turn_and_ctx_into_history(tmp_path):
    ctx = types.SimpleNamespace(
        memory_root=tmp_path, self_profile_max_chars=1200,
        current_turn=42, external_ctx=True,
        mind_state=types.SimpleNamespace(recent_outputs=[]),
    )
    tool = PinSelf(section="self", op="add", target="", text="喜歡監控數字")
    result = await tool.run(ctx)
    assert "已 pin" in result
    hist = tmp_path / "self_history.jsonl"
    (ev,) = [json.loads(l) for l in hist.read_text(encoding="utf-8").splitlines()]
    assert ev["turn"] == 42 and ev["external_ctx"] is True and ev["kind"] == "pin_add"
