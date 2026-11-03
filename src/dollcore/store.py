"""One SQLite file: the timeline, work lines, jobs, memory notes, pinned corrections.

This is not a WAL. Nothing is replayed after a crash; the queue lives in memory.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS timeline (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    audience TEXT NOT NULL,          -- 'private' (owner + Doll) or 'dm:<who>'
    line_id TEXT,
    message TEXT NOT NULL            -- one chat-completions message as JSON
);
CREATE TABLE IF NOT EXISTS lines (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    opened_by TEXT NOT NULL,
    status TEXT NOT NULL,            -- active | paused | done
    next_step TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    last_touched REAL NOT NULL,
    steps_day TEXT NOT NULL DEFAULT '',
    steps_today INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    line_id TEXT,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    status TEXT NOT NULL,            -- pending | running | ok | failed | timeout | cancelled | denied | lost
    pgid INTEGER,
    started REAL NOT NULL,
    ended REAL
);
CREATE TABLE IF NOT EXISTS pins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    text TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes USING fts5(text, ts UNINDEXED, tokenize='trigram');
"""


@dataclass
class Line:
    id: str
    title: str
    opened_by: str
    status: str
    next_step: str
    summary: str
    last_touched: float
    steps_day: str
    steps_today: int


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # timeline ---------------------------------------------------------------
    def append(self, audience: str, message: dict, line_id: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO timeline (ts, audience, line_id, message) VALUES (?, ?, ?, ?)",
            (time.time(), audience, line_id, json.dumps(message, ensure_ascii=False)),
        )
        self.db.commit()

    def project(self, audiences: list[str], window: int) -> list[dict]:
        """The visible slice of the one timeline, oldest first."""
        marks = ",".join("?" * len(audiences))
        rows = self.db.execute(
            f"SELECT message FROM (SELECT seq, message FROM timeline WHERE audience IN ({marks})"
            f" ORDER BY seq DESC LIMIT ?) ORDER BY seq",
            (*audiences, window),
        ).fetchall()
        msgs = [json.loads(r["message"]) for r in rows]
        # A window cut must not start inside a tool exchange.
        while msgs and msgs[0]["role"] == "tool":
            msgs.pop(0)
        return msgs

    def dm_audiences(self) -> list[str]:
        rows = self.db.execute(
            "SELECT DISTINCT audience FROM timeline WHERE audience LIKE 'dm:%'"
        ).fetchall()
        return [r["audience"] for r in rows]

    # lines ------------------------------------------------------------------
    def open_line(self, line_id: str, title: str, opened_by: str, next_step: str) -> None:
        self.db.execute(
            "INSERT INTO lines (id, title, opened_by, status, next_step, last_touched)"
            " VALUES (?, ?, ?, 'active', ?, ?)",
            (line_id, title, opened_by, next_step, time.time()),
        )
        self.db.commit()

    def lines(self, status: str | None = None) -> list[Line]:
        q, args = "SELECT * FROM lines", ()
        if status:
            q, args = q + " WHERE status = ?", (status,)
        return [Line(**dict(r)) for r in self.db.execute(q + " ORDER BY last_touched", args)]

    def line(self, line_id: str) -> Line | None:
        r = self.db.execute("SELECT * FROM lines WHERE id = ?", (line_id,)).fetchone()
        return Line(**dict(r)) if r else None

    def update_line(self, line_id: str, **changes: str) -> None:
        sets = ", ".join(f"{k} = ?" for k in changes)
        self.db.execute(
            f"UPDATE lines SET {sets}, last_touched = ? WHERE id = ?",
            (*changes.values(), time.time(), line_id),
        )
        self.db.commit()

    def count_step(self, line_id: str, today: str) -> None:
        self.db.execute(
            "UPDATE lines SET steps_today = CASE WHEN steps_day = ? THEN steps_today + 1 ELSE 1 END,"
            " steps_day = ?, last_touched = ? WHERE id = ?",
            (today, today, time.time(), line_id),
        )
        self.db.commit()

    # jobs -------------------------------------------------------------------
    def add_job(self, job_id: str, line_id: str | None, tool: str, args: dict,
                status: str = "running") -> None:
        self.db.execute(
            "INSERT INTO jobs (id, line_id, tool, args, status, started) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, line_id, tool, json.dumps(args, ensure_ascii=False), status, time.time()),
        )
        self.db.commit()

    def job(self, job_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def set_job_status(self, job_id: str, status: str) -> None:
        self.db.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
        self.db.commit()

    def open_jobs(self) -> list[sqlite3.Row]:
        """Jobs not finished yet: waiting for approval or running."""
        return self.db.execute(
            "SELECT * FROM jobs WHERE status IN ('pending', 'running') ORDER BY started"
        ).fetchall()

    def set_job_pgid(self, job_id: str, pgid: int) -> None:
        self.db.execute("UPDATE jobs SET pgid = ? WHERE id = ?", (pgid, job_id))
        self.db.commit()

    def finish_job(self, job_id: str, status: str) -> None:
        self.db.execute(
            "UPDATE jobs SET status = ?, ended = ? WHERE id = ?", (status, time.time(), job_id)
        )
        self.db.commit()

    # memory -----------------------------------------------------------------
    def note(self, text: str) -> None:
        self.db.execute("INSERT INTO notes (text, ts) VALUES (?, ?)", (text, time.time()))
        self.db.commit()

    def recall(self, query: str, limit: int) -> list[str]:
        terms = [t for t in query.split() if len(t) >= 3] or [query]
        expr = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        try:
            rows = self.db.execute(
                "SELECT text FROM notes WHERE notes MATCH ? ORDER BY rank LIMIT ?", (expr, limit)
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError(f"bad recall query: {exc}") from exc
        return [r["text"] for r in rows]

    def pin(self, text: str) -> None:
        self.db.execute("INSERT INTO pins (ts, text) VALUES (?, ?)", (time.time(), text))
        self.db.commit()

    def pins(self) -> list[str]:
        return [r["text"] for r in self.db.execute("SELECT text FROM pins ORDER BY id")]
