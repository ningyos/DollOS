"""PromptTemplate — model-family-specific prompt rendering."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod

from pydantic import BaseModel


class PromptTemplate(ABC):
    """Render a (system, user, prefill) tuple into the single prompt string
    the model expects.

    For "server-applied" templates (e.g. Anthropic / OpenAI chat completions
    where the API takes messages instead of a raw prompt), a concrete
    PromptTemplate may be a no-op stub and the corresponding Provider would
    talk in messages directly. Plan 3 v1 doesn't ship such a Provider, but
    the interface allows it.
    """

    @abstractmethod
    def render(
        self,
        *,
        system: str,
        user: str,
        prefill: str,
        tools: list[type[BaseModel]] | None = None,
    ) -> str:
        ...


def _format_tools_block(tools: list[type[BaseModel]]) -> str:
    """Render the `# Tools` system-prompt section — Qwen3 native format.

    Strips pydantic JSON Schema boilerplate (`title` fields, duplicate
    `description` at parameters level), uses OpenAI/Hermes function envelope,
    and serializes JSON with no whitespace. Saves ~37% vs raw model_json_schema().
    Uses Qwen3 canonical preamble wording from tokenizer_config.json.
    """

    def _compact_schema(cls: type[BaseModel]) -> dict:
        raw = cls.model_json_schema()
        props_raw = raw.get("properties", {})
        props: dict = {}
        for fname, finfo in props_raw.items():
            entry: dict = {}
            if "type" in finfo:
                entry["type"] = finfo["type"]
            if "description" in finfo:
                entry["description"] = finfo["description"]
            if "default" in finfo:
                entry["default"] = finfo["default"]
            if "minimum" in finfo:
                entry["minimum"] = finfo["minimum"]
            if "maximum" in finfo:
                entry["maximum"] = finfo["maximum"]
            props[fname] = entry
        return {
            "type": "function",
            "function": {
                "name": cls.__name__,
                "description": (cls.__doc__ or "").strip(),
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": raw.get("required", []),
                },
            },
        }

    schemas = [_compact_schema(cls) for cls in tools]
    schemas_json = "\n".join(
        json.dumps(s, ensure_ascii=False, separators=(",", ":")) for s in schemas
    )
    return (
        "\n\n# Tools\n\n"
        "You may call one or more functions to assist with the user query.\n\n"
        "You are provided with function signatures within <tools></tools> XML tags:\n"
        "<tools>\n"
        f"{schemas_json}\n"
        "</tools>\n\n"
        "For each function call, return a json object with function name "
        "and arguments within <tool_call></tool_call> XML tags:\n"
        "<tool_call>\n"
        '{"name": <function-name>, "arguments": <args-json-object>}\n'
        "</tool_call>"
    )


class Qwen3ThinkingTemplate(PromptTemplate):
    """Qwen3.x thinking-model ChatML.

    Opens the <think> block inside the assistant turn so prefill content
    goes inside the thinking block. Renders an optional `# Tools` section
    in the system prompt for tool calling.
    """

    def render(
        self,
        *,
        system: str,
        user: str,
        prefill: str,
        tools: list[type[BaseModel]] | None = None,
    ) -> str:
        if tools:
            system = system + _format_tools_block(tools)
        rendered = (
            f"<|im_start|>system\n{system}<|im_end|>\n"
            f"<|im_start|>user\n{user}<|im_end|>\n"
            f"<|im_start|>assistant\n<think>\n"
        )
        if prefill:
            rendered += prefill
        return rendered


class Qwen3PlainTemplate(PromptTemplate):
    """Qwen3.x ChatML with thinking immediately closed.

    Inner Voice's small models. Rejects non-empty tools — small-model
    code paths must not attempt tool calling (raises NotImplementedError
    to surface misuse loudly).
    """

    def render(
        self,
        *,
        system: str,
        user: str,
        prefill: str,
        tools: list[type[BaseModel]] | None = None,
    ) -> str:
        if tools:
            raise NotImplementedError(
                "Qwen3PlainTemplate does not support tool calling; "
                "use Qwen3ThinkingTemplate for tool-calling code paths."
            )
        rendered = (
            f"<|im_start|>system\n{system}<|im_end|>\n"
            f"<|im_start|>user\n{user}<|im_end|>\n"
            f"<|im_start|>assistant\n<think>\n\n</think>\n\n"
        )
        if prefill:
            rendered += prefill
        return rendered
