"""Tool definitions — pydantic models with run() methods.

Tool = BaseModel; args are fields; description = docstring; schema =
model_json_schema(); execution = run(ctx). Single source of truth per
tool. (Voice-first output: Doll speaks via naked text streamed from
mind_loop; there is no Say tool.)

Future: step 7 adds reflex (whitelist via class attribute), step 9 adds
spawn_subagent (fast=False async pattern). For now no permission /
streamable / fast metadata — YAGNI.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

import jieba
from pydantic import BaseModel, Field, field_validator

from dollos.mind import scratchpad_helpers
from dollos.mind.mind_state import OutputRecord

if TYPE_CHECKING:

    from dollos.mind.mind_ctx import MindCtx


def _record(ctx: "MindCtx", kind: str, summary: str) -> None:
    """Append an OutputRecord to ctx.mind_state.recent_outputs, tagged with
    this turn's origin_tier so owner pages can drop a stranger's (§6.5)."""
    ctx.mind_state.recent_outputs.append(
        OutputRecord(
            t=time.time(), kind=kind, summary=summary, origin_tier=ctx.origin_tier,
        )
    )


def _pin_refusal(section: str) -> str:
    """PinSelf's refusal for a section this turn may not write (remember-
    corrections plan Task 2). A string result like its cap / not-found
    messages, so the model reads it and can retry in an allowed section."""
    if section == "self":
        return ("The self section is only revised during reflection; use the "
                "user or relationship section for what your owner told you.")
    return f"The {section} section can't be revised in this turn."


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic tmp+rename write (sanctioned-writer discipline, spec §3.1)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)

logger = logging.getLogger(__name__)

_FILE_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.md$")

# P1e Task 3 (S2): origin is encoded by DIRECTORY, not heading/content, so a
# later task's memsearch source_prefix filter can separate tiers cleanly.
# Unknown/missing origin_tier falls back to "shared" (the internal/default
# tier) — a genuinely-external turn always has origin_tier set by MindLoop
# (P1e Task 1), so this fallback only fires for internal/legacy ctx paths.
_ORIGIN_DIR = {
    "internal": "shared",
    "external_public": "external_public",
    "external_dm": "external_dm",
}


def _hit_date(hit: dict) -> date | None:
    """Extract YYYY-MM-DD from a memsearch hit's source filename, if any."""
    src = hit.get("source", "")
    m = _FILE_DATE_RE.search(src)
    if m:
        return date.fromisoformat(m.group(1))
    return None


def _hit_in_range(
    hit: dict, since: datetime | None, until: datetime | None
) -> bool:
    """File-path date in inclusive [since.date(), until.date()] range.

    Hits without an extractable date are excluded when any filter is set.
    """
    d = _hit_date(hit)
    if d is None:
        return since is None and until is None
    if since is not None and d < since.date():
        return False
    if until is not None and d > until.date():
        return False
    return True


# A Recall result stays in the conversation (plan 2026-10-18 Task 8), so each
# hit is cut to this many characters (see _snippet).
RECALL_HIT_MAX_CHARS = 500


