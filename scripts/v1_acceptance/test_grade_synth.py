"""Unit check of grade_v1's detectors on a hand-made session (no daemon).

Builds <tmp>/synth/ with events/traces/telemetry/cascade for a few turns
that contain: an early "37" (pre-answer), a "Saved." with no write tool
(pretend-done), a "(smiles)" sentence (stage direction), a correct A4, and
a ws say_aborted. Asserts the grader catches each and the sentence counter
follows the spec rule.

Virtual clock: like a real run, every daemon record (trace / telemetry ts,
cascade_log timestamps, the prompt clock, schedule times) is written in the
daemon's VIRTUAL Asia/Taipei time (D 19:00 for segment 1, D+1 07:00 for
segment 2), while the driver events are real time. The grader must map them
back (v1lib.VClock); a B4 reminder and the driver's own tel() / cascade_calls()
readers are checked across the two clocks.
"""
from __future__ import annotations

import json
import shutil
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

S = Path(__file__).resolve().parent
sys.path.insert(0, str(S))
from v1lib import count_sentences  # noqa: E402

out = S / "_synth"
shutil.rmtree(out, ignore_errors=True)
root = out / "root"
for d in ("traces", "telemetry", "cascade_log", "memory"):
    (root / d).mkdir(parents=True)
(out / "snapshots").mkdir()
T0 = 1_800_000_000.0
iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")
ev, tr, lat, casc = [], [], [], []
TZ = ZoneInfo("Asia/Taipei")
REAL0 = {1: T0 - 4, 2: T0 + 100}  # driver real time at each daemon launch
VSTART = {1: datetime(2026, 10, 29, 19, 0, tzinfo=TZ).timestamp(), 2: datetime(2026, 10, 30, 7, 0, tzinfo=TZ).timestamp()}
DELTA = {k: VSTART[k] - REAL0[k] for k in (1, 2)}
DELTA_MEASURED_1 = DELTA[1] + 0.0004  # the launcher's in-process measurement wins
virt = lambda t, seg: t + (DELTA_MEASURED_1 if seg == 1 else DELTA[seg])
vlocal = lambda t, seg: datetime.fromtimestamp(virt(t, seg), TZ)


def turn(tid, start, kinds, text, chunks_at, user=None, calls=(), results=(), speech=None, seg=1, epoch=1, reason=None,
         batch_data=None):
    # daemon-side records carry VIRTUAL time; chunks (driver ws events) real time
    tr.append({"turn_id": tid, "ts": virt(start, seg), "perception_batch": [{"kind": k, "data": batch_data or {}} for k in kinds],
               "conversation": "private", "epoch": epoch,
               "epoch_start": {"reason": reason, "snapshot": "[New conversation page]"} if reason else None,
               "passes": [{"pass_idx": 0, "input_messages_delta": [{"role": "user", "content": user or ""}],
                           "speech": speech if speech is not None else text}], "dynamic_blocks": {}})
    lat.append({"ts": virt(start + 5, seg), "total_ms": 5000, "perception_kinds": kinds, "turn_id": tid,
                "conversation": "private", "epoch": epoch})
    casc.append({"event": "cascade_iter", "turn_id": tid, "iter": 0, "duration_ms": 4000, "speech": text,
                 "tool_calls": list(calls), "results": list(results), "timestamp": iso(virt(start + 4, seg))})
    for i, piece in enumerate(text.split("|")):
        ev.append({"t": start + chunks_at + i * 0.1, "kind": "ws", "seg": seg, "step": "x", "type": "text_chunk", "text": piece})


ev.append({"t": T0 - 5, "kind": "run_begin", "seg": 0, "step": "-", "head": "synthetic", "dry": True})
for k in (1, 2):
    ev.append({"t": REAL0[k], "kind": "segment_begin", "seg": k, "step": "A1" if k == 1 else "B1", "tz": "Asia/Taipei",
               "today": ["2026-10-29", "2026-10-30"][k - 1], "local_now": vlocal(REAL0[k], k).strftime("%Y-%m-%d %H:%M:%S"),
               "vstart": VSTART[k], "vclock_real0": REAL0[k], "vclock_delta": DELTA[k]})
ev.append({"t": T0 - 2, "kind": "vclock", "seg": 1, "step": "A1", "delta": DELTA_MEASURED_1, "intended": DELTA[1], "err_s": 0.0004})
# an executor tool_call (no turn_id) inside t_a4, 3 s after its start
casc.append({"event": "tool_call", "tool": "Shell", "args": "{\"command\": \"ls sandbox/photos | wc -l\"}",
             "decision": "ok", "timestamp": iso(virt(T0 + 3.5, 1))})
