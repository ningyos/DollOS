# Plan: Cascade decision log + structlog adoption

**Worktree**: `.worktrees/cascade-log/`
**Branch**: `cascade-log`
**Date**: 2026-05-10

## Why

Each cascade iter generates SEEN/INTENT/REVIEW/MOOD/TOOL via grammar +
dispatches a tool with args + gets result. None of this is durably
captured today — once cascade ends, only the user-facing Say is
visible (in transcripts) and a 1-line compact summary (in rolling buffer).

For dev observability + future analysis (replay debug / tool selection
patterns / mood-vs-decision correlation / fine-tune data), need
structured per-iter log.

Adopt `structlog` while at it — DollOS has many structured events
(tool dispatch, mood update, subagent spawn) that benefit from
unified structured logging. `rich` skipped (pure ergonomic, no
function unlocked).

## Out of scope

- Log rotation / retention — daily file separation is enough.
- Replay / debug UI — dev uses `jq` / `grep` on JSONL.
- Streaming / WS dev tap.
- Config flag to disable cascade log.
- Surfacing cascade log to Doll via memsearch (this is dev-only,
  not Self).
- Migrating ALL existing `logger.warning/exception` to structlog —
  stdlib logging coexists; only new cascade log path uses structlog
  primarily.
- `rich` integration.

## Architecture

```
dispatcher._respond
  ├─ _cascade_logger.start_turn() → turn_id (uuid)
  ├─ for iter in cascade:
  │    ├─ stream big-model
  │    ├─ parse + dispatch tools
  │    └─ _cascade_logger.log_iter(turn_id, iter, assistant_text, results, duration_ms)
  │           ├─ parse think fields (SEEN/INTENT/REVIEW/MOOD/TOOL)
  │           └─ structlog.info("cascade_iter", **fields) → JSONL file
  └─ ...
```

Output: `data/cascade_log/{YYYY-MM-DD}.jsonl`, append-only, one JSON
object per line.

structlog config:
- One logger named `"cascade"` configured at boot
- File handler → `data/cascade_log/{date}.jsonl`
- JSONRenderer
- Auto timestamp processor

Existing stdlib `logger.warning/exception` calls keep working. Module-
level `logger = logging.getLogger(...)` unchanged.

## Changes

### 1. Add `structlog` dependency

`pyproject.toml`:
```toml
dependencies = [
    ...,
    "structlog>=24.0",
]
```

`uv lock` to update lockfile.

### 2. New module: `src/dollos/cascade_log.py`

```python
"""Cascade decision log — structured per-iter event capture for
dev observability.

Logs to data/cascade_log/{date}.jsonl via structlog. One line per
cascade iter, captures think fields + tool call + result.
"""

import logging
import re
import uuid
from datetime import date
from pathlib import Path

import structlog

logger = logging.getLogger(__name__)


_THINK_FIELD_RES = {
    "seen": re.compile(r"^SEEN:\s*(.+)$", re.MULTILINE),
    "intent": re.compile(r"^INTENT:\s*(.+)$", re.MULTILINE),
    "review": re.compile(r"^REVIEW:\s*(.+)$", re.MULTILINE),
    "mood": re.compile(r"^MOOD:\s*(.+)$", re.MULTILINE),
    "tool": re.compile(r"^TOOL:\s*(.+?)$", re.MULTILINE),
}


def _parse_think(assistant_text: str) -> dict[str, str]:
    out = {}
    for field, regex in _THINK_FIELD_RES.items():
        m = regex.search(assistant_text)
        if m:
            out[field] = m.group(1).strip()
    return out


class CascadeLogger:
    """Wraps a structlog logger to write per-iter records to JSONL."""

    def __init__(self, log_root: Path):
        self._log_root = log_root
        self._log = structlog.get_logger("cascade")

    def start_turn(self) -> str:
        return uuid.uuid4().hex[:8]

    def log_iter(
        self,
        *,
        turn_id: str,
        iter: int,
        assistant_text: str,
        tool_calls: list[dict] | None = None,
        results: list | None = None,  # list[ToolResult]
        duration_ms: int | None = None,
    ) -> None:
        try:
            fields = _parse_think(assistant_text)
            self._log.info(
                "cascade_iter",
                turn_id=turn_id,
                iter=iter,
                duration_ms=duration_ms,
                **fields,
                tool_calls=[
                    {"name": tc.get("name"), "args": tc.get("arguments")}
                    for tc in (tool_calls or [])
                ],
                results=[
                    {
                        "tool_name": r.tool_name,
                        "success": r.success,
                        "detail": (r.detail or "")[:500],
                    }
                    for r in (results or [])
                ],
            )
        except Exception:
            logger.exception("cascade log_iter failed; continuing")
```

### 3. New module: `src/dollos/logging_config.py`

Sets up structlog for the daemon. Called from `kernel.py` `__init__`:

