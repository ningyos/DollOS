"""Perception types fed into the MindLoop."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Perception:
    kind: str
    at: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        age = time.time() - self.at
        age_s = f"{age:.1f}s ago"
        if self.kind == "UserSpoke":
            return f"[{age_s}] UserSpoke: {self.data.get('text','')!r}"
        if self.kind == "ToolResultArrived":
            tool = self.data.get("tool", "?")
            cmd = self.data.get("command", "")
            rc = self.data.get("returncode", "?")
            out = self.data.get("stdout", "")
            err = self.data.get("stderr", "")
            # truncate long output
            if len(out) > 600:
                out = out[:300] + f"\n...(truncated, total {len(out)} chars)...\n" + out[-200:]
            err_s = f"\n  stderr: {err[:200]}" if err else ""
            return (
                f"[{age_s}] ToolResultArrived: tool={tool} cmd={cmd!r} rc={rc}\n"
                f"  stdout: {out}{err_s}"
            )
        if self.kind == "IdleTick":
            return f"[{age_s}] IdleTick"
        if self.kind == "Awoke":
            return f"[{age_s}] Awoke"
        return f"[{age_s}] {self.kind}: {self.data}"


def UserSpoke(text: str) -> Perception:
    return Perception("UserSpoke", data={"text": text})


def ToolResultArrived(
    tool: str, command: str, returncode: int, stdout: str, stderr: str, dispatch_id: str = ""
) -> Perception:
    return Perception(
        "ToolResultArrived",
        data={
            "tool": tool,
            "command": command,
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "dispatch_id": dispatch_id,
        },
    )


def IdleTick() -> Perception:
    return Perception("IdleTick")


def Awoke() -> Perception:
    return Perception("Awoke")
