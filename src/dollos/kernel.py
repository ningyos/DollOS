"""DollOS kernel: wires LLM adapter, memsearch, IPC server, and MindLoop together."""

import asyncio
import logging
import signal
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from dollos.cascade_log import CascadeLogger
from dollos.character import DollPack
from dollos.config import Settings
from dollos.ipc.messages import (
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
from dollos.llm.adapter import LLMAdapter
from dollos.llm.composed import ComposedLLMAdapter
from dollos.llm.templates import Qwen3ThinkingTemplate
from dollos.llm.transport import LlamaCppProvider
from dollos.logging_config import configure_cascade_logging
from dollos.memory import FtsMemory
from dollos.mind.consolidation import ConsolidationTrigger
from dollos.mind.evolution_trigger import EvolutionTrigger
from dollos.mind.mind_ctx import MindCtx
from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_state import Perception, load_state
from dollos.mind.perception_queue import PerceptionQueue
from dollos.mind.reflection_observer import ReflectionObserver
from dollos.mind.sink_resolver import SinkResolver
from dollos.monitor_runner import MonitorRunner
from dollos.perception.cognition import CognitionWorker
from dollos.perception.system_pulse import SystemPulse
from dollos.prompts import PromptRenderer
from dollos.schedule import due_entries, load_schedule
from dollos.shell_runner import ShellRunner
from dollos.telemetry.llm_calls import TelemetryRecorder
from dollos.tool_outputs import ToolOutputStore
from dollos.tools import MAIN_TOOLS
from dollos.voice.engines import ASR_REGISTRY, TTS_REGISTRY, ASREngine, TTSEngine
from dollos.voice.pack import load_voice_config, resolve_voice_kwargs
from dollos.voice.session import VoiceSession
from dollos.voice.sink import TTSObservingSink
from dollos.wal.perception_log import PerceptionWAL
from dollos.wal.pidfile import PidFile, RestartKind
from dollos.workflow import WorkflowRunner

logger = logging.getLogger(__name__)

# Sentinel for the three-piece system-prompt seam (spec §3.1). Rendered into
# scaffolding.jinja's seam line, then split out — chosen so it can never occur
# in natural prose or pack content.
_CURRENT_SELF_SEAM = "\x00\x00DOLLOS_CURRENT_SELF_SEAM\x00\x00"


def split_scaffolding(renderer, **ctx) -> tuple[str, str]:
    """Render scaffolding with the seam sentinel and split into (prefix,
    suffix). ``prefix + suffix`` is byte-identical to a seam-less render, so a
    run with no sanctioned ``current_self`` reproduces today's prompt exactly
    (spec §3.1)."""
    rendered = renderer.render("scaffolding", current_self_seam=_CURRENT_SELF_SEAM, **ctx)
    prefix, _, suffix = rendered.partition(_CURRENT_SELF_SEAM)
    return prefix, suffix


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
            max_concurrency=settings.llm.max_concurrency,
        )
    raise ValueError(f"unknown provider: {settings.llm.provider}")


def _build_template(settings: Settings) -> Qwen3ThinkingTemplate:
    if settings.llm.template == "qwen3-thinking":
        return Qwen3ThinkingTemplate()
    raise ValueError(f"unknown template: {settings.llm.template}")


