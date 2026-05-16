"""Action types and tolerant parser."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class Action:
    kind: str
    payload: dict[str, Any]

    def __repr__(self) -> str:
        return f"Action({self.kind}, {self.payload})"


_VALID_KINDS = {
    "Say", "Think", "SetFocus", "OpenLoop", "CloseLoop",
    "Dispatch",
}


def _strip_fences(text: str) -> str:
    text = text.strip()
    # remove ```json ... ``` fences
    m = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def _extract_json_array(text: str) -> str | None:
    """Find first balanced [ ... ] in text."""
    start = text.find("[")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_actions(raw: str) -> tuple[list[Action], str | None]:
    """Return (actions, fallback_text). If fallback_text is set, treat as Think.
    Tolerant: tries JSON first, then strips fences, then extracts first array.
    Plain text → fallback_text returned.
    """
    if not raw or not raw.strip():
        return [], None

    cleaned = _strip_fences(raw)

    candidates = [cleaned]
    arr = _extract_json_array(cleaned)
    if arr and arr != cleaned:
        candidates.append(arr)

    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            continue
        actions: list[Action] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            kind = item.get("action") or item.get("kind") or item.get("type")
            if not kind or kind not in _VALID_KINDS:
                continue
            payload = {k: v for k, v in item.items() if k not in ("action", "kind", "type")}
            actions.append(Action(kind, payload))
        if actions:
            return actions, None
        # parsed but no valid actions — treat as fallback
        return [], raw.strip()

    # nothing parsed — fallback
    return [], raw.strip()
