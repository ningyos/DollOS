"""Tests for cascade.tool_loop shared dispatch + friendly error formatting."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from dollos.cascade.tool_loop import dispatch_one, format_unknown_tool, format_validation_error
from dollos.tools import ReadToolOutput, Recall, SetFocus, Shell


def test_format_unknown_tool_lists_available():
    msg = format_unknown_tool("Foo", {"Shell": Shell, "Recall": Recall})
    assert "Foo" in msg
    assert "Shell" in msg and "Recall" in msg


def test_format_validation_error_names_field_not_raw_wall():
    """限制超界(limit ge=1) → 友善訊息含工具名+欄位名+給定值，不含 pydantic 原始牆。"""
    try:
        ReadToolOutput.model_validate({"id": "x", "offset": 0, "limit": 0})
    except ValidationError as e:
        msg = format_validation_error(e, "ReadToolOutput")
    assert "ReadToolOutput" in msg
    assert "limit" in msg
    assert "0" in msg
    # 不是 pydantic 原始錯誤牆（不含 URL / 'validation error(s) for'）
    assert "https://" not in msg
    assert "validation error" not in msg.lower()


@pytest.mark.asyncio
async def test_dispatch_one_unknown_tool_friendly(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    ctx = _make_mind_ctx(tmp_path)
    r = await dispatch_one("Nope", {}, ctx, {"Shell": Shell})
    assert r is not None and r.success is False
    assert "Nope" in r.detail and "Shell" in r.detail


@pytest.mark.asyncio
async def test_dispatch_one_validation_friendly(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    ctx = _make_mind_ctx(tmp_path)
    registry = {"ReadToolOutput": ReadToolOutput}
    r = await dispatch_one(
        "ReadToolOutput", {"id": "x", "offset": 0, "limit": 0}, ctx, registry
    )
    assert r is not None and r.success is False
    assert "limit" in r.detail
    assert "validation error" not in r.detail.lower()


@pytest.mark.asyncio
async def test_dispatch_one_success_returns_detail(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    ctx = _make_mind_ctx(tmp_path)
    r = await dispatch_one(
        "SetFocus", {"text": "writing the plan"}, ctx, {"SetFocus": SetFocus}
    )
    assert r is not None and r.success is True
    assert "writing the plan" in r.detail
    assert ctx.mind_state.focus == "writing the plan"


@pytest.mark.asyncio
async def test_dispatch_one_runtime_error_returns_failed_result(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    from pydantic import BaseModel

    class _Boom(BaseModel):
        async def run(self, ctx):
            raise RuntimeError("kaboom")

    ctx = _make_mind_ctx(tmp_path)
    r = await dispatch_one("Boom", {}, ctx, {"Boom": _Boom})
    assert r is not None and r.success is False
    assert "runtime error" in r.detail and "kaboom" in r.detail
