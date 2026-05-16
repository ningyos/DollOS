"""DollOS kernel: wires LLM adapter, memsearch, and IPC server together."""

import asyncio
import logging
import signal
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path

from memsearch import MemSearch

from dollos.cascade_log import CascadeLogger
from dollos.character import DollPack
from dollos.config import Settings
from dollos.dispatcher import EventDispatcher
from dollos.logging_config import configure_cascade_logging
from dollos.events import DailyPlanEvent, DiaryEvent, ScheduledEvent, UserTextEvent
from dollos.schedule import due_entries, load_schedule
from dollos.inner_voice import InnerVoice
from dollos.instinct import Instinct, SmallModelInstinct
from dollos.ipc.messages import (
    ErrorMsg,
    ICECandidateIn,
    ServerMessage,
    TextInput,
    UtteranceEnd,
    UtteranceStart,
    WebRTCAnswerOut,
    WebRTCOfferIn,
)
from dollos.ipc.server import WebSocketServer
from dollos.voice.engines import ASR_REGISTRY, ASREngine, TTS_REGISTRY, TTSEngine
from dollos.voice.pack import load_voice_config, resolve_voice_kwargs
from dollos.voice.session import VoiceSession
from dollos.voice.sink import TTSObservingSink
from dollos.llm.adapter import LLMAdapter
from dollos.llm.composed import ComposedLLMAdapter
from dollos.llm.templates import Qwen3PlainTemplate, Qwen3ThinkingTemplate
from dollos.llm.transport import LlamaCppProvider
from dollos.prompts import PromptRenderer
from dollos.monitor_runner import MonitorRunner
from dollos.shell_runner import ShellRunner
from dollos.subagent import SubagentRunner
from dollos.tool_outputs import ToolOutputStore

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


