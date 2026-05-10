"""Tool definitions — pydantic models with run() methods.

Step 6 minimal: two tools (Say, NoteMemory). Tool = BaseModel; args are
fields; description = docstring; schema = model_json_schema(); execution
= run(ctx). Single source of truth per tool.

Future: step 7 adds reflex (whitelist via class attribute), step 9 adds
spawn_subagent (fast=False async pattern). For now no permission /
streamable / fast metadata — YAGNI.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, field_validator

from dollos.ipc.messages import ServerMessage, TextChunk
from dollos.memory_writer import append_transcript
from dollos.process_registry import ProcessRegistry

if TYPE_CHECKING:
    from memsearch import MemSearch

    from dollos.subagent import SubagentRunner

logger = logging.getLogger(__name__)

SHELL_OUTPUT_MAX_CHARS = 8000
MONITOR_DEFAULT_TIMEOUT_S = 60
MONITOR_MAX_TIMEOUT_S = 600

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
    if d is not None:
        return f"- {d.isoformat()} {hit.get('content', '')}"
    return f"- {hit.get('content', '')}"


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    half = cap // 2
    head = text[:half]
    tail = text[-half:]
    dropped = len(text) - 2 * half
    return f"{head}\n...[truncated {dropped} chars]...\n{tail}"


@dataclass
class ToolCtx:
    """Narrow execution context passed to Tool.run().

    `subagent_runner` is set on the main-cascade ctx so SpawnSubagent can
    schedule background workers; remains None inside a sub-cascade (and
    SUB_TOOLS doesn't include SpawnSubagent anyway, so subagent recursion
    is structurally impossible).

    `subagent_report` is None in the main cascade and set by the Report
    tool inside a sub-cascade — SubagentRunner reads it back to build the
    SubagentResultEvent.
    """

    sink: asyncio.Queue[ServerMessage | None] | None
    memory_root: Path
    memsearch: MemSearch
    transcripts_root: Path
    subagent_runner: "SubagentRunner | None" = None
    subagent_report: dict | None = None
    process_registry: ProcessRegistry | None = None
    pending_signal: asyncio.Event | None = None


class Say(BaseModel):
    """Stream text to the user. Call this whenever Doll wants to speak."""

    text: str = Field(description="What Doll says to the user.")

    async def run(self, ctx: ToolCtx) -> None:
        ctx.sink.put_nowait(TextChunk(text=self.text))
        try:
            await append_transcript(
                transcripts_root=ctx.transcripts_root,
                memsearch=ctx.memsearch,
                role="doll",
                text=self.text,
            )
        except Exception:
            logger.exception("transcript append failed for Say")


class NoteMemory(BaseModel):
    """Record a fact into Doll's memory (daily markdown + memsearch index)."""

    text: str = Field(
        description="The fact to record. One sentence, declarative."
    )

    async def run(self, ctx: ToolCtx) -> None:
        path = ctx.memory_root / "shared" / f"{date.today():%Y-%m-%d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Sync append + index inside async — append is a single small write
        # (microseconds). asyncio.to_thread wrap is YAGNI for step 6.
        with path.open("a") as f:
            f.write(f"- {self.text}\n")
        await ctx.memsearch.index_file(path)


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

    async def run(self, ctx: ToolCtx) -> None:
        path = ctx.memory_root / "shared" / f"{date.today():%Y-%m-%d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%H:%M:%S")
        with path.open("a") as f:
            f.write(f"\n## 日記 ({timestamp})\n\n{self.content}\n")
        await ctx.memsearch.index_file(path)


