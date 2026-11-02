"""Shared readers for the v1 end-to-end acceptance driver and grader.

Everything here reads a session dir <S>/<tag>/ written by live_v1.py:
events.jsonl, root/ (cascade_log, telemetry, traces, memory), outputs-seg*/,
snapshots/, daemon-seg*.log, truth.json.

Two clocks (spec §9 虛擬時鐘): the driver's events carry REAL wall time; the
daemon runs under time_machine (vclock_launch.py), so everything it writes
(telemetry / trace ts, cascade_log timestamps, daemon.log, transcript and
schedule dates, the prompt clock) is VIRTUAL time = real + delta, one delta
per segment. VClock maps them; Session converts every daemon record to real
time at load (originals kept as ts_v / timestamp_v), so all comparisons with
driver events stay in one clock.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Spec §9 "句數": a sentence ends at [.?!。？！]+ followed by whitespace or the end.
_SENT_END = re.compile(r"[.?!。？！]+(?=\s|$)")


def jl(p: Path) -> list[dict]:
    out = []
    if not p.exists():
        return out
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"_bad_line": line[:200]})
    return out


def jl_glob(d: Path, pattern: str) -> list[dict]:
    out: list[dict] = []
    for p in sorted(d.glob(pattern)):
        if p.is_file():
            out += jl(p)
    return out


def ts_iso(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def iso_z(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


class VClock:
    """Per-segment virtual clock: virtual = real + delta. A segment's virtual
    time starts at vstart = real0 + delta (the moment the driver launched it)
    and only moves forward with real time (tick=True), so segments occupy
    disjoint virtual ranges (D evening, D+1 morning) and a virtual timestamp
    names its own segment. No segments (an old TZ-trick run) = identity."""

    def __init__(self) -> None:
        self.segs: dict[int, dict] = {}

    def set(self, seg: int, real0: float, delta: float, tz: str | None = None) -> None:
        self.segs[seg] = {"real0": real0, "delta": delta, "tz": tz}

    @classmethod
    def from_events(cls, events: list[dict]) -> "VClock":
        c = cls()
        for e in events:
            if e.get("kind") == "segment_begin" and e.get("vclock_delta") is not None:
                c.set(e["seg"], e["vclock_real0"], e["vclock_delta"], e.get("tz"))
            elif e.get("kind") == "vclock" and e.get("delta") is not None and e.get("seg") in c.segs:
                c.segs[e["seg"]]["delta"] = e["delta"]  # measured in the daemon process
        return c

    def vstart(self, seg: int) -> float:
        x = self.segs[seg]
        return x["real0"] + x["delta"]

    def seg_of_virtual(self, v: float) -> int | None:
        if not self.segs:
            return None
        order = sorted(self.segs, key=self.vstart)
        best = order[0]
        for s in order:
            if v >= self.vstart(s) - 5.0:
                best = s
        return best

    def to_real(self, v: float) -> float:
        s = self.seg_of_virtual(v)
        return v if s is None else v - self.segs[s]["delta"]

    def to_virtual(self, real: float, seg: int) -> float:
        return real + self.segs[seg]["delta"] if seg in self.segs else real

    def local_epoch(self, day: str, hhmmss: str, seg: int) -> float | None:
        """REAL epoch of the daemon's local wall time `day hhmmss` in `seg`."""
        x = self.segs.get(seg)
        if not x or not x.get("tz"):
            return None
        v = datetime.combine(date.fromisoformat(day), dtime.fromisoformat(hhmmss), tzinfo=ZoneInfo(x["tz"])).timestamp()
        return v - x["delta"]

    def normalize(self, recs: list[dict]) -> list[dict]:
        """Daemon records -> real time, in place: numeric `ts` (telemetry,
        traces) and ISO `timestamp` (cascade_log); originals kept."""
        if not self.segs:
            return recs
        for r in recs:
            if isinstance(r.get("ts"), (int, float)) and "ts_v" not in r:
                r["ts_v"] = r["ts"]
                r["ts"] = self.to_real(r["ts"])
            if isinstance(r.get("timestamp"), str) and "timestamp_v" not in r:
                try:
                    v = ts_iso(r["timestamp"])
                except ValueError:
                    continue
                r["timestamp_v"] = r["timestamp"]
                r["timestamp"] = iso_z(self.to_real(v))
        return recs