def build_voice_engines(
    pack_dir: Path,
    *,
    data_root: Path,
    voice_asr: dict | None,
    voice_tts: dict | None,
) -> "tuple[ASREngine, TTSEngine] | None":
    """Construct ASR+TTS engines for the active session.

    ASR is built straight from the DollOS-level ``[voice.asr]`` config —
    it is character-agnostic.

    TTS merges the DollOS-level ``[voice.tts]`` config (engine selection +
    infra) with the character pack's per-engine identity block.

    Returns ``None`` if either DollOS-level section is missing. Raises
    ``ValueError`` for unknown engine names or a missing pack variant.
    """
    if voice_asr is None or voice_tts is None:
        return None

    asr_name = voice_asr.get("engine")
    if asr_name not in ASR_REGISTRY:
        raise ValueError(f"unknown ASR engine in [voice.asr]: {asr_name!r}")
    asr_kwargs = {k: v for k, v in voice_asr.items() if k != "engine"}
    asr_kwargs.setdefault("data_root", data_root)
    asr = ASR_REGISTRY[asr_name](**asr_kwargs)

    cfg = load_voice_config(Path(pack_dir))
    tts_name, tts_kwargs = resolve_voice_kwargs(cfg, voice_tts)
    if tts_name not in TTS_REGISTRY:
        raise ValueError(f"unknown TTS engine in [voice.tts]: {tts_name!r}")
    tts_kwargs.setdefault("data_root", data_root)
    tts = TTS_REGISTRY[tts_name](**tts_kwargs)
    return asr, tts


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
        self._doll_pack = DollPack.load(settings.character.pack)
        cascade_log_root = settings.data.root / "cascade_log"
        configure_cascade_logging(cascade_log_root)
        self._cascade_logger = CascadeLogger(cascade_log_root)
        self._tool_output_dir = Path(tempfile.mkdtemp(prefix="dollos-tools-"))
        self._tool_output_store = ToolOutputStore(self._tool_output_dir)
        # Two-stage wiring: SubagentRunner / ShellRunner need a dispatch_fn,
        # dispatcher needs the runners. Build runners first with no dispatch_fn,
        # then build dispatcher referencing them, then point runners at
        # dispatcher.dispatch.
        self.shell_runner = ShellRunner(
            cwd=settings.data.root,
            tool_output_store=self._tool_output_store,
        )
        self.monitor_runner = MonitorRunner(cwd=settings.data.root)
        self.subagent_runner = SubagentRunner(
            adapter=self.adapter,
            renderer=self.renderer,
            memory_root=settings.data.root / "memory",
            memsearch=self.memsearch,
            transcripts_root=settings.data.root / "memory" / "transcripts",
            shell_runner=self.shell_runner,
            monitor_runner=self.monitor_runner,
            tool_output_store=self._tool_output_store,
        )
        self.dispatcher = EventDispatcher(
            adapter=self.adapter,
            inner_voice=self.inner_voice,
            instinct=self.instinct,
            renderer=self.renderer,
            identity=self._doll_pack.identity,
            memory_root=settings.data.root / "memory",
            memsearch=self.memsearch,
            transcripts_root=settings.data.root / "memory" / "transcripts",
            subagent_runner=self.subagent_runner,
            cascade_logger=self._cascade_logger,
            shell_runner=self.shell_runner,
            monitor_runner=self.monitor_runner,
            tool_output_store=self._tool_output_store,
        )
        self.subagent_runner.set_dispatch_fn(self.dispatcher.dispatch)
        self.shell_runner.set_dispatch_fn(self.dispatcher.dispatch)
        self.monitor_runner.set_dispatch_fn(self.dispatcher.dispatch)
        self._voice_sessions: dict[int, VoiceSession] = {}  # keyed by id(sink)
        self._pack_dir = Path(settings.character.pack)
        self._data_root = settings.data.root
        self.server = WebSocketServer(
            host=settings.ipc.host,
            port=settings.ipc.port,
            handler=self._handle_message,
            on_connect=self._handle_connect,
            on_disconnect=self._handle_disconnect,
            sink_factory=self._make_sink,
        )
        self._shutdown = asyncio.Event()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._schedule_task: asyncio.Task[None] | None = None
        # Active connection sink — set by _handle_connect, cleared by
        # _handle_disconnect. Used by scheduler-fired events that need a
        # sink (Phase 1 ScheduledEvent / DailyPlanEvent).
        self._active_sink: "asyncio.Queue[ServerMessage | None] | None" = None
        # Per-day fired set — scheduler dedupe across its 30s polling.
        self._fired_today: dict[date, set[time]] = {}
        # Track per-day bootstrap so reconnects within a day don't refire.
        self._bootstrapped_dates: set[date] = set()

    def _make_sink(self) -> "asyncio.Queue[ServerMessage | None]":
        """Build a TTSObservingSink that fetches the voice session at speak-time."""
        holder: dict = {}
        sink = TTSObservingSink(
            voice_session_provider=lambda: self._voice_sessions.get(holder["id"]),
        )
        holder["id"] = id(sink)
        return sink

    async def _handle_message(
        self, msg, sink: "asyncio.Queue[ServerMessage | None]"
    ) -> None:
        if isinstance(msg, TextInput):
            self.dispatcher.dispatch(UserTextEvent(text=msg.text, response_sink=sink))
        elif isinstance(msg, WebRTCOfferIn):
            answer_sdp = await self._handle_offer(msg.sdp, sink)
            sink.put_nowait(WebRTCAnswerOut(sdp=answer_sdp))
        elif isinstance(msg, ICECandidateIn):
            session = self._voice_sessions.get(id(sink))
            if session is not None:
                await session.handle_ice_candidate(
                    candidate=msg.candidate,
                    sdpMid=msg.sdpMid,
                    sdpMLineIndex=msg.sdpMLineIndex,
                )
        elif isinstance(msg, UtteranceStart):
            session = self._voice_sessions.get(id(sink))
            if session is not None:
                await session.handle_utterance_start(sample_rate=msg.sample_rate)
        elif isinstance(msg, UtteranceEnd):
            session = self._voice_sessions.get(id(sink))
            if session is not None:
                await session.handle_utterance_end()
        else:
            logger.warning("unhandled message type: %r", type(msg).__name__)

    async def _handle_offer(
        self, offer_sdp: str, sink: "asyncio.Queue[ServerMessage | None]"
    ) -> str:
        voice_asr = (
            self.settings.voice.asr.model_dump()
            if self.settings.voice.asr is not None
            else None
        )
        voice_tts = (
            self.settings.voice.tts.model_dump()
            if self.settings.voice.tts is not None
            else None
        )
        engines = build_voice_engines(
            self._pack_dir,
            data_root=self._data_root,
            voice_asr=voice_asr,
            voice_tts=voice_tts,
        )
        if engines is None:
            raise RuntimeError(
                "voice not configured: DollOS [voice.asr] and [voice.tts] "
                "must both be set, and the active character pack must "
                f"include a matching [tts.<engine>] block at {self._pack_dir}/voice/engine.toml"
            )
        asr, tts = engines

        async def _on_user_text(text: str) -> None:
            self.dispatcher.dispatch(UserTextEvent(text=text, response_sink=sink))

        session = VoiceSession(asr=asr, tts=tts, on_user_text=_on_user_text)
        self._voice_sessions[id(sink)] = session
        return await session.handle_offer(offer_sdp)

    # Keep legacy name for backward compatibility with any external callers.
    _handle_text_input = _handle_message

    async def _handle_connect(
        self, sink: "asyncio.Queue[ServerMessage | None]"
    ) -> None:
        """WebSocketServer on_connect hook — exposes the live sink.

        Scheduled / bootstrap events fired while a client is connected use
        this sink so output reaches that client. Bootstrap deferred to the
        first connect (gap #5) so DailyPlanEvent's response can stream
        straight to a real consumer.
        """
        self._active_sink = sink
        await self._maybe_bootstrap_plan()

    async def _handle_disconnect(self, sink: "asyncio.Queue[ServerMessage | None]") -> None:
        """WebSocketServer on_disconnect hook — drops the live sink and closes
        any associated VoiceSession."""
        session = self._voice_sessions.pop(id(sink), None)
        if session is not None:
            try:
                await session.close()
            except Exception:
                logger.exception("voice session close raised")
        if self._active_sink is sink:
            self._active_sink = None

    def _active_sink_or_dummy(
        self,
    ) -> "asyncio.Queue[ServerMessage | None]":
        """Return live client sink if connected, else a fresh queue.

        The dummy queue is never read — pushes succeed and are silently
        discarded with the queue at GC. Sufficient for fire-and-forget
        scheduler events when no UI is listening.
        """
        if self._active_sink is not None:
            return self._active_sink
        return asyncio.Queue()

    async def _maybe_bootstrap_plan(self) -> None:
        """Fire DailyPlanEvent if today has no schedule yet (gap #5).

        Runs on every connect but only fires once per day (the
        ``_bootstrapped_dates`` guard) and only if no schedule.toml exists.
        """
        today = date.today()
        if today in self._bootstrapped_dates:
            return
        path = (
            self.settings.data.root
            / "memory"
            / "schedule"
            / f"{today:%Y-%m-%d}.toml"
        )
        if path.exists():
            self._bootstrapped_dates.add(today)
            return
        self._bootstrapped_dates.add(today)
        # Bootstrap is daemon-internal planning — Doll's Say output goes to
        # /dev/null so the smoke client doesn't see it as a turn response.
        # WriteSchedule still works (writes to file regardless of sink).
        self.dispatcher.dispatch(DailyPlanEvent(response_sink=asyncio.Queue()))

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

    async def _schedule_runner(self) -> None:
        """Background task: every 30s, fire any due ScheduledEvents.

        Reads ``data/memory/schedule/{today}.toml``, finds entries within a
        1-minute window of now (per ``due_entries``), and dispatches a
        ``ScheduledEvent`` for each. Past entries from earlier in the day
        are skipped (gap #7) — daemon-offline misses are not replayed.
        """
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=30.0
                )
                return  # shutdown signaled
            except TimeoutError:
                pass

            today = date.today()
            path = (
                self.settings.data.root
                / "memory"
                / "schedule"
                / f"{today:%Y-%m-%d}.toml"
            )
            schedule = load_schedule(path)
            if schedule is None:
                continue
            now = datetime.now()
            fired = self._fired_today.setdefault(today, set())
            for entry in due_entries(schedule, now, fired):
                fired.add(entry.time)
                sink = self._active_sink_or_dummy()
                self.dispatcher.dispatch(
                    ScheduledEvent(
                        entry_time=entry.time,
                        intent=entry.intent,
                        response_sink=sink,
                    )
                )

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
            self._schedule_task = asyncio.create_task(self._schedule_runner())
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
                if self._schedule_task is not None:
                    self._schedule_task.cancel()
                    await asyncio.gather(
                        self._schedule_task, return_exceptions=True
                    )
                # Stop subagents and shell runner BEFORE dispatcher so any
                # final result event has a live dispatcher to enter (in
                # practice cancellation skips the result event; ordering
                # kept explicit per plan).
                await self.subagent_runner.stop()
                await self.shell_runner.stop()
                await self.monitor_runner.stop()
                await self.dispatcher.stop()
                self._tool_output_store.cleanup()
        finally:
            pass   # memsearch has no close(); Milvus Lite is file-based
