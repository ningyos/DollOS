"""DollOS kernel — placeholder during memsearch pivot. Rewritten in Task 4."""

import asyncio  # noqa: F401
import logging
import signal  # noqa: F401
from collections.abc import AsyncIterator  # noqa: F401

from dollos.config import Settings
from dollos.ipc.messages import ErrorMsg, ServerMessage, TextChunk, TextInput, TurnEnd  # noqa: F401
from dollos.ipc.server import WebSocketServer  # noqa: F401
from dollos.llm.adapter import LLMAdapter
from dollos.llm.composed import ComposedLLMAdapter
from dollos.llm.templates import Qwen3ThinkingTemplate
from dollos.llm.transport import LlamaCppProvider
from dollos.prompts import PromptRenderer  # noqa: F401

logger = logging.getLogger(__name__)


def build_adapter(settings: Settings) -> LLMAdapter:
    provider = _build_provider(settings)
    template = _build_template(settings)
    return ComposedLLMAdapter(provider=provider, template=template)


def _build_provider(settings: Settings) -> LlamaCppProvider:
    if settings.llm.provider == "llamacpp":
        return LlamaCppProvider(
            base_url=settings.llm.base_url,
            timeout_s=settings.llm.timeout_s,
        )
    raise ValueError(f"unknown provider: {settings.llm.provider}")


def _build_template(settings: Settings) -> Qwen3ThinkingTemplate:
    if settings.llm.template == "qwen3-thinking":
        return Qwen3ThinkingTemplate()
    raise ValueError(f"unknown template: {settings.llm.template}")


class DollOS:
    """Placeholder during memsearch pivot — full impl in Task 4."""

    def __init__(self, settings: Settings) -> None:
        raise NotImplementedError(
            "DollOS is being rewritten for memsearch pivot. See Task 4."
        )
