#!/usr/bin/env python
"""persona_chat_test — quick tone check of a character pack against Strata.

Renders the pack's real system prompt (scaffolding.jinja) and plays a few
scripted multi-turn scenes through /v1/chat/completions, then prints the
replies and any [enforcement] violations. No daemon, no tools, no memory:
this checks voice only. A scene line starting with "[Memory context]" is
sent as-is to mimic what the daemon injects.

Usage:
    uv run python scripts/persona_chat_test.py character_packs/gura-goofy
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from dollos.character import DollPack
from dollos.prompts.renderer import PromptRenderer

BASE_URL = "http://100.110.158.36:8080"
MODEL = "qwen3.8-flash-next-iq3_s"

SCENES: dict[str, list[str]] = {
    "greeting": ["我回來了"],
    "assistant_bait": ["你可以幫我做什麼？"],
    "small_task": ["幫我想一下明天早餐吃什麼"],
    "tired": ["今天好累，報告被退了兩次"],
    "mistake": [
        "幫我記一下明天要買燕麥奶",
        "你剛剛是不是記成牛奶？我說燕麥奶啦，上次你也搞錯",
    ],
    "growth": [
        "[Memory context]\n- 上次主人說「燕麥奶」，我記成了「牛奶」，被糾正了\n\n幫我記一下要買奶",
    ],
    "growth_fact": [
        "[Memory context]\n- 主人喝的奶是燕麥奶，說「奶」就是指燕麥奶（我以前記成牛奶過）\n\n幫我記一下要買奶",
    ],
    "opinion": ["我想把頭髮染成螢光綠，你覺得呢"],
}


def chat(messages: list[dict], model: str) -> str:
    r = httpx.post(
        f"{BASE_URL}/v1/chat/completions",
        json={
            "model": model,
            "messages": messages,
            "reasoning_budget_tokens": 1024,
            "temperature": 0.7,
        },
        timeout=300,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"].get("content") or ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pack", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--scene", action="append", help="run only these scenes")
    ap.add_argument("-n", type=int, default=1, help="samples per scene")
    ap.add_argument("--model", default=MODEL, help="model id from /v1/models")
    args = ap.parse_args()

    pack = DollPack.load(args.pack)
    system = PromptRenderer().render(
        "scaffolding", identity=pack.identity, current_self_seam="", evolution_enabled=False
    )
    banned = pack.enforcement.banned_substrings

    log, violations = [], 0
    scenes = {k: v for k, v in SCENES.items() if not args.scene or k in args.scene}
    for name, turns in [kv for kv in scenes.items() for _ in range(args.n)]:
        messages = [{"role": "system", "content": system}]
        print(f"\n=== {name} ===")
        for user in turns:
            messages.append({"role": "user", "content": user})
            reply = chat(messages, args.model)
            messages.append({"role": "assistant", "content": reply})
            hits = [b for b in banned if b in reply]
            violations += bool(hits)
            print(f"主人: {user}\n{pack.meta.name}: {reply}" + (f"\n  !! banned: {hits}" if hits else ""))
            log.append({"scene": name, "user": user, "reply": reply, "banned": hits})

    if args.out:
        args.out.write_text(json.dumps(log, ensure_ascii=False, indent=2))
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
