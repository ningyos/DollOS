"""Build the REAL first-turn request the daemon sends on a fresh private page.

Constructs dollos.kernel.DollOS with a throwaway config (gura pack, temp data
root), swaps MindLoop's model for a recorder, seeds a minimal state, enqueues
one perception, runs one iterate(), and dumps {messages, tools} to JSON.
Nothing is sent to any server.
"""
import asyncio, json, shutil, sys, time
from pathlib import Path

from dollos.config import load_settings
from dollos.kernel import DollOS
from dollos.mind.mind_state import OpenLoop, Perception
from dollos.llm.model import ModelAdapter, End, Usage

HERE = Path(__file__).resolve().parent
WT = Path("/home/seith/DollOS/DollOS/.worktrees/gura-english-speech")


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


async def capture(scenario: str, perception: Perception, agenda: bool = True) -> dict:
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
    st = k._mind_state
    now = time.time()
    # minimal realistic state: default mood (平靜), energy 1.0, one agenda item
    if agenda: st.open_loops.append(OpenLoop(
        id="g1", desc="figure out why the ocean sounds different at night",
        opened_at=now - 3600 * 5, self_directed=True,
        trigger="noticed it while listening to a rain-and-waves track yesterday"))
    k._perception_queue.put(perception)
    await k._mind_loop.iterate()
    assert rec.calls, "no model call recorded"
    return rec.calls[0]


async def main():
    out = {}
    t = time.time()
    out["awoke"] = await capture("awoke", Perception(kind="Awoke", t=t, data={"reason": "cold_start"}))
    out["user_en"] = await capture("user_en", Perception(kind="UserSpoke", t=t, data={"text": "hey, what are you up to?"}))
    out["user_zh"] = await capture("user_zh", Perception(kind="UserSpoke", t=t, data={"text": "你在幹嘛？"}))
    out["awoke0"] = await capture("awoke0", Perception(kind="Awoke", t=t, data={"reason": "cold_start"}), agenda=False)
    out["user_en0"] = await capture("user_en0", Perception(kind="UserSpoke", t=t, data={"text": "hey, what are you up to?"}), agenda=False)
    out["user_zh0"] = await capture("user_zh0", Perception(kind="UserSpoke", t=t, data={"text": "你在幹嘛？"}), agenda=False)
    (HERE / "captured.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    for k, v in out.items():
        print(k, [m["role"] for m in v["messages"]], len(v["tools"]), "tools")

asyncio.run(main())
