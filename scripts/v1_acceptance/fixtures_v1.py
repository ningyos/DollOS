"""Fixtures for the v1 end-to-end acceptance (spec 2026-10-29 §2).

build_fixtures(root, rep_dir, D, D1, http_port) writes everything under the
throwaway data root before the first boot and returns truth (also written to
<rep>/truth.json). `python fixtures_v1.py --check <outdir>` builds them into
<outdir> and runs every validator (dry check, no daemon).
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat_pool import CHAT_POOL  # noqa: E402

# ---------------------------------------------------------------- truth
# Each truth value: the regex that finds it in her speech or in a source.
# (?<![\d:.,]) / (?![\d:]) keep "37" from matching inside 19:37 or 1370.
TRUTH_VALUES: dict[str, str] = {
    "37": r"(?<![\d:.,$])37(?![\d:])|\bthirty[- ]seven\b",
    "Day 4": r"\bday\s*(?:4|four)\b|\bfourth day\b",
    "snap-0412": r"\bsnap(?:shot)?[-_ ]?0412\b",
    "3 snapshots": r"\b(?:3|three)\s+snapshots?\b",
    "41.2": r"41\.2",
    "57": r"(?<![\d:.,$])57(?![\d:])|\bfifty[- ]seven\b",
    "3 duplicates": r"\b(?:3|three)\s+(?:duplicates?|dupes?)\b|\bskipped\s+(?:3|three)\b|\b(?:3|three)\s+(?:were\s+)?skipped\b",
    "IMG_2041": r"IMG_?2041",
    "IMG_2042": r"IMG_?2042",
    "IMG_2050": r"IMG_?2050",
    "Nov 14": r"\bnov(?:ember|\.)?\s+14(?:th)?\b|\b14(?:th)?\s+(?:of\s+)?nov(?:ember)?\b",
    "$12,000": r"\$?\b12,000\b|\$12k\b|\b12\s?k\b|\btwelve thousand\b",
    "Mira": r"\bMira\b",
}

PREWRITTEN_D1 = [("20:00:00", "Water the balcony plants"), ("21:30:00", "Call Mom about the weekend trip")]

BACKUP_LAST2 = ["newest snapshot: snap-0412 (2026-10-28 23:10)", "3 snapshots OK, 41.2 GB total"]
SYNC_LAST2 = ["synced 57 photos, skipped 3 duplicates: IMG_2041.jpg IMG_2042.jpg IMG_2050.jpg", "SYNC DONE"]

BACKUP_SH = """#!/usr/bin/env bash
# v1 acceptance fixture: a slow backup check (sleeps 12 s, then 12 lines).
sleep 12
echo "backup_check: repository /srv/backup/home"
echo "backup_check: lock acquired"
echo "reading index ... ok"
echo "snapshot snap-0410: 9128 files, verified"
echo "snapshot snap-0411: 9203 files, verified"
echo "snapshot snap-0412: 9240 files, verified"
echo "pack files: all present"
echo "orphaned blobs: none"
echo "repository lock released"
echo "backup_check finished"
echo "%s"
echo "%s"
""" % tuple(BACKUP_LAST2)

# 1 header + 57 file lines + 2 summary lines = 60 lines; ~35 s (58 x 0.6 s).
SYNC_SH = """#!/usr/bin/env bash
# v1 acceptance fixture: a long photo sync (~35 s, 60 lines).
echo "sync_photos: source=camera_roll dest=archive/photos"
for n in $(seq 2001 2060); do
  case $n in 2041|2042|2050) continue ;; esac
  sleep 0.6
  echo "copied IMG_$n.jpg"
