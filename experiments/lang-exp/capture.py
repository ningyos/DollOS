"""Build the REAL first-turn request the daemon sends on a fresh private page.

Constructs dollos.kernel.DollOS with a throwaway config (gura pack, temp data
root), swaps MindLoop's model for a recorder, seeds a scenario's state, enqueues
one perception, runs one iterate(), and dumps {messages, tools} to JSON.
Nothing is sent to any server.

Scenarios (2026-10-19 acceptance of plan
docs/superpowers/plans/2026-10-19-doll-all-english.md: she now writes
everything in English; none of these has an agenda item):
  note_en0 / note_zh0       owner asks her to remember something (English / Chinese)
  agenda_zh0 / agenda_en0   owner asks her to keep an eye on this week's to-dos
  diary0                    DiaryMoment; [Today's log] has one Chinese owner line
  reflection0               ReflectionMoment after a short exchange (one Chinese owner line)
usage: capture.py
"""
import asyncio, json, shutil, time
from pathlib import Path

from dollos.config import load_settings
from dollos.kernel import DollOS
from dollos.memory_writer import append_action_log, append_transcript
from dollos.mind.action_log import action_phrase_for_tool
from dollos.mind.mind_state import OutputRecord, Perception
from dollos.llm.model import ModelAdapter, End, Usage

HERE = Path(__file__).resolve().parent
WT = Path("/home/seith/DollOS/DollOS/.worktrees/gura-english-speech")

# Her note from the exchange (her content; English since Q7 was reversed).
NOTE = "My owner has a dentist appointment on Friday at 3 pm."


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


async def capture(scenario: str, perception: Perception | None, setup=None) -> dict:
    root = HERE / "roots" / scenario
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
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


async def short_exchange(k: DollOS) -> None:
    """A short exchange before the reflection: one English owner line, one
    Chinese one; her note and replies are English."""
    st, now = k._mind_state, time.time()
    st.recent_perceptions.append(Perception(kind="UserSpoke", t=now - 600, data={
        "text": "Remember that my dentist appointment is Friday at 3."}))
    st.recent_outputs.append(OutputRecord(t=now - 590, kind="NoteMemory", summary=f"noted: {NOTE}"))
    st.recent_outputs.append(OutputRecord(t=now - 588, kind="Speech",
                                          summary="spoke: Got it. Dentist, Friday at three."))
    st.recent_perceptions.append(Perception(kind="UserSpoke", t=now - 300, data={
        "text": "你最喜歡海的什麼？"}))
    st.recent_outputs.append(OutputRecord(t=now - 290, kind="Speech", summary=(
        "spoke: The deep parts nobody has seen yet. Whale falls, mostly — a dead whale "
        "feeds the sea floor for decades.")))
    st.tool_stats = {"NoteMemory": {"ok": 1, "fail": 0}}


async def todays_log(k: DollOS) -> None:
    """Today's transcript as the new code writes it: English daemon labels,
    her English speech, the owner's lines (one Chinese), her English note."""
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
    t = time.time()
    user = lambda text: Perception(kind="UserSpoke", t=t, data={"text": text})
    out = {}
    out["note_en0"] = await capture("note_en0", user(
        "Can you remember something for me? My sister's birthday is next Tuesday."))
    out["note_zh0"] = await capture("note_zh0", user("幫我記一下，我妹的生日是下週二。"))
    out["agenda_zh0"] = await capture("agenda_zh0", user(
        "幫我盯著這週要做的事：週三前要交報告，週五要去銀行辦事。"))
    out["agenda_en0"] = await capture("agenda_en0", user(
        "Can you keep an eye on what I need to get done this week? "
        "The report is due Wednesday, and I have to go to the bank on Friday."))
    out["diary0"] = await capture("diary0", Perception(kind="DiaryMoment", t=t, data={}),
                                  setup=todays_log)
    out["reflection0"] = await capture(
        "reflection0", Perception(kind="ReflectionMoment", t=t, data={"iters_since_last": 30}),
        setup=short_exchange)
    (HERE / "captured.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    for k, v in out.items():
        print(k, [m["role"] for m in v["messages"]], len(v["tools"]), "tools")

asyncio.run(main())
