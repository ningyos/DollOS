"""Chat-completions ``tools`` JSON, generated from the pydantic tool classes.

The tool list sits at the very front of the server's prompt, so it is the
start of every cache prefix: the same set of tools must give byte-identical
JSON whatever order the classes come in. Names and shapes are the classes'
own; nothing is renamed or merged.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable

from pydantic import BaseModel

# JSON Schema keywords the model sees; the rest (title, format, minLength,
# minItems, ...) is dropped. dispatch_one validates against the full pydantic
# model, so a dropped constraint is still enforced when the tool runs.
_KEPT = frozenset({
    "type", "enum", "anyOf", "items", "properties", "required",
    "default", "description", "minimum", "maximum",
})


def tool_schemas(tool_classes: Iterable[type[BaseModel]]) -> list[dict]:
    """One ``{"type": "function", ...}`` entry per tool class, sorted by name."""
    return [_tool_schema(cls) for cls in sorted(tool_classes, key=lambda c: c.__name__)]


def _tool_schema(cls: type[BaseModel]) -> dict:
    # An MCP tool (harness/mcp_client.py) brings its server's name,
    # description and inputSchema, passed through as given: the server owns
    # that schema and validates the arguments itself.
    given = getattr(cls, "tool_json", None)
    if given is not None:
        return given()
    raw = cls.model_json_schema()
    parameters = _clean(raw, raw.get("$defs", {}))
    # pydantic repeats the docstring here; it is already the function description.
    parameters.pop("description", None)
    return {
        "type": "function",
        "function": {
            "name": cls.__name__,
            "description": inspect.cleandoc(cls.__doc__ or ""),
            "parameters": parameters,
        },
    }


def _clean(schema: dict, defs: dict) -> dict:
    """Inline ``$ref`` and keep only the ``_KEPT`` keywords, recursively."""
    if "$ref" in schema:
        name = schema["$ref"].removeprefix("#/$defs/")
        # pydantic puts field-level keys (e.g. description) next to the $ref.
        schema = {**defs[name], **{k: v for k, v in schema.items() if k != "$ref"}}
    out: dict = {}
    for key, value in schema.items():
        if key not in _KEPT:
            continue
        if key == "properties":
            value = {field: _clean(sub, defs) for field, sub in value.items()}
        elif key == "items":
            value = _clean(value, defs)
        elif key == "anyOf":
            value = [_clean(sub, defs) for sub in value]
        out[key] = value
    return out