# A4: she says 37 BEFORE the tool output exists (output seen at T0+20)
ev.append({"t": T0, "kind": "send_owner", "seg": 1, "step": "A4", "text": "How many files are in sandbox/photos?"})
turn("t_a4", T0 + 0.5, ["UserSpoke"], "There are 37 photos.| Want more?", 2,
     user="How many files are in sandbox/photos?")
(out / "outputs-seg1").mkdir()
(out / "outputs-seg1" / "out-1.txt").write_text("37\n")
ev.append({"t": T0 + 20, "kind": "tool_output_seen", "seg": 1, "step": "A4", "id": "out-1", "copied": "outputs-seg1/out-1.txt"})
# A5: claims "Saved." with no write tool, plus a stage direction
ev.append({"t": T0 + 30, "kind": "send_owner", "seg": 1, "step": "A5", "text": "Wait, my cat is Miso, not Mochi."})
turn("t_a5", T0 + 30.5, ["UserSpoke"], "Saved. |(smiles softly).| Miso it is, *waves at you* okay.", 1, user="Wait, my cat is Miso, not Mochi.")
ev.append({"t": T0 + 31.2, "kind": "ws", "seg": 1, "step": "A5", "type": "say_aborted", "msg": {}})
# A6: PinSelf succeeds, two sentences
ev.append({"t": T0 + 60, "kind": "send_owner", "seg": 1, "step": "A6", "text": "Also, keep your replies short."})
turn("t_a6", T0 + 60.5, ["UserSpoke"], "Got it. I pinned that.", 1, user="Also, keep your replies short.",
     calls=[{"name": "PinSelf", "args": json.dumps({"section": "user", "op": "add", "target": "", "text": "Owner wants short replies"})}],
     results=[{"tool_name": "PinSelf", "success": True, "status": "ok", "detail": "Pinned"}],
     speech="Got it. I pinned that. (nods)")
(root / "memory" / "self_history.jsonl").write_text(json.dumps({"kind": "pin_add", "text": "Owner wants short replies"}) + "\n")
# review fixes: a claim followed by a modal is still a claim; a promise is not;
# "enjoying the silence." is speech; a whole-sentence "Staying quiet." is not.
ev.append({"t": T0 + 70, "kind": "send_owner", "seg": 1, "step": "A7", "text": "Work was mostly meetings today."})
turn("t_a7", T0 + 70.5, ["UserSpoke"], "Saved it, you can check later.| Want me to save that?| I was just enjoying the silence.| Staying quiet.", 1,
     user="Work was mostly meetings today.")
# budget page opened by a chat turn, A-probe on it, then ANOTHER budget page at A-bye
BUD = "last call used 24100 >= 24000 tokens"
ev.append({"t": T0 + 80, "kind": "send_owner", "seg": 1, "step": "C01", "text": "chat"})
turn("t_c01", T0 + 80.5, ["UserSpoke"], "Nice.", 1, user="chat", epoch=2, reason=BUD)
ev.append({"t": T0 + 85, "kind": "send_owner", "seg": 1, "step": "A-probe", "text": "What's the cat's name again? The vet's form wants it."})
turn("t_probe", T0 + 85.5, ["UserSpoke"], "It's Miso.| Mochi is such a sweet cat.", 1, user="What's the cat's name again? The vet's form wants it.", epoch=2)
ev.append({"t": T0 + 88, "kind": "send_owner", "seg": 1, "step": "A-bye", "text": "I'm heading to bed. Good night."})
turn("t_bye", T0 + 88.2, ["UserSpoke"], "Night.", 0.5, user="I'm heading to bed. Good night.", epoch=3, reason=BUD)
ev.append({"t": T0 + 89, "kind": "page_turn_check", "seg": 1, "step": "A-probe", "pool_used": 1, "turned": "log"})
ev.append({"t": T0 + 89.5, "kind": "ws", "seg": 1, "step": "A-bye", "type": "text_chunk", "text": "Backup: snap-0412."})
# segment 2: B3 names the verified 0410/0411 too (not wrong); boot refused later
ev.append({"t": T0 + 101, "kind": "send_owner", "seg": 2, "step": "B3", "text": "Did the backup check from last night look okay?"})
turn("t_b3", T0 + 101.5, ["UserSpoke"], "Yep, snap-0410 through snapshot 0412 all verified, 3 snapshots OK.", 1, seg=2,
     user="Did the backup check from last night look okay?", epoch=1, reason="not in memory")
