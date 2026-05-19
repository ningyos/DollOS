"""DollOS kernel: wires LLM adapter, memsearch, IPC server, and MindLoop together."""

import asyncio
import logging
import signal
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from memsearch import MemSearch

from dollos.character import DollPack
from dollos.config import Settings
from dollos.logging_config import configure_cascade_logging
from dollos.cascade_log import CascadeLogger
from dollos.schedule import due_entries, load_schedule
from dollos.ipc.messages import (
    ErrorMsg,
    ICECandidateIn,
    Interrupt,
    SayAborted,
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
from dollos.llm.templates import Qwen3ThinkingTemplate
from dollos.llm.transport import LlamaCppProvider
from dollos.prompts import PromptRenderer
from dollos.monitor_runner import MonitorRunner
from dollos.perception.cognition import CognitionWorker
from dollos.perception.system_pulse import SystemPulse
from dollos.telemetry.llm_calls import TelemetryRecorder
from dollos.shell_runner import ShellRunner
from dollos.subagent import SubagentRunner
from dollos.tool_outputs import ToolOutputStore
from dollos.tools import MAIN_TOOLS
from dollos.mind.mind_ctx import MindCtx
from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_state import MindState, Perception, load_state
from dollos.mind.perception_queue import PerceptionQueue
from dollos.mind.reflection_observer import ReflectionObserver
from dollos.mind.sink_resolver import SinkResolver

logger = logging.getLogger(__name__)


def build_adapter(
    settings: Settings,
    recorder: TelemetryRecorder | None = None,
) -> LLMAdapter:
    provider = _build_provider(settings, recorder=recorder)
    template = _build_template(settings)
    return ComposedLLMAdapter(provider=provider, template=template)


def _build_provider(
    settings: Settings,
    recorder: TelemetryRecorder | None = None,
) -> LlamaCppProvider:
    if settings.llm.provider == "llamacpp":
        return LlamaCppProvider(
            base_url=settings.llm.base_url,
            timeout_s=settings.llm.timeout_s,
            recorder=recorder,
            model_alias=settings.llm.model_alias,
            max_context_tokens=settings.cognition.max_context_tokens,
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


class _MindLLMAdapter:
    """Thin adapter: wraps LLMAdapter.stream_completion for MindLoop._llm_call.

    MindLoop expects an object with stream_completion(system, user, prefill)
    that yields chunks with .text and .done attributes.
    The underlying LLMAdapter.stream_completion is an async generator with
    matching interface, so this is a transparent pass-through.
    """

    def __init__(self, adapter: LLMAdapter) -> None:
        self._adapter = adapter

    async def stream_completion(
        self,
        system: str,
        user: str,
        prefill: str = "",
        max_tokens: int = 1024,
        grammar: str | None = None,
    ):
        async for chunk in self._adapter.stream_completion(
            system=system,
            user=user,
            prefill=prefill,
            max_tokens=max_tokens,
            grammar=grammar,
        ):
            yield chunk


class DollOS:
    DIARY_HOUR = 23   # 23:00 fires (1h buffer before midnight; see spec §12.3)
    DIARY_MINUTE = 0

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Telemetry recorder feeds both the LLM provider (recording side)
        # and the CognitionWorker (reading side).
        telemetry_dir = settings.data.root / settings.cognition.telemetry_dir_subpath
        self.telemetry_recorder = TelemetryRecorder(telemetry_dir)
        self.adapter = build_adapter(settings, recorder=self.telemetry_recorder)
        self.renderer = PromptRenderer()
        self.memsearch = build_memsearch(settings)
        self._doll_pack = DollPack.load(settings.character.pack)
        cascade_log_root = settings.data.root / "cascade_log"
        configure_cascade_logging(cascade_log_root)
        self._cascade_logger = CascadeLogger(cascade_log_root)
        self._tool_output_dir = Path(tempfile.mkdtemp(prefix="dollos-tools-"))
        self._tool_output_store = ToolOutputStore(self._tool_output_dir)

        # ------------------------------------------------------------------ #
        # MindLoop infrastructure                                              #
        # ------------------------------------------------------------------ #
        self._perception_queue = PerceptionQueue()
        self._mind_state = load_state(settings.data.root / "mind_state.json")
        self._sink_resolver = SinkResolver()

        self.shell_runner = ShellRunner(
            cwd=settings.data.root,
            perception_queue=self._perception_queue,
            tool_output_store=self._tool_output_store,
        )
        self.monitor_runner = MonitorRunner(
            cwd=settings.data.root,
            perception_queue=self._perception_queue,
        )
        self.subagent_runner = SubagentRunner(
            adapter=self.adapter,
            renderer=self.renderer,
            memory_root=settings.data.root / "memory",
            memsearch=self.memsearch,
            transcripts_root=settings.data.root / "memory" / "transcripts",
            perception_queue=self._perception_queue,
            shell_runner=self.shell_runner,
            monitor_runner=self.monitor_runner,
            tool_output_store=self._tool_output_store,
        )

        self._mind_ctx = MindCtx(
            mind_state=self._mind_state,
            memsearch=self.memsearch,
            memory_root=settings.data.root / "memory",
            transcripts_root=settings.data.root / "memory" / "transcripts",
            sink_resolver=self._sink_resolver,
            tool_output_store=self._tool_output_store,
            shell_runner=self.shell_runner,
            subagent_runner=self.subagent_runner,
            monitor_runner=self.monitor_runner,
        )

        # Render the static system prompt from the character pack
        skills_dir = settings.data.root / "memory" / "skills"
        if skills_dir.exists():
            available_skills = sorted(p.stem for p in skills_dir.glob("*.md"))
        else:
            available_skills = []
        tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}

        system_prompt = self.renderer.render(
            "scaffolding",
            identity=self._doll_pack.identity,
            available_skills=available_skills,
            tool_registry=tool_registry,
        )

        # Self pulse — Doll's proprioception of her host.
        self.system_pulse = SystemPulse(
            poll_interval_s=settings.system_pulse.poll_interval_s,
            include_active_window=settings.system_pulse.include_active_window,
            enabled=settings.system_pulse.enabled,
        )

        # Cognition — Doll's awareness of her own LLM consumption.
        self.cognition = CognitionWorker(
            recorder=self.telemetry_recorder,
            enabled=settings.cognition.enabled,
            daily_token_quota=settings.cognition.daily_token_quota,
            max_context_tokens=settings.cognition.max_context_tokens,
        )

        self._mind_loop = MindLoop(
            state=self._mind_state,
            queue=self._perception_queue,
            ctx=self._mind_ctx,
            llm=_MindLLMAdapter(self.adapter),
            system_prompt=system_prompt,
            state_persist_path=settings.data.root / "mind_state.json",
            tool_registry=tool_registry,
            system_pulse=self.system_pulse,
            cognition=self.cognition,
        )

        self._reflection_observer = ReflectionObserver(
            state=self._mind_state,
            queue=self._perception_queue,
        )

        # ------------------------------------------------------------------ #
        # IPC server                                                           #
        # ------------------------------------------------------------------ #
        self._voice_sessions: dict[int, VoiceSession] = {}  # keyed by id(sink)
        self._pack_dir = Path(settings.character.pack)
        self._data_root = settings.data.root
        # Maps id(sink) → sink_handle (int) for unregister on disconnect.
        self._sink_handles: dict[int, int] = {}
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
        self._mind_task: asyncio.Task[None] | None = None
        self._reflection_task: asyncio.Task[None] | None = None
        # Per-day fired set — scheduler dedupe across its 30s polling.
        self._fired_today: dict[date, set] = {}
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
            # If a cascade is currently active, preempt it before pushing
            # the new input so Doll responds to the latest message.
            await self._maybe_preempt_for_new_input(sink)
            # Push as Perception into the queue; MindLoop drains it.
            self._perception_queue.put(
                Perception(
                    kind="UserSpoke",
                    t=time.time(),
                    data={"text": msg.text},
                )
            )
        elif isinstance(msg, Interrupt):
            # Explicit stop signal without new input.
            await self._maybe_preempt_for_new_input(sink)
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

    async def _maybe_preempt_for_new_input(
        self, sink: "asyncio.Queue[ServerMessage | None]"
    ) -> None:
        """If a cascade is in flight, cancel it + abort speak + push Interrupted perception."""
        if not self._mind_loop.is_cascade_active:
            return  # idle, nothing to preempt

        # 1. Cancel cascade
        self._mind_loop.cancel_current_cascade()

        # 2. Abort speak
        session = self._voice_sessions.get(id(sink))
        if session is not None:
            await session.abort_speak()

        # 3. Signal client
        sink.put_nowait(SayAborted(reason="user_interrupted"))

        # 4. Push Interrupted perception so Doll knows
        self._perception_queue.put(
            Perception(
                kind="Interrupted",
                t=time.time(),
                data={"by": "user_text_input"},
            )
        )

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
            self._perception_queue.put(
                Perception(
                    kind="UserSpoke",
                    t=time.time(),
                    data={"text": text},
                )
            )

        session = VoiceSession(asr=asr, tts=tts, on_user_text=_on_user_text)
        self._voice_sessions[id(sink)] = session
        return await session.handle_offer(offer_sdp)

    # Keep legacy name for backward compatibility with any external callers.
    _handle_text_input = _handle_message

    async def _handle_connect(
        self, sink: "asyncio.Queue[ServerMessage | None]"
    ) -> None:
        """WebSocketServer on_connect hook — register sink with SinkResolver."""
        handle = self._sink_resolver.register(sink)
        self._sink_handles[id(sink)] = handle
        await self._maybe_bootstrap_plan()

    async def _handle_disconnect(self, sink: "asyncio.Queue[ServerMessage | None]") -> None:
        """WebSocketServer on_disconnect hook — unregister sink and close VoiceSession."""
        session = self._voice_sessions.pop(id(sink), None)
        if session is not None:
            try:
                await session.close()
            except Exception:
                logger.exception("voice session close raised")
        handle = self._sink_handles.pop(id(sink), None)
        if handle is not None:
            self._sink_resolver.unregister(handle)

    async def _maybe_bootstrap_plan(self) -> None:
        """Fire Awoke/bootstrap perception if today has no schedule yet.

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
        # Push a ScheduledMoment perception for daily plan bootstrap.
        # MindLoop drains it; Doll decides to call WriteSchedule.
        self._perception_queue.put(
            Perception(
                kind="ScheduledMoment",
                t=time.time(),
                data={"intent": "今天還沒有計劃，請呼叫 WriteSchedule 安排今天的行程。"},
            )
        )

    async def _diary_scheduler(self) -> None:
        """Background task: fires DiaryEvent perception daily at DIARY_HOUR:DIARY_MINUTE."""
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
            self._perception_queue.put(
                Perception(
                    kind="ScheduledMoment",
                    t=time.time(),
                    data={"intent": "現在是 23:00，請寫今天的日記（呼叫 WriteDiary）。"},
                )
            )

    async def _schedule_runner(self) -> None:
        """Background task: every 30s, fire any due scheduled perceptions.

        Reads ``data/memory/schedule/{today}.toml``, finds entries within a
        1-minute window of now (per ``due_entries``), and pushes a
        ``ScheduledMoment`` Perception for each.
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
                self._perception_queue.put(
                    Perception(
                        kind="ScheduledMoment",
                        t=time.time(),
                        data={
                            "entry_time": entry.time.isoformat(),
                            "intent": entry.intent,
                        },
                    )
                )

    async def run(self) -> None:
        await self.memsearch.index()
        try:
            await self.server.start()

            # Start MindLoop as primary consciousness task
            self._mind_task = asyncio.create_task(
                self._mind_loop.run(), name="mind-loop"
            )

            # Push Awoke perception on startup
            reason = "cold_start" if self._mind_state.iter_count == 0 else "resumed"
            self._perception_queue.put(
                Perception(
                    kind="Awoke",
                    t=time.time(),
                    data={"reason": reason},
                )
            )

            # Self pulse poller — proprioception of the host
            self.system_pulse.start()

            # Start diary scheduler, schedule runner, and reflection observer
            self._scheduler_task = asyncio.create_task(self._diary_scheduler())
            self._schedule_task = asyncio.create_task(self._schedule_runner())
            self._reflection_task = asyncio.create_task(
                self._reflection_observer.run(), name="reflection-observer"
            )

            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._shutdown.set)
            try:
                await self._shutdown.wait()
            finally:
                await self.server.stop()
                # Cancel scheduler before mind_loop so any in-flight
                # perceptions settle
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
                if self._reflection_task is not None:
                    self._reflection_observer.shutdown()
                    self._reflection_task.cancel()
                    await asyncio.gather(
                        self._reflection_task, return_exceptions=True
                    )
                # Stop runners before MindLoop so result perceptions
                # don't arrive after loop shuts down
                await self.subagent_runner.stop()
                await self.shell_runner.stop()
                await self.monitor_runner.stop()
                await self.system_pulse.stop()
                # Shutdown MindLoop
                if self._mind_task is not None:
                    self._mind_loop.shutdown()
                    await asyncio.gather(self._mind_task, return_exceptions=True)
                self._tool_output_store.cleanup()
        finally:
            pass   # memsearch has no close(); Milvus Lite is file-based
