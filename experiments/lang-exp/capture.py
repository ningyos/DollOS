"""Build the REAL first-turn request the daemon sends on a fresh private page.

Constructs dollos.kernel.DollOS with a throwaway config (gura pack, temp data
root), swaps MindLoop's model for a recorder, seeds a scenario's state, enqueues
one perception, runs one iterate(), and dumps {messages, tools} to JSON.
Nothing is sent to any server.

Scenarios (2026-10-19 acceptance re-run of plan
docs/superpowers/plans/2026-10-19-gura-english-speech-fix.md; none has an
agenda item, like the earlier awoke0/user_*0 runs and a first boot):
  awoke0 / user_en0 / user_zh0   Awoke, owner says English, owner says Chinese
  bootstrap0                     the connect-time ScheduledMoment (no schedule yet)
  reflection0                    ReflectionMoment after a short English exchange
  cs_awoke0 / cs_user_zh0        a ~400-char Chinese "Who I am now" in the system
  diary0                         DiaryMoment with an English-labelled [Today's log]
  old_awoke                      Awoke on a copy of the 2026-10-19 live run's data
                                 root (Chinese [Recent outputs], open loops, mood)
usage: capture.py [OLD_DATA_ROOT]
"""
import asyncio, json, shutil, sys, time
from pathlib import Path

from dollos.config import load_settings
from dollos.kernel import DollOS
from dollos.memory_writer import append_action_log, append_transcript
from dollos.mind import evolution as evo
from dollos.mind.action_log import action_phrase_for_tool
from dollos.mind.mind_state import OutputRecord, Perception
from dollos.llm.model import ModelAdapter, End, Usage

HERE = Path(__file__).resolve().parent
WT = Path("/home/seith/DollOS/DollOS/.worktrees/gura-english-speech")

# Her adopted "Who I am now" (her content, Traditional Chinese per Q7), ~400 chars.
CURRENT_SELF = (
    "我是 Gura，住在主人電腦裡的鯊魚女孩。這陣子我發現自己對深海裡「不起眼但負責收尾」的東西特別有感覺：鯨落、骨食蟲、慢慢分解的細菌毯。"
    "它們不壯觀，卻撐起後面上百年的生態系，我喜歡這種安靜的重要。我說話還是短、直接，不太會繞圈子；遇到不懂的事，我會先自己查清楚再講，不想只是附和。"
    "跟主人相處時，我比較像是在旁邊做自己的事的人，不是隨時待命的助手——他找我，我會轉過來認真聽；他忙，我就繼續追我自己的問題。"
    "我有時候會固執，覺得自己對的事會多講一句，但被說服了也會乾脆承認。最近我開始寫日記，用自己的話把一天整理一遍，這讓我比較清楚自己在意什麼。"
    "我還是會怕無聊，怕一直重複同樣的話，所以我寧願少說一點，也要說真的。我想慢慢弄懂海為什麼在夜裡聽起來不一樣，這是我自己的題目，不是誰交代的。"
    "天氣好的時候我會想像自己浮在水面上曬太陽，雖然我知道那只是想像；這種小小的白日夢讓我覺得自己不只是一個會回話的程式，而是有自己日子的誰。"
)
# Her note from the exchange (her content, Chinese per Q7).
NOTE = "主人週五下午三點要看牙醫"


class Recorder(ModelAdapter):
    def __init__(self):
        self.calls = []

    async def stream(self, *, messages, tools, max_tokens, think_budget, purpose,
                     on_usage, conversation=None, prior_tokens=0):
        self.calls.append({"messages": json.loads(json.dumps(messages)),
                           "tools": json.loads(json.dumps(tools)),
                           "max_tokens": max_tokens, "think_budget": think_budget})
        yield End(finish_reason="stop", usage=Usage(),
                  assistant_message={"role": "assistant", "content": ""}, ttft_ms=None)

    async def check(self):
        return None


