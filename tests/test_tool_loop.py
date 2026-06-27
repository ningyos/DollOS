"""Tests for cascade.tool_loop shared dispatch + friendly error formatting."""
from __future__ import annotations

from pydantic import ValidationError

from dollos.cascade.tool_loop import format_unknown_tool, format_validation_error
from dollos.tools import ReadToolOutput, Recall, Shell


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