class Shell(BaseModel):
    """Spawn a shell command in the background. Returns a handle immediately.

    The command runs as a fresh subprocess (no cd persistence between calls)
    with the daemon's user permissions and ``cwd = data/`` (the parent of
    memory/). stdout + stderr are merged on the pipe.

    This call **does not block** — Shell returns a handle string like
    ``"sh-1"``. Use ``Monitor(handle=...)`` to wait for the output and exit
    code. For short commands (pwd, ls), feel free to emit Shell + Monitor in
    the same cascade iteration; for long commands, you can do other work and
    Monitor later.
    """

    command: str = Field(
        description="The shell command to run (will be passed to bash -c)."
    )

    async def run(self, ctx: ToolCtx) -> str:
        if ctx.process_registry is None:
            return (
                "[Shell unavailable: no process_registry on this ctx — "
                "cannot dispatch background subprocess]"
            )
        cwd = ctx.memory_root.parent
        proc = await asyncio.create_subprocess_shell(
            self.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd),
        )
        handle = ctx.process_registry.register(proc, self.command)
        return (
            f"shell {handle} dispatched (command={self.command!r}). "
            f"Use Monitor({handle!r}) to wait."
        )


class Monitor(BaseModel):
    """Wait for a background process (from Shell) to finish.

    Blocks the cascade iteration until the process exits or the timeout
    elapses. On success, returns ``[exit N]\\n{output}`` (output truncated
    to 8000 chars). On timeout, the process is left running — Monitor again
    or Cancel it.
    """

    handle: str = Field(
        description="Handle returned by Shell (e.g., 'sh-1')."
    )
    timeout_s: int = Field(
        default=MONITOR_DEFAULT_TIMEOUT_S,
        ge=1,
        le=MONITOR_MAX_TIMEOUT_S,
        description=(
            f"Wait at most this many seconds. Default "
            f"{MONITOR_DEFAULT_TIMEOUT_S}, max {MONITOR_MAX_TIMEOUT_S}."
        ),
    )

    async def run(self, ctx: ToolCtx) -> str:
        if ctx.process_registry is None:
            return (
                "[Monitor unavailable: no process_registry on this ctx]"
            )
        managed = ctx.process_registry.get(self.handle)
        if managed is None:
            return (
                f"unknown handle {self.handle!r} "
                f"(already monitored or not found)"
            )
        proc = managed.proc
        proc_task = asyncio.create_task(proc.communicate())
        pending_signal = ctx.pending_signal
        pending_task: asyncio.Task | None = None
        waiters: list[asyncio.Task] = [proc_task]
        if pending_signal is not None:
            pending_task = asyncio.create_task(pending_signal.wait())
            waiters.append(pending_task)

        try:
            done, _ = await asyncio.wait(
                waiters,
                timeout=self.timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            proc_task.cancel()
            if pending_task is not None:
                pending_task.cancel()
            raise

        # Pending event won the race — early return; process keeps running.
        if pending_task is not None and pending_task in done:
            proc_task.cancel()
            return (
                f"Monitor({self.handle!r}) interrupted by pending event. "
                f"Process still running. Use Cancel({self.handle!r}) to kill, "
                f"or skip and respond to pending."
            )

        # Process completed naturally.
        if proc_task in done:
            if pending_task is not None:
                pending_task.cancel()
            try:
                stdout, _ = proc_task.result()
            except Exception as e:
                return (
                    f"Monitor({self.handle!r}) error reading output: {e}"
                )
            output = stdout.decode("utf-8", errors="replace") if stdout else ""
            ctx.process_registry.remove(self.handle)
            return _truncate(
                f"[exit {proc.returncode}]\n{output}",
                SHELL_OUTPUT_MAX_CHARS,
            )

        # Timeout — neither task completed.
        proc_task.cancel()
        if pending_task is not None:
            pending_task.cancel()
        return (
            f"Monitor({self.handle!r}) timed out after "
            f"{self.timeout_s}s. Process still running. "
            f"Use Monitor again or Cancel."
        )


class Cancel(BaseModel):
    """Kill a background process started by Shell.

    Sends SIGKILL. Process must already be registered (via Shell). Use when
    Monitor was interrupted and you don't need the result, or when a process
    times out and you want to stop it.
    """

    handle: str = Field(
        description="Handle returned by Shell (e.g., 'sh-1')."
    )

    async def run(self, ctx: ToolCtx) -> str:
        if ctx.process_registry is None:
            return "[Cancel unavailable: no process_registry on this ctx]"
        managed = ctx.process_registry.get(self.handle)
        if managed is None:
            return f"unknown handle {self.handle!r}"
        proc = managed.proc
        if proc.returncode is not None:
            ctx.process_registry.remove(self.handle)
            return (
                f"process {self.handle!r} already exited with code "
                f"{proc.returncode}"
            )
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass  # process already gone
        except Exception as e:
            return f"failed to kill {self.handle!r}: {e}"
        ctx.process_registry.remove(self.handle)
        return f"killed {self.handle!r}"


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

    async def run(self, ctx: ToolCtx) -> str:
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
                f"建議：用 Shell 動手做 / Say 直接回答 / 用 Recall 找其他相關記憶。"
                f"不要再猜其他 skill 名字。"
            )
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

    async def run(self, ctx: ToolCtx) -> str:
        hits = await ctx.memsearch.search(self.query, top_k=5)
        if self.since is not None or self.until is not None:
            hits = [h for h in hits if _hit_in_range(h, self.since, self.until)]
        if not hits:
            return "[no relevant memory]"
        return "\n".join(_format_hit(h) for h in hits)


