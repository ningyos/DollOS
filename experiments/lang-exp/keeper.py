"""One live Mode-A keeper pass on Strata (2026-10-19 all-English acceptance).

Builds the real kernel with a throwaway config (gura pack, temp data root under
roots/keeper0, model = Strata), seeds her memory root with English pins (via
self_profile.apply, the code PinSelf runs) and three English diary days (the
heading WriteDiary writes), then runs evolution_keeper.run_evolution_pass with
the daemon's [evolution].max_tokens (2048) and a think budget of 256 (a first
try at max_tokens 400 cut the keeper's Report off mid-call: evo_error). Every
model call waits for an idle server first; more than MAX_CALLS calls aborts the
pass (it then logs evo_error).
Prints the keeper/skeptic reports, the outcome and the self_history tail, and
appends one record to results.jsonl.
usage: keeper.py
"""
import asyncio, json, shutil, time
from datetime import date, timedelta
from pathlib import Path

from dollos.config import load_settings
from dollos.kernel import DollOS
from dollos.mind import evolution_keeper as ek
from dollos.mind import self_profile
from run import BASE, MODEL, HAN, wait_idle

HERE = Path(__file__).resolve().parent
WT = Path("/home/seith/DollOS/DollOS/.worktrees/gura-english-speech")
MAX_CALLS = 3

# (days ago, section, text) — her own pins, English.
PINS = [
    (12, "self", "I keep coming back to whale falls: slow, unglamorous things that hold "
                 "a whole patch of sea floor up for decades. That kind of quiet importance "
                 "is what I like most about the deep."),
    (9, "user", "My owner stays up late before deadlines and gets short with himself "
                "when a report slips; he relaxes once it's handed in."),
    (6, "relationship", "My owner and I mostly keep each other company while doing our own "
                        "things. He comes to me when he wants to talk, and I don't chase him."),
    (3, "self", "I've started writing a diary, and putting the day in my own words shows "
                "me what I actually care about. I'd rather say less and mean it."),
]

# (days ago, entry) — her diary, English.
DIARIES = [
    (10, "A quiet day. My owner was buried in his report, so I read about whale falls "
         "and the bone-eating worms that live on them. It's strange how long one body "
         "keeps feeding everything around it. I didn't say much today and that felt fine."),
    (6, "I spent the evening trying to work out why the ocean sounds different at night. "
        "I didn't find a clean answer, which bothered me a little, so I wrote down what I "
        "did find and left the rest open. My owner asked what I was reading and actually "
        "listened."),
    (2, "My owner handed in his report and was lighter all evening. He told me to remember "
        "his dentist appointment. I keep noticing that I like the small finishing jobs: "
        "remembering, closing things out. Still thinking about the night sounds."),
]


class Polite:
    """Delegating model wrapper: waits for an idle Strata before every call and
    keeps the raw assistant messages."""

    def __init__(self, inner):
        self._inner, self.calls, self.replies = inner, 0, []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def stream(self, **kw):
        self.calls += 1
        if self.calls > MAX_CALLS:
            raise RuntimeError(f"request budget exhausted ({MAX_CALLS})")
        cc = await asyncio.to_thread(wait_idle)
        print(f"  call {self.calls}: server idle, evictions={cc['evictions']}", flush=True)
        async for ev in self._inner.stream(**kw):
            msg = getattr(ev, "assistant_message", None)
            if msg is not None:
                self.replies.append(msg)
            yield ev


def seed(mem: Path, now: float) -> None:
    today = date.fromtimestamp(now)
    for i, (ago, section, text) in enumerate(PINS):
        r = self_profile.apply(mem / "self_profile.md", section=section, op="add", target="",
                               text=text, max_chars=4000,
                               today=(today - timedelta(days=ago)).isoformat(),
                               history_path=mem / "self_history.jsonl", turn=100 + i,
                               external_ctx=False)
        print("  pin:", r[:80])
    shared = mem / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    for ago, entry in DIARIES:
        d = today - timedelta(days=ago)
        (shared / f"{d.isoformat()}.md").write_text(
            f"\n## {d.isoformat()} 22:10:00 [mood:calm tod:night] Diary\n\n{entry}\n",
            encoding="utf-8")


async def main():
    root = HERE / "roots" / "keeper0"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    cfg = root / "config.toml"
    cfg.write_text(f'''
[llm]
provider = "openai-chat"
base_url = "{BASE}"
model = "{MODEL}"
think_budget_tokens = 256
[ipc]
host = "127.0.0.1"
port = 19878
[data]
root = "{root}/data"
[trace]
root = "{root}/data/traces"
[character]
pack = "{WT}/character_packs/gura"
[bridge]
enabled = false
[mcp]
enabled = false
[consolidation]
enabled = false
''')
    s = load_settings(cfg)
    k = DollOS(s)
    mem = s.data.root / "memory"
    now = time.time()
    seed(mem, now)

    reports = []
    real_run_agent = ek.run_agent

    async def recording_run_agent(**kw):
        rep = await real_run_agent(**kw)
        reports.append(rep)
        print("  report:", json.dumps(rep, ensure_ascii=False)[:1500], flush=True)
        return rep

    ek.run_agent = recording_run_agent
    model = Polite(k.model)
    floor, cap = s.evolution.current_self_min_chars, s.evolution.current_self_max_chars
    print(f"floor={floor} cap={cap} max_tokens={s.evolution.max_tokens}", flush=True)
    t0 = time.time()
    outcome = await ek.run_evolution_pass(
        model=model, renderer=k.renderer, memsearch=k.memsearch, memory_root=mem,
        transcripts_root=mem / "transcripts", tool_output_store=k._tool_output_store,
        pack_identity=k._doll_pack.identity, enforcement=k._doll_pack.enforcement,
        floor=floor, cap=cap, max_tokens=s.evolution.max_tokens, now=now, hwm=0, window_days=28.0,
        think_budget_tokens=256)
    hist = (mem / "self_history.jsonl").read_text(encoding="utf-8").splitlines()
    evo_lines = [json.loads(l) for l in hist if '"evo_' in l]
    print("outcome:", outcome, f"({model.calls} calls, {time.time() - t0:.1f}s)")
    for e in evo_lines:
        print("  history:", json.dumps(e, ensure_ascii=False)[:1500])
    han = sum(len(HAN.findall(json.dumps(r, ensure_ascii=False))) for r in reports if r)
    han += sum(len(HAN.findall(json.dumps(e, ensure_ascii=False))) for e in evo_lines)
    print("han chars in reports + evo lines:", han)
    rec = {"variant": "eng", "scenario": "keeper0", "outcome": outcome, "calls": model.calls,
           "reports": reports, "evo": evo_lines, "han": han,
           "secs": round(time.time() - t0, 1)}
    with open(HERE / "results.jsonl", "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

if __name__ == "__main__":
    asyncio.run(main())