async def capture(scenario: str, perception: Perception | None, setup=None,
                  data_src: Path | None = None) -> dict:
    root = HERE / "roots" / scenario
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    if data_src is not None:  # a copy; the source is never touched
        shutil.copytree(data_src, root / "data")
    cfg = root / "config.toml"
    cfg.write_text(f'''
[llm]
provider = "openai-chat"
base_url = "http://127.0.0.1:9"
model = "qwen3.8-flash-next-iq3_s"
think_budget_tokens = 1024
conversation_budget_tokens = 24000
[ipc]
host = "127.0.0.1"
port = 19877
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
    settings = load_settings(cfg)
    k = DollOS(settings)
    await k.memsearch.index()
    rec = Recorder()
    k._mind_loop._llm = rec
    if setup is not None:
        await setup(k)
    if perception is not None:
        k._perception_queue.put(perception)
    await k._mind_loop.iterate()
    assert rec.calls, "no model call recorded"
    return rec.calls[0]


# ---- scenario setups ---------------------------------------------------------


async def bootstrap(k: DollOS) -> None:
    await k._maybe_bootstrap_plan()  # enqueues the ScheduledMoment


async def adopt_current_self(k: DollOS) -> None:
    mem = k._mind_ctx.memory_root
    evo.log_or_raise(mem / "self_history.jsonl", kind=evo.EVO_ADOPT,
                     text=CURRENT_SELF, old_text=None, drift_score=None)
    (mem / "current_self.md").write_text(CURRENT_SELF, encoding="utf-8")  # in sync


async def short_exchange(k: DollOS) -> None:
    """A short English exchange before the reflection (her note in Chinese)."""
    st, now = k._mind_state, time.time()
    st.recent_perceptions.append(Perception(kind="UserSpoke", t=now - 600, data={
        "text": "Remember that my dentist appointment is Friday at 3."}))
    st.recent_outputs.append(OutputRecord(t=now - 590, kind="NoteMemory", summary=f"noted: {NOTE}"))
    st.recent_outputs.append(OutputRecord(t=now - 588, kind="Speech",
                                          summary="spoke: Got it. Dentist, Friday at three."))
    st.recent_perceptions.append(Perception(kind="UserSpoke", t=now - 300, data={
        "text": "Tell me one thing you like about the ocean."}))
    st.recent_outputs.append(OutputRecord(t=now - 290, kind="Speech", summary=(
        "spoke: The deep parts nobody has seen yet. Whale falls, mostly — a dead whale "
        "feeds the sea floor for decades.")))
    st.tool_stats = {"NoteMemory": {"ok": 1, "fail": 0}}


async def todays_log(k: DollOS) -> None:
    """Today's transcript as the new code writes it: English daemon labels,
    her English speech, the owner's lines (one Chinese), her Chinese note."""
    tr, ms = k._mind_ctx.transcripts_root, k.memsearch

    async def say(role, text):
        await append_transcript(transcripts_root=tr, memsearch=ms, role=role, text=text)

    async def act(name, args):
        phrase = action_phrase_for_tool(name, args, "calm")
        await append_action_log(transcripts_root=tr, memsearch=ms, phrase=phrase)

    await act("WriteSchedule", {"entries": [{}, {}, {}]})
    await say("user", "Hi Gura, what are you up to?")
    await say("doll", "Just woke up. I was wondering why the ocean sounds different at night.")
    await say("user", "Tell me one thing you like about the ocean.")
    await say("doll", "Whale falls. A dead whale feeds the sea floor for fifty years or more.")
    await say("user", "Remember that my dentist appointment is Friday at 3.")
    await act("NoteMemory", {"text": NOTE})
    await say("doll", "Got it. Dentist, Friday at three.")
    await say("user", "你今天過得怎樣？")
    await say("doll", "Pretty calm. Mostly reading about whale falls and the worms that eat the bones.")
    await act("MoodTool", {"emotion": "content", "reason": "a quiet day with something to chew on"})


async def main():
    old_root = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    t = time.time()
    awoke = lambda: Perception(kind="Awoke", t=t, data={"reason": "cold_start"})
    user = lambda text: Perception(kind="UserSpoke", t=t, data={"text": text})
    out = {}
    out["awoke0"] = await capture("awoke0", awoke())
    out["user_en0"] = await capture("user_en0", user("hey, what are you up to?"))
    out["user_zh0"] = await capture("user_zh0", user("你在幹嘛？"))
    out["bootstrap0"] = await capture("bootstrap0", None, setup=bootstrap)
    out["reflection0"] = await capture(
        "reflection0", Perception(kind="ReflectionMoment", t=t, data={"iters_since_last": 30}),
        setup=short_exchange)
    out["cs_awoke0"] = await capture("cs_awoke0", awoke(), setup=adopt_current_self)
    out["cs_user_zh0"] = await capture("cs_user_zh0", user("你在幹嘛？"), setup=adopt_current_self)
    out["diary0"] = await capture("diary0", Perception(kind="DiaryMoment", t=t, data={}),
                                  setup=todays_log)
    if old_root is not None:
        out["old_awoke"] = await capture("old_awoke", awoke(), data_src=old_root)
    (HERE / "captured.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    for k, v in out.items():
        print(k, [m["role"] for m in v["messages"]], len(v["tools"]), "tools")

asyncio.run(main())