class SpawnSubagent(BaseModel):
    """Dispatch an ephemeral sub-worker to handle a task in the background.

    Returns immediately with a dispatch confirmation. The subagent runs in
    parallel with its own minimal toolset (Shell / NoteMemory / Recall /
    InvokeSkill / Report) and MUST end by calling Report. When it finishes
    (or times out / errors), the structured outcome comes back as a NEW
    turn's perception — you'll see it as a fresh user message starting with
    「你派出的 subagent 回來了」 and can react to it then.
    """

    task: str = Field(
        description=(
            "What the subagent should do. Be concrete: it has no character, "
            "no memory context, and no Doll persona — just the SUB_TOOLS toolkit "
            "and this single instruction string."
        )
    )
    timeout_s: int = Field(
        ge=1,
        le=600,
        description=(
            "Wall-clock seconds before the subagent is killed. Estimate from "
            "task complexity (30 short, 300 long; max 600). No default — pick "
            "a number every time."
        ),
    )

    async def run(self, ctx: ToolCtx) -> str:
        if ctx.subagent_runner is None:
            return (
                "[SpawnSubagent unavailable: no subagent runner on this ctx — "
                "you may be running inside a subagent, which cannot recurse]"
            )
        sub_id = str(uuid.uuid4())[:8]
        ctx.subagent_runner.spawn(
            sub_id=sub_id,
            task=self.task,
            timeout_s=self.timeout_s,
            response_sink=ctx.sink,
        )
        return (
            f"subagent {sub_id} dispatched "
            f"(task={self.task!r}, timeout={self.timeout_s}s). "
            f"Result will arrive as a new turn when it finishes."
        )


class Report(BaseModel):
    """Terminate this subagent and report the structured outcome to Doll.

    Subagent-only tool. MUST be called exactly once before the subagent ends
    — the cascade ends naturally after the call (Report.run returns None).
    The args become the SubagentResultEvent fields Doll sees on her next turn.
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

    async def run(self, ctx: ToolCtx) -> None:
        # Side-effect: stash args into ctx for SubagentRunner to pick up.
        # Returning None ends the cascade naturally (no tool_response cycle).
        ctx.subagent_report = {
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

    async def run(self, ctx: ToolCtx) -> str:
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
        timestamp = datetime.now().strftime("%H:%M:%S")
        with md_path.open("a") as f:
            f.write(f"\n## 計劃 ({timestamp})\n\n")
            for e in parsed:
                f.write(f"- {e.time:%H:%M:%S} — {e.intent}\n")
        await ctx.memsearch.index_file(md_path)

        return f"Schedule written: {len(parsed)} entries"


MAIN_TOOLS: list[type[BaseModel]] = [
    Say, NoteMemory, WriteDiary, WriteSchedule, Shell, Monitor, Cancel,
    InvokeSkill, Recall, SpawnSubagent,
]

SUB_TOOLS: list[type[BaseModel]] = [
    Shell, Monitor, Cancel, NoteMemory, Recall, InvokeSkill, Report,
]

# Back-compat alias — many modules / tests import TOOLS directly.
TOOLS: list[type[BaseModel]] = MAIN_TOOLS
