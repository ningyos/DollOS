"""Configuration: TOML loading + pydantic validation."""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # OpenAI-compatible /v1/chat/completions with native tool calling.
    provider: Literal["openai-chat"] = "openai-chat"
    base_url: str
    # the id the server lists in /v1/models; the boot check refuses any other
    model: str
    api_key: str = ""  # sent as a Bearer token only when set
    # covers a cold prefill plus queueing behind other clients; the server
    # sends no keep-alive during prefill
    timeout_s: float = 300.0
    # concurrent LLM generations; Strata has one compute slot
    max_concurrency: int = 1
    # thinking tokens per call (reasoning_budget_tokens); max_tokens is
    # raised by this much so thinking never eats the answer budget
    think_budget_tokens: int = 1024
    # a conversation opens a new page once a call's prompt + completion
    # reaches this (the server has no /tokenize; the last call's numbers count)
    conversation_budget_tokens: int = Field(default=24000, gt=0)


class IPCConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 9876


class LogConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class DataConfig(BaseModel):
    """Root for all DollOS-generated data. data/ 不存在 = fresh launch."""
    model_config = ConfigDict(extra="forbid")

    root: Path = Path("data")

    @field_validator("root", mode="before")
    @classmethod
    def _expand_user(cls, v: object) -> object:
        if isinstance(v, (str, Path)):
            return Path(v).expanduser()
        return v


class MemsearchConfig(BaseModel):
    """memsearch knobs (paths derived from data.root in kernel.build_memsearch)."""
    model_config = ConfigDict(extra="forbid")

    top_k: int = 10


class MemoryConfig(BaseModel):
    """Memory-WRITE policy knobs (how Doll records memory, not where it's stored).

    ``primary_language`` is the language of everything Doll writes down —
    notes, diary, who I am now, self-profile, agenda, schedule — regardless of
    the source language; original-language proper nouns / technical terms
    where natural are fine. It is described in the system message's
    [Memory guideline] (prompt engineering), NOT enforced by a subsystem.
    """
    model_config = ConfigDict(extra="forbid")

    primary_language: str = "English"


class ConversationHistoryConfig(BaseModel):
    """Conversation history sliding window — recent turn transcripts prepended
    to every LLM call so Doll sees her own prior reasoning across turns."""
    model_config = ConfigDict(extra="forbid")

    max_turns: int = Field(
        6,
        ge=1,
        le=50,
        description="conversation window size in turns; default 6 (industry standard tail buffer)",
    )


class CharacterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack: Path

    @field_validator("pack", mode="before")
    @classmethod
    def _expand_user(cls, v: object) -> object:
        if isinstance(v, (str, Path)):
            return Path(v).expanduser()
        return v


class VoiceASRSettings(BaseModel):
    """DollOS-level ASR config (character-agnostic).

    All extra keys are passed through as kwargs to the ASR engine
    constructor, so engine-specific knobs (model_id, device, etc.) live
    here directly.
    """

    model_config = ConfigDict(extra="allow")

    engine: str


class VoiceTTSSettings(BaseModel):
    """DollOS-level TTS config (infra + engine selection).

    Per-character identity (ref_audio, transcript, instruction, voice
    profile path, ...) lives in the character pack's
    ``voice/engine.toml``.  Everything else (engine selection, model_id,
    device, sampling defaults) lives here and is merged with the pack at
    session-build time.
    """

    model_config = ConfigDict(extra="allow")

    engine: str


class VoiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asr: VoiceASRSettings | None = None
    tts: VoiceTTSSettings | None = None


class SystemPulseConfig(BaseModel):
    """Proprioception poller — surfaces host vitals as a [Self pulse] block,
    and (alerts_enabled) wakes Doll via a PulseMoment when a vital crosses a
    negative/actionable/worsening threshold. See spec
    docs/superpowers/specs/2026-07-09-system-pulse-proactive-trigger-design.md.
    """
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    poll_interval_s: float = 60.0
    include_active_window: bool = True   # privacy opt-out

    # Proactive trigger (spec §6). alerts_enabled=False → PulseObserver never
    # starts; behavior is exactly today's passive-only [Self pulse] block.
    alerts_enabled: bool = True
    alert_throttle_s: float = 900.0      # global min interval between alerts (15 min)
    window_stuck_s: float = 5400.0       # same-window continuous-present threshold (90 min)


class BridgeConfig(BaseModel):
    """Discord-bridge internalization pointer (spec §4).

    最小指標區塊:只有 enabled + 指向獨立 bridge.toml 的 config 路徑。
    真正的 [discord] token/owner 表留在 bridge.toml,daemon 只知道那個檔案的路徑。
    restart 旋鈕 / retention 都是 service_supervisor.py 的模組常數,不在此。
    """
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False                 # opt-in;預設關 → 零開銷
    config: Path | None = None            # 指向獨立 bridge.toml(enabled 時 required)

    @field_validator("config", mode="before")
    @classmethod
    def _expand_user(cls, v: object) -> object:
        if isinstance(v, (str, Path)):
            return Path(v).expanduser()
        return v

    @model_validator(mode="after")
    def _require_config_when_enabled(self) -> "BridgeConfig":
        if self.enabled and self.config is None:
            raise ValueError("[bridge].enabled=true 需要 [bridge].config 指向 bridge.toml")
        return self