```python
"""structlog configuration for DollOS.

Configures cascade-specific structured JSONL output to file.
General daemon logging continues via stdlib `logging`.
"""

import logging
from pathlib import Path
from datetime import date

import structlog


def configure_cascade_logging(log_root: Path) -> None:
    """Configure the 'cascade' structlog logger to emit JSONL to
    {log_root}/{date}.jsonl. Idempotent."""
    log_root.mkdir(parents=True, exist_ok=True)
    log_file = log_root / f"{date.today():%Y-%m-%d}.jsonl"

    # Ensure stdlib root logger has a file handler for cascade only.
    cascade_logger = logging.getLogger("cascade")
    cascade_logger.handlers.clear()
    cascade_logger.propagate = False  # don't bleed into root

    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    cascade_logger.addHandler(handler)
    cascade_logger.setLevel(logging.INFO)

    # structlog processor chain: timestamp + JSON
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
```

### 4. `src/dollos/kernel.py` — wire CascadeLogger

In `__init__` after settings load:
```python
from dollos.cascade_log import CascadeLogger
from dollos.logging_config import configure_cascade_logging

cascade_log_root = settings.data.root / "cascade_log"
configure_cascade_logging(cascade_log_root)
self._cascade_logger = CascadeLogger(cascade_log_root)

self.dispatcher = EventDispatcher(
    ...,
    cascade_logger=self._cascade_logger,
    ...,
)
```

### 5. `src/dollos/dispatcher.py` — wire CascadeLogger to _respond

`__init__` accepts `cascade_logger: CascadeLogger`.

`_respond`:
```python
import time

async def _respond(self, doll_event, sink):
    turn_id = self._cascade_logger.start_turn()
    # ... existing setup ...
    
    iter_num = 0
    while True:
        iter_num += 1
        iter_start = time.monotonic()
        
        # ... stream + parse + dispatch ...
        
        duration_ms = int((time.monotonic() - iter_start) * 1000)
        self._cascade_logger.log_iter(
            turn_id=turn_id,
            iter=iter_num,
            assistant_text="".join(assistant_buf),
            tool_calls=parsed_tool_calls,  # accumulated from parser
            results=results,
            duration_ms=duration_ms,
        )
        
        if not results:
            break
        # ... rest of loop ...
```

Need to accumulate parsed tool_calls in a list during the iter
(currently they're dispatched immediately and the dict is discarded).
Capture into `parsed_tool_calls: list[dict]` for log_iter.

### 6. Tests

`tests/test_cascade_log.py` (new):
- `test_parse_think_extracts_all_5_fields`: feed assistant text with
  all 5 fields, assert dict contains all.
- `test_parse_think_handles_missing_fields`: subset of fields → only
  present ones in dict.
- `test_log_iter_writes_jsonl_line`: build CascadeLogger pointing at
  tmp dir, call log_iter, read file, assert one JSON line with
  expected keys.
- `test_log_iter_truncates_long_tool_detail`: detail with 5000 chars →
  truncated to 500 in log.
- `test_log_iter_swallows_exceptions`: pass malformed inputs → no
  exception raised.
- `test_start_turn_returns_unique_id`: 2 calls → different ids.

`tests/test_dispatcher.py`:
- New `_FakeCascadeLogger` with `start_turn` + `log_iter` recording.
- `test_dispatcher_logs_cascade_iter`: dispatch event, assert logger
  called once per iter with turn_id + iter + parsed fields.
- `test_dispatcher_log_iter_includes_tool_calls_and_results`: cascade
  with one tool call, assert log includes parsed tool_calls and
  results.
- Update `_make_dispatcher` helper to pass `cascade_logger=` (default
  no-op fake).

`tests/test_kernel.py`:
- Update fixture to construct CascadeLogger via configure helper.

### 7. Run pytest

`uv run pytest`. All green.

## Risks

- **structlog config bleeding to other tests**: pytest tests share
  process; if test_kernel calls `configure_cascade_logging`, all
  subsequent tests see configured structlog. Mitigation: pass log
  paths explicitly into CascadeLogger; configure call is idempotent.
- **File handle leak**: stdlib FileHandler holds the file open. On
  daemon shutdown / log rotation across midnight, may need to refresh.
  Acceptable for MVP.
- **Performance**: per-iter JSON serialize + file write adds
  ~1-3ms latency. Negligible vs LLM call (~3-5s).
- **Timestamp file name vs midnight rollover**: file name picked at
  boot (`{date.today()}`); long-running daemon writes to yesterday's
  file after midnight. Acceptable; consolidation script can split
  later.

## Acceptance

- [ ] `uv run pytest` 全綠.
- [ ] After T1-T8 smoke run, `data/cascade_log/{date}.jsonl` exists
      with one line per cascade iter, each containing turn_id / iter /
      seen / intent / review / mood / tool / tool_calls / results /
      duration_ms.
- [ ] Lines are valid JSON (parseable with `jq`).
- [ ] No regression: T1-T8 still 7-8/8.
