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
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from dollos.ipc.messages import ServerMessage, TextChunk
from dollos.memory_writer import append_transcript

if TYPE_CHECKING:
    from memsearch import MemSearch

logger = logging.getLogger(__name__)

SHELL_DEFAULT_TIMEOUT_S = 30
SHELL_MAX_TIMEOUT_S = 300
SHELL_OUTPUT_MAX_CHARS = 8000


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
    """Narrow execution context passed to Tool.run()."""

    sink: asyncio.Queue[ServerMessage | None]
    memory_root: Path
    memsearch: MemSearch
    transcripts_root: Path


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
        timestamp = datetime.now().strftime("%H:%M")
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
    """

    query: str = Field(
        description=(
            "What to search for in memory. Specific keywords work best."
        )
    )

    async def run(self, ctx: ToolCtx) -> str:
        hits = await ctx.memsearch.search(self.query, top_k=5)
        if not hits:
            return "[no relevant memory]"
        return "\n".join(f"- {h['content']}" for h in hits)


TOOLS: list[type[BaseModel]] = [
    Say, NoteMemory, WriteDiary, Shell, InvokeSkill, Recall,
]
