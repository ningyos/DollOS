"""DollOS kernel: wires LLM adapter, memsearch, and IPC server together."""

import asyncio
import logging
import signal
from collections.abc import AsyncIterator

from memsearch import MemSearch

from dollos.config import Settings
from dollos.inner_voice import InnerVoice
from dollos.ipc.messages import ErrorMsg, ServerMessage, TextChunk, TextInput, TurnEnd
from dollos.ipc.server import WebSocketServer
from dollos.llm.adapter import LLMAdapter
from dollos.llm.composed import ComposedLLMAdapter
from dollos.llm.templates import Qwen3PlainTemplate, Qwen3ThinkingTemplate
from dollos.llm.transport import LlamaCppProvider
from dollos.prompts import PromptRenderer

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


def build_memsearch(settings: Settings) -> MemSearch:
    """Construct memsearch rooted at data.root / memory / shared.

    step 10 will extend `paths` to include the active character's
    private directory (data.root/memory/<character_id>). v1 only has shared.
    """
    shared_path = settings.data.root / "memory" / "shared"
    shared_path.mkdir(parents=True, exist_ok=True)
    return MemSearch(paths=[str(shared_path)], embedding_provider="onnx")


def build_inner_voice(
    settings: Settings, memsearch: MemSearch, renderer: PromptRenderer
) -> InnerVoice:
    """Construct InnerVoice wired to a small llama.cpp model + memsearch.

    v1 hardcodes (LlamaCppProvider, Qwen3PlainTemplate) for the small LLM.
    """
    provider = LlamaCppProvider(
        base_url=settings.inner_voice.base_url,
        timeout_s=settings.inner_voice.timeout_s,
    )
    llm = ComposedLLMAdapter(provider=provider, template=Qwen3PlainTemplate())
    return InnerVoice(
        memsearch=memsearch,
        llm=llm,
        renderer=renderer,
        default_top_k=settings.memsearch.top_k,
    )


class DollOS:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.adapter = build_adapter(settings)
        self.renderer = PromptRenderer()
        self.memsearch = build_memsearch(settings)
        self.inner_voice = build_inner_voice(settings, self.memsearch, self.renderer)
        self._character_profile = settings.character.profile_path.read_text()
        self.server = WebSocketServer(
            host=settings.ipc.host,
            port=settings.ipc.port,
            handler=self._handle_text_input,
        )
        self._shutdown = asyncio.Event()

    async def _handle_text_input(self, msg: TextInput) -> AsyncIterator[ServerMessage]:
        try:
            system = self.renderer.render(
                "scaffolding", character=self._character_profile
            )
            recall = await self.inner_voice.recall(msg.text)
            prefill = f"<think>\n{recall}GOAL: "
            async for chunk in self.adapter.stream_completion(
                system=system, user=msg.text, prefill=prefill,
            ):
                if chunk.text:
                    yield TextChunk(text=chunk.text)
                if chunk.done:
                    break
            yield TurnEnd()
        except Exception as e:
            logger.exception("handler error")
            yield ErrorMsg(message=f"handler error: {e}")

    async def run(self) -> None:
        await self.memsearch.index()
        try:
            await self.server.start()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._shutdown.set)
            try:
                await self._shutdown.wait()
            finally:
                await self.server.stop()
        finally:
            pass   # memsearch has no close(); Milvus Lite is file-based