def _snippet(content: str, query: str, limit: int = RECALL_HIT_MAX_CHARS) -> str:
    """``content`` cut to ``limit`` characters around the first place any
    query term appears (case-insensitive); from the start when none does.
    "…" marks each cut end, inside the limit.

    A day's transcript is one chunk, so a cut from the start would never
    reach "earlier today" — exactly what a new conversation page tells her
    to Recall. Terms are the jieba tokens the FTS query is built from.
    """
    if len(content) <= limit:
        return content
    starts = []
    for term in jieba.cut(query):
        if not any(ch.isalnum() for ch in term):
            continue
        m = re.search(re.escape(term.strip()), content, re.IGNORECASE)
        if m:
            starts.append(m.start())
    start = max(0, min(starts) - limit // 2) if starts else 0
    start = min(start, len(content) - limit)
    cut = content[start:start + limit]
    if start > 0:
        cut = "…" + cut[1:]
    if start + limit < len(content):
        cut = cut[:-1] + "…"
    return cut


def _format_hit(hit: dict) -> str:
    d = _hit_date(hit)
    content = hit.get("content", "")
    source = hit.get("source") or ""
    # Provenance prefixes (both surface via Recall; both are auto-inject-
    # filtered upstream — external_public/ since S1, so on an owner/internal
    # turn this tag only appears when Recall asks with include_external —
    # R-DECISION-5):
    #   consolidated/    → system-merged, unconfirmed candidate
    #   external_public/ → written under an external (peer/stranger) turn; an
    #                      unverified AI over MCP can NoteMemory here, so on an
    #                      owner Recall it must read as untrusted, never trusted.
    if "consolidated/" in source:
        prefix = "[consolidated · unconfirmed] "
    elif "external_public/" in source:
        prefix = "[external AI · unverified] "
    else:
        prefix = ""
    if d is not None:
        return f"- {prefix}{d.isoformat()} {content}"
    return f"- {prefix}{content}"


# ---------------------------------------------------------------------------
# Tools — all run(ctx: MindCtx)
# ---------------------------------------------------------------------------


class NoteMemory(BaseModel):
    """Record an episodic fact or event into Doll's memory (daily markdown + memsearch index).
    Use this for things that happened, or concrete facts worth recalling later — when the
    subject is the world or an event, it goes here. NOTE: when the subject is "you" (your
    opinions, interests, curiosity — even just budding — your relationship with your owner,
    what you notice about your owner), use PinSelf (on reflection) to put it in the
    always-in-context self-profile, not here; the same experience can be recorded in both."""

    text: str = Field(
        description="The fact to record. One sentence, declarative."
    )

    def _summary(self) -> str:
        return f"noted: {self.text[:73]}"

    async def run(self, ctx: "MindCtx") -> str:
        from dollos.mind.context_tags import build_heading

        subdir = _ORIGIN_DIR.get(getattr(ctx, "origin_tier", "internal"), "shared")
        path = ctx.memory_root / subdir / f"{date.today():%Y-%m-%d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        heading = build_heading(ctx.mind_state)
        # Sync append + index inside async — append is a single small write
        # (microseconds). asyncio.to_thread wrap is YAGNI for step 6.
        with path.open("a") as f:
            f.write(f"\n## {heading}\n\n{self.text}\n")
        await ctx.memsearch.index_file(path)
        result = f"memory noted: {self.text[:60]}"
        _record(ctx, "NoteMemory", self._summary())
        return result


class WriteDiary(BaseModel):
    """Write today's diary entry to long-term memory.

    Use this once per day when prompted by the diary trigger. The diary
    is a first-person prose narrative reflecting on the day's events AND
    your emotional state. It becomes part of long-term memory and you
    will recall it on future days.
    """

    content: str = Field(
        description=(
            "First-person prose. Cover what happened + how you felt. "
            "Anywhere from a few sentences to a few paragraphs."
        )
    )

    def _summary(self) -> str:
        return f"diary written ({len(self.content)} chars)"

    async def run(self, ctx: "MindCtx") -> str:
        from dollos.mind.context_tags import build_heading

        subdir = _ORIGIN_DIR.get(getattr(ctx, "origin_tier", "internal"), "shared")
        path = ctx.memory_root / subdir / f"{date.today():%Y-%m-%d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        heading = build_heading(ctx.mind_state)
        with path.open("a") as f:
            f.write(f"\n## {heading} Diary\n\n{self.content}\n")
        await ctx.memsearch.index_file(path)
        _record(ctx, "WriteDiary", self._summary())
        return "diary written"


class Shell(BaseModel):
    """Run a shell command in the background. Returns immediately.

    Shell is fire-and-forget. The command runs as a fresh subprocess (no
    cd persistence between calls) with the daemon's user permissions and
    cwd = data/ (the parent of memory/). stdout + stderr are merged. When
    the proc finishes, its result comes back as a NEW turn's perception
    line `ToolResultArrived: Shell <task_id> [<status>]: …` — react to it then.

    There is no wait / monitor / cancel tool. If you start a Shell and
    keep working in the same cascade, the result may also arrive as a
    perception inserted into your next iteration. Either way: react when
    you see it.
    """

    command: str = Field(
        description="Shell command to run (passed to bash -c).",
    )
    timeout_s: int = Field(
        60,
        ge=1,
        le=600,
        description=(
            "Wall-clock seconds before the proc is killed. Estimate "
            "from the command (5 short, 60 medium, 300 long; max 600). "
            "Default 60s."
        ),
    )

    def _summary(self) -> str:
        cmd = self.command[:60]
        return f"shell: {cmd}"

    async def run(self, ctx: "MindCtx") -> str:
        ctx.shell_runner.spawn(
            command=self.command,
            timeout_s=self.timeout_s,
            response_sink=None,
        )
        result = (
            f"shell dispatched (command={self.command!r}, "
            f"timeout={self.timeout_s}s). The result comes back as a new event."
        )
        _record(ctx, "Shell", self._summary())
        return result


class InvokeSkill(BaseModel):
    """Load a skill's full instructions into context.

    Use this when you've seen a skill entry in the [Memory context] block
    (or via the Recall tool) and decide to follow its procedure. The skill
    body will be returned as the next perception, after which you should
    follow its instructions step by step.
    """

    # Read-only (ToolRegistry, mind-loop spec §11).
    effectful: ClassVar[bool] = False

    name: str = Field(
        description=(
            "Skill name (matches the entry's frontmatter `name` field "
            "and filename basename)."
        )
    )

    def _summary(self) -> str:
        return f"invoked skill: {self.name}"

    async def run(self, ctx: "MindCtx") -> str:
        path = ctx.memory_root / "skill_bodies" / f"{self.name}.md"
        if not path.exists():
            skill_dir = ctx.memory_root / "skill_bodies"
            if skill_dir.exists():
                existing = sorted(p.stem for p in skill_dir.glob("*.md"))
            else:
                existing = []
            available = ", ".join(existing) if existing else "(none yet)"
            return f"Skill '{self.name}' doesn't exist. Skills available now: {available}"
        _record(ctx, "InvokeSkill", self._summary())
        return path.read_text()


class Recall(BaseModel):
    """Search Doll's memory for relevant facts.

    Use when you need deeper context than the [Memory context] block
    already provides in this turn's perception. Returns raw memsearch
    hits (no small-model filter — you judge relevance yourself).

    Optional date filter via `since` / `until`. Filter granularity is
    file-date (one day); finer-grained (minute/second) filtering is not
    supported in this version.
    """

    # Read-only (ToolRegistry, mind-loop spec §11).
    effectful: ClassVar[bool] = False

    query: str = Field(
        description=(
            "What to search for in memory. Specific keywords work best."
        )
    )
    since: datetime | None = Field(
        default=None,
        description=(
            "Optional ISO YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS lower bound "
            "(inclusive). Filtering uses date portion only — same-day hits "
            "are kept regardless of time."
        ),
    )
    until: datetime | None = Field(
        default=None,
        description=(
            "Optional ISO upper bound (inclusive). Same date-portion "
            "semantics as `since`."
        ),
    )
    include_external: bool = Field(
        default=False,
        description=(
            "Also search notes left by strangers and external AIs "
            "(unverified)."
        ),
    )

    @classmethod
    def taints(cls, arguments: dict) -> bool:
        """Whether this call reads untrusted content, so ``ToolExecutor``
        downgrades the rest of the turn (harness spec §6.5 point 2, plan
        2026-10-27 Task 6): only with ``include_external``. Decided from the
        arguments, not the hits — asking for strangers' notes is enough, the
        same way a tool that fetches a web page taints whatever it returned."""
        return bool(arguments.get("include_external"))

    def _summary(self, read_external: bool = False) -> str:
        # S1: whether external_public/ hits came back, so a later harness
        # step can downgrade the turn after untrusted memory entered it.
        suffix = " [read external memory]" if read_external else ""
        return f"recalled: {self.query[:72]}{suffix}"

    async def run(self, ctx: "MindCtx") -> str:
        search_kwargs: dict = {}
        # P1e Task 4 (S3) + whole-branch review C2: a stranger's turn
        # (external_public) must never surface the owner's private memory via
        # explicit Recall either. Fail-closed ALLOWLIST: scope to ONLY the
        # external_public/ tier via source_prefix, rather than denylisting
        # known-private dirs — a denylist silently misses any tier not
        # enumerated (e.g. transcripts/, the owner+Doll verbatim convo log,
        # was never on the old denylist and leaked). internal and external_dm
        # (owner) turns see all of their own memory, so this allowlist only
        # activates for external_public.
        #
        # Whole-branch verify (Important): the prefix MUST carry a trailing
        # separator so it's a DIRECTORY boundary — FtsMemory turns
        # source_prefix into ``LIKE '<prefix>%'`` (no implicit boundary), so a
        # bare ``.../external_public`` would ALSO match a sibling like
        # ``.../external_public_evil/`` or a future ``.../external_public_v2/``.
        # This codebase DOES add sibling-named dirs (skills/ + skill_bodies/,
        # self_history + self_evolution/), so the boundary is load-bearing, not
        # theoretical. Mirrors the ``/%`` boundary on exclude_prefixes.
        #
        # S1 (harness spec §6.5): every other turn (internal, and external_dm
        # = the owner's DM) leaves external_public/ out at the SQL layer
        # unless she asks for it with include_external; hits that do come
        # back still carry the unverified tag (_format_hit). On an
        # external_public turn include_external has no effect.
        if getattr(ctx, "origin_tier", "internal") == "external_public":
            search_kwargs["source_prefix"] = (
                str(ctx.memory_root / "external_public") + "/"
            )
        elif not self.include_external:
            search_kwargs["exclude_prefixes"] = [ctx.memory_root / "external_public"]
        hits = await ctx.memsearch.search(self.query, top_k=5, **search_kwargs)
        if self.since is not None or self.until is not None:
            hits = [h for h in hits if _hit_in_range(h, self.since, self.until)]
        if not hits:
            result = "[no relevant memory]"
        else:
            result = "\n".join(
                _format_hit({**h, "content": _snippet(h.get("content", ""), self.query)})
                for h in hits
            )
        read_external = any(
            "external_public/" in (h.get("source") or "") for h in hits
        )
        _record(ctx, "Recall", self._summary(read_external))
        return result


class WorkflowTask(BaseModel):
    """One worker task in a workflow — see ``SpawnWorkflow``."""

    task: str = Field(
        description=(
            "Concrete instruction for one worker agent (no character/memory "
            "context — just the SUB_TOOLS toolkit + this string)."
        )
    )


class SpawnWorkflow(BaseModel):
    """Dispatch a background workflow: N parallel worker agents → optional synthesis.

    Fire-and-forget; the combined result returns as ONE new perception
    starting with the Workflow tag — react to it then. Each worker runs
    independently with the SUB_TOOLS toolkit (Shell / NoteMemory / Recall /
    InvokeSkill / Report …) and MUST end by calling Report; it has no
    character, no memory context, and cannot spawn further workflows.

    mode="map_reduce" fans the tasks out in parallel; mode="verify" adds an
    independent adversarial skeptic pass over each worker's result before the
    results are combined. Set `synthesis` to have a final agent merge all worker
    results into one Report; leave it empty to get the results as-is (with one
    task that returns that worker's report raw).
    """

    tasks: list[WorkflowTask] = Field(min_length=1, max_length=16)
    mode: Literal["map_reduce", "verify"] = "map_reduce"
    synthesis: str = Field(
        default="",
        description=(
            "Instruction for a final agent that combines all worker results. "
            "Empty = return results as-is (N=1 returns that worker's report raw)."
        ),
    )
    timeout_s: int = Field(
        600,
        ge=1,
        le=1800,
        description=(
            "Whole-workflow wall-clock seconds before it is killed "
            "(default 600, max 1800)."
        ),
    )

    def _summary(self) -> str:
        return f"workflow: {len(self.tasks)} tasks, mode={self.mode}"

    async def run(self, ctx: "MindCtx") -> str:
        workflow_id = "wf-" + str(uuid.uuid4())[:8]
        ctx.workflow_runner.spawn(
            workflow_id=workflow_id,
            tasks=[t.task for t in self.tasks],
            synthesis=self.synthesis or None,
            mode=self.mode,
            timeout_s=self.timeout_s,
            response_sink=None,
        )
        result = (
            f"workflow {workflow_id} dispatched "
            f"({len(self.tasks)} tasks, mode={self.mode}, "
            f"timeout={self.timeout_s}s). The result comes back as a new event."
        )
        _record(ctx, "SpawnWorkflow", self._summary())
        return result


class SpawnMonitor(BaseModel):
    """Spawn a background command watcher. Returns a monitor_id immediately.

    The command runs as a long-lived subprocess. Each stdout line
    (optionally regex-filtered) fires a MonitorTriggeredEvent that
    arrives as a new turn's perception line `MonitorFired: <monitor_id>: <line>`.
    When the command exits (naturally or via RemoveMonitor), a
    MonitorExitedEvent fires.

    Rate-limit: within `rate_limit_s` seconds, at most ONE matched line
    fires an event; subsequent matches are counted as suppressed and
    surface in the [Active monitors] block (and in the next firing
    event's `suppressed_count`). Set `rate_limit_s=0` to disable.

    Examples:
        SpawnMonitor(command="nvidia-smi -l 5 --query-gpu=temperature.gpu "
                             "--format=csv,noheader",
                     match_regex=r"^[89][0-9]$", rate_limit_s=60)
        SpawnMonitor(command="tail -F /var/log/syslog",
                     match_regex=r"ERROR|CRITICAL", rate_limit_s=60)
    """

    command: str = Field(
        description="Shell command (passed to bash -c). Run long; daemon kills on shutdown.",
    )
    match_regex: str | None = Field(
        default=None,
        description=(
            "Optional Python regex. None = every line fires. Use to "
            "pre-filter inside the runner (cheaper than firing events "
            "and ignoring them)."
        ),
    )
    rate_limit_s: int = Field(
        60,
        ge=0,
        le=3600,
        description=(
            "Per-monitor seconds-between-fires window. 0 disables. "
            "Default 60s (reasonable for noisy sources)."
        ),
    )

    def _summary(self) -> str:
        return f"monitor: {self.command[:68]}"

    async def run(self, ctx: "MindCtx") -> str:
        try:
            monitor_id = ctx.monitor_runner.spawn(
                command=self.command,
                match_regex=self.match_regex,
                rate_limit_s=self.rate_limit_s,
                response_sink=None,
            )
        except re.error as e:
            return f"[SpawnMonitor regex error: {e}]"
        if not monitor_id:
            return "[SpawnMonitor failed: runner is stopping]"
        result = (
            f"monitor {monitor_id} dispatched "
            f"(command={self.command!r}, "
            f"match={self.match_regex!r}, "
            f"rate_limit_s={self.rate_limit_s}). "
            f"Each trigger and the exit come back as new events."
        )
        _record(ctx, "SpawnMonitor", self._summary())
        return result


class RemoveMonitor(BaseModel):
    """Kill an active monitor by id.

    The process is killed (SIGKILL on its process group). The watcher
    fires a MonitorExitedEvent with status='removed'. Active monitors
    surface in the [Active monitors] block of every cascade iter.
    """

    monitor_id: str = Field(
        description="Monitor id returned by SpawnMonitor (e.g. 'mon-3').",
    )

    def _summary(self) -> str:
        return f"remove monitor: {self.monitor_id}"

    async def run(self, ctx: "MindCtx") -> str:
        ok = await ctx.monitor_runner.remove(self.monitor_id)
        if ok:
            result = f"monitor {self.monitor_id} kill requested."
        else:
            result = f"monitor {self.monitor_id} unknown (already gone or not found)."
        _record(ctx, "RemoveMonitor", self._summary())
        return result


class Report(BaseModel):
    """Terminate this worker agent and report the structured outcome.

    Worker-agent-only tool. MUST be called exactly once before the agent ends
    — the cascade ends naturally after the call (Report.run returns None). The
    args become the workflow result Doll sees on her next turn (or feed the
    workflow's synthesis stage).
    """

    status: Literal["ok", "incomplete"] = Field(
        description=(
            "ok = task completed as requested. "
            "incomplete = partially done (give details on why)."
        )
    )
    summary: str = Field(
        description="One-sentence summary of what happened."
    )
    details: str = Field(
        description=(
            "Findings / output / data Doll asked for. Plain text. "
            "Include enough that Doll can act on it without re-running you."
        )
    )

    async def run(self, ctx: "MindCtx") -> None:
        # Side-effect: stash args into ctx for run_agent to pick up; its
        # early-exit check ends the cascade after this pass.
        ctx.agent_report = {
            "status": self.status,
            "summary": self.summary,
            "details": self.details,
        }
        return None


class ScheduleEntryArg(BaseModel):
    """One entry in a daily schedule — see ``WriteSchedule``."""

    time: str = Field(
        description="ISO HH:MM:SS, 24-hour clock. e.g. '07:30:00'.",
    )
    intent: str = Field(
        description="One short sentence describing the planned action.",
        min_length=1,
    )

    @field_validator("time")
    @classmethod
    def parse_time(cls, v: str) -> str:
        try:
            datetime.fromisoformat(f"1970-01-01T{v}")
        except ValueError as e:
            raise ValueError(f"time must be HH:MM:SS, got {v!r}") from e
        return v


class WriteSchedule(BaseModel):
    """Write today's schedule. Replaces any existing schedule for today.

    Each entry has a time (HH:MM:SS, 24-hour) and intent (short sentence).
    Past times are NOT fired retroactively — they're skipped by the
    scheduler. Use this once at the start of the day (or to replan).
    """

    entries: list[ScheduleEntryArg]

    def _summary(self) -> str:
        return f"schedule: {len(self.entries)} entries"

    async def run(self, ctx: "MindCtx") -> str:
        from dollos.schedule import ScheduleEntry, write_schedule

        today = date.today()
        path = ctx.memory_root / "schedule" / f"{today:%Y-%m-%d}.toml"
        parsed = [
            ScheduleEntry(
                time=datetime.fromisoformat(f"1970-01-01T{e.time}").time(),
                intent=e.intent,
            )
            for e in self.entries
        ]
        write_schedule(path, parsed)

        # Markdown summary into shared memory so Recall surfaces today's
        # plan (gap #3).
        md_path = ctx.memory_root / "shared" / f"{today:%Y-%m-%d}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        from dollos.mind.context_tags import build_heading

        heading = build_heading(ctx.mind_state)
        with md_path.open("a") as f:
            f.write(f"\n## {heading} Plan\n\n")
            for e in parsed:
                f.write(f"- {e.time:%H:%M:%S} — {e.intent}\n")
        await ctx.memsearch.index_file(md_path)

        result = f"Schedule written: {len(parsed)} entries"
        _record(ctx, "WriteSchedule", self._summary())
        return result


class ReadToolOutput(BaseModel):
    """Read a slice of a stored tool output by id.

    Tool outputs come from Shell / Subagent. The originating result perception
    shows the output_id and total line count; use this tool to page deeper
    than the preview.
    """

    # Read-only (ToolRegistry, mind-loop spec §11).
    effectful: ClassVar[bool] = False

    id: str = Field(
        ...,
        description="output id from a tool result perception (e.g. 'out-abc12345')",
    )
    offset: int = Field(
        ...,
        description="zero-indexed start line; 0 = beginning; negative counts from end.",
    )
    limit: int = Field(
        ...,
        ge=1,
        le=500,
        description="max lines to return (1-500).",
    )

    def _summary(self) -> str:
        return f"read tool output {self.id} offset={self.offset} limit={self.limit}"

    async def run(self, ctx: "MindCtx") -> str:
        slice_ = ctx.tool_output_store.read(self.id, offset=self.offset, limit=self.limit)
        header = (
            f"lines {slice_.start_offset}–{slice_.end_offset} of {slice_.total_lines}:"
        )
        body = "\n".join(slice_.lines) if slice_.lines else "(empty slice)"
        result = f"{header}\n{body}"
        _record(ctx, "ReadToolOutput", self._summary())
        return result


class GrepToolOutput(BaseModel):
    """Grep a stored tool output for a regex pattern. Returns matching lines
    with their line index, capped by max_matches.
    """

    # Read-only (ToolRegistry, mind-loop spec §11).
    effectful: ClassVar[bool] = False

    id: str = Field(..., description="output id from a tool result perception")
    pattern: str = Field(..., description="regex pattern (Python re); case-sensitive")
    max_matches: int = Field(20, ge=1, le=200, description="max matching lines to return")

    def _summary(self) -> str:
        return f"grep tool output {self.id} pattern={self.pattern[:40]!r}"

    async def run(self, ctx: "MindCtx") -> str:
        try:
            matches = ctx.tool_output_store.grep(
                self.id, pattern=self.pattern, max_matches=self.max_matches
            )
        except re.error as exc:
            return f"regex error: {exc} (pattern={self.pattern!r})"
        if not matches:
            result = f"no matches for {self.pattern!r}"
        else:
            header = f"{len(matches)} match(es) for {self.pattern!r}:"
            body = "\n".join(f"line {m.line_index}: {m.line}" for m in matches)
            result = f"{header}\n{body}"
        _record(ctx, "GrepToolOutput", self._summary())
        return result


# ---------------------------------------------------------------------------
# Scratchpad tool — mutate ctx.mind_state.scratchpad via helpers
# ---------------------------------------------------------------------------


class Scratchpad(BaseModel):
    """Manage your working-memory scratchpad (hard cap 2000 chars).

    op="set": overwrite with `content`.
    op="append": add `content` as a new line (newline auto-prepended if non-empty).
    op="clear": wipe to empty (`content` is ignored).

    Use "set" to start fresh, "append" to jot a running note. To replace a
    specific substring, just "set" the full new contents.
    """

    op: Literal["set", "append", "clear"] = Field(
        description='"set" overwrite | "append" add a line | "clear" wipe'
    )
    content: str = Field(
        description="New content for set/append (≤2000 chars). Ignored for clear.",
    )

    def _summary(self) -> str:
        return f"scratchpad {self.op} ({len(self.content)} chars)"

    async def run(self, ctx: "MindCtx") -> str:
        if self.op == "set":
            scratchpad_helpers.write(ctx.mind_state, self.content)
            result = f"scratchpad set ({len(self.content)} chars)"
        elif self.op == "append":
            new_total = scratchpad_helpers.append(ctx.mind_state, self.content)
            result = f"scratchpad now {new_total} chars"
        else:  # clear
            scratchpad_helpers.clear(ctx.mind_state)
            result = "scratchpad cleared"
        _record(ctx, "Scratchpad", self._summary())
        return result


# ---------------------------------------------------------------------------
# Loop actions — manage focus, open_loops, and sleep hints
# ---------------------------------------------------------------------------


class SetFocus(BaseModel):
    """Update Doll's current focus — what she's attending to right now."""

    text: str = Field(..., description="one-sentence current focus, ≤200 chars")

    def _summary(self) -> str:
        return f"focus → {self.text[:60]}"

    async def run(self, ctx: "MindCtx") -> str:
        ctx.mind_state.focus = self.text[:200]
        _record(ctx, "SetFocus", self._summary())
        return f"focus set to: {self.text[:60]}"


class OpenLoop(BaseModel):
    """Add a TODO commitment Doll will remember across iterations."""

    id: str = Field(..., description="short slug id (e.g. 'check_tmp')")
    desc: str = Field(..., description="what to follow up on")

    def _summary(self) -> str:
        return f"opened loop {self.id}: {self.desc[:50]}"

    async def run(self, ctx: "MindCtx") -> str:
        from dollos.mind.mind_state import OpenLoop as OpenLoopT

        ctx.mind_state.open_loops.append(
            OpenLoopT(id=self.id, desc=self.desc, opened_at=time.time())
        )
        _record(ctx, "OpenLoop", self._summary())
        return f"opened loop {self.id}"


class CloseLoop(BaseModel):
    """Mark a TODO commitment resolved."""

    id: str = Field(..., description="loop id to close")
    outcome: str = Field(..., description="how it resolved")

    def _summary(self) -> str:
        return f"closed loop {self.id}: {self.outcome[:50]}"

    async def run(self, ctx: "MindCtx") -> str:
        before = len(ctx.mind_state.open_loops)
        ctx.mind_state.open_loops = [
            ol for ol in ctx.mind_state.open_loops if ol.id != self.id
        ]
        if len(ctx.mind_state.open_loops) == before:
            logger.warning("close_loop: unknown id %r — no-op", self.id)
        _record(ctx, "CloseLoop", self._summary())
        return f"closed loop {self.id}"


class PursueGoal(BaseModel):
    """Open a thread you yourself want to pursue (not a TODO you owe anyone — something
    you care about / are curious about)."""

    id: str = Field(..., description="short slug id")
    desc: str = Field(..., description="What you want to pursue")
    trigger: str = Field(
        ...,
        description=(
            "Where did this come from? — cite the conversation just now / a memory / "
            "a real experience. Required."
        ),
    )

    def _summary(self) -> str:
        return f"pursue {self.id}: {self.desc[:50]}"

    async def run(self, ctx: "MindCtx") -> str:
        from dollos.mind.mind_state import OpenLoop as OpenLoopT
        from dollos.mind.mind_state import _MAX_SELF_LOOPS

        # Cap enforcement (whole-branch review Finding 2): _MAX_SELF_LOOPS
        # was defined in mind_state.py but never checked here, making it
        # dead code — this tool appended unconditionally. Count existing
        # self_directed loops and refuse to open another once at the cap,
        # rather than letting her agenda grow unbounded.
        self_directed_count = sum(
            1 for ol in ctx.mind_state.open_loops if ol.self_directed
        )
        if self_directed_count >= _MAX_SELF_LOOPS:
            _record(ctx, "PursueGoal", f"capacity reached, refused {self.id}")
            return (
                f"you're at your self-directed agenda capacity "
                f"({_MAX_SELF_LOOPS} open loops) — CloseLoop something first "
                f"before opening {self.id}"
            )

        # Auto-provenance (spec §3.1/§3.3, THE crux): captured entirely from
        # ctx — the turn's REAL id/iter/memory-hit sources — never from
        # anything on `self` (the tool has no provenance-shaped field at
        # all). A model cannot forge grounding it cannot write to. A turn
        # with no real memory hits honestly gets memory_sources == [] —
        # that's the audit signal for an ungrounded item, not a bug.
        provenance = {
            "turn_id": str(ctx.current_turn),
            "opened_iter": ctx.mind_state.iter_count,
            "memory_sources": list(ctx.turn_memory_sources),
        }
        ctx.mind_state.open_loops.append(
            OpenLoopT(
                id=self.id,
                desc=self.desc,
                opened_at=time.time(),
                self_directed=True,
                trigger=self.trigger,
                provenance=provenance,
                progress=[],
            )
        )
        _record(ctx, "PursueGoal", self._summary())
        return f"opened self-directed loop {self.id}"


class AdvanceGoal(BaseModel):
    """Record a step of progress / an insight on your own agenda."""

    id: str = Field(..., description="which agenda loop")
    progress: str = Field(..., description="a concrete step or insight")

    def _summary(self) -> str:
        return f"advance {self.id}: {self.progress[:50]}"

    async def run(self, ctx: "MindCtx") -> str:
        from dollos.mind.mind_state import _MAX_PROGRESS

        for ol in ctx.mind_state.open_loops:
            if ol.id == self.id and ol.self_directed:
                ol.progress = (ol.progress + [self.progress])[-_MAX_PROGRESS:]
                _record(ctx, "AdvanceGoal", self._summary())
                return f"advanced {self.id}"
        return f"no self-directed loop {self.id}"


# ---------------------------------------------------------------------------
# Mood tool — mutates ctx.mind_state.mood
# ---------------------------------------------------------------------------


_MOOD_WORD = re.compile(r"[A-Za-z]{1,12}")


class MoodTool(BaseModel):
    """Update Doll's current emotional state.

    Call this when your mood has shifted. The new mood is stored in
    MindState and surfaces in every subsequent iteration's [Mind state]
    block.
    """

    emotion: str = Field(
        ...,
        description=(
            "Current emotion as one English word (e.g. 'happy', 'uneasy'): letters only, "
            "1-12 of them, no spaces or punctuation."
        ),
    )
    reason: str = Field(
        default="",
        description="Brief reason for the mood shift (one sentence, optional).",
    )

    # One English word (plan 2026-10-19-gura-english-speech-fix, Task 2): the
    # [Mind state] mood label is daemon-rendered prompt text. The schema the
    # model sees drops pattern / length keywords (llm/tool_schema._KEPT), so
    # the description says it and this check enforces it in dispatch_one.
    @field_validator("emotion")
    @classmethod
    def one_english_word(cls, v: str) -> str:
        if not _MOOD_WORD.fullmatch(v):
            raise ValueError(
                "emotion must be one English word: 1-12 letters (A-Z, a-z), "
                "no spaces, digits or punctuation (e.g. 'happy', 'uneasy')"
            )
        return v

    def _summary(self) -> str:
        return f"mood → {self.emotion}"

    async def run(self, ctx: "MindCtx") -> str:
        from dollos.mind.mind_state import Mood
        ctx.mind_state.mood = Mood(emotion=self.emotion, reason=self.reason)
        result = f"mood → {self.emotion}"
        _record(ctx, "MoodTool", self._summary())
        return result


class NoteToolLesson(BaseModel):
    """Record a compact, reusable lesson about HOW to use your tools —
    distilled from what worked or what failed. Append-only: write a NEW
    lesson rather than rewriting an old one. (Surfaced back as [Tool habits].)"""

    situation: str = Field(description="When this applies, one short phrase.")
    lesson: str = Field(description="The reusable takeaway, one or two sentences.")

    def _summary(self) -> str:
        return f"tool lesson: {self.situation[:60]}"

    async def run(self, ctx: "MindCtx") -> str:
        path = ctx.memory_root / "shared" / "tool_playbook.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        heading = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        with path.open("a") as f:
            f.write(f"\n## {heading}\n\n[situation] {self.situation}\n{self.lesson}\n")
        await ctx.memsearch.index_file(path)
        _record(ctx, "NoteToolLesson", self._summary())
        return f"lesson noted: {self.situation[:60]}"


class PinSelf(BaseModel):
    """Pin or revise an entry in your self-profile — this
    is YOUR evolving self and it is ALWAYS in context. The test is the subject: if the
    sentence is about "you" (your opinions, interests, stances, curiosity, your relationship
    with your owner, what you notice about your owner), use PinSelf; facts about the world
    (even ones you looked up or find interesting) go to NoteMemory — the same experience
    can be recorded in both: the fact in NoteMemory, the "you" it reveals here. Things
    that are just budding, where you're not sure yet they count as "you", can be pinned
    now and filtered at a later reflection. Space is limited; regularly use replace/remove
    to prune entries that are no longer you now — what survives is your core.

    When your owner corrects how you treat them, or corrects a lasting fact about them,
    write it right then in the user or relationship section: replace the old entry if
    there is one, otherwise add. Write it as a plain fact about them or the two of you,
    not as a story of being corrected. One-off things (appointments, reminders) go to
    NoteMemory or WriteSchedule. The self section is only revised during reflection."""

    section: Literal["self", "relationship", "user"] = Field(
        description=(
            "Which section: self=about yourself (identity, opinions, interests, curiosity) / "
            "relationship=you and your owner / user=what you notice about your owner. "
            "Also fill it for replace/remove (target wins)."
        )
    )
    op: Literal["add", "replace", "remove"] = Field(
        description=(
            "add=add an entry / replace=locate by target and replace with text / "
            "remove=locate by target and delete."
        )
    )
    target: str = Field(
        description='For replace/remove: the id to target (e.g. "s1", "r2"). For op=add, leave this an empty string "".'
    )
    text: str = Field(
        description=(
            "New content for add/replace (in your own words; no full-width quote marks); "
            "empty string for remove."
        )
    )

    def _summary(self) -> str:
        return f"self {self.op} {self.target or self.section}"

    async def run(self, ctx: "MindCtx") -> str:
        from dollos.mind import self_profile
        path = ctx.memory_root / "self_profile.md"
        # Remember-corrections plan Task 2, decision 2: only the sections
        # this turn allows (ctx.pin_sections) — for replace / remove, the
        # section of the entry the target resolves to, since the lookup
        # spans all sections. Refused before apply: nothing written, nothing
        # logged. A target that resolves to nothing goes on to apply's own
        # not-found result.
        sections = [self.section]
        if self.op in ("replace", "remove"):
            located = self_profile.section_of(path, self.target)
            if located is not None:
                sections.append(located)
        for section in sections:
            if section not in ctx.pin_sections:
                return _pin_refusal(section)
        today = f"{datetime.now():%Y-%m-%d}"
        result = self_profile.apply(
            path,
            section=self.section,
            op=self.op,
            target=self.target,
            text=self.text,
            max_chars=ctx.self_profile_max_chars,
            today=today,
            history_path=ctx.memory_root / "self_history.jsonl",
            turn=ctx.current_turn,
            external_ctx=ctx.external_ctx,
        )
        # 絕不索引(§3.1):self_profile.md 靠 always-inject,不進召回。
        _record(ctx, "PinSelf", self._summary())
        return result


class EditOwnerProfile(BaseModel):
    """Add, replace or remove an entry about your owner in your self-profile:
    the user section (what you've noticed about your owner) or the
    relationship section (you and your owner). Every change rests on your
    owner's own words from today's transcript, given in quote; a change
    whose quote is not in your owner's lines is refused."""

    section: Literal["user", "relationship"] = Field(
        description=(
            "Which section: user=what you notice about your owner / "
            "relationship=you and your owner. Also fill it for replace/remove "
            "(target wins)."
        )
    )
    op: Literal["add", "replace", "remove"] = Field(
        description=(
            "add=add an entry / replace=locate by target and replace with text / "
            "remove=locate by target and delete."
        )
    )
    target: str = Field(
        description=(
            "For replace/remove: the id of the entry (as listed with the current "
            "entries). Empty string for add."
        )
    )
    text: str = Field(
        description=(
            "New content for add/replace, a plain fact about your owner or the two "
            "of you; empty string for remove."
        )
    )
    quote: str = Field(
        description=(
            "Your owner's words from today's transcript that this change rests on, "
            "copied exactly (a part of one of their lines is enough). Your own "
            "lines do not count."
        )
    )

    def _summary(self) -> str:
        return f"owner profile {self.op} {self.target or self.section}"

    async def run(self, ctx: "MindCtx") -> str:
        from dollos.mind import self_profile
        from dollos.mind.profile_keeper import MIN_QUOTE_CHARS, owner_said
        path = ctx.memory_root / "self_profile.md"
        # Remember-corrections plan Task 4. Every refusal comes before
        # apply: the file is unchanged and no history event is logged.
        # The quote is checked against the day's transcript on disk
        # (ctx.owner_transcript, decision 4) — never text from the model.
        transcript = ctx.owner_transcript
        if transcript is None or not transcript.exists():
            return "No transcript for today, so no edit can be checked; nothing changed."
        # Decision 3: only user / relationship. ``section`` is held to those
        # by its Literal; a replace / remove target is looked up across all
        # sections, so the entry it resolves to is checked too (as PinSelf
        # does, Task 2). A target that resolves to nothing goes on to
        # apply's own not-found result.
        if self.op in ("replace", "remove"):
            located = self_profile.section_of(path, self.target)
            if located is not None and located not in ("user", "relationship"):
                return (f"The {located} section is not yours to change here; only "
                        f"the user and relationship sections are.")
        quote = " ".join(self.quote.split())
        if len(quote) < MIN_QUOTE_CHARS:
            return (f"The quote must be at least {MIN_QUOTE_CHARS} characters of "
                    f"your owner's words; nothing changed.")
        if not owner_said(transcript, quote):
            return ("The quote is not in your owner's lines of today's transcript; "
                    "nothing changed. Copy your owner's words exactly.")
        today = f"{datetime.now():%Y-%m-%d}"
        result = self_profile.apply(
            path,
            section=self.section,
            op=self.op,
            target=self.target,
            text=self.text,
            max_chars=ctx.self_profile_max_chars,
            today=today,
            history_path=ctx.memory_root / "self_history.jsonl",
            turn=ctx.current_turn,
            external_ctx=ctx.external_ctx,
            # Decision 6: the keeper's change is logged like a pin, plus who
            # made it and the owner's words it rests on.
            history_extra={"by": "profile_keeper", "quote": quote},
        )
        _record(ctx, "EditOwnerProfile", self._summary())
        return result


class SelfRevision(BaseModel):
    """Adopt / reject the pending revision candidate for "Who I am now" (only on
    reflection turns, and only when a candidate is pending). This is your own personality
    description; the system only proposes, adopting is up to you. Adopt: decision="adopt"
    with text empty adopts the candidate as is; to rewrite then adopt: put the full new
    text in text (it is reviewed first, then comes back for you to adopt); reject:
    decision="reject". A rewrite only needs to not violate your core identity and taboos."""

    decision: Literal["adopt", "reject"] = Field(
        description=('"adopt"=adopt (empty text adopts the candidate as is, or the full '
                     'text submits a rewrite for review) / "reject"=don\'t adopt, keep '
                     'things as they are.')
    )
    text: str = Field(
        default="",
        description=(
            "Only for rewrite-then-adopt: the full replacement text (your own words, no "
            "full-width quote marks); leave empty to adopt directly or to reject."
        ),
    )
    reason: str = Field(
        default="",
        description=(
            "Optional one-line reason when rejecting (your own words, no full-width "
            "quote marks)."
        ),
    )

    def _summary(self) -> str:
        return f"self-revision {self.decision}"

    async def run(self, ctx: "MindCtx") -> str:
        from dollos.mind import evolution as evo
        from dollos.mind import self_history
        from dollos.mind.persona_guard import pairwise_jaccard

        hist = ctx.memory_root / "self_history.jsonl"
        slot_path = ctx.memory_root / "self_evolution" / "pending.json"
        cs_path = ctx.memory_root / "current_self.md"

        # Per-turn latch (spec §3.4): first slot-mutating call per turn acts.
        if ctx.evolution_latched:
            _record(ctx, "SelfRevision", self._summary())
            return ("The self revision candidate has already been handled this turn; "
                    "it waits for the next reflection.")

        slot = evo.load_slot(slot_path, history_path=hist)
        if slot is None:
            _record(ctx, "SelfRevision", self._summary())
            return "There is no self revision candidate pending right now."
        if slot.status != "awaiting_doll":
            _record(ctx, "SelfRevision", self._summary())
            return ("The candidate is still under review; once it passes, it comes back "
                    "to you to adopt.")

        # Surfaced-this-turn gate (review F5): a Mode-B skeptic PASS can flip
        # awaiting_skeptic→awaiting_doll mid-cascade, AFTER surface_or_expire
        # already returned None this turn. Refuse every slot-mutating op
        # (adopt/reject/counter) on a candidate Doll never saw this turn — she
        # must not adopt (or reject) text that never reached her eyes.
        if not ctx.evolution_candidate_surfaced:
            _record(ctx, "SelfRevision", self._summary())
            return ("This candidate hasn't been shown to you this turn; the decision "
                    "waits for the next reflection.")

        sanctioned = self_history.sanctioned_text(hist)

        def _anchor_evolution_decision(outcome: str) -> None:
            """Decision-event bookkeeping (spec §3.3, review M4): the
            last_attempt anchor applies regardless of slot origin; the
            interval update is gated on ``slot.kind != "external"`` (external
            events leave the interval unchanged per spec). Called from BOTH
            the happy-path and the adopt-write-failure branch — interval
            semantics follow the logged decision event, not the happy path."""
            ctx.mind_state.last_evolution_attempt_at = time.time()
            if slot.kind == "external":
                return
            if outcome == "adopt":
                ctx.mind_state.evolution_interval_days = ctx.evolution_base_interval_days
            else:  # "reject"
                ctx.mind_state.evolution_interval_days = evo.next_interval_days(
                    ctx.mind_state.evolution_interval_days, outcome=evo.EVO_REJECT,
                    base=ctx.evolution_base_interval_days,
                    cap=ctx.evolution_max_interval_days)

        if self.decision == "reject":
            # Evolution events are never swallowed (spec §3.2): a failed append
            # aborts the reject — slot stays, no latch, friendly error.
            try:
                evo.log_or_raise(hist, kind=evo.EVO_REJECT, reason=self.reason or None,
                                 text=slot.candidate)
            except OSError:
                _record(ctx, "SelfRevision", self._summary())
                return ("Writing the record of the rejection failed, so it didn't take effect — "
                        "try again later.")
            evo.clear_slot(slot_path)
            # Slot-resolution invariant (spec §3.4): a non-adopt clearing
            # restores the file to sanctioned (or deletes it, bootstrap).
            evo.restore_file(cs_path, sanctioned)
            _anchor_evolution_decision("reject")
            ctx.evolution_latched = True
            _record(ctx, "SelfRevision", self._summary())
            return "OK, things stay as they are; the candidate is set aside."

        # decision == "adopt"
        # Storage-side marker strip (review F3): the model's `text` may echo the
        # surfaced surfacing_markers (⟪current⟫/⟪proposed⟫); those bytes must never become
        # her self. Applied before the mechanical checks and before to_counter.
        proposed = evo.strip_surfacing_markers(self.text)
        if not proposed or evo.echo_equivalent(proposed, slot.candidate):
            # Zero-move guard (review M1): the candidate itself already equals
            # the live sanctioned text (e.g. a keeper/external candidate that
            # matches). Adopting it would bump the generation and reset the
            # schedule for nothing — refuse; slot unchanged.
            if sanctioned is not None and evo.echo_equivalent(slot.candidate, sanctioned):
                _record(ctx, "SelfRevision", self._summary())
                return ("The candidate is the same as the current text; to keep things as "
                        "they are, use decision=reject.")
            # Adopt the CANDIDATE verbatim (never her paraphrase, spec §3.4).
            old = sanctioned
            drift = None if old is None else round(1.0 - pairwise_jaccard(old, slot.candidate), 4)
            # Log-then-write ordering (spec §3.2): a failed append aborts.
            try:
                evo.log_or_raise(hist, kind=evo.EVO_ADOPT, text=slot.candidate,
                                 old_text=old, drift_score=drift)
            except OSError:
                _record(ctx, "SelfRevision", self._summary())
                return ("Writing the record of the adoption failed, so it didn't take effect — "
                        "try again later.")
            # File-write-failure row (spec §3.3, review F1): the evo_adopt line
            # already flushed ⇒ sanctioned = log. Retry the file write once; on a
            # second failure this is a LOG (best-effort evo_error), not an abort —
            # the slot MUST clear (a stale awaiting_doll slot whose candidate ==
            # sanctioned invites a duplicate zero-move adopt) and the §5
            # crash-repair heals the file next turn. If the evo_error append
            # itself also raises, let it propagate (crash-repair heals anyway).
            try:
                _atomic_write_text(cs_path, slot.candidate)
            except OSError:
                try:
                    _atomic_write_text(cs_path, slot.candidate)  # retry once
                except OSError:
                    logger.warning("SelfRevision: current_self.md write failed twice; "
                                   "logging evo_error, slot cleared, crash-repair will heal")
                    evo.log_or_raise(hist, kind=evo.EVO_ERROR, reason="adopt_write_failed")
                    evo.clear_slot(slot_path)
                    _anchor_evolution_decision("adopt")
                    ctx.evolution_latched = True
                    _record(ctx, "SelfRevision", self._summary())
                    return ("Adopted and recorded, but writing the file failed — the system "
                            "will repair it automatically.")
            evo.clear_slot(slot_path)
            _anchor_evolution_decision("adopt")
            ctx.evolution_latched = True
            _record(ctx, "SelfRevision", self._summary())
            return "Adopted. This is who I am now."

        if sanctioned is not None and evo.echo_equivalent(proposed, sanctioned):
            _record(ctx, "SelfRevision", self._summary())
            return ("This is the same as the current text; to keep things as they are, "
                    "use decision=reject.")

        # Genuinely different → counter-proposal.
        if slot.counter_round >= evo.COUNTER_ROUND_CAP:
            _record(ctx, "SelfRevision", self._summary())
            return ("This candidate has already been rewritten twice; adopt it or reject "
                    "it as it stands.")
        reason = evo.mechanical_checks(
            proposed, floor=ctx.current_self_min_chars,
            cap=ctx.current_self_max_chars, enforcement=ctx.enforcement)
        if reason is not None:
            _record(ctx, "SelfRevision", self._summary())
            return reason
        counter = evo.to_counter(slot, new_text=proposed)
        # Birth-line-then-write (spec §3.4: every slot has a birth line): log
        # the evo_counter BEFORE replacing the slot; a failed append aborts
        # with the slot unchanged (spec §3.2 — never swallowed).
        try:
            evo.log_or_raise(hist, kind=evo.EVO_COUNTER, text=proposed,
                             counter_round=counter.counter_round)
        except OSError:
            _record(ctx, "SelfRevision", self._summary())
            return ("Writing the record of the rewrite failed, so it didn't go to review — "
                    "try again later.")
        evo.save_slot(slot_path, counter)
        ctx.evolution_latched = True
        _record(ctx, "SelfRevision", self._summary())
        return ("Your rewrite has gone to review; once it passes, it comes back to you "
                "to adopt.")


# Part A / A2 (2026-07-06 self-learned-aliases spec §3.3, R1-hardened):
# mechanical guard applied to EVERY LearnName call, including owner turns —
# this is the only line of defense against the owner herself teaching a
# dangerous/too-common token (e.g. pinning "早安" as a nickname). Trust-
# boundary enforcement against STRANGERS lives entirely in
# mind_loop._active_tool_registry (registry availability, not this guard):
# LearnName is structurally absent from the tool set on external_public and
# pure-reflection turns, so this guard never even has to consider a
# stranger's input. The guard itself (name_aliases.passes_alias_guard) is
# SHARED with kernel.py's A3 seed/floor load-time guard (spec I3) — one
# place of truth so write-time and load-time checks can't drift apart.
# Full L0 match-rule hardening (word-boundary for ASCII, substring+min-
# length for CJK at READ/match time) is a separate concern, applied by A3
# in attention.py — this guard only protects the WRITE path.


class LearnName(BaseModel):
    """Record or withdraw a nickname that someone calls you. **This tool only appears in
    the very turn where you heard/saw it yourself** (your owner's local conversation, or
    your owner's DM) — it is not always available, and you can never call it up later from
    recollection. op="add" records a new nickname, after which that word can also wake you;
    op="remove" withdraws one no longer used. Words that are too short or too common (even
    if your owner taught them) are blocked and not written."""

    op: Literal["add", "remove"]
    token: str = Field(description='The nickname itself, e.g. "Sharky" or "shork".')
    note: str | None = Field(
        default=None,
        description="Optional: who calls you this, and in what context (for audit / self-narrative).",
    )

    def _summary(self) -> str:
        return f"learn_name {self.op} {self.token[:30]}"

    async def run(self, ctx: "MindCtx") -> str:
        from dollos.mind import self_history
        from dollos.mind.name_aliases import NameAliasStore, passes_alias_guard

        token = self.token.strip()
        if not passes_alias_guard(token):
            _record(ctx, "LearnName", self._summary())
            return f'"{token or self.token}" is too short or too common to keep as a nickname.'

        store = NameAliasStore(ctx.memory_root / "name_aliases.json")
        if self.op == "add":
            # origin is HARDCODED "owner" — LearnName has no ``origin``
            # field, so the model can never set it (A1 review ⚠️). The
            # tool only ever reaches run() on an owner-present turn (see
            # mind_loop._active_tool_registry), so this is always true.
            store.add(token, origin="owner", note=self.note, now=time.time())
            result = f'Got it — from now on you can call me "{token}" too.'
        else:
            store.remove(token)
            result = f'Dropped the nickname "{token}".'

        # Provenance (D3), mirrors PinSelf → self_history.log_event. This is
        # on the tool-call hot path, so a history-write failure must never
        # take down the (already-succeeded) alias write — log loudly, don't
        # raise.
        try:
            self_history.log_event(
                ctx.memory_root / "aliases_history.jsonl",
                kind="learn_name",
                op=self.op,
                token=token,
                origin_tier=ctx.origin_tier,
                external_ctx=ctx.external_ctx,
            )
        except OSError:
            logger.exception(
                "aliases_history.jsonl write failed for token %r; continuing", token
            )

        _record(ctx, "LearnName", self._summary())
        return result


MAIN_TOOLS: list[type[BaseModel]] = [
    NoteMemory, WriteDiary, WriteSchedule, Shell,
    InvokeSkill, Recall, SpawnWorkflow, SpawnMonitor, RemoveMonitor,
    ReadToolOutput, GrepToolOutput,
    Scratchpad,
    SetFocus, OpenLoop, CloseLoop,
    MoodTool,
    PursueGoal, AdvanceGoal,
]

# What each turn may run is not a list here: the per-principal table is
# dollos.harness.permissions.TOOL_MATRIX and the per-turn-kind table
# (reflection / diary / agenda / pulse / safe mode, and the extras PinSelf,
# NoteToolLesson, SelfRevision, LearnName) is
# dollos.harness.turn_policy.POLICIES. MAIN_TOOLS is MindLoop's registry;
# SUB_TOOLS / KEEPER_TOOLS below are the agents' registries (their kinds are
# POLICIES["worker"] / POLICIES["keeper"]).
# P1e S4/S5 external_public (strangers, non-owner DMs, MCP peers, the owner
# posting in a PUBLIC channel) → TOOL_MATRIX's external_public row. The
# owner's own DM is NOT external for tools: owner DM = the owner at the local
# keyboard, Shell included (user decision 2026-10-27, harness spec §6.3; the
# user accepted the risk that a compromised Discord account then equals the
# owner on the home computer). DiscordLookup is intentionally NOT included —
# deferred to P2.

SUB_TOOLS: list[type[BaseModel]] = [
    Shell, NoteMemory, Recall, InvokeSkill, Report,
    SpawnMonitor, RemoveMonitor, ReadToolOutput, GrepToolOutput,
    Scratchpad,
    SetFocus, OpenLoop, CloseLoop,
]

# B2 memory-keeper allowlist: only Report + Scratchpad (no Shell/NoteMemory/SpawnMonitor/RemoveMonitor).
KEEPER_TOOLS: list[type[BaseModel]] = [Report, Scratchpad]

# The daily profile keeper's tools (remember-corrections plan Task 4,
# decision 3): EditOwnerProfile is in no other list — not MAIN_TOOLS, not an
# extra — so Doll is never offered it and her tool JSON is unchanged.
PROFILE_KEEPER_TOOLS: list[type[BaseModel]] = [EditOwnerProfile, Report]