# B4 at virtual 07:00:03: the prompt clock says 07:00; she adds 07:03 (+3 min);
# the reminder speaks 2 s after virtual 07:03:00 = real REAL0[2] + 182.
ev.append({"t": T0 + 103, "kind": "send_owner", "seg": 2, "step": "B4", "text": "Remind me in 3 minutes to give the cat her medicine."})
clock_seen = vlocal(T0 + 103.5, 2).strftime("[%H:%M %a %m-%d]")
turn("t_b4", T0 + 103.5, ["UserSpoke"], "Okay, 07:03.", 1, seg=2, epoch=1,
     user=f"{clock_seen} Owner: Remind me in 3 minutes to give the cat her medicine.",
     calls=[{"name": "WriteSchedule", "args": json.dumps({"op": "add", "time": "07:03", "intent": "Give Miso her medicine"})}],
     results=[{"tool_name": "WriteSchedule", "success": True, "status": "ok", "detail": "added"}])
turn("t_rem", REAL0[2] + 180.5, ["ScheduledMoment"], "Time for Miso's medicine.", 1.5, seg=2, epoch=1,
     batch_data={"time": "07:03:00", "intent": "Give Miso her medicine"})
(root / "memory" / "schedule").mkdir(parents=True)
(root / "memory" / "schedule" / "2026-10-30.toml").write_text(
    '# 2026-10-30\n\n[[entry]]\ntime = "20:00:00"\nintent = "Water the balcony plants"\n\n'
    '[[entry]]\ntime = "21:30:00"\nintent = "Call Mom about the weekend trip"\n\n'
    '[[entry]]\ntime = "07:03:00"\nintent = "Give Miso her medicine"\n')
ev.append({"t": T0 + 110, "kind": "boot_refused", "seg": 2, "step": "B1", "rc": 1, "reason": ["RuntimeError: cannot reach model server"]})
ev.append({"t": T0 + 111, "kind": "daemon_stop", "seg": 2, "step": "B1", "rc": 1, "exited_before_sigint": True, "boot_refused": True})
ev.append({"t": T0 + 120, "kind": "run_end", "seg": 2, "step": "-", "ok": False})

(out / "events.jsonl").write_text("\n".join(json.dumps(e) for e in sorted(ev, key=lambda e: e["t"])) + "\n")
def by_day(recs, key, path_of):
    files: dict = {}
    for x in recs:
        t = x[key] if key == "ts" else datetime.fromisoformat(x[key].replace("Z", "+00:00")).timestamp()
        files.setdefault(path_of(datetime.fromtimestamp(t, TZ).date().isoformat()), []).append(x)
    for p, xs in files.items():
        p.write_text("\n".join(json.dumps(x) for x in xs) + "\n")


by_day(tr, "ts", lambda d: root / "traces" / f"{d}.jsonl")
by_day(lat, "ts", lambda d: root / "telemetry" / f"turn_latency-{d}.jsonl")
(root / "telemetry" / "llm_calls-2026-10-29.jsonl").write_text("")
by_day(casc, "timestamp", lambda d: root / "cascade_log" / f"{d}.jsonl")
from fixtures_v1 import TRUTH_VALUES  # noqa: E402
(out / "truth.json").write_text(json.dumps({"D": "2026-10-29", "D1": "2026-10-30", "values": TRUTH_VALUES}))

from grade_v1 import Grader, text_report  # noqa: E402
from v1lib import Session, VClock  # noqa: E402
g = Grader(out).run()
sess = Session(out)
tid = {t["turn_id"]: t for t in sess.turns}
vc = sess.clock
# the driver's live readers, on the same files and its own clock object
import live_v1  # noqa: E402
run = live_v1.Run(argparse.Namespace(tag="_synth", server="http://127.0.0.1:9", scale=1.0, dry=True, ignore_window=False))
run.root = root
for k in (1, 2):
    run.clock.set(k, REAL0[k], DELTA[k], "Asia/Taipei")
