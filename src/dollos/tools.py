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

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, field_validator

from dollos.ipc.messages import ServerMessage
from dollos.mind import scratchpad_helpers
from dollos.mind.mind_state import OutputRecord

if TYPE_CHECKING:
    from dollos.memory import FtsMemory

    from dollos.mind.mind_ctx import MindCtx
    from dollos.mind.mind_state import MindState
    from dollos.monitor_runner import MonitorRunner
    from dollos.shell_runner import ShellRunner
    from dollos.tool_outputs import ToolOutputStore


def _record(ctx: "MindCtx", kind: str, summary: str) -> None:
    """Append an OutputRecord to ctx.mind_state.recent_outputs."""
    ctx.mind_state.recent_outputs.append(
        OutputRecord(t=time.time(), kind=kind, summary=summary)
    )

logger = logging.getLogger(__name__)

_FILE_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.md$")


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


def _format_hit(hit: dict) -> str:
    d = _hit_date(hit)
    content = hit.get("content", "")
    # B2 candidate pull-only: consolidated/ hits surface via Recall with a
    # provenance prefix so Doll knows they are unconfirmed candidates.
    prefix = "[系統整併·待確認] " if "consolidated/" in (hit.get("source") or "") else ""
    if d is not None:
        return f"- {prefix}{d.isoformat()} {content}"
    return f"- {prefix}{content}"


# ---------------------------------------------------------------------------
# Tools — all run(ctx: MindCtx)
# ---------------------------------------------------------------------------


class NoteMemory(BaseModel):
    """Record an episodic fact or event into Doll's memory (daily markdown + memsearch index).
    Use this for things that happened, or concrete facts worth recalling later — 主詞是世界
    或事件的,歸這裡。NOTE: 主詞是「你」的(你的看法、興趣、好奇——即使剛萌芽——你和主人
    的關係、你注意到的主人),用 PinSelf(reflection 時)記進 always-in-context 的
    self-profile,不歸這裡;同一個經驗可以兩邊各記一筆。"""

    text: str = Field(
        description="The fact to record. One sentence, declarative."
    )

    def _summary(self) -> str:
        return f"noted: {self.text[:73]}"

    async def run(self, ctx: "MindCtx") -> str:
        from dollos.mind.context_tags import build_heading

        path = ctx.memory_root / "shared" / f"{date.today():%Y-%m-%d}.md"
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

        path = ctx.memory_root / "shared" / f"{date.today():%Y-%m-%d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        heading = build_heading(ctx.mind_state)
        with path.open("a") as f:
            f.write(f"\n## {heading} 日記\n\n{self.content}\n")
        await ctx.memsearch.index_file(path)
        _record(ctx, "WriteDiary", self._summary())
        return "diary written"


class Shell(BaseModel):
    """Run a shell command in the background. Returns immediately.

    Shell is fire-and-forget. The command runs as a fresh subprocess (no
    cd persistence between calls) with the daemon's user permissions and
    cwd = data/ (the parent of memory/). stdout + stderr are merged. When
    the proc finishes, its result comes back as a NEW turn's perception
    starting with 「你執行的 shell 命令回來了」 — react to it then.

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
            f"timeout={self.timeout_s}s). 結果會以新事件回來。"
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
            return (
                f"Skill '{self.name}' 不存在。"
                f"目前可用 skills: {available}\n"
                f"建議：用 Shell 動手做 / 直接用語音回答 / 用 Recall 找其他相關記憶。"
                f"不要再猜其他 skill 名字。"
            )
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

    def _summary(self) -> str:
        return f"recalled: {self.query[:72]}"

    async def run(self, ctx: "MindCtx") -> str:
        hits = await ctx.memsearch.search(self.query, top_k=5)
        if self.since is not None or self.until is not None:
            hits = [h for h in hits if _hit_in_range(h, self.since, self.until)]
        if not hits:
            result = "[no relevant memory]"
        else:
            result = "\n".join(_format_hit(h) for h in hits)
        _record(ctx, "Recall", self._summary())
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
            f"timeout={self.timeout_s}s). 結果會以新事件回來。"
        )
        _record(ctx, "SpawnWorkflow", self._summary())
        return result


class SpawnMonitor(BaseModel):
    """Spawn a background command watcher. Returns a monitor_id immediately.

    The command runs as a long-lived subprocess. Each stdout line
    (optionally regex-filtered) fires a MonitorTriggeredEvent that
    arrives as a new turn's perception starting with 「monitor 觸發」.
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
            f"觸發 / 結束都會以新事件回來。"
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
        # Side-effect: stash args into ctx for run_agent to pick up.
        # Returning None ends the cascade naturally (no tool_response cycle).
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
            f.write(f"\n## {heading} 計劃\n\n")
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