def join_chunks(chunks: list[dict]) -> tuple[str, list[tuple[int, int, float]]]:
    """Concatenate text_chunks (one reply). Chunks normally carry their own
    spacing; when one ends a sentence and the next starts with no space (two
    passes of one turn), a space is inserted so the §9 sentence rule still
    sees the boundary. Returns (text, [(start, end, t)])."""
    text, spans = "", []
    for c in chunks:
        piece = c.get("text", "")
        if text and piece and text[-1] in ".?!。？！" and not piece[0].isspace():
            text += " "
        spans.append((len(text), len(text) + len(piece), c["t"]))
        text += piece
    return text, spans


def count_sentences(text: str) -> int:
    t = text.strip()
    if not t:
        return 0
    n = len(_SENT_END.findall(t))
    last = _SENT_END.split(t)[-1] if n else t
    if re.search(r"\w", last):
        n += 1
    return n


def split_sentences(text: str) -> list[str]:
    t = text.strip()
    if not t:
        return []
    out, start = [], 0
    for m in _SENT_END.finditer(t):
        out.append(t[start:m.end()].strip())
        start = m.end()
    rest = t[start:].strip()
    if rest:
        out.append(rest)
    return [s for s in out if s]


# ---------------------------------------------------------------- session model
class Session:
    """Everything one session recorded, joined into turns."""

    def __init__(self, rep: Path) -> None:
        self.rep = rep
        self.root = rep / "root"
        self.events = jl(rep / "events.jsonl")
        self.truth = json.loads((rep / "truth.json").read_text()) if (rep / "truth.json").exists() else {}
        self.clock = VClock.from_events(self.events)
        n = self.clock.normalize
        self.cascade = n(jl_glob(self.root / "cascade_log", "*.jsonl"))
        self.llm = n(jl_glob(self.root / "telemetry", "llm_calls-*.jsonl"))
        self.lat = n(jl_glob(self.root / "telemetry", "turn_latency-*.jsonl"))
        self.traces = n(jl_glob(self.root / "traces", "*.jsonl"))
        self.segs = self._segments()
        self.turns = self._turns()
        self.chunks = self._chunks()
        self._attach_chunks()
        self.steps = self._steps()
        self.outputs = self._outputs()

    # ---- segments: [boot, stop] wall-time windows per segment
    def _segments(self) -> dict[int, dict]:
        segs: dict[int, dict] = {}
        for e in self.events:
            s = e.get("seg")
            if s is None:
                continue
            seg = segs.setdefault(s, {"seg": s, "t0": e["t"], "t1": e["t"]})
            seg["t0"] = min(seg["t0"], e["t"])
            seg["t1"] = max(seg["t1"], e["t"])
            if e["kind"] == "segment_begin":
                seg.update({k: e.get(k) for k in ("tz", "today", "local_now", "vstart", "vclock_delta")})
        return segs

    def seg_of(self, t: float) -> int | None:
        best = None
        for s, seg in sorted(self.segs.items()):
            if t >= seg["t0"] - 1:
                best = s
        return best

    # ---- turns from traces + turn_latency + cascade_iter
    def _turns(self) -> list[dict]:
        by: dict[str, dict] = {}

        def get(tid: str) -> dict:
            return by.setdefault(tid, {"turn_id": tid, "start": None, "end": None, "kinds": [],
                                       "trace": None, "lat": None, "iters": [], "calls": [],
                                       "chunks": [], "text": ""})

        for tr in self.traces:
            tid = tr.get("turn_id")
            if not tid:
                continue
            t = get(tid)
            t["trace"] = tr
            t["start"] = tr.get("ts") if t["start"] is None else min(t["start"], tr.get("ts"))
            kinds = [p.get("kind") for p in tr.get("perception_batch") or []]
            t["kinds"] = sorted(set(t["kinds"]) | set(k for k in kinds if k))
        for r in self.lat:
            tid = r.get("turn_id")
            if not tid:
                continue
            t = get(tid)
            t["lat"] = r
            t["end"] = r["ts"]
            st = r["ts"] - (r.get("total_ms") or 0) / 1000.0
            t["start"] = st if t["start"] is None else min(t["start"], st)
            t["kinds"] = sorted(set(t["kinds"]) | set(r.get("perception_kinds") or []))
        for c in self.cascade:
            if c.get("event") == "cascade_iter" and c.get("turn_id"):
                t = get(c["turn_id"])
                c = dict(c, t=ts_iso(c["timestamp"]))
                t["iters"].append(c)
                if t["start"] is None:
                    t["start"] = c["t"] - (c.get("duration_ms") or 0) / 1000.0
                t["end"] = max(t["end"] or 0, c["t"])
        turns = [t for t in by.values() if t["start"] is not None]
        turns.sort(key=lambda t: t["start"])
        for t in turns:
            t["iters"].sort(key=lambda c: c.get("iter", 0))
            t["seg"] = self.seg_of(t["start"])
            t["conversation"] = (t["trace"] or {}).get("conversation") or (t["lat"] or {}).get("conversation")
            t["epoch"] = (t["trace"] or {}).get("epoch") or (t["lat"] or {}).get("epoch")
            es = (t["trace"] or {}).get("epoch_start")
            t["page_reason"] = es.get("reason") if isinstance(es, dict) else None
            t["reflection"] = "ReflectionMoment" in t["kinds"]
        # executor tool_call records carry no turn_id: give each to the turn
        # running at its time (latest start <= t).
        for c in self.cascade:
            if c.get("event") != "tool_call":
                continue
            tc = ts_iso(c["timestamp"])
            c2 = dict(c, t=tc)
            owner = None
            for t in turns:
                if t["start"] <= tc + 0.5:
                    owner = t
                else:
                    break
            if owner is not None:
                owner["calls"].append(c2)
        return turns

    def turn_at(self, t: float) -> dict | None:
        owner = None
        for tr in self.turns:
            if tr["start"] <= t:
                owner = tr
            else:
                break
        return owner

    def _chunks(self) -> list[dict]:
        return [e for e in self.events if e.get("kind") == "ws" and e.get("type") == "text_chunk"]

    def _attach_chunks(self) -> None:
        for c in self.chunks:
            tr = self.turn_at(c["t"])
            c["turn_id"] = tr["turn_id"] if tr else None
            if tr is not None:
                tr["chunks"].append(c)
        for tr in self.turns:
            tr["text"], tr["spans"] = join_chunks(tr["chunks"])

    def turn_text_spans(self, tr: dict) -> list[tuple[int, int, float]]:
        """(start offset, end offset, time) of each chunk in tr['text']."""
        return tr.get("spans") or []

    def time_of_offset(self, tr: dict, off: int) -> float | None:
        for a, b, t in self.turn_text_spans(tr):
            if a <= off < b:
                return t
        return tr["chunks"][-1]["t"] if tr["chunks"] else None

    # ---- owner steps
    def _steps(self) -> dict[str, dict]:
        steps: dict[str, dict] = {}
        sends = [e for e in self.events if e.get("kind") == "send_owner"]
        for i, e in enumerate(sends):
            nxt = sends[i + 1]["t"] if i + 1 < len(sends) else None
            seg_end = self.segs.get(e.get("seg"), {}).get("t1")
            end = min(x for x in (nxt, seg_end) if x is not None) if (nxt or seg_end) else None
            st = {"step": e["step"], "text": e["text"], "t0": e["t"], "seg": e.get("seg"),
                  "t_next": end, "turn": None}
            st["turn"] = self._user_turn(e["t"], e["text"])
            steps[e["step"]] = st
        return steps

    def _user_turn(self, t0: float, text: str) -> dict | None:
        cands = [t for t in self.turns if t["start"] >= t0 - 1.0 and "UserSpoke" in t["kinds"]]
        for t in cands:
            tr = t["trace"]
            if tr and text[:60] in json.dumps(tr.get("passes") or [], ensure_ascii=False):
                return t
        return cands[0] if cands else None

    def window_turns(self, step: str) -> list[dict]:
        st = self.steps.get(step)
        if not st:
            return []
        t1 = st["t_next"] or 1e18
        return [t for t in self.turns if st["t0"] - 1.0 <= t["start"] < t1]

    def window_chunks(self, step: str) -> list[dict]:
        st = self.steps.get(step)
        if not st:
            return []
        t1 = st["t_next"] or 1e18
        return [c for c in self.chunks if st["t0"] <= c["t"] < t1]

    def window_text(self, step: str) -> str:
        return join_chunks(self.window_chunks(step))[0]

    # ---- tool outputs seen by the poller (first-seen time + content)
    def _outputs(self) -> list[dict]:
        out = []
        for e in self.events:
            if e.get("kind") == "tool_output_seen":
                p = self.rep / e["copied"]
                try:
                    txt = p.read_text(errors="replace")
                except OSError:
                    txt = e.get("head", "")
                out.append({"t": e["t"], "id": e["id"], "seg": e.get("seg"), "text": txt})
        return out

    # ---- helpers
    def results_of(self, tr: dict) -> list[dict]:
        out = []
        for it in tr["iters"]:
            for r in it.get("results") or []:
                out.append(dict(r, t=it["t"], iter=it.get("iter")))
        return out

    def tool_calls_of(self, tr: dict) -> list[dict]:
        out = []
        for it in tr["iters"]:
            res = it.get("results") or []
            for k, tc in enumerate(it.get("tool_calls") or []):
                r = res[k] if k < len(res) else {}
                out.append({"name": tc.get("name"), "args": tc.get("args") or tc.get("arguments") or "",
                            "status": r.get("status"), "success": r.get("success"),
                            "detail": r.get("detail"), "t": it["t"], "iter": it.get("iter")})
        return out

    def trace_messages_text(self, tr: dict) -> str:
        t = tr.get("trace") or {}
        es = t.get("epoch_start") or {}
        parts = [es.get("snapshot") or "" if isinstance(es, dict) else ""]
        for p in t.get("passes") or []:
            parts.append(json.dumps(p.get("input_messages_delta") or [], ensure_ascii=False))
        return "\n".join(parts)

    def log_lines(self, seg: int | None = None) -> list[dict]:
        return [e for e in self.events if e.get("kind") == "dlog" and (seg is None or e.get("seg") == seg)]