class McpConfig(BaseModel):
    """MCP-server internalization pointer (spec 2026-07-06 §A.4).

    最小指標區塊(P1):只有 enabled + 指向獨立 mcp.toml 的 config 路徑。
    真正的 bind_host/bind_port(+P2 的 debug_secret/query_token)留在 mcp.toml,
    daemon 只知道那個檔案的路徑。逐欄鏡射 BridgeConfig。
    """
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False                 # opt-in;預設關 → 零開銷
    config: Path | None = None            # 指向獨立 mcp.toml(enabled 時 required)
    query_token: str | None = None        # non-empty enables the debug read-query surface (spec §C.3)

    @field_validator("config", mode="before")
    @classmethod
    def _expand_user(cls, v: object) -> object:
        if isinstance(v, (str, Path)):
            return Path(v).expanduser()
        return v

    @model_validator(mode="after")
    def _require_config_when_enabled(self) -> "McpConfig":
        if self.enabled and self.config is None:
            raise ValueError("[mcp].enabled=true 需要 [mcp].config 指向 mcp.toml")
        return self


class McpClientsPointer(BaseModel):
    """Where ``mcp_clients.toml`` is (harness plan 2026-10-27 Task 7): the
    outside MCP servers whose tools Doll may use. A pointer, the way
    ``[mcp].config`` points at mcp.toml, resolved against the daemon's
    working directory like it. No file there = no MCP tools (opt-in), not
    an error; an invalid file fails boot (``harness.mcp_client``)."""
    model_config = ConfigDict(extra="forbid")

    config: Path = Path("mcp_clients.toml")

    @field_validator("config", mode="before")
    @classmethod
    def _expand_user(cls, v: object) -> object:
        if isinstance(v, (str, Path)):
            return Path(v).expanduser()
        return v


class CognitionConfig(BaseModel):
    """Mind-state vitals — surfaces LLM consumption as a [Cognition] block.

    Token quota is purely a "stamina" axis for Doll's self-awareness; it does
    NOT block calls when exceeded. Set ``daily_token_quota = None`` to omit
    the quota line entirely.
    """
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    daily_token_quota: int | None = 500_000
    session_token_quota: int | None = None       # reserved; not yet surfaced
    telemetry_dir_subpath: str = "telemetry"     # relative to data.root
    max_context_tokens: int = 131_072            # Qwen3.6 default


class ConsolidationConfig(BaseModel):
    """B2 sleep-time consolidation — idle-triggered transcript-to-candidate driver."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    idle_threshold_s: int = 300
    min_interval_s: int = 3600
    max_tokens: int = 2048
    # One keeper run (consolidation, then the profile keeper, each gets it).
    # 40k chars of transcript ≈ 21k tokens ≈ 70 s cold prefill on Strata
    # (~310 tok/s, measured 2026-10-29), plus decode (~35 tok/s) over a few
    # tool calls and a possible re-prefill if the shared slot was taken.
    agent_timeout_s: int = 300
    # How much of the day's conversation (the end) the keepers read. A
    # normal day (~50 owner messages) is 15–20k chars; the tail must cover a
    # whole day or an early correction never reaches the profile keeper.
    transcript_tail_chars: int = 40_000


class ProfileKeeperConfig(BaseModel):
    """Daily profile keeper (remember-corrections spec §2.2, plan decision 5):
    runs after each successful consolidation, over the same day's transcript,
    and writes the owner's corrections into self_profile's user /
    relationship sections. Same shape as consolidation's settings; the
    defaults mirror the consolidation keeper's (``think_budget_tokens`` =
    ``[llm].think_budget_tokens``'s default)."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_tokens: int = 2048
    think_budget_tokens: int = 1024
    transcript_tail_chars: int = 40_000


class EnergyConfig(BaseModel):
    """B3 energy system — active-consumes, idle-restores, prompt-injects."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    cost_per_turn: float = 0.05
    restore_per_tick: float = 0.05
    idle_threshold_s: int = 600
    restore_debounce_s: int = 300
    # 代謝 vital model (spec 2026-07-10 §2.2, Task 2): token-driven drain
    # replaces the flat cost_per_turn as the primary formula. Calibrated so
    # a typical turn's drain lands near the old cost_per_turn; smoke-tuned
    # (spec §7 D4). cost_per_turn is now ONLY the D1 flat_legacy degrade
    # (both prompt and completion token counts missing for the whole turn).
    token_per_energy_unit: float = 24000.0  # calibrated from llm_calls telemetry (median (completion+0.25·prompt)≈1193) so a typical turn ≈ cost_per_turn (0.05); smoke-tuned per-install (spec §7 D4)
    # 代謝 vital model §2.3 (Task 5): hot-GPU multiplier on the token drain,
    # reusing bucket_gpu_temp's cool/warm/hot buckets (55/75°C). Smoke-tuned
    # (spec §7 D4), same status as token_per_energy_unit above.
    thermal_multiplier_warm: float = 1.15
    thermal_multiplier_hot: float = 1.4


class SelfProfileConfig(BaseModel):
    """A1 self-profile — Doll-pinned always-inject evolving self."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_chars: int = 4000


