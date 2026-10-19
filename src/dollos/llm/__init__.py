"""The model interface: ModelAdapter over OpenAI-compatible chat completions."""

from dollos.llm.chat_completions import ChatCompletionsAdapter
from dollos.llm.model import (
    End,
    ModelAdapter,
    ModelError,
    ModelEvent,
    SpeechDelta,
    ThinkDelta,
    ToolCall,
    ToolCalls,
    Usage,
    tool_message,
)
from dollos.llm.tool_schema import tool_schemas

__all__ = [
    "ChatCompletionsAdapter",
    "End",
    "ModelAdapter",
    "ModelError",
    "ModelEvent",
    "SpeechDelta",
    "ThinkDelta",
    "ToolCall",
    "ToolCalls",
    "Usage",
    "tool_message",
    "tool_schemas",
]
