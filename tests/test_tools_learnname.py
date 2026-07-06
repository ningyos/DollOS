"""Part A / A2 — ``LearnName`` tool: mechanical guard + write path.

Registry gating (the security-load-bearing half) is covered separately in
tests/test_mind_loop_learnname_gate.py. This file covers the tool's own
behavior once it IS in the registry: the mechanical guard (L2, applies even
in owner turns) and the write/remove path, including that ``origin`` is
hardcoded to "owner" and cannot be influenced by the model's output.
"""
from __future__ import annotations

import json

import pytest

from dollos.mind.name_aliases import NameAliasStore
from dollos.tools import LearnName
from tests._dispatcher_helpers import _make_mind_ctx


@pytest.mark.asyncio
async def test_add_valid_nickname_is_written_and_active(tmp_path):
    ctx = _make_mind_ctx(tmp_path)
    msg = await LearnName(op="add", token="小鯊").run(ctx)
    store = NameAliasStore(tmp_path / "name_aliases.json")
    assert "小鯊" in store.active_tokens()
    assert "小鯊" in msg


@pytest.mark.asyncio
async def test_add_too_short_token_is_rejected_and_not_written(tmp_path):
    ctx = _make_mind_ctx(tmp_path)
    msg = await LearnName(op="add", token="小").run(ctx)
    store = NameAliasStore(tmp_path / "name_aliases.json")
    assert "小" not in store.active_tokens()
    assert store.active_tokens() == frozenset()
    assert msg  # friendly-error string, non-empty


@pytest.mark.asyncio
async def test_add_denylisted_token_is_rejected_and_not_written(tmp_path):
    ctx = _make_mind_ctx(tmp_path)
    for bad in ("hey", "hello", "everyone", "大家", "你", "ok"):
        msg = await LearnName(op="add", token=bad).run(ctx)
        assert bad not in NameAliasStore(tmp_path / "name_aliases.json").active_tokens()
        assert msg


@pytest.mark.asyncio
async def test_add_denylist_is_case_insensitive_for_ascii(tmp_path):
    ctx = _make_mind_ctx(tmp_path)
    await LearnName(op="add", token="HEY").run(ctx)
    store = NameAliasStore(tmp_path / "name_aliases.json")
    assert "HEY" not in store.active_tokens()
    assert "hey" not in store.active_tokens()


@pytest.mark.asyncio
async def test_remove_removes_previously_added_token(tmp_path):
    ctx = _make_mind_ctx(tmp_path)
    await LearnName(op="add", token="小鯊").run(ctx)
    await LearnName(op="remove", token="小鯊").run(ctx)
    store = NameAliasStore(tmp_path / "name_aliases.json")
    assert "小鯊" not in store.active_tokens()


@pytest.mark.asyncio
async def test_origin_field_not_on_model():
    """Structural proof the model CANNOT set origin: the field doesn't
    exist on LearnName at all."""
    assert "origin" not in LearnName.model_fields


@pytest.mark.asyncio
async def test_origin_is_always_owner_even_if_extra_kwarg_passed(tmp_path):
    """Even if a rogue caller/decoder tried to smuggle an ``origin`` kwarg
    into the tool call, pydantic has no such field to bind it to, and the
    write path hardcodes origin="owner" regardless (A1 review ⚠️)."""
    ctx = _make_mind_ctx(tmp_path)
    tool = LearnName.model_validate({"op": "add", "token": "小鯊", "origin": "stranger"})
    await tool.run(ctx)
    entries = NameAliasStore(tmp_path / "name_aliases.json")._load()
    assert entries["小鯊"].origin == "owner"


@pytest.mark.asyncio
async def test_add_writes_provenance_history(tmp_path):
    ctx = _make_mind_ctx(tmp_path)
    ctx.origin_tier = "external_dm"
    ctx.external_ctx = True
    await LearnName(op="add", token="小鯊", note="owner called me this in DM").run(ctx)
    hist_path = tmp_path / "aliases_history.jsonl"
    assert hist_path.exists()
    lines = hist_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["token"] == "小鯊"
    assert event["op"] == "add"
    assert event["origin_tier"] == "external_dm"
    assert event["external_ctx"] is True
    assert "ts" in event


@pytest.mark.asyncio
async def test_guard_rejected_add_does_not_write_provenance(tmp_path):
    ctx = _make_mind_ctx(tmp_path)
    await LearnName(op="add", token="hey").run(ctx)
    hist_path = tmp_path / "aliases_history.jsonl"
    assert not hist_path.exists()
