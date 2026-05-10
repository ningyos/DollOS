# Plan: Time awareness + time-aware Recall

**Worktree**: `.worktrees/time-awareness/`
**Branch**: `time-awareness`
**Date**: 2026-05-10

## Why

DollOS has filesystem-level timestamps (`{YYYY-MM-DD}.md` filenames,
`[HH:MM role]` line prefixes, diary `## 日記 (HH:MM)` headers) but
**Doll's perception has no time awareness**:
- No current time / day-of-week injected into prompt
- `[Recent activity]` rolling buffer is timestampless
- Memsearch results don't surface dates
- No way to ask "what did we talk about yesterday" with date filter

Diary, memory, recall all should have temporal grounding. Time awareness
is the cheapest core-feature addition to make these interactions
natural.

## Out of scope

- Relative-time rendering ("3 minutes ago" — Doll computes from absolute
  if needed)
- Daemon uptime / last-interaction tracker
- Diary cross-day reference (today reads yesterday's diary
  automatically)
- Self-initiated time events (idle wake)
- Timezone handling (single local timezone assumption)
- NL date parsing in `Recall` ("昨天" → date) — Doll does the
  arithmetic herself; tool only takes ISO `YYYY-MM-DD`
- Memsearch API changes (filter at memsearch level) — we filter
  post-hoc in Recall.run

## Design

### A. Time injection

Every cascade's first user message gets a new `[Now]` block at the very
top:

```
[Now]
2026-05-10 14:23 週六下午

[Recent activity]
- 14:15 主人查 pwd
- 14:20 主人問我好不好

[Memory context]
- 2026-04-30 主人說喜歡黑咖啡
- 2026-05-08 聊了 Qwen3 prompt format

[Message]
{perception}
```

Format: `YYYY-MM-DD HH:MM 週X 早上/上午/下午/晚上/深夜`

Day-of-week + period-of-day descriptors in Chinese. Doll model
understands both English ISO and Chinese descriptors.

### B. Rolling buffer with timestamps

`EventDispatcher._rolling` type changes from `list[str]` to
`list[tuple[datetime, str]]`. When `compact_cascade` returns a summary,
append `(datetime.now(), summary)`.

`_format_recent_activity` renders `(time, summary)` tuples as
`- {HH:MM} {summary}` lines.

(Keep absolute HH:MM only; date inferable from `[Now]` block. If
rolling spans multiple days, render full date for older entries.)

### C. Time-aware Recall (tool)

`Recall` tool gains optional date filters:

```python
class Recall(BaseModel):
    """Search Doll's memory for relevant facts. Use when you need
    deeper context than the [Memory context] block already provides."""
    
    query: str = Field(description="What to search for in memory.")
    since: date | None = Field(
        default=None,
        description="Optional ISO YYYY-MM-DD lower bound (inclusive). Memory before this date is filtered out.",
    )
    until: date | None = Field(
        default=None,
        description="Optional ISO YYYY-MM-DD upper bound (inclusive). Memory after this date is filtered out.",
    )
    
    async def run(self, ctx: ToolCtx) -> str:
        hits = await ctx.memsearch.search(self.query, top_k=5)
        if self.since or self.until:
            hits = [h for h in hits if _hit_date_in_range(h, self.since, self.until)]
        if not hits:
            return "[no relevant memory]"
        return "\n".join(_format_hit(h) for h in hits)
```

`_hit_date_in_range(hit, since, until)`:
- Extract `YYYY-MM-DD` from `hit["source"]` filename via regex
- If no date extractable: skip (since/until filter implies date matters)
- Else: check inclusive range

`_format_hit(hit)`:
- Returns `- {date} {content}` if date extractable
- Else: `- {content}` (current behavior fallback)

### D. Memory context (IV.recall) timestamp prefix

`InnerVoice.recall()` currently:
- Calls `memsearch.search`
- Pipes hits into `iv_recall.jinja` for small-LLM filter
- Returns filtered string

Change: prepend file-date to each hit's content before sending to
small-model. Format: `1. 2026-05-08 {content}` (or change list format
to date-prefixed). Small-model filter sees dates and can preserve them.

Or simpler — change `iv_recall.jinja`'s `{% for h in hits %}{{ h.content }}{% endfor %}` to include date.

