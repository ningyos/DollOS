"""Scratchpad — Doll's ephemeral working memory.

A 2000-char text document, auto-rendered at the top of every Doll
perception. Doll writes / edits / clears via the four pydantic tools
in this module. Lifetime: daemon process. Storage: in-memory string,
no file backing.

See docs/superpowers/specs/2026-05-16-scratchpad-design.md.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from dollos.tools import ToolCtx


class Scratchpad:
    """In-memory working memory for Doll. Bounded at 2000 chars."""

    HARD_CAP = 2000

    def __init__(self) -> None:
        self._content = ""
        # No lock: tool calls within a cascade iter are awaited sequentially.
        # If true concurrent mutation becomes a real scenario, add asyncio.Lock.

    def read(self) -> str:
        return self._content

    def write(self, content: str) -> None:
        if len(content) > self.HARD_CAP:
            raise ValueError(
                f"scratchpad write exceeds {self.HARD_CAP} char cap "
                f"({len(content)} chars). Edit or Clear first."
            )
        self._content = content

    def append(self, text: str) -> int:
        sep = "\n" if self._content else ""
        new_total = len(self._content) + len(sep) + len(text)
        if new_total > self.HARD_CAP:
            raise ValueError(
                f"scratchpad append would exceed {self.HARD_CAP} chars "
                f"({new_total} after append). Edit or Clear first."
            )
        self._content = self._content + sep + text
        return new_total

    def edit(self, old: str, new: str) -> None:
        count = self._content.count(old)
        if count == 0:
            raise ValueError(f"old_string not found in scratchpad: {old!r}")
        if count > 1:
            raise ValueError(
                f"old_string appears {count} times — add more context to disambiguate"
            )
        new_content = self._content.replace(old, new, 1)
        if len(new_content) > self.HARD_CAP:
            raise ValueError(
                f"edit would push scratchpad to {len(new_content)} chars "
                f"(cap {self.HARD_CAP})."
            )
        self._content = new_content

    def clear(self) -> None:
        self._content = ""


class WriteScratchpad(BaseModel):
    """Overwrite the scratchpad with new content.

    Hard cap 2000 chars. Use this when starting fresh or when existing
    content is irrelevant to current work.
    """

    content: str = Field(..., description="full new scratchpad contents (≤2000 chars)")

    async def run(self, ctx: "ToolCtx") -> str:
        ctx.scratchpad.write(self.content)
        return f"scratchpad set ({len(self.content)} chars)"


class AppendScratchpad(BaseModel):
    """Append a line to the end of the scratchpad.

    A newline separator is auto-prepended if the scratchpad is non-empty.
    Raises ValueError if appending would exceed 2000 chars.
    """

    text: str = Field(..., description="text to append as a new line")

    async def run(self, ctx: "ToolCtx") -> str:
        new_total = ctx.scratchpad.append(self.text)
        return f"scratchpad now {new_total} chars"


class EditScratchpad(BaseModel):
    """Replace a unique substring in the scratchpad.

    Same semantics as Claude Code's Edit tool: old_string must appear
    exactly once in the current contents. Use longer old_string with
    surrounding context if a short substring is ambiguous.
    """

    old_string: str = Field(..., description="exact substring to replace; must appear exactly once")
    new_string: str = Field(..., description="replacement text")

    async def run(self, ctx: "ToolCtx") -> str:
        ctx.scratchpad.edit(self.old_string, self.new_string)
        return "scratchpad edited"


class ClearScratchpad(BaseModel):
    """Wipe the scratchpad to empty."""

    async def run(self, ctx: "ToolCtx") -> str:
        ctx.scratchpad.clear()
        return "scratchpad cleared"
