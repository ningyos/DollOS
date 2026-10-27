"""tool_schemas: chat-completions tool JSON generated from the pydantic tools."""

import inspect
import json

from pydantic import BaseModel, Field

import dollos.tools as tools_mod
from dollos.harness.executor import dispatch_one
from dollos.llm.tool_schema import tool_schemas
from dollos.tools import (
    MAIN_TOOLS,
    Recall,
    Report,
    Scratchpad,
    SpawnWorkflow,
    WriteSchedule,
)

KEPT = {
    "type", "enum", "anyOf", "items", "properties", "required",
    "default", "description", "minimum", "maximum",
}

# Every tool class in tools.py: a BaseModel with a run(). Nested argument
# models (WorkflowTask, ScheduleEntryArg) have no run() and are not tools.
ALL_TOOLS = [
    cls for _, cls in inspect.getmembers(tools_mod, inspect.isclass)
    if issubclass(cls, BaseModel) and cls.__module__ == tools_mod.__name__
    and hasattr(cls, "run")
]


def _keywords(schema: dict) -> set[str]:
    """Every JSON Schema keyword used, recursively. Property names are field
    names, not keywords, so they are not collected."""
    found = set(schema)
    for name, sub in schema.get("properties", {}).items():
        found |= _keywords(sub)
    if "items" in schema:
        found |= _keywords(schema["items"])
    for sub in schema.get("anyOf", []):
        found |= _keywords(sub)
    return found


def _params(cls: type[BaseModel]) -> dict:
    (entry,) = tool_schemas([cls])
    return entry["function"]["parameters"]


def test_every_tool_class_generates_a_clean_schema():
    names = {cls.__name__ for cls in ALL_TOOLS}
    assert {"LearnName", "PinSelf", "SelfRevision", "Report"} <= names
    assert {cls.__name__ for cls in MAIN_TOOLS} <= names

    for entry, cls in zip(tool_schemas(ALL_TOOLS), sorted(ALL_TOOLS, key=lambda c: c.__name__)):
        assert entry["type"] == "function"
        fn = entry["function"]
        assert fn["name"] == cls.__name__
        assert fn["description"] == inspect.cleandoc(cls.__doc__)
        params = fn["parameters"]
        assert params["type"] == "object"
        # The docstring is already the function description.
        assert "description" not in params
        keywords = _keywords(params)
        assert "$ref" not in keywords and "$defs" not in keywords
        assert "title" not in keywords
        assert keywords <= KEPT, (cls.__name__, keywords - KEPT)


def test_nested_list_items_keep_their_properties():
    entries = _params(WriteSchedule)["properties"]["entries"]
    assert entries["type"] == "array"
    assert set(entries["items"]["properties"]) == {"time", "intent"}
    assert entries["items"]["required"] == ["time", "intent"]

    tasks = _params(SpawnWorkflow)["properties"]["tasks"]
    assert tasks["type"] == "array"
    assert set(tasks["items"]["properties"]) == {"task"}


def test_enum_and_any_of_survive():
    assert _params(Scratchpad)["properties"]["op"]["enum"] == ["set", "append", "clear"]
    assert _params(Report)["properties"]["status"]["enum"] == ["ok", "incomplete"]
    since = _params(Recall)["properties"]["since"]
    assert since["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert since["default"] is None


def test_same_tools_in_any_order_give_identical_json():
    forward = json.dumps(tool_schemas(ALL_TOOLS), ensure_ascii=False)
    backward = json.dumps(tool_schemas(list(reversed(ALL_TOOLS))), ensure_ascii=False)
    assert forward == backward
    names = [e["function"]["name"] for e in tool_schemas(ALL_TOOLS)]
    assert names == sorted(names)


def test_field_named_like_a_keyword_is_kept_and_ref_siblings_merge():
    class Inner(BaseModel):
        """Inner doc."""

        title: str = Field(description="a field that happens to be called title")

    class Outer(BaseModel):
        """Outer doc."""

        inner: Inner = Field(description="field-level description")

    params = _params(Outer)
    inner = params["properties"]["inner"]
    # pydantic puts the field description next to the $ref; it wins.
    assert inner["description"] == "field-level description"
    assert inner["properties"]["title"] == {
        "description": "a field that happens to be called title", "type": "string",
    }


async def test_dispatch_one_rejects_values_the_server_does_not_check(tmp_path):
    """The server passes enum violations straight through; dispatch_one is
    where they stop."""
    from tests._dispatcher_helpers import _make_mind_ctx
    ctx = _make_mind_ctx(tmp_path)
    r = await dispatch_one(
        "Scratchpad", {"op": "delete", "content": "x"}, ctx, {"Scratchpad": Scratchpad},
    )
    assert r is not None and r.success is False
    assert "op" in r.detail and "delete" in r.detail


def test_every_tool_schema_is_english():
    """Plan 2026-10-19 Task 2: the tool list is the start of every private
    request; its descriptions are the daemon's words, so they carry no CJK
    (examples included, e.g. LearnName's nickname)."""
    from tests._cjk import cjk_runs

    for entry in tool_schemas(ALL_TOOLS):
        text = json.dumps(entry, ensure_ascii=False)
        assert cjk_runs(text) == [], (entry["function"]["name"], cjk_runs(text))


def test_result_descriptions_name_the_real_perception_lines():
    """Shell / SpawnMonitor describe how their results come back with the
    perception line she actually sees, not a stale paraphrase."""
    shell = inspect.cleandoc(tools_mod.Shell.__doc__)
    monitor = inspect.cleandoc(tools_mod.SpawnMonitor.__doc__)
    assert "ToolResultArrived: Shell" in shell
    assert "MonitorFired:" in monitor


def test_mood_emotion_is_described_as_one_english_word():
    desc = _params(tools_mod.MoodTool)["properties"]["emotion"]["description"]
    assert "one English word" in desc
    assert "'happy'" in desc and "'uneasy'" in desc
    assert "phrase" not in desc