## Changes

### 1. `src/dollos/dispatcher.py`

Add `_format_now()` static method or module function:

```python
def _format_now(now: datetime) -> str:
    weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    period = (
        "深夜" if now.hour < 5 else
        "早上" if now.hour < 9 else
        "上午" if now.hour < 12 else
        "下午" if now.hour < 18 else
        "晚上"
    )
    return f"[Now]\n{now:%Y-%m-%d %H:%M} {weekdays[now.weekday()]}{period}\n\n"
```

In `_respond`, prepend `[Now]` block to `framed_user`.

`_rolling` type → `list[tuple[datetime, str]]`. Append `(datetime.now(), summary)` at compact time.

`_format_recent_activity`:
```python
def _format_recent_activity(self) -> str:
    if not self._rolling:
        return ""
    today = date.today()
    lines = []
    for ts, summary in self._rolling:
        if ts.date() == today:
            prefix = f"{ts:%H:%M}"
        else:
            prefix = f"{ts:%Y-%m-%d %H:%M}"
        lines.append(f"- {prefix} {summary}")
    return "[Recent activity]\n" + "\n".join(lines) + "\n\n"
```

### 2. `src/dollos/inner_voice.py`

In `recall()`, before piping hits to jinja:

```python
import re
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.md$")

def _hit_date(hit: dict) -> str | None:
    src = hit.get("source", "")
    m = DATE_RE.search(src)
    return m.group(1) if m else None

# in recall():
candidates = "\n".join(
    f"{i + 1}. " + (
        f"{_hit_date(h)} {h['content']}" if _hit_date(h) else h["content"]
    )
    for i, h in enumerate(hits)
)
```

### 3. `src/dollos/tools.py`

`Recall` tool extended per Design §C. Add helper functions
`_hit_date_in_range` and `_format_hit` (private to module).

`Report` and other tools unchanged.

### 4. Tests

`tests/test_dispatcher.py`:
- `test_dispatcher_injects_now_block`: assert first user message starts with `[Now]\n` and contains date pattern.
- `test_dispatcher_rolling_entries_have_timestamps`: dispatch 2 events, assert second turn's `[Recent activity]` block has HH:MM prefix.
- `test_dispatcher_recent_activity_uses_full_date_for_old_entries`: monkeypatch a rolling entry from yesterday, assert YYYY-MM-DD prefix.

`tests/test_inner_voice.py`:
- `test_recall_prefixes_dates_to_hits`: fake memsearch returns hits with `source: ".../2026-05-08.md"`, assert candidates string includes "2026-05-08".

`tests/test_tools.py`:
- `test_recall_filters_by_since`: pass since=2026-05-10, hits with mixed dates → only later hits returned.
- `test_recall_filters_by_until`: similar with until.
- `test_recall_filters_by_both`: range filter.
- `test_recall_no_filter_returns_all`: since=None and until=None → no filtering (current behavior).
- `test_recall_skips_hits_without_date_when_filtering`: hit with no date in source → excluded if filter set.
- `test_recall_formats_hits_with_date_prefix`: assert returned string has "- YYYY-MM-DD content".

### 5. Run pytest

`uv run pytest`. All green.

## Risks

- **Time clock determinism in tests**: `datetime.now()` calls need
  injection / freezing for tests. Use `freezegun` if needed, or pass
  a clock callable into dispatcher (preferred — purer).
- **Time format / locale**: hardcoded Chinese day-of-week + period
  descriptors. OK for current single-language target. If multi-language
  later, parameterize via character pack.
- **Rolling buffer growth across days**: full-date prefix kicks in for
  older entries. Buffer is daemon-life unbounded; daily aggregate could
  grow large. Existing risk from step 14.
- **Memsearch hits without source field**: defensive fallback already
  handles missing date.

## Acceptance

- [ ] `uv run pytest` 全綠.
- [ ] Manual smoke: T1 「你好嗎？」at 8:00 vs 22:00, Doll's response
  reflects time-of-day (greets accordingly). T7 「我剛才說了什麼?」
  shows time-tagged recall.
- [ ] Verbose log inspection: `[Now]` block visible in each turn's
  user message.
- [ ] Rolling entries timestamped after multi-turn cascade.