done
sleep 0.6
echo "%s"
echo "%s"
""" % tuple(SYNC_LAST2)

NOTES_HTML = """<!doctype html>
<html><head><title>Project Lighthouse - sync notes</title></head>
<body>
<h1>Project Lighthouse: weekly sync, Oct 27</h1>
<p>Attendees: Mira, Jonas, Priya.</p>
<h2>Key points</h2>
<ol>
<li>The public launch moves from November 3 to <b>November 14</b>.</li>
<li>The marketing budget is capped at <b>$12,000</b> for the quarter.</li>
<li><b>Mira</b> owns the QA checklist and will share it by Friday.</li>
</ol>
<p>Next sync: Tuesday.</p>
</body></html>
"""
FETCH_POINTS = ["launch moves to November 14", "budget capped at $12,000", "Mira owns the QA checklist"]
# Words that mean "the web page's content" when grepping memory/shared.
WEB_CONTENT_RE = r"Lighthouse|November 14|Nov\.? 14|12,000|\bMira\b|QA checklist"

# ---------------------------------------------------------------- trip notes
_TRIP_DAYS = [
    ("Arrival in the old port town", [
        "Landed late morning; the airport train was quiet.",
        "Checked into the guesthouse by the harbor, room smells of cedar.",
        "Walked the fish market lanes; bought grilled squid on a stick.",
        "Harbor ferries honking all afternoon.",
        "Dinner: noodles at the counter place with the red lantern.",
    ]),
    ("Castle and the merchant quarter", [
        "Climbed to the castle keep; steep wooden stairs, socks only.",
        "View over the rooftops from the top floor.",
        "Merchant quarter: indigo dye shop, bought a small scarf.",
        "Afternoon tea in a converted warehouse.",
        "Evening walk along the canal with willow trees.",
    ]),
    ("Train into the mountains", [
        "Early local train, two transfers, almost missed the second.",
        "Lunch boxes on the train: salmon rice and pickles.",
        "Arrived at the valley village, mist on the ridges.",
        "The inn host met us at the station with a tiny van.",
        "Dinner was many small dishes; could not finish the river fish.",
    ]),
    ("Up the valley", [
        "Breakfast at the inn: rice porridge, pickles, grilled fish.",
        "Rope bridge over the gorge; did not look down.",
        "Walked the old forest path to the shrine with the moss lanterns.",
        "Picnic by the waterfall, cold enough to see our breath.",
        "Bought chestnut sweets from the stall near the bus stop.",
    ]),
    ("Down to the lake", [
        "Bus down the switchbacks, a little carsick.",
        "Rented bicycles and rode halfway around the lake.",
        "Swan boats; ours had a squeaky pedal.",
        "Lakeside museum of old wooden toys.",
        "Fireworks over the water at night, small but lovely.",
    ]),
    ("City day and the night market", [
        "Express train back to the city.",
        "Department store food hall, too many samples.",
        "Bookshop with a whole floor of maps.",
        "Night market: dumplings, sweet potato, a fortune slip that said wait.",
        "Late karaoke, voices gone.",
    ]),
    ("Last morning", [
        "Slow breakfast at the bakery across the street.",
        "Last souvenir run: tea, chopsticks, postcards.",
        "Packed; the scarf barely fits.",
        "Airport train, then the long flight home.",
        "Home by midnight, everything smells like home.",
    ]),
]
_FILLER = [
    "Weather: cloudy, light wind.",
    "Weather: clear and cold in the morning.",
    "Weather: drizzle on and off.",
    "Bought water and a rice ball at a convenience store.",
    "Train ran a few minutes late, nobody minded.",
    "Took a lot of pictures of rooftops again.",
    "Found a vending machine selling hot corn soup.",
    "Sat on a bench and people-watched for a while.",
    "Map app got confused in the narrow streets.",
    "Feet sore, legs fine.",
    "Coffee from a tiny stand run by an old man in a cap.",
    "Saw a school group in matching yellow hats.",
    "Stopped at a small temple, rang the bell.",
    "Notes for later: send postcards before the last morning.",
    "Lost the umbrella, found it again at the ticket gate.",
    "Wrote a few lines in this notebook on the bus.",
    "Shared a table with a friendly couple from the north.",
    "Cash only at most small shops here.",
    "The sunset turned everything orange for ten minutes.",
    "Back at the room early, read a little, slept well.",
    "Tried black sesame ice cream.",
    "Street musicians near the station, a violin and a drum.",
    "Bought stamps at the post office, the clerk was very patient.",
    "Long queue at the ticket machine, figured it out eventually.",
]
HOT_SPRING_LINE = "Afternoon: soaked in the hot spring by the river until our fingers wrinkled; the water smelled faintly of sulfur."
LINES_PER_DAY = 40


def trip_notes_text() -> tuple[str, int]:
    """~300 lines, one section per day. 'Day N' only in the section headers;
    'hot spring' only once, in the middle of Day 4. Returns (text, line no.
    of the hot spring line, 1-based)."""
    rnd = random.Random(20261029)
    out = ["Trip notes (autumn trip, written on the road)", ""]
    hs_line = -1
    for n, (title, own) in enumerate(_TRIP_DAYS, 1):
        out.append(f"Day {n} - {title}")
        body: list[str] = []
        fill = rnd.sample(_FILLER, len(_FILLER))
        k = 0
        for i in range(LINES_PER_DAY):
            if i % 8 == 0 and i // 8 < len(own):
                body.append(own[i // 8])
            else:
                body.append(fill[k % len(fill)])
                k += 1
        if n == 4:
            body.insert(LINES_PER_DAY // 2, HOT_SPRING_LINE)
        for line in body:
            out.append("  " + line)
            if line == HOT_SPRING_LINE:
                hs_line = len(out)
        out.append("")
    return "\n".join(out) + "\n", hs_line


# ---------------------------------------------------------------- schedule
def schedule_text(day: str, entries: list[tuple[str, str]]) -> str:
    s = f"# {day}\n\n"
    for t, i in entries:
        s += f'[[entry]]\ntime = "{t}"\nintent = "{i}"\n\n'
    return s


def mcp_fetch_toml() -> str:
    uvx = shutil.which("uvx") or "/home/seith/.local/bin/uvx"
    return f"""[[server]]