def build_memsearch(settings: Settings) -> FtsMemory:
    """Construct the embedder-free FtsMemory rooted at data.root / memory.

    Indexes shared/, transcripts/, and skills/ markdown into an FTS5 + jieba
    index at data.root/memory/fts.db (derived, disposable).

    skills/ holds skill entry files (frontmatter + short description); they ARE indexed
    so RECALL surfaces them. skill_bodies/ holds full skill instructions and is NOT
    indexed — it is loaded on demand by the InvokeSkill tool. skill_bodies/ is also
    NOT auto-created at startup; Doll creates it lazily via Shell when she writes
    a new skill body.
    """
    memory_root = settings.data.root / "memory"
    shared_path = memory_root / "shared"
    transcripts_path = memory_root / "transcripts"
    skills_path = memory_root / "skills"
    shared_path.mkdir(parents=True, exist_ok=True)
    transcripts_path.mkdir(parents=True, exist_ok=True)
    skills_path.mkdir(parents=True, exist_ok=True)
    return FtsMemory(
        paths=[
            str(shared_path),
            str(transcripts_path),
            str(skills_path),
        ],
        db_path=memory_root / "fts.db",
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
    """Thin adapter wrapping LLMAdapter for the MindLoop streaming cascade.

    MindLoop expects an object exposing both:
      - `stream_completion(system, user, prefill, …)` — pass 1 (single prompt);
      - `stream_messages(system, messages, …)` — pass ≥ 2 (the in-turn sync-tool
        re-feed cascade, spec §7.1), preserving the
        user → assistant(think+tool_call) → user(<tool_response>) → assistant
        alternation via the template's `render_messages`.

    Both yield chunks with `.text` and `.done`; this is a transparent
    pass-through to the underlying `LLMAdapter`.
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
        purpose: str = "cascade",
    ):
        async for chunk in self._adapter.stream_completion(
            system=system,
            user=user,
            prefill=prefill,
            max_tokens=max_tokens,
            grammar=grammar,
            purpose=purpose,
        ):
            yield chunk

    async def stream_messages(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 1024,
        grammar: str | None = None,
        purpose: str = "cascade",
    ):
        async for chunk in self._adapter.stream_messages(
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            grammar=grammar,
            purpose=purpose,
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
        wal_path = settings.data.root / "wal" / "perceptions.jsonl"
        self._wal = PerceptionWAL(wal_path)
        self._pidfile = PidFile(settings.data.root / "daemon.pid")
        self._restart_kind: RestartKind = RestartKind.COLD  # default, updated in run()
        self._perception_queue = PerceptionQueue(wal=self._wal)
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
        self.workflow_runner = WorkflowRunner(
            adapter=self.adapter,
            renderer=self.renderer,
            memory_root=settings.data.root / "memory",
            memsearch=self.memsearch,
            transcripts_root=settings.data.root / "memory" / "transcripts",
            perception_queue=self._perception_queue,
            shell_runner=self.shell_runner,
            monitor_runner=self.monitor_runner,
            tool_output_store=self._tool_output_store,
            cascade_logger=self._cascade_logger,
        )

        self._mind_ctx = MindCtx(
            mind_state=self._mind_state,
            memsearch=self.memsearch,
            memory_root=settings.data.root / "memory",
            transcripts_root=settings.data.root / "memory" / "transcripts",
            sink_resolver=self._sink_resolver,
            tool_output_store=self._tool_output_store,
            shell_runner=self.shell_runner,
            workflow_runner=self.workflow_runner,
            monitor_runner=self.monitor_runner,
            self_profile_max_chars=settings.self_profile.max_chars,
            evolution_enabled=settings.evolution.enabled,
            current_self_min_chars=settings.evolution.current_self_min_chars,
            current_self_max_chars=settings.evolution.current_self_max_chars,
            enforcement=self._doll_pack.enforcement,
        )

        # Render the static system prompt from the character pack
        skills_dir = settings.data.root / "memory" / "skills"
        if skills_dir.exists():
            available_skills = sorted(p.stem for p in skills_dir.glob("*.md"))
        else:
            available_skills = []
        tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}

        system_prompt_prefix, system_prompt_suffix = split_scaffolding(
            self.renderer,
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
            system_prompt=system_prompt_prefix,
            system_prompt_suffix=system_prompt_suffix,
            state_persist_path=settings.data.root / "mind_state.json",
            tool_registry=tool_registry,
            system_pulse=self.system_pulse,
            cognition=self.cognition,
            wal=self._wal,
            primary_language=settings.memory.primary_language,
            cascade_logger=self._cascade_logger,
            energy_enabled=settings.energy.enabled,
            cost_per_turn=settings.energy.cost_per_turn,
            self_profile_enabled=settings.self_profile.enabled,
            enforcement=self._doll_pack.enforcement,
            evolution_enabled=settings.evolution.enabled,
            current_self_min_chars=settings.evolution.current_self_min_chars,
            current_self_max_chars=settings.evolution.current_self_max_chars,
            pending_max_surfacings=settings.evolution.pending_max_surfacings,
            pending_min_age_days=settings.evolution.pending_min_age_days,
        )

        self._reflection_observer = ReflectionObserver(
            state=self._mind_state,
            queue=self._perception_queue,
        )

        # ConsolidationTrigger — sleep-time memory consolidation (B2)
        self._consolidation_trigger = ConsolidationTrigger(
            state=self._mind_state,
            persist_path=settings.data.root / "mind_state.json",
            adapter=self.adapter,
            renderer=self.renderer,
            memsearch=self.memsearch,
            memory_root=settings.data.root / "memory",
            transcripts_root=settings.data.root / "memory" / "transcripts",
            tool_output_store=self._tool_output_store,
            consolidated_dir=settings.data.root / "memory" / "shared" / "consolidated",
            system_pulse=self.system_pulse,
            idle_threshold_s=settings.consolidation.idle_threshold_s,
            min_interval_s=settings.consolidation.min_interval_s,
            max_tokens=settings.consolidation.max_tokens,
            agent_timeout_s=settings.consolidation.agent_timeout_s,
            transcript_tail_chars=settings.consolidation.transcript_tail_chars,
            energy_enabled=settings.energy.enabled,
            restore_per_tick=settings.energy.restore_per_tick,
            energy_idle_threshold_s=settings.energy.idle_threshold_s,
            energy_restore_debounce_s=settings.energy.restore_debounce_s,
        )

        # EvolutionTrigger — 慢變演化 Mode-B verdict-only re-verdict (spec §3.3)
        self._evolution_trigger = EvolutionTrigger(
            state=self._mind_state,
            adapter=self.adapter,
            renderer=self.renderer,
            memsearch=self.memsearch,
            memory_root=settings.data.root / "memory",
            transcripts_root=settings.data.root / "memory" / "transcripts",
            tool_output_store=self._tool_output_store,
            pack_identity=self._doll_pack.identity,
            consolidation_trigger=self._consolidation_trigger,
            idle_threshold_s=settings.evolution.idle_threshold_s,
        )
        self._evolution_trigger_task: asyncio.Task[None] | None = None

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
        self._consolidation_trigger_task: asyncio.Task[None] | None = None
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
            # Cancel any in-flight consolidation keeper so it yields the
            # LLM semaphore slot before Doll's response cascade starts.
            self._cancel_consolidation()
            self._cancel_evolution()
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

    def _cancel_consolidation(self) -> None:
        """Cancel any in-flight consolidation keeper task when the user speaks.

        Called at both UserSpoke ingress points (text + voice) so an active
        memory-keeper agent yields its semaphore slot immediately rather than
        competing with Doll's response cascade.
        """
        trig = getattr(self, "_consolidation_trigger", None)
        if trig is not None:
            trig.cancel_current()

    def _cancel_evolution(self) -> None:
        """Cancel any in-flight evolution re-verdict task when the user speaks.

        Mirrors ``_cancel_consolidation`` — called at both UserSpoke ingress
        points (text + voice) so an active Mode-B skeptic agent yields its
        semaphore slot immediately rather than competing with Doll's response
        cascade.
        """
        trig = getattr(self, "_evolution_trigger", None)
        if trig is not None:
            trig.cancel_current()

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
            # Cancel any in-flight consolidation keeper (M3: voice ingress).
            self._cancel_consolidation()
            self._cancel_evolution()
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
            # Prune stale date keys to prevent unbounded growth over months.
            stale_fired = [d for d in self._fired_today if d < today]
            for d in stale_fired:
                del self._fired_today[d]
            self._bootstrapped_dates = {d for d in self._bootstrapped_dates if d >= today}

            path = (
                self.settings.data.root
                / "memory"
                / "schedule"
                / f"{today:%Y-%m-%d}.toml"
            )
            try:
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
            except Exception:
                logger.warning(
                    "_schedule_runner: error loading/processing schedule for %s",
                    today,
                    exc_info=True,
                )
                continue

    async def _replay_wal(self) -> None:
        """Push any pending WAL perceptions back into the queue before mind_loop starts.

        Perceptions arriving here have seq already set (from the previous run's
        WAL); PerceptionQueue.put() will skip re-appending. They land in the
        in-memory queue and mind_loop drains them in normal order.
        """
        pending = list(self._wal.iter_pending())
        if not pending:
            return
        logger.info(
            "wal: replaying %d pending perceptions from previous run",
            len(pending),
        )
        for seq, p in pending:
            p.seq = seq  # ensure seq is set so put() skips append
            self._perception_queue.put(p)

    async def run(self) -> None:
        self._restart_kind = self._pidfile.acquire()
        if self._restart_kind == RestartKind.DIRTY:
            logger.warning("dirty restart detected — previous daemon crashed")
        await self.memsearch.index()
        try:
            await self.server.start()

            # Replay any pending WAL entries from a previous (possibly crashed) run
            await self._replay_wal()

            # Start MindLoop as primary consciousness task
            self._mind_task = asyncio.create_task(
                self._mind_loop.run(), name="mind-loop"
            )

            # Push Awoke perception on startup
            if self._restart_kind == RestartKind.DIRTY:
                reason = "recovered"
            elif self._mind_state.iter_count > 0:
                reason = "resumed"
            else:
                reason = "cold_start"
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
            if self.settings.consolidation.enabled:
                self._consolidation_trigger_task = asyncio.create_task(
                    self._consolidation_trigger.run(), name="consolidation-trigger"
                )
            if self.settings.evolution.enabled:
                self._evolution_trigger_task = asyncio.create_task(
                    self._evolution_trigger.run(), name="evolution-trigger"
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
                await self.workflow_runner.stop()
                await self.shell_runner.stop()
                await self.monitor_runner.stop()
                await self.system_pulse.stop()
                # Stop consolidation trigger + in-flight keeper BEFORE memsearch.close()
                # (spec §10, R2 M4): an in-flight keeper agent uses memsearch; closing
                # sqlite while it runs causes a crash.  We must await BOTH the poll
                # task (_consolidation_trigger_task) AND the keeper task
                # (trigger.current_task) — gathering only the poll task is not enough.
                _ct = getattr(self, "_consolidation_trigger", None)
                if _ct is not None:
                    _ct.shutdown()  # sets _shutdown + cancels current_task
                    _current_keeper = _ct.current_task  # grab before event loop clears it
                else:
                    _current_keeper = None
                if self._consolidation_trigger_task is not None:
                    self._consolidation_trigger_task.cancel()
                _consolidation_tasks = [
                    t for t in [self._consolidation_trigger_task, _current_keeper]
                    if t is not None
                ]
                if _consolidation_tasks:
                    await asyncio.gather(*_consolidation_tasks, return_exceptions=True)
                # Stop evolution trigger + in-flight skeptic BEFORE memsearch.close()
                # (mirrors the consolidation teardown above, same reasoning: an
                # in-flight skeptic agent uses memsearch; closing sqlite while it
                # runs causes a crash). Await BOTH the poll task
                # (_evolution_trigger_task) AND the skeptic task (trigger.current_task).
                _et = getattr(self, "_evolution_trigger", None)
                if _et is not None:
                    _et.shutdown()  # sets _shutdown + cancels current_task
                    _current_skeptic = _et.current_task  # grab before event loop clears it
                else:
                    _current_skeptic = None
                if self._evolution_trigger_task is not None:
                    self._evolution_trigger_task.cancel()
                _evolution_tasks = [
                    t for t in [self._evolution_trigger_task, _current_skeptic]
                    if t is not None
                ]
                if _evolution_tasks:
                    await asyncio.gather(*_evolution_tasks, return_exceptions=True)
                # Shutdown MindLoop
                if self._mind_task is not None:
                    self._mind_loop.shutdown()
                    await asyncio.gather(self._mind_task, return_exceptions=True)
                self._tool_output_store.cleanup()
                self.memsearch.close()
        finally:
            self._pidfile.release()
