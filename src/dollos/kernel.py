"""DollOS kernel: wires LLM adapter, memsearch, IPC server, and MindLoop together."""

import asyncio
import logging
import signal
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from dollos.cascade_log import CascadeLogger
from dollos.character import DollPack
from dollos.config import Settings
from dollos.ipc.batch_accumulator import BatchAccumulator
from dollos.ipc.channel_registry import ChannelRegistry
from dollos.ipc.messages import (
    ChannelEvent,
    ChannelRegister,
    ICECandidateIn,
    Interrupt,
    QueryRecent,
    QueryResult,
    QueryState,
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
from dollos.llm.grammar_probe import assert_bounded_repetition_supported
from dollos.llm.templates import Qwen3ThinkingTemplate
from dollos.llm.transport import LlamaCppProvider
from dollos.logging_config import configure_cascade_logging
from dollos.memory import FtsMemory
from dollos.mind.agenda_observer import AgendaObserver
from dollos.mind.attention import AttentionGate
from dollos.mind.consolidation import ConsolidationTrigger
from dollos.mind.evolution_trigger import EvolutionTrigger
from dollos.mind.mind_ctx import MindCtx
from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_state import Perception, load_state
from dollos.mind.name_aliases import NameAliasStore, passes_alias_guard
from dollos.mind.perception_queue import PerceptionQueue
from dollos.mind.pulse_observer import PulseObserver
from dollos.mind.reflection_observer import ReflectionObserver
from dollos.mind.sink_resolver import SinkResolver
from dollos.mind.trace import TraceWriter
from dollos.monitor_runner import MonitorRunner
from dollos.perception.cognition import CognitionWorker
from dollos.perception.system_pulse import SystemPulse
from dollos.prompts import PromptRenderer
from dollos.schedule import due_entries, load_schedule
from dollos.service_supervisor import _RETENTION_DAYS, ServiceSpec, ServiceSupervisor
from dollos.shell_runner import ShellRunner
from dollos.telemetry.llm_calls import TelemetryRecorder
from dollos.telemetry.turn_latency import TurnLatencyRecorder
from dollos.telemetry.vitals import VitalsRecorder
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

    Indexes shared/, transcripts/, skills/, external_public/, and
    external_dm/ markdown into an FTS5 + jieba index at
    data.root/memory/fts.db (derived, disposable).

    skills/ holds skill entry files (frontmatter + short description); they ARE indexed
    so RECALL surfaces them. skill_bodies/ holds full skill instructions and is NOT
    indexed — it is loaded on demand by the InvokeSkill tool. skill_bodies/ is also
    NOT auto-created at startup; Doll creates it lazily via Shell when she writes
    a new skill body.

    Whole-branch review I3: external_public/ and external_dm/ (P1e Task 3)
    are indexed here too — NoteMemory/WriteDiary index each file individually
    on write, but a FULL reindex (this function, called from ``index()``)
    previously walked only [shared, transcripts, skills], so external-tier
    memory would silently vanish from the FTS index on the next full
    reindex. Safe to add BECAUSE C2 turned external_public retrieval into a
    fail-closed ALLOWLIST (source_prefix=external_public/ only) — indexing
    external_dm/ here can no longer leak to a stranger's turn regardless of
    reindex timing.
    """
    memory_root = settings.data.root / "memory"
    shared_path = memory_root / "shared"
    transcripts_path = memory_root / "transcripts"
    skills_path = memory_root / "skills"
    external_public_path = memory_root / "external_public"
    external_dm_path = memory_root / "external_dm"
    shared_path.mkdir(parents=True, exist_ok=True)
    transcripts_path.mkdir(parents=True, exist_ok=True)
    skills_path.mkdir(parents=True, exist_ok=True)
    external_public_path.mkdir(parents=True, exist_ok=True)
    external_dm_path.mkdir(parents=True, exist_ok=True)
    return FtsMemory(
        paths=[
            str(shared_path),
            str(transcripts_path),
            str(skills_path),
            str(external_public_path),
            str(external_dm_path),
        ],
        db_path=memory_root / "fts.db",
    )


def build_alias_provider(
    settings: Settings, doll_pack: DollPack,
) -> Callable[[], frozenset[str]]:
    """Build the ``alias_provider`` closure ``AttentionGate`` calls at L0
    match time (2026-07-06 self-learned-aliases spec §3.5, A3 — the task
    that closes the loop from ``LearnName`` writes back to name-wake).

    Unions three sources, each passed through the SAME mechanical guard
    (``passes_alias_guard`` — min-length + denylist, spec I3) that A2's
    ``LearnName`` applies at write time:

    1. Pack seed — ``doll_pack.meta.name`` + ``doll_pack.meta.aliases``
       (D1b). Permanent floor; never persisted, never prunable.
    2. Config floor — ``settings.attention.name_aliases`` (optional admin
       static floor; may be empty).
    3. Learned tokens — ``NameAliasStore.active_tokens()`` from
       ``{memory_root}/name_aliases.json``, what owner-taught ``LearnName``
       calls write.

    Seeds (1) + floor (2) are constant for the process lifetime — computed
    once, below. A seed/floor token that fails the guard (too short or
    denylisted, e.g. a pack whose ``meta.name`` is a single CJK character)
    is DROPPED and a warning logged — an honest boundary (spec §3.4): that
    pack's name-wake silently won't fire, but a dangerous "unprunable" seed
    can never become an unprunable wake-anything landmine.

    Learned tokens (3) are mtime-gated: only re-read
    ``name_aliases.json`` when its ``st_mtime_ns`` changes (nanosecond
    resolution — a plain ``st_mtime`` has only ~1s resolution on some
    filesystems, so two writes in the same tick could otherwise miss
    invalidation) (owner writes are rare), to avoid a disk read on every
    single message. The whole check is fail-closed (M1): if ``stat()``
    raises, the closure returns the LAST-GOOD frozenset it built (or just
    the seed+floor set, if it never successfully read the file) — it
    never raises into ``_l0_signal`` and never widens the wake-eligible
    set on error. Two ``OSError`` cases are distinguished (Part A
    whole-branch review, Minor): ``FileNotFoundError`` (the file simply
    hasn't been created yet — the DEFAULT state for a fresh install until
    the owner first teaches a nickname via ``LearnName``) is expected and
    silent (DEBUG at most) rather than a WARNING, since it would otherwise
    spam a WARNING on every qualifying message on the L0 hot path for the
    entire lifetime of an install with no learned aliases. Any OTHER
    ``OSError`` (permissions, transient I/O) is a genuine anomaly and still
    logs a WARNING.
    """
    memory_root = settings.data.root / "memory"
    alias_path = memory_root / "name_aliases.json"
    store = NameAliasStore(alias_path)

    raw_seed_floor = {
        doll_pack.meta.name,
        *doll_pack.meta.aliases,
        *settings.attention.name_aliases,
    }
    guarded_seed_floor: set[str] = set()
    for token in raw_seed_floor:
        if passes_alias_guard(token):
            guarded_seed_floor.add(token)
        else:
            logger.warning(
                "alias_provider: seed/floor token %r failed the mechanical "
                "guard (too short or denylisted) and was dropped from the "
                "wake-eligible set — that name/alias will not wake her "
                "(spec I3)", token,
            )
    seed_floor: frozenset[str] = frozenset(guarded_seed_floor)

    # mtime-gated cache. ``tokens`` starts as just the seed floor so a
    # stat() failure on the very first call (e.g. name_aliases.json doesn't
    # exist yet, cold start) still returns a sane fail-closed set rather
    # than an empty one.
    cache: dict[str, object] = {"mtime": None, "tokens": seed_floor}

    def _provider() -> frozenset[str]:
        try:
            mtime_ns = alias_path.stat().st_mtime_ns
            if mtime_ns != cache["mtime"]:
                learned = frozenset(
                    t for t in store.active_tokens() if passes_alias_guard(t)
                )
                cache["mtime"] = mtime_ns
                cache["tokens"] = seed_floor | learned
        except FileNotFoundError:
            # Expected, long-lived state for a fresh install/early dogfood:
            # the owner hasn't taught a nickname yet, so name_aliases.json
            # was never created. Silent (DEBUG) — NOT a WARNING, since this
            # is the DEFAULT state, not an anomaly, and would otherwise spam
            # the log on every qualifying public message on the L0 hot path.
            # Still fail-closed: falls through to cache["tokens"] (just the
            # seed+floor set, on the very first call).
            logger.debug(
                "alias_provider: name_aliases.json does not exist yet "
                "(no nickname taught) — using seed/floor wake-eligible set",
            )
        except OSError:
            logger.warning(
                "alias_provider: name_aliases.json stat failed — falling "
                "back to the last-good wake-eligible set (fail-closed, "
                "never widens)",
            )
        return cache["tokens"]  # type: ignore[return-value]

    return _provider


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
        on_usage: Callable[[int | None, int | None], None] | None = None,
    ):
        async for chunk in self._adapter.stream_completion(
            system=system,
            user=user,
            prefill=prefill,
            max_tokens=max_tokens,
            grammar=grammar,
            purpose=purpose,
            on_usage=on_usage,
        ):
            yield chunk

    async def stream_messages(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 1024,
        grammar: str | None = None,
        purpose: str = "cascade",
        on_usage: Callable[[int | None, int | None], None] | None = None,
    ):
        async for chunk in self._adapter.stream_messages(
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            grammar=grammar,
            purpose=purpose,
            on_usage=on_usage,
        ):
            yield chunk


def _derive_daemon_ws(ipc) -> str:
    """Derive the bridge's `--daemon` WS URL from the daemon's own IPC bind
    settings (svc-internalization spec §3.1). A wildcard bind address
    (0.0.0.0 / :: / "") isn't connectable from a sibling process — rewrite
    to loopback, since the bridge always runs on the same host as the daemon
    (subprocess, not remote)."""
    host = ipc.host
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):  # bare IPv6 literal (e.g. "::1")
        host = f"[{host}]"                        # bracket it — ws://::1:PORT is unparseable
    return f"ws://{host}:{ipc.port}"


def _perception_text(p) -> str:
    """Best-effort text extractor for a Perception's data dict, for the
    QueryRecent debug read surface (P2 Task 1). ChannelMessage bodies key
    their text as ``content`` (mind_prompt.py:432,441); ``text`` is a
    fallback for other kinds that happen to carry it."""
    d = p.data or {}
    return str(d.get("content") or d.get("text") or "")


class DollOS:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Telemetry recorder feeds both the LLM provider (recording side)
        # and the CognitionWorker (reading side).
        telemetry_dir = settings.data.root / settings.cognition.telemetry_dir_subpath
        self.telemetry_recorder = TelemetryRecorder(telemetry_dir)
        # 延遲壓縮 Part 1 Task 2: turn-level think/speak/first-speak latency
        # telemetry, same directory as the per-call TelemetryRecorder above —
        # daily-rotated JSONL with a distinct filename prefix
        # (turn_latency-*.jsonl vs llm_calls-*.jsonl), so both recorders
        # share one dir with zero collision.
        self.turn_latency_recorder = TurnLatencyRecorder(telemetry_dir)
        # 代謝 vital (spec 2026-07-10 §2.2, Task 3): per-turn RL substrate,
        # separate dir/prefix from both recorders above (vitals-*.jsonl under
        # data/traces/vitals, alongside the finetune trace writer's own
        # data/traces root — no filename collision, distinct subdir).
        self.vitals_recorder = VitalsRecorder(settings.data.root / "traces" / "vitals")
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
        self._channel_registry = ChannelRegistry()
        # AttentionGate (P1c Task 4): admits/drops ChannelEvent-originated
        # ChannelMessage perceptions BEFORE they reach the queue — default
        # silence for a non-admitted message (dropped, not enqueued) is the
        # anti-亂回 gate. Flow-agnostic: kernel is the only caller, wired via
        # note_reply through MindLoop's on_turn_complete callback below (no
        # mind_loop -> attention import).
        #
        # alias_provider (2026-07-06 self-learned-aliases spec §3.5, A3):
        # replaces the frozen `name_aliases=` list — the closure unions pack
        # seed + config floor + learned tokens from name_aliases.json at
        # match time, so a nickname the owner teaches via LearnName becomes
        # wake-eligible without a daemon restart.
        self._attention = AttentionGate(
            alias_provider=build_alias_provider(settings, self._doll_pack),
            always_wake_channels=settings.attention.always_wake_channels,
            owner_id=settings.attention.owner_id,
            max_session_turns=settings.attention.max_session_turns,
            window_base_s=settings.attention.window_base_s,
            window_decay=settings.attention.window_decay,
            debounce_engaged_s=settings.attention.debounce_engaged_s,
            debounce_cold_s=settings.attention.debounce_cold_s,
        )
        # BatchAccumulator (P1c Task 5): differentiated debounce between
        # admission and enqueue — an admitted ChannelEvent doesn't become a
        # perception immediately, it joins this channel's open batch (or
        # starts one) and is enqueued when the window fires. The
        # constructor's window_s is only a fallback for a call that omits
        # window_s; every real call site below supplies an explicit
        # per-call window via AttentionGate.window_for, so this fallback is
        # never actually exercised.
        self._accumulator = BatchAccumulator(
            enqueue=self._enqueue_channel_batch,
            window_s=settings.attention.debounce_cold_s,
        )

        self.shell_runner = ShellRunner(
            cwd=settings.data.root,
            perception_queue=self._perception_queue,
            tool_output_store=self._tool_output_store,
        )
        self.monitor_runner = MonitorRunner(
            cwd=settings.data.root,
            perception_queue=self._perception_queue,
        )

        # ServiceSupervisor (svc-internalization Task C): OS-level watchdog
        # for long-lived supervised services. v1 registers a single service
        # (discord-bridge) only when opted in AND its config file exists —
        # a missing config file with enabled=true is a misconfiguration, not
        # silently ignored (logged as an error, not registered).
        self.service_supervisor = ServiceSupervisor()
        if settings.bridge.enabled:
            if settings.bridge.config is None or not settings.bridge.config.exists():
                logger.error(
                    "bridge enabled but config missing (%s) — not registering",
                    settings.bridge.config,
                )
            else:
                self.service_supervisor.register(self._build_bridge_spec(settings))
        if settings.mcp.enabled:
            if settings.mcp.config is None or not settings.mcp.config.exists():
                logger.error(
                    "mcp enabled but config missing (%s) — not registering",
                    settings.mcp.config,
                )
            else:
                self.service_supervisor.register(self._build_mcp_spec(settings))

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
            channel_registry=self._channel_registry,
            tool_output_store=self._tool_output_store,
            shell_runner=self.shell_runner,
            workflow_runner=self.workflow_runner,
            monitor_runner=self.monitor_runner,
            self_profile_max_chars=settings.self_profile.max_chars,
            evolution_enabled=settings.evolution.enabled,
            current_self_min_chars=settings.evolution.current_self_min_chars,
            current_self_max_chars=settings.evolution.current_self_max_chars,
            enforcement=self._doll_pack.enforcement,
            evolution_base_interval_days=settings.evolution.base_interval_days,
            evolution_max_interval_days=settings.evolution.max_interval_days,
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
            evolution_enabled=settings.evolution.enabled,
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

        # P1f trace — finetune-corpus writer, gated on config (紀錄=訓練資料,
        # enabled by default). No fallback: disabled just means trace_writer
        # stays None and MindLoop behaves exactly as before this wiring.
        trace_writer = None
        if settings.trace.enabled:
            trace_writer = TraceWriter(Path(settings.trace.root))

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
            token_per_energy_unit=settings.energy.token_per_energy_unit,
            thermal_multiplier_warm=settings.energy.thermal_multiplier_warm,
            thermal_multiplier_hot=settings.energy.thermal_multiplier_hot,
            self_profile_enabled=settings.self_profile.enabled,
            enforcement=self._doll_pack.enforcement,
            evolution_enabled=settings.evolution.enabled,
            current_self_min_chars=settings.evolution.current_self_min_chars,
            current_self_max_chars=settings.evolution.current_self_max_chars,
            pending_max_surfacings=settings.evolution.pending_max_surfacings,
            pending_min_age_days=settings.evolution.pending_min_age_days,
            trace_writer=trace_writer,
            model_id=settings.llm.model_alias,
            on_turn_complete=self._on_turn_complete,
            diary_max_log_chars=settings.diary.max_log_chars,
            turn_latency_recorder=self.turn_latency_recorder,
            vitals_recorder=self.vitals_recorder,
        )

        self._reflection_observer = ReflectionObserver(
            state=self._mind_state,
            queue=self._perception_queue,
        )

        # AgendaObserver — self-directed agenda self-wake (spec 2026-07-07
        # §4.1), mirrors ReflectionObserver above.
        self._agenda_observer = AgendaObserver(
            state=self._mind_state,
            queue=self._perception_queue,
            energy_idle_threshold_s=settings.energy.idle_threshold_s,
        )

        # PulseObserver — proactive host-vitals wake (spec 2026-07-09 §7),
        # mirrors AgendaObserver. Shares the perception queue; policy lives in
        # evaluate_alerts. Gated on start() by system_pulse.alerts_enabled.
        self._pulse_observer = PulseObserver(
            system_pulse=self.system_pulse,
            queue=self._perception_queue,
            throttle_s=settings.system_pulse.alert_throttle_s,
            window_stuck_s=settings.system_pulse.window_stuck_s,
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

        # EvolutionTrigger — 慢變演化 Mode-A keeper + Mode-B verdict-only
        # re-verdict (spec §3.3)
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
            persist_path=settings.data.root / "mind_state.json",
            idle_threshold_s=settings.evolution.idle_threshold_s,
            base_interval_days=settings.evolution.base_interval_days,
            max_interval_days=settings.evolution.max_interval_days,
            min_history_events=settings.evolution.min_history_events,
            min_diary_days=settings.evolution.min_diary_days,
            enforcement=self._doll_pack.enforcement,
            floor=settings.evolution.current_self_min_chars,
            cap=settings.evolution.current_self_max_chars,
            max_tokens=settings.evolution.max_tokens,
            agent_timeout_s=settings.evolution.agent_timeout_s,
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
        # Maps id(sink) → {channel_id: sink_handle} for external ChannelRegister
        # registrations made on this connection, so disconnect can unregister
        # both the SinkResolver handle and the ChannelRegistry entry (carry I-1).
        self._channel_sink_handles: dict[int, dict[str, int]] = {}
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
        self._agenda_task: asyncio.Task[None] | None = None
        self._pulse_task: asyncio.Task[None] | None = None
        self._consolidation_trigger_task: asyncio.Task[None] | None = None
        # Per-day fired set — scheduler dedupe across its 30s polling.
        self._fired_today: dict[date, set] = {}
        # Track per-day bootstrap so reconnects within a day don't refire.
        self._bootstrapped_dates: set[date] = set()

    def _build_bridge_spec(self, settings: Settings) -> ServiceSpec:
        """Build the discord-bridge's ServiceSpec: argv (already-resolved
        absolute paths) + WS URL derivation (svc-internalization spec §3.1).
        Token never appears here — only the bridge.toml *path* does; the
        bridge process itself reads the token out of that file."""
        argv = (
            sys.executable, "-m", "dollos.discord_bridge",
            "--daemon", _derive_daemon_ws(settings.ipc),
            "--config", str(settings.bridge.config.expanduser().resolve()),
            "--data-root", str(settings.data.root.expanduser().resolve()),
            "--retention-days", str(_RETENTION_DAYS),
        )
        return ServiceSpec(
            name="discord-bridge", argv=argv,
            on_gave_up=self._emit_bridge_down_perception,
        )

    def _emit_bridge_down_perception(self, name: str, rc: int | None) -> None:
        """Terminal event: Discord is entirely offline (crash-loop cap hit).
        Doll can't fix a config typo, but she should perceive that she lost
        an online channel (spec §9-3; virtual-being positioning + weak-model
        soft-mechanism three-facet visibility). `on_gave_up` runs
        synchronously inside the supervise task — only enqueue here, never
        do heavy work."""
        try:
            self._perception_queue.put(Perception(
                kind="BridgeDown",
                t=time.time(),
                data={"service": name, "rc": rc},
            ))
        except Exception:
            logger.exception("failed to emit BridgeDown perception")

    def _build_mcp_spec(self, settings: Settings) -> ServiceSpec:
        """Build the dollos-mcp ServiceSpec: argv (already-resolved absolute
        paths) + WS URL derivation. Mirrors _build_bridge_spec. The mcp.toml
        secret/bind config never appears here — only its *path* does; the
        mcp process reads it itself (same discipline as the bridge token)."""
        argv = (
            sys.executable, "-m", "dollos.mcp_server",
            "--daemon", _derive_daemon_ws(settings.ipc),
            "--config", str(settings.mcp.config.expanduser().resolve()),
            "--data-root", str(settings.data.root.expanduser().resolve()),
        )
        return ServiceSpec(
            name="mcp-server", argv=argv,
            on_gave_up=self._emit_mcp_down_perception,
        )

    def _emit_mcp_down_perception(self, name: str, rc: int | None) -> None:
        """Terminal event: the MCP peer channel is entirely offline (crash-loop
        cap hit). Mirror _emit_bridge_down_perception — synchronous callback
        inside the supervise task, only enqueue, never do heavy work."""
        try:
            self._perception_queue.put(Perception(
                kind="McpDown",
                t=time.time(),
                data={"service": name, "rc": rc},
            ))
        except Exception:
            logger.exception("failed to emit McpDown perception")

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
        elif isinstance(msg, ChannelRegister):
            self._channel_registry.register(
                msg.channel_id, locus=msg.locus, kind=msg.kind
            )
            if msg.locus == "external":
                # carry I-1: the connection's sink must be addressable by
                # this channel_id, registered atomically with the registry
                # entry (dual-register into ChannelRegistry + SinkResolver).
                self._register_external_sink(sink, msg.channel_id)
        elif isinstance(msg, ChannelEvent):
            d = msg.payload
            # Envelope channel_id is authoritative: it is the value that
            # matches the ChannelRegister/SinkResolver handle and drives
            # downstream reply routing (bucket key → origin →
            # SinkResolver(origin) → AddressedText). Spread payload FIRST so
            # a payload channel_id can never overwrite it. Built BEFORE the
            # admission gate so AttentionGate always sees a channel_id, even
            # when the raw bridge payload happens to omit one.
            event = {**d, "channel_id": msg.channel_id}
            now = time.time()
            # P1c Task 5: read the differentiated-debounce window BEFORE
            # admit() runs. admit() always stamps last_activity=now on a
            # successful admit, so window_for()'s is_engaged() check would
            # read "engaged" unconditionally if evaluated afterward — a
            # channel with no session yet (cold) must still get the LONG
            # window for this very message, even though it becomes engaged
            # the instant admission succeeds.
            # Whole-branch review Important #2: a DM or owner message is a
            # direct 1:1 conversation, not cold public chatter — it must get
            # the short engaged window even with no session open yet, so
            # window_for is told is_dm/author_is_owner explicitly rather
            # than relying on is_engaged (which would misclassify it as
            # cold on the very first message).
            window = self._attention.window_for(
                msg.channel_id,
                now,
                is_dm=bool(event.get("is_dm")),
                author_is_owner=bool(event.get("author_is_owner")),
            )
            # P1c Task 4: default silence — a ChannelEvent that isn't admitted
            # is DROPPED here, before it ever becomes a perception. The
            # ambient log (bridge-side) already has the full corpus; the
            # daemon keeps nothing for a dropped message.
            decision = self._attention.admit(event, now)
            if not decision.admit:
                return
            if event.get("author_is_owner"):
                # Owner speaking from an external channel is TextInput-
                # equivalent: preempt/cancel just like UserSpoke ingress.
                # Strangers do NOT preempt/cancel (spec §3.2). Owner is
                # always L0-admitted (DM or mention), so this branch is only
                # reachable post-admission in practice — but the admission
                # gate above is structurally first regardless. This fires
                # immediately, ahead of the debounce below: the debounce is
                # about batching the reply-TRIGGER, not about delaying an
                # interrupt of Doll's current cascade.
                await self._maybe_preempt_for_new_input(sink)
                self._cancel_consolidation()
                self._cancel_evolution()
            # P1c Task 5: debounce — the admitted perception is not enqueued
            # immediately, it joins this channel's BatchAccumulator batch
            # (engaged→short / cold→long window) and is enqueued when that
            # window fires (see _enqueue_channel_batch below).
            await self._accumulator.add(
                msg.channel_id, {"t": now, "event": event}, window
            )
        elif isinstance(msg, (QueryState, QueryRecent)):
            # Debug-only read side-channel (spec §C.3). FAIL-CLOSED: the IPC
            # server has no connection auth, so authorize by a daemon-known
            # token here, BEFORE any state is read.
            expected = self.settings.mcp.query_token
            if not expected or msg.token != expected:
                logger.warning(
                    "rejected %s: missing/invalid query_token", msg.type
                )
                sink.put_nowait(QueryResult(query_id=msg.query_id, ok=False, payload={}))
                return
            if isinstance(msg, QueryState):
                # Synchronous snapshot — no await between read and put_nowait.
                # current_self is NOT a MindState field: it is the ratified
                # prose from self_history.jsonl's latest evo_adopt (mirror
                # mind_loop.py:242-245). sanctioned_text is a sync file read
                # → snapshot-safe (no await).
                from dollos.mind import self_history
                cs = self_history.sanctioned_text(
                    self.settings.data.root / "memory" / "self_history.jsonl"
                ) or ""
                payload = {
                    "mood": self._mind_state.mood.emotion,  # Mood is a dataclass; take the display field, NOT str(Mood)
                    "current_self": cs,
                }
            else:  # QueryRecent
                n = max(0, min(msg.n, 100))  # clamp
                # REUSE the canonical external-safe filter (do NOT reinvent —
                # a security filter with two definitions drifts).
                # _public_safe_perceptions allowlists kind=="ChannelMessage"
                # AND not author_is_owner; a future sensitive kind is
                # excluded by default (fail-closed on the KIND allowlist).
                # recent_outputs is deliberately excluded entirely: OutputRecord
                # has no origin field so it can't be scoped (fail-closed).
                from dollos.mind.mind_prompt import _public_safe_perceptions
                items = [
                    {"kind": p.kind, "text": _perception_text(p)[:500], "ts": p.t}
                    for p in _public_safe_perceptions(list(self._mind_state.recent_perceptions))
                ]
                payload = {"items": (items[-n:] if n else [])}  # n==0 → [] (items[-0:] would return ALL)
            sink.put_nowait(QueryResult(query_id=msg.query_id, ok=True, payload=payload))
            return
        else:
            logger.warning("unhandled message type: %r", type(msg).__name__)

    def _enqueue_channel_batch(self, items: list[dict]) -> None:
        """BatchAccumulator flush callback (P1c Task 5): puts one
        ChannelMessage Perception per debounced item onto the queue,
        preserving each item's original arrival timestamp rather than the
        (later) flush time. A single flush is always one channel's batch —
        the accumulator keys pending items by channel_id and never merges
        two channels into one flush — so this stays per-channel
        single-origin, matching PerceptionQueue.drain_grouped's expectation
        that a batch is one contiguous conversation, not crosstalk."""
        for item in items:
            self._perception_queue.put(
                Perception(kind="ChannelMessage", t=item["t"], data=item["event"])
            )

    def _register_external_sink(
        self, sink: "asyncio.Queue[ServerMessage | None]", channel_id: str
    ) -> None:
        """Carry I-1: dual-register this connection's sink into SinkResolver
        under (locus="external", channel_id) so cascades addressed to this
        channel resolve back to the bridge connection. Tracks the handle so
        disconnect can unregister both this and the ChannelRegistry entry.
        """
        handle = self._sink_resolver.register(
            sink, locus="external", channel_id=channel_id
        )
        self._channel_sink_handles.setdefault(id(sink), {})[channel_id] = handle

    def _on_turn_complete(self, origin: str | None, spoke: bool) -> None:
        """MindLoop's turn-complete hook (P1c Task 4): advances AttentionGate's
        per-channel engagement session AFTER Doll actually speaks.

        ``origin`` is the turn's ``current_origin`` (a Discord channel_id) or
        ``None`` for an internal turn — only a ChannelMessage perception ever
        sets it, so ``origin is not None`` already implies an external-origin
        turn; no separate locus check is needed. ``AttentionGate.note_reply``
        is itself a no-op for a channel_id with no open session, so this is
        safe to call unconditionally whenever both guards pass.
        """
        if spoke and origin is not None:
            self._attention.note_reply(origin, time.time())

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
        # Carry I-1: unregister any external channel registrations this sink
        # made, from both SinkResolver and ChannelRegistry.
        # Whole-branch review (mcp-p1-peer): also reap the AttentionGate
        # Session each such channel_id may have opened. Unlike the other
        # three per-call structures reaped here, the Session was previously
        # NOT reaped on disconnect, so AttentionGate._sessions grew
        # unbounded for the MCP connector's one-shot channel_ids
        # (mcp:<conn>:<call>, unique per talk() call) across the daemon's
        # entire uptime, surviving even connector restarts.
        for channel_id, channel_handle in self._channel_sink_handles.pop(
            id(sink), {}
        ).items():
            self._sink_resolver.unregister(channel_handle)
            self._channel_registry.unregister(channel_id)
            self._attention.forget(channel_id)

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
        """Background task: fires a DiaryMoment perception daily at [diary].hour:[diary].minute."""
        d = self.settings.diary
        while not self._shutdown.is_set():
            now = datetime.now()
            target = now.replace(
                hour=d.hour, minute=d.minute,
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
                    kind="DiaryMoment",
                    t=time.time(),
                    data={},
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
            # Fail-closed startup probe (spec §11 / R1-7): an old llama-server
            # rejects GBNF `{m,n}` bounded repetition (Task 1's think
            # line-length bound depends on it) with a per-request HTTP error,
            # not a Python build-time raise — no-fallback can't catch that.
            # Abort boot here, before the server/IPC comes up, rather than
            # let every conversation turn crash later.
            await assert_bounded_repetition_supported(self.adapter)

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

            # ServiceSupervisor: looks after registered long-lived services
            # (v1 = discord-bridge). Must come after server.start() — the
            # bridge needs to connect to the daemon's WS.
            self.service_supervisor.start()

            # Start diary scheduler, schedule runner, and reflection observer
            if self.settings.diary.enabled:
                self._scheduler_task = asyncio.create_task(self._diary_scheduler())
            self._schedule_task = asyncio.create_task(self._schedule_runner())
            self._reflection_task = asyncio.create_task(
                self._reflection_observer.run(), name="reflection-observer"
            )
            self._agenda_task = asyncio.create_task(
                self._agenda_observer.run(), name="agenda-observer"
            )
            # Proactive pulse alerts (spec §7). Gated separately from the passive
            # poller: alerts_enabled=False keeps today's passive-only behavior.
            if self.settings.system_pulse.alerts_enabled:
                self._pulse_task = asyncio.create_task(
                    self._pulse_observer.run(), name="pulse-observer"
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
                if self._agenda_task is not None:
                    self._agenda_observer.shutdown()
                    self._agenda_task.cancel()
                    await asyncio.gather(
                        self._agenda_task, return_exceptions=True
                    )
                if self._pulse_task is not None:
                    self._pulse_observer.shutdown()
                    self._pulse_task.cancel()
                    await asyncio.gather(
                        self._pulse_task, return_exceptions=True
                    )
                # Stop runners before MindLoop so result perceptions
                # don't arrive after loop shuts down
                await self.workflow_runner.stop()
                await self.shell_runner.stop()
                await self.monitor_runner.stop()
                # Early stop (idempotent): while the daemon is still alive,
                # let the bridge tear down its gateway connection cleanly
                # instead of spending the graceful-shutdown window reconnecting
                # to a daemon that's already going away.
                await self.service_supervisor.stop()
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
                # Flush any ChannelEvent batches still sitting inside their
                # debounce window (P1c Task 5) so a message admitted just
                # before shutdown isn't silently lost — must run BEFORE
                # mind_loop.shutdown() below so the flushed perceptions
                # reach the queue while mind_loop is still draining it.
                await self._accumulator.flush_all()
                # Shutdown MindLoop
                if self._mind_task is not None:
                    self._mind_loop.shutdown()
                    await asyncio.gather(self._mind_task, return_exceptions=True)
                self._tool_output_store.cleanup()
                self.memsearch.close()
        finally:
            # Backstop (idempotent — safe even after the early stop() above
            # already ran): guards against an orphaned bridge if init crashes
            # somewhere between construction (@~450, before this outer try)
            # and the inner finally. getattr guard mirrors the `_ct` pattern
            # above out of caution, though construction happens before the
            # outer try starts so `self.service_supervisor` normally exists.
            _svc = getattr(self, "service_supervisor", None)
            if _svc is not None:
                await _svc.stop()
            self._pidfile.release()