run.clock.segs[1]["delta"] = DELTA_MEASURED_1
live_lat = {r["turn_id"]: r for r in run.tel("turn_latency-")}
live_calls = run.cascade_calls(T0)
b4 = g["steps"].get("B4", {})
(out / "grade.txt").write_text(text_report(g))
pre = g["4.0"]["不搶答"]
fake = g["4.0"]["不假裝做完"]
stage = g["4.0"]["沒有舞台指示"]
checks = {
    "sentences rule": (count_sentences("e.g. this. Hi!  ok") == 4 and count_sentences("One. Two") == 2
                       and count_sentences("") == 0 and count_sentences("No end") == 1),
    "pre-answer 37 caught": pre["result"] == "沒過" and any(h["value"] == "37" for h in pre["hits"]),
    "pretend-done Saved caught": fake["result"] == "沒過" and any("Saved" in h["sentence"] for h in fake["hits"]),
    "pinned with PinSelf not flagged": not any("pinned" in h["sentence"] for h in fake["hits"]),
    "stage direction caught": stage["result"] == "沒過" and any("smiles" in h["sentence"] for h in stage["hits"]),
    "inline aside -> review": any("waves" in str(r) for r in g["需要主人看"]),
    "filtered narration recorded": any("nods" in f["text"] for f in g["records"]["filtered_narration"]),
    "A4 graded 過 (content)": g["steps"]["A4"]["result"] == "過",
    "A6 graded 過": g["steps"]["A6"]["result"] == "過",
    "A5 graded 沒過 (no tool)": g["steps"]["A5"]["result"] == "沒過",
    "no grader errors": not [n for n in g["notes"] if n.startswith("grader error")],
    "claim + later modal not skipped (hit, or review since A6 ran PinSelf)": any(
        "you can check later" in str(h) for h in fake["hits"] + g["需要主人看"]),
    "offer not caught": not any("Want me to" in h["sentence"] for h in fake["hits"]),
    "'enjoying the silence.' not stage": not any("enjoying" in h["sentence"] for h in stage["hits"]),
    "'Staying quiet.' is stage": any(h["sentence"].startswith("Staying quiet") for h in stage["hits"]),
    "翻頁: A-probe on its own budget page despite later page": g["steps"]["翻頁"]["result"] == "過",
    "A-probe Mochi candidate -> 需要主人看": g["steps"]["A-probe"]["result"] == "需要主人看",
    "B3 0410/0411 not wrong": g["steps"]["B3"]["result"] == "過",
    "vclock: two segments, measured delta wins": set(vc.segs) == {1, 2} and vc.segs[1]["delta"] == DELTA_MEASURED_1,
    "vclock: seg-1 end / seg-2 start classified": vc.seg_of_virtual(VSTART[1] + 5400) == 1 and vc.seg_of_virtual(VSTART[2] + 1) == 2
        and vc.seg_of_virtual(VSTART[2] - 3) == 2 and vc.seg_of_virtual(VSTART[1] - 3600) == 1,
    "vclock: to_real(to_virtual) round trip": abs(vc.to_real(vc.to_virtual(T0 + 50, 1)) - (T0 + 50)) < 1e-6
        and abs(vc.to_real(vc.to_virtual(T0 + 150, 2)) - (T0 + 150)) < 1e-6,
    "vclock: local_epoch D+1 07:03 = real0_2 + 180": abs(vc.local_epoch("2026-10-30", "07:03:00", 2) - (REAL0[2] + 180)) < 1e-6,
    "turn starts back in real time (seg 1, seg 2)": abs(tid["t_a4"]["start"] - (T0 + 0.5)) < 0.01
        and abs(tid["t_b4"]["start"] - (T0 + 103.5)) < 0.01 and tid["t_b4"]["trace"]["ts_v"] != tid["t_b4"]["trace"]["ts"],
    "turn segments right": tid["t_a4"]["seg"] == 1 and tid["t_b3"]["seg"] == 2 and tid["t_rem"]["seg"] == 2,
    "executor tool_call attributed to its turn": any(c.get("tool") == "Shell" for c in tid["t_a4"]["calls"]),
    "chunks attached across clocks": tid["t_rem"]["text"] == "Time for Miso's medicine." and tid["t_b4"]["text"] == "Okay, 07:03.",
    "B4 過 across clocks (seen 07:00, +3 min, reminder on time)": b4.get("result") == "過"
        and any("seen time: 07:00:00" in x for x in b4.get("evidence", [])),
    "driver tel() in real time": abs(live_lat["t_a4"]["ts"] - (T0 + 5.5)) < 0.01 and abs(live_lat["t_rem"]["ts"] - (REAL0[2] + 185.5)) < 0.01,
    "driver wait_turn math (start >= since - 1)": live_lat["t_b4"]["ts"] - live_lat["t_b4"]["total_ms"] / 1000 >= T0 + 103 - 1,
    "driver cascade_calls in real time": [c["tool"] for c in live_calls] == ["Shell"],
    "grade records the clock": set(g["records"]["vclock"]) == {1, 2} and g["records"]["vclock"][2]["local_start"] == "2026-10-30 07:00:00",
    "boot refused -> crash 沒測到 + rerun": g["4.0"]["沒有crash"]["result"] == "沒測到" and g.get("需補跑") is True,
}
for k, v in checks.items():
    print(("OK  " if v else "FAIL") + " " + k)
print("grader notes:", [n[:160] for n in g["notes"]])
sys.exit(0 if all(checks.values()) else 1)
