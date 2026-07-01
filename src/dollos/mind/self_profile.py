"""A1 self-profile store — Doll-pinned, always-injected evolving self.

Pure read-modify-write over a markdown file with three fixed sections and
id-tagged bullets: ``- [<id>·<YYYY-MM-DD>] <text>``. No indexing, no LLM.
Kept as a standalone module (like scratchpad_helpers) so the parse / id /
cap / locate logic is unit-testable in isolation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SECTION_TITLES: dict[str, str] = {
    "self": "我學到的自己",
    "relationship": "我和主人",
    "user": "我注意到的主人",
}
SECTION_ORDER: list[str] = ["self", "relationship", "user"]
_PREFIX: dict[str, str] = {"self": "s", "relationship": "r", "user": "u"}
_TITLE_TO_SECTION: dict[str, str] = {v: k for k, v in SECTION_TITLES.items()}

# - [s1·2026-06-30] text
_BULLET_RE = re.compile(r"^- \[([a-z]\d+)·(\d{4}-\d{2}-\d{2})\] (.*)$")

# <!-- counters: self=2 relationship=0 user=1 -->
# Persists the per-section high-water mark so ids are never reused after a
# remove (a plain "max of currently-present ids" would reset to 1 once a
# section's last bullet is removed). Invisible to _BULLET_RE / render_block.
_COUNTER_RE = re.compile(r"^<!-- counters: (.*) -->$")


@dataclass
class Bullet:
    id: str
    date: str
    text: str


def _empty_sections() -> dict[str, list[Bullet]]:
    return {k: [] for k in SECTION_ORDER}


def _parse(text: str) -> dict[str, list[Bullet]]:
    sections = _empty_sections()
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            title = stripped[3:].strip()
            current = _TITLE_TO_SECTION.get(title)
            continue
        m = _BULLET_RE.match(stripped)
        if m and current is not None:
            sections[current].append(Bullet(id=m.group(1), date=m.group(2), text=m.group(3)))
    return sections


def _parse_counters(text: str) -> dict[str, int]:
    """High-water mark per section, persisted across removes. Reconciled
    against any bullets actually present so a hand-edited/missing footer
    can never cause id reuse."""
    counters = {k: 0 for k in SECTION_ORDER}
    for line in text.splitlines():
        m = _COUNTER_RE.match(line.strip())
        if m:
            for part in m.group(1).split():
                key, _, val = part.partition("=")
                if key in counters and val.isdigit():
                    counters[key] = int(val)
    return counters


def _serialize(sections: dict[str, list[Bullet]], counters: dict[str, int]) -> str:
    out: list[str] = []
    for key in SECTION_ORDER:
        out.append(f"## {SECTION_TITLES[key]}")
        for b in sections[key]:
            out.append(f"- [{b.id}·{b.date}] {b.text}")
    out.append("<!-- counters: " + " ".join(f"{k}={counters[k]}" for k in SECTION_ORDER) + " -->")
    return "\n".join(out) + "\n"


def _next_id(sections: dict[str, list[Bullet]], counters: dict[str, int], section: str) -> str:
    prefix = _PREFIX[section]
    present = [int(b.id[len(prefix):]) for b in sections[section]
               if b.id.startswith(prefix) and b.id[len(prefix):].isdigit()]
    high_water = max(counters[section], *present) if present else counters[section]
    counters[section] = high_water + 1
    return f"{prefix}{counters[section]}"


def _find(sections: dict[str, list[Bullet]], target: str) -> tuple[str, int] | None:
    for key in SECTION_ORDER:
        for i, b in enumerate(sections[key]):
            if b.id == target:
                return key, i
    return None


def _existing_ids(sections: dict[str, list[Bullet]]) -> str:
    ids = [b.id for key in SECTION_ORDER for b in sections[key]]
    return "、".join(ids) if ids else "(目前沒有任何條目)"


def apply(path: Path, *, section: str, op: str, target: str, text: str,
          max_chars: int, today: str) -> str:
    """Read-modify-write self_profile.md. Returns a human-readable result or a
    friendly-error string (never raises for cap/locate misses)."""
    raw = path.read_text() if path.exists() else ""
    sections = _parse(raw)
    counters = _parse_counters(raw)

    if op == "add":
        new_id = _next_id(sections, counters, section)
        sections[section].append(Bullet(id=new_id, date=today, text=text))
        result = f"已 pin 到「{SECTION_TITLES[section]}」:{new_id}"
    elif op == "replace":
        found = _find(sections, target)
        if found is None:
            return f"找不到 id {target};現有:{_existing_ids(sections)}。請貼正確的 id。"
        key, i = found
        sections[key][i] = Bullet(id=target, date=today, text=text)
        result = f"已更新 {target}"
    elif op == "remove":
        found = _find(sections, target)
        if found is None:
            return f"找不到 id {target};現有:{_existing_ids(sections)}。請貼正確的 id。"
        key, i = found
        sections[key].pop(i)
        result = f"已移除 {target}"
    else:
        return f"未知 op:{op}"

    serialized = _serialize(sections, counters)
    if op in ("add", "replace") and len(serialized) > max_chars:
        return (f"self-profile 已達上限({max_chars} 字),寫入後會是 {len(serialized)} 字。"
                f"先 remove/replace 一些再 pin。")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized)
    return result


def render_block(path: Path) -> str | None:
    """Body for the [Self profile] block; None if no bullets anywhere.
    Empty sections (no bullets) are skipped."""
    if not path.exists():
        return None
    sections = _parse(path.read_text())
    if not any(sections[k] for k in SECTION_ORDER):
        return None
    out: list[str] = []
    for key in SECTION_ORDER:
        if not sections[key]:
            continue
        out.append(f"## {SECTION_TITLES[key]}")
        for b in sections[key]:
            out.append(f"- [{b.id}·{b.date}] {b.text}")
    return "\n".join(out)
