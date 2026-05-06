"""DollOS kernel: wires LLM adapter, memsearch, and IPC server together."""

import asyncio
import logging
import signal
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

from memsearch import MemSearch

from dollos.config import Settings
from dollos.dispatcher import EventDispatcher
from dollos.events import DiaryEvent, UserTextEvent
from dollos.inner_voice import InnerVoice
from dollos.instinct import Instinct, SmallModelInstinct
from dollos.ipc.messages import ErrorMsg, ServerMessage, TextInput
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
    """Construct memsearch rooted at data.root / memory / shared, transcripts, and skills.

    skills/ holds skill entry files (frontmatter + short description); they ARE indexed
    so RECALL surfaces them. skill_bodies/ holds full skill instructions and is NOT
    indexed — it is loaded on demand by the InvokeSkill tool. skill_bodies/ is also
    NOT auto-created at startup; Doll creates it lazily via Shell when she writes
    a new skill body.
    """
    shared_path = settings.data.root / "memory" / "shared"
    transcripts_path = settings.data.root / "memory" / "transcripts"
    skills_path = settings.data.root / "memory" / "skills"
    shared_path.mkdir(parents=True, exist_ok=True)
    transcripts_path.mkdir(parents=True, exist_ok=True)
    skills_path.mkdir(parents=True, exist_ok=True)
    return MemSearch(
        paths=[
            str(shared_path),
            str(transcripts_path),
            str(skills_path),
        ],
        embedding_provider="onnx",
    )


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


def build_instinct(
    settings: Settings, renderer: PromptRenderer
) -> Instinct:
    """Construct SmallModelInstinct wired to the small llama.cpp model.

    Uses the same `inner_voice` config block as InnerVoice — both are
    small-model utilities. v1 hardcodes (LlamaCppProvider, Qwen3PlainTemplate).
    """
    provider = LlamaCppProvider(
        base_url=settings.inner_voice.base_url,
        timeout_s=settings.inner_voice.timeout_s,
    )
    adapter = ComposedLLMAdapter(provider=provider, template=Qwen3PlainTemplate())
    return SmallModelInstinct(adapter=adapter, renderer=renderer)


class DollOS:
    DIARY_HOUR = 23   # 23:00 fires (1h buffer before midnight; see spec §12.3)
    DIARY_MINUTE = 0

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.adapter = build_adapter(settings)
        self.renderer = PromptRenderer()
        self.memsearch = build_memsearch(settings)
        self.inner_voice = build_inner_voice(settings, self.memsearch, self.renderer)
        self.instinct = build_instinct(settings, self.renderer)
        self._character_profile = settings.character.profile_path.read_text()
        self.dispatcher = EventDispatcher(
            adapter=self.adapter,
            inner_voice=self.inner_voice,
            instinct=self.instinct,
            renderer=self.renderer,
            character_profile=self._character_profile,
            memory_root=settings.data.root / "memory",
            memsearch=self.memsearch,
            transcripts_root=settings.data.root / "memory" / "transcripts",
        )
        self.server = WebSocketServer(
            host=settings.ipc.host,
            port=settings.ipc.port,
            handler=self._handle_text_input,
        )
        self._shutdown = asyncio.Event()
        self._scheduler_task: asyncio.Task[None] | None = None

    async def _handle_text_input(self, msg: TextInput) -> AsyncIterator[ServerMessage]:
        sink: asyncio.Queue[ServerMessage | None] = asyncio.Queue()
        self.dispatcher.dispatch(UserTextEvent(text=msg.text, response_sink=sink))
        while True:
            item = await sink.get()
            if item is None:
                return
            yield item

    async def _diary_scheduler(self) -> None:
        """Background task: fires DiaryEvent daily at DIARY_HOUR:DIARY_MINUTE."""
        while not self._shutdown.is_set():
            now = datetime.now()
            target = now.replace(
                hour=self.DIARY_HOUR, minute=self.DIARY_MINUTE,
                second=0, microsecond=0,
            )
            if target <= now:
                target = target + timedelta(days=1)
            sleep_s = (target - now).total_seconds()
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=sleep_s
                )
                return  # shutdown signaled
            except TimeoutError:
                pass  # time to fire
            sink: asyncio.Queue[ServerMessage | None] = asyncio.Queue()
            asyncio.create_task(self._drain_diary_sink(sink))
            self.dispatcher.dispatch(DiaryEvent(response_sink=sink))

    async def _drain_diary_sink(
        self, sink: asyncio.Queue[ServerMessage | None]
    ) -> None:
        """Consume diary event sink to None sentinel; logs ErrorMsg only."""
        while True:
            item = await sink.get()
            if item is None:
                return
            if isinstance(item, ErrorMsg):
                logger.error("diary event error: %s", item.message)
            # TextChunk / TurnEnd silently consumed

    async def run(self) -> None:
        await self.memsearch.index()
        try:
            await self.server.start()
            # Start diary scheduler
            self._scheduler_task = asyncio.create_task(self._diary_scheduler())
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._shutdown.set)
            try:
                await self._shutdown.wait()
            finally:
                await self.server.stop()
                # Cancel scheduler before dispatcher.stop so any in-flight
                # diary turn can finish via dispatcher's task tracking
                if self._scheduler_task is not None:
                    self._scheduler_task.cancel()
                    await asyncio.gather(
                        self._scheduler_task, return_exceptions=True
                    )
                await self.dispatcher.stop()
        finally:
            pass   # memsearch has no close(); Milvus Lite is file-based