class EvolutionConfig(BaseModel):
    """慢變演化 — the slow, ratified 「現在的我」 personality prose (spec §3.6).

    ``enabled = false`` freezes the machinery (no trigger, no tool, no tripwire
    side-effects) but ALREADY-SANCTIONED text keeps rendering — disabling
    evolution must not amputate an adopted self (R3′). The interval/material
    knobs are consumed by Plan 3's keeper (Mode A); they are defined here once so
    Plan 3 adds no config churn.
    """
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # Sized for English (~3.3x the chars of the same content in Chinese).
    # Only new text is checked; an adopted shorter Chinese one stays in_sync.
    current_self_max_chars: int = 2000
    current_self_min_chars: int = 250
    base_interval_days: float = 7.0    # floats — live smoke clamps to sub-day
    max_interval_days: float = 28.0
    idle_threshold_s: int = 600
    min_history_events: int = 8
    min_diary_days: int = 14
    pending_max_surfacings: int = 5
    pending_min_age_days: float = 2.0
    # Mode-A keeper LLM budget (mirrors ConsolidationConfig). Larger than the
    # keeper's __init__ defaults (1024/120, sized for Mode B's one-word verdict):
    # a keeper generating a 250–2000-char candidate + citations through a <think>
    # block truncates at 1024. Mode B's skeptic shares these harmlessly.
    max_tokens: int = 2048
    agent_timeout_s: int = 240


class DiaryConfig(BaseModel):
    """Daily diary scheduler — fires a DiaryMoment perception once a day."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    hour: int = 23
    minute: int = 0
    max_log_chars: int = 40000   # [Today's log] safety ceiling (usually whole day)


class TraceSettings(BaseModel):
    """finetune 語料 trace(spec §3.6)。預設開:紀錄=訓練資料,越早累積越好。"""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    root: str = "data/traces"


class AttentionSettings(BaseModel):
    """AttentionGate 參數(P1c spec §3.3/§3.4)— L0 硬規則 + engagement session。

    ``owner_id`` is accepted here for interface completeness with
    ``AttentionGate.__init__`` (required kwarg), but ``AttentionGate`` never
    reads ``self._owner_id`` beyond storing it (confirmed by grep — the
    daemon derives ``author_is_owner`` bridge-side, in
    ``discord_bridge/controller.py``, and stamps the boolean into the
    ``ChannelEvent`` payload; the daemon itself never sees the raw owner id
    string). Defaults to ``""`` — inert until/unless a future L0 signal
    needs the raw id daemon-side.
    """
    model_config = ConfigDict(extra="forbid")

    name_aliases: list[str] = Field(default_factory=list)
    always_wake_channels: list[str] = Field(default_factory=list)
    owner_id: str = ""
    max_session_turns: int = 6
    window_base_s: float = 90.0
    window_decay: float = 0.6
    debounce_engaged_s: float = 2.0
    debounce_cold_s: float = 8.0


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm: LLMConfig
    ipc: IPCConfig = Field(default_factory=lambda: IPCConfig())
    log: LogConfig = Field(default_factory=lambda: LogConfig())
    data: DataConfig = Field(default_factory=lambda: DataConfig())
    memsearch: MemsearchConfig = Field(default_factory=lambda: MemsearchConfig())
    memory: MemoryConfig = Field(default_factory=lambda: MemoryConfig())
    conversation_history: ConversationHistoryConfig = Field(
        default_factory=lambda: ConversationHistoryConfig()
    )
    character: CharacterConfig
    voice: VoiceSettings = Field(default_factory=lambda: VoiceSettings())
    system_pulse: SystemPulseConfig = Field(default_factory=lambda: SystemPulseConfig())
    cognition: CognitionConfig = Field(default_factory=lambda: CognitionConfig())
    consolidation: ConsolidationConfig = Field(default_factory=lambda: ConsolidationConfig())
    profile_keeper: ProfileKeeperConfig = Field(default_factory=lambda: ProfileKeeperConfig())
    diary: DiaryConfig = Field(default_factory=lambda: DiaryConfig())
    energy: EnergyConfig = Field(default_factory=lambda: EnergyConfig())
    self_profile: SelfProfileConfig = Field(default_factory=lambda: SelfProfileConfig())
    evolution: EvolutionConfig = Field(default_factory=lambda: EvolutionConfig())
    trace: TraceSettings = Field(default_factory=lambda: TraceSettings())
    attention: AttentionSettings = Field(default_factory=lambda: AttentionSettings())
    bridge: BridgeConfig = Field(default_factory=lambda: BridgeConfig())
    mcp: McpConfig = Field(default_factory=lambda: McpConfig())
    mcp_clients: McpClientsPointer = Field(default_factory=lambda: McpClientsPointer())


def load_settings(path: Path) -> Settings:
    """Load and validate a TOML config file."""
    with path.open("rb") as f:
        data = tomllib.load(f)
    return Settings.model_validate(data)
