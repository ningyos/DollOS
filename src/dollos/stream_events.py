"""Typed events emitted by the streaming parser as it consumes LLM tokens."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class SpeakChunk:
    """A chunk of text outside <think>/<tool_call>. Stream to TTS."""
    text: str


@dataclass(frozen=True)
class ToolCallReady:
    """A complete <tool_call>...</tool_call> has been parsed."""
    name: str
    arguments: dict


@dataclass(frozen=True)
class StreamDone:
    """LLM stream finished."""


StreamEvent = Union[SpeakChunk, ToolCallReady, StreamDone]