# ---------------------------------------------------------------------------
# Mood tool — mutates ctx.mind_state.mood
# ---------------------------------------------------------------------------


class MoodTool(BaseModel):
    """Update Doll's current emotional state.

    Call this when your inner <think> mood assessment has shifted. The new
    mood is stored in MindState and surfaces in every subsequent iteration's
    [Mind state] block.
    """

    emotion: str = Field(
        ...,
        description="Current emotion in one Chinese word or short phrase (e.g. '開心', '有點擔心').",
    )
    reason: str = Field(
        default="",
        description="Brief reason for the mood shift (one sentence, optional).",
    )

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
    """Pin or revise an entry in your self-profile (reflection turns only) — this
    is YOUR evolving self and it is ALWAYS in context. 判斷標準是主詞:句子是關於
    「你」的(你的看法、興趣、立場、好奇、你和主人的關係、你注意到的主人),就用
    PinSelf;關於世界的事實(即使是你查到、你覺得有趣的),用 NoteMemory——同一個
    經驗可以兩邊各記一筆,事實歸 NoteMemory,它揭露的「你」歸這裡。剛萌芽、還不
    確定算不算「你」的也可以先 pin,之後反思時再篩。空間有限,定期用 replace/remove
    淘汰「已經不是現在的你」的條目——活下來的才是你的核心。"""

    section: Literal["self", "relationship", "user"] = Field(
        description="哪一段:self=關於你自己(身分、看法、興趣、好奇) / relationship=你和主人 / user=你注意到的主人。replace/remove 也填(以 target 為準)。"
    )
    op: Literal["add", "replace", "remove"] = Field(
        description="add=新增一條 / replace=用 target 定位換成 text / remove=用 target 定位刪除。"
    )
    target: str = Field(
        description='For replace/remove: the id to target (e.g. "s1", "r2"). For op=add, leave this an empty string "".'
    )
    text: str = Field(
        description="add/replace 的新內容(你自己的話,別用全形引號「」『』);remove 時填空字串。"
    )

    def _summary(self) -> str:
        return f"self {self.op} {self.target or self.section}"

    async def run(self, ctx: "MindCtx") -> str:
        from dollos.mind import self_profile
        path = ctx.memory_root / "self_profile.md"
        today = f"{datetime.now():%Y-%m-%d}"
        result = self_profile.apply(
            path,
            section=self.section,
            op=self.op,
            target=self.target,
            text=self.text,
            max_chars=ctx.self_profile_max_chars,
            today=today,
        )
        # 絕不 index_file(§3.1):self_profile.md 靠 always-inject,不進召回。
        _record(ctx, "PinSelf", self._summary())
        return result


MAIN_TOOLS: list[type[BaseModel]] = [
    NoteMemory, WriteDiary, WriteSchedule, Shell,
    InvokeSkill, Recall, SpawnWorkflow, SpawnMonitor, RemoveMonitor,
    ReadToolOutput, GrepToolOutput,
    Scratchpad,
    SetFocus, OpenLoop, CloseLoop,
    MoodTool,
]

REFLECTION_TOOLS: list[type[BaseModel]] = MAIN_TOOLS + [NoteToolLesson, PinSelf]

SUB_TOOLS: list[type[BaseModel]] = [
    Shell, NoteMemory, Recall, InvokeSkill, Report,
    SpawnMonitor, RemoveMonitor, ReadToolOutput, GrepToolOutput,
    Scratchpad,
    SetFocus, OpenLoop, CloseLoop,
]

# B2 memory-keeper allowlist: only Report + Scratchpad (no Shell/NoteMemory/SpawnMonitor/RemoveMonitor).
KEEPER_TOOLS: list[type[BaseModel]] = [Report, Scratchpad]