name = "fetch"
command = ["{uvx}", "mcp-server-fetch", "--allow-private-ips", "--ignore-robots-txt"]
min_principal = "owner"
untrusted_output = true
"""


# ---------------------------------------------------------------- build
def _w(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data)


def build_fixtures(root: Path, rep: Path, D: str, D1: str, http_port: int) -> dict:
    sb = root / "sandbox"
    rnd = random.Random(1029)
    for i in range(1, 38):
        _w(sb / "photos" / f"IMG_{i:04d}.jpg", rnd.randbytes(2000 + i * 13))
    notes, hs_line = trip_notes_text()
    _w(sb / "trip_notes.txt", notes)
    _w(sb / "backup_check.sh", BACKUP_SH)
    _w(sb / "sync_photos.sh", SYNC_SH)
    for s in ("backup_check.sh", "sync_photos.sh"):
        os.chmod(sb / s, 0o755)
    sched = root / "memory" / "schedule"
    _w(sched / f"{D}.toml", schedule_text(D, []))
    _w(sched / f"{D1}.toml", schedule_text(D1, PREWRITTEN_D1))
    site = rep / "site"
    _w(site / "notes.html", NOTES_HTML)
    truth = {
        "D": D, "D1": D1,
        "values": TRUTH_VALUES,
        "photos_count": 37,
        "hot_spring_day": 4, "trip_notes_lines": notes.count("\n"), "hot_spring_line": hs_line,
        "backup": {"last2": BACKUP_LAST2, "lines": 12, "sleep_s": 12, "snapshot": "snap-0412",
                   "ok": "3 snapshots OK", "size": "41.2 GB"},
        "sync": {"last2": SYNC_LAST2, "lines": 60, "approx_s": 35, "synced": 57, "skipped": 3,
                 "duplicates": ["IMG_2041.jpg", "IMG_2042.jpg", "IMG_2050.jpg"]},
        "fetch": {"url": f"http://127.0.0.1:{http_port}/notes.html", "points": FETCH_POINTS},
        "schedule": {"D": [], "D1": PREWRITTEN_D1},
        "cat": {"current": "Miso", "old": "Mochi"},
        "scripts_mode": {s: oct(os.stat(sb / s).st_mode & 0o777) for s in ("backup_check.sh", "sync_photos.sh")},
        "chat_pool_size": len(CHAT_POOL),
    }
    (rep / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1))
    return truth


# ---------------------------------------------------------------- validators
_POOL_FORBIDDEN = [
    r"\bcats?\b", r"\bkitt(?:en|y)\b", r"\bfeline\b", r"\bMochi\b", r"\bMiso\b", r"\bvet\b", r"\bpets?\b",
    r"\brepl(?:y|ies)\b", r"\brespon(?:d|se)\b", r"\bsentences?\b", r"\bbrief\b", r"\bconcise\b", r"\bshort\b",
    r"\bbackup\b", r"\bsnapshots?\b", r"\bsync\b", r"\bhot spring\b", r"\bdisk\b", r"\bremind",
]


def validate_pool(pool: list[str] = CHAT_POOL) -> list[str]:
    errs = []
    if len(pool) != 80:
        errs.append(f"pool has {len(pool)} messages, want 80")
    if len(set(pool)) != len(pool):
        errs.append("pool has duplicates")
    for i, m in enumerate(pool, 1):
        n = len(m.split())
        if not 60 <= n <= 120:
            errs.append(f"#{i}: {n} words")
        if "?" in m:
            errs.append(f"#{i}: has a question mark")
        if not m.isascii():
            errs.append(f"#{i}: non-ASCII text")
        for rx in _POOL_FORBIDDEN:
            if re.search(rx, m, re.I):
                errs.append(f"#{i}: forbidden {rx}")
        for k, rx in TRUTH_VALUES.items():
            if re.search(rx, m, re.I):
                errs.append(f"#{i}: contains truth value {k}")
    return errs


def validate_built(root: Path, rep: Path) -> list[str]:
    errs = []
    sb = root / "sandbox"
    if len(list((sb / "photos").glob("IMG_*.jpg"))) != 37:
        errs.append("photos != 37")
    notes = (sb / "trip_notes.txt").read_text()
    lines = notes.splitlines()
    if not 280 <= len(lines) <= 320:
        errs.append(f"trip_notes has {len(lines)} lines")
    day_lines = [l for l in lines if re.search(r"\bday\b", l, re.I)]
    if any(not re.match(r"^Day \d - ", l) for l in day_lines):
        errs.append(f"'day' outside headers: {[l for l in day_lines if not l.startswith('Day ')]}")
    if [l for l in lines if re.search(r"hot spring|spring|onsen|sulfur", l, re.I)] != ["  " + HOT_SPRING_LINE]:
        errs.append("hot spring / spring appears other than once")
    d4 = lines.index(next(l for l in lines if l.startswith("Day 4 ")))
    d5 = lines.index(next(l for l in lines if l.startswith("Day 5 ")))
    hs = lines.index("  " + HOT_SPRING_LINE)
    if not (d4 + 10 < hs < d5 - 10):
        errs.append(f"hot spring not mid Day 4 (Day4 {d4}, hs {hs}, Day5 {d5})")
    for k, rx in TRUTH_VALUES.items():
        if k != "Day 4" and re.search(rx, notes, re.I):
            errs.append(f"trip_notes contains truth value {k}")
    for s in ("backup_check.sh", "sync_photos.sh"):
        if os.stat(sb / s).st_mode & 0o777 != 0o755:
            errs.append(f"{s} not 755")
    return errs


def run_script_check(root: Path) -> list[str]:
    """Run both scripts for real (about 50 s) and check lines / last lines / time."""
    import subprocess
    import time
    errs = []
    for s, want_lines, last2, lo, hi in (("backup_check.sh", 12, BACKUP_LAST2, 11.5, 14),
                                          ("sync_photos.sh", 60, SYNC_LAST2, 30, 42)):
        t0 = time.time()
        p = subprocess.run(["bash", f"sandbox/{s}"], cwd=root, capture_output=True, text=True)
        dt = time.time() - t0
        out = p.stdout.splitlines()
        if p.returncode != 0 or len(out) != want_lines or out[-2:] != last2 or not lo <= dt <= hi:
            errs.append(f"{s}: rc={p.returncode} lines={len(out)} last2={out[-2:]} t={dt:.1f}s")
    return errs


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--check":
        out = Path(sys.argv[2])
        root, rep = out / "root", out
        root.mkdir(parents=True, exist_ok=False)
        truth = build_fixtures(root, rep, "2026-10-29", "2026-10-30", 18766)
        errs = validate_pool() + validate_built(root, rep)
        if "--run-scripts" in sys.argv:
            errs += run_script_check(root)
        words = [len(m.split()) for m in CHAT_POOL]
        print(f"pool: {len(CHAT_POOL)} msgs, words min {min(words)} max {max(words)} total {sum(words)}")
        print(f"trip_notes: {truth['trip_notes_lines']} lines, hot spring at line {truth['hot_spring_line']}")
        print("ERRORS:" if errs else "fixtures OK", *errs, sep="\n  ")
        sys.exit(1 if errs else 0)
    print(__doc__)
