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
    """Render the `# Tools` system-prompt section for Qwen3 native tool calling."""
    schemas = [
        {
            "name": cls.__name__,
            "description": (cls.__doc__ or "").strip(),
            "parameters": cls.model_json_schema(),
        }
        for cls in tools
    ]
    schemas_json = json.dumps(schemas, ensure_ascii=False, indent=2)
    return (
        "\n\n# Tools\n\n"
        "You have tools. To call a tool, emit:\n"
        "<tool_call>\n"
        '{"name": "<tool_name>", "arguments": {<args>}}\n'
        "</tool_call>\n\n"
        "After </think>, output ONLY <tool_call> blocks. "
        "Plain text after </think> is invalid.\n\n"
        "Available tools:\n"
        "<tools>\n"
        f"{schemas_json}\n"
        "</tools>"
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
        parts = [
            "<|im_start|>system",
            system,
            "<|im_end|>",
            "<|im_start|>user",
            user,
            "<|im_end|>",
            "<|im_start|>assistant",
            "<think>",
            "",
        ]
        rendered = "\n".join(parts)
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
        parts = [
            "<|im_start|>system",
            system,
            "<|im_end|>",
            "<|im_start|>user",
            user,
            "<|im_end|>",
            "<|im_start|>assistant",
            "<think>",
            "",
            "</think>",
            "",
            "",
        ]
        rendered = "\n".join(parts)
        if prefill:
            rendered += prefill
        return rendered