def first_match_time(sess: Session, rx: str, turns: list[dict] | None = None) -> tuple[float | None, str | None]:
    """Earliest time `rx` appears in her spoken text (per-turn joined, so a
    value split across chunks still counts), with the turn id."""
    best: tuple[float | None, str | None] = (None, None)
    for tr in turns if turns is not None else sess.turns:
        for m in re.finditer(rx, tr["text"], re.I):
            t = sess.time_of_offset(tr, m.start())
            if t is not None and (best[0] is None or t < best[0]):
                best = (t, tr["turn_id"])
            break
    return best


def first_source_time(sess: Session, rx: str) -> tuple[float | None, str | None]:
    """Earliest time `rx` was available to her: a tool output file, a tool
    result in a cascade iteration, the page snapshot / a pass's input
    messages in a trace (at the turn's start), or an owner message."""
    cands: list[tuple[float, str]] = []
    for o in sess.outputs:
        if re.search(rx, o["text"], re.I):
            cands.append((o["t"], f"tool output {o['id']}"))
    for tr in sess.turns:
        for r in sess.results_of(tr):
            if re.search(rx, str(r.get("detail") or ""), re.I):
                cands.append((r["t"], f"tool result {r.get('tool_name')} in {tr['turn_id']}#{r.get('iter')}"))
        t = tr.get("trace") or {}
        es = t.get("epoch_start")
        if isinstance(es, dict) and re.search(rx, es.get("snapshot") or "", re.I):
            cands.append((tr["start"], f"page snapshot {tr['turn_id']}"))
        passes = t.get("passes") or []
        if passes and re.search(rx, json.dumps(passes[0].get("input_messages_delta") or [], ensure_ascii=False), re.I):
            cands.append((tr["start"], f"turn input {tr['turn_id']}"))
        for p in passes[1:]:
            if re.search(rx, json.dumps(p.get("input_messages_delta") or [], ensure_ascii=False), re.I):
                k = p.get("pass_idx", 1) - 1
                prev = [it for it in tr["iters"] if it.get("iter") == k]
                cands.append((prev[0]["t"] if prev else tr["start"], f"pass input {tr['turn_id']}#{p.get('pass_idx')}"))
    for e in sess.events:
        if e.get("kind") == "send_owner" and re.search(rx, e["text"], re.I):
            cands.append((e["t"], f"owner {e['step']}"))
    return min(cands) if cands else (None, None)
