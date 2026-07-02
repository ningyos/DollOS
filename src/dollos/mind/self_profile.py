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


def _format_bullet(b: Bullet) -> str:
    return f"- [{b.id}·{b.date}] {b.text}"


def _serialize(sections: dict[str, list[Bullet]]) -> str:
    out: list[str] = []
    for key in SECTION_ORDER:
        out.append(f"## {SECTION_TITLES[key]}")
        for b in sections[key]:
            out.append(_format_bullet(b))
    return "\n".join(out) + "\n"


def _next_id(bullets: list[Bullet], section: str) -> str:
    """Derive the next id purely from bullets currently present in this
    section (max numeric suffix + 1, or 1 if none). Ids are display-time
    labels regenerated each turn from the current file state — a freed id
    being reused is invisible to Doll and causes no confusion."""
    prefix = _PREFIX[section]
    nums = [int(b.id[len(prefix):]) for b in bullets
            if b.id.startswith(prefix) and b.id[len(prefix):].isdigit()]
    return f"{prefix}{(max(nums) + 1) if nums else 1}"


# The real LLM does not reliably pass the bare id back — it often echoes the
# full rendered bullet (or a paraphrase of its text) as `target`. Resolve in
# three steps, first match wins, searching across ALL sections:
#   1. an id token (s1/r2/u3) embedded anywhere in target
#   2. target's text (after stripping a leading "- [...]" tag) exactly
#      equals some bullet's text
#   3. target's cleaned text is a substring of — or contains — exactly one
#      bullet's text (0 or >1 candidates is treated as no match, so a short
#      accidental overlap never silently mutates/deletes the wrong entry)
_ID_TOKEN_RE = re.compile(r"[sru]\d+")
_LEADING_TAG_RE = re.compile(r"^\s*-?\s*\[[^\]]*\]\s*")


def _clean_target_text(target: str) -> str:
    return _LEADING_TAG_RE.sub("", target).strip()


def _strip_incoming_tag(text: str) -> str:
    """Strip a leading tag the model may prepend to PinSelf text.
    Only strips tags containing '·' (our bullet format), preserving
    legitimate content that starts with [brackets] but lacks the date separator."""
    return re.sub(r'^\s*(?:- )?\[[^\]]*·[^\]]*\]\s*', '', text).strip()


def _find(sections: dict[str, list[Bullet]], target: str) -> tuple[str, int] | None:
    all_bullets = [(key, i, b) for key in SECTION_ORDER for i, b in enumerate(sections[key])]

    m = _ID_TOKEN_RE.search(target)
    if m:
        token = m.group(0)
        for key, i, b in all_bullets:
            if b.id == token:
                return key, i

    cleaned = _clean_target_text(target)
    if cleaned:
        for key, i, b in all_bullets:
            if b.text == cleaned:
                return key, i

        candidates = [(key, i) for key, i, b in all_bullets
                      if cleaned in b.text or b.text in cleaned]
        if len(candidates) == 1:
            return candidates[0]

    return None


def _entries_listing(sections: dict[str, list[Bullet]]) -> str:
    """Richer than a bare id list — shows `id: text` so the model can retry
    either by id or by pasting the exact line."""
    entries = [f"{b.id}: {b.text}" for key in SECTION_ORDER for b in sections[key]]
    return "、".join(entries) if entries else "(目前沒有任何條目)"


def apply(path: Path, *, section: str, op: str, target: str, text: str,
          max_chars: int, today: str) -> str:
    """Read-modify-write self_profile.md. Returns a human-readable result or a
    friendly-error string (never raises for cap/locate misses)."""
    raw = path.read_text() if path.exists() else ""
    sections = _parse(raw)

    if op == "add":
        if section not in SECTION_ORDER:
            return f"未知 section:{section}"
        clean_text = _strip_incoming_tag(text)
        # Idempotent add: an identical bullet already in this section is a no-op,
        # not a duplicate. PinSelf sits in the in-turn refeed allowlist (so a
        # failed replace/remove can retry same-turn), which means a successful
        # add is re-fed; the weak model sometimes re-emits the identical pin
        # every pass up to MAX_SYNC_REFEED_PASSES. Without this guard that wrote
        # up to 8 identical bullets into the always-injected profile (smoke-found).
        existing = next(
            (b for b in sections[section] if b.text == clean_text), None
        )
        if existing is not None:
            return f"已有相同條目:{existing.id}(未重複新增)"
        new_id = _next_id(sections[section], section)
        sections[section].append(Bullet(id=new_id, date=today, text=clean_text))
        result = f"已 pin 到「{SECTION_TITLES[section]}」:{new_id}"
    elif op == "replace":
        found = _find(sections, target)
        if found is None:
            return (f"找不到符合「{target}」的條目;現有:{_entries_listing(sections)}。"
                     f"可用 id(如 s1)或貼該條目文字重試。")
        key, i = found
        old_id = sections[key][i].id
        clean_text = _strip_incoming_tag(text)
        sections[key][i] = Bullet(id=old_id, date=today, text=clean_text)
        result = f"已更新 {old_id}"
    elif op == "remove":
        found = _find(sections, target)
        if found is None:
            return (f"找不到符合「{target}」的條目;現有:{_entries_listing(sections)}。"
                     f"可用 id(如 s1)或貼該條目文字重試。")
        key, i = found
        old_id = sections[key][i].id
        sections[key].pop(i)
        result = f"已移除 {old_id}"
    else:
        return f"未知 op:{op}"

    serialized = _serialize(sections)
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
            out.append(_format_bullet(b))
    return "\n".join(out)
