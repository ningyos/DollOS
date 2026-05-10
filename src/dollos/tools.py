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
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from dollos.ipc.messages import ServerMessage, TextChunk
from dollos.memory_writer import append_transcript

if TYPE_CHECKING:
    from memsearch import MemSearch

    from dollos.subagent import SubagentRunner

logger = logging.getLogger(__name__)

SHELL_DEFAULT_TIMEOUT_S = 30
SHELL_MAX_TIMEOUT_S = 300
SHELL_OUTPUT_MAX_CHARS = 8000

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
    """Execute a shell command. Returns combined stdout+stderr.

    Subprocess runs with the daemon's user permissions. Working directory
    starts at settings.data.root each call (cd does NOT persist between
    calls — each Shell invocation is a fresh subprocess).

    Use this for any system inspection (ls, cat, find, ps, ...) or any
    command-line task. Output is truncated to 8000 chars total if longer.
    """

    command: str = Field(
        description="The shell command to run (will be passed to bash -c)."
    )
    timeout_s: int = Field(
        default=SHELL_DEFAULT_TIMEOUT_S,
        ge=1,
        le=SHELL_MAX_TIMEOUT_S,
        description=(
            f"Seconds before timeout. Default {SHELL_DEFAULT_TIMEOUT_S}, "
            f"max {SHELL_MAX_TIMEOUT_S}."
        ),
    )

    async def run(self, ctx: ToolCtx) -> str:
        cwd = ctx.memory_root.parent
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["bash", "-c", self.command],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return f"[shell timeout after {self.timeout_s}s]"
        combined = proc.stdout
        if proc.stderr:
            combined += proc.stderr
        prefix = f"[exit {proc.returncode}]\n"
        body = _truncate(combined, SHELL_OUTPUT_MAX_CHARS)
        return prefix + body


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


MAIN_TOOLS: list[type[BaseModel]] = [
    Say, NoteMemory, WriteDiary, Shell, InvokeSkill, Recall, SpawnSubagent,
]

SUB_TOOLS: list[type[BaseModel]] = [
    Shell, NoteMemory, Recall, InvokeSkill, Report,
]

# Back-compat alias — many modules / tests import TOOLS directly.
TOOLS: list[type[BaseModel]] = MAIN_TOOLS
