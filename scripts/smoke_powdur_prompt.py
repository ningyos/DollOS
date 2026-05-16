"""Prompt-rendering smoke for Powdur character pack.

Loads the Powdur DollPack, renders the `scaffolding` system prompt,
and prints what would be sent to the big model. No LLM is called.

Run:
    uv run python scripts/smoke_powdur_prompt.py
"""
from __future__ import annotations

from pathlib import Path

from dollos.character import DollPack
from dollos.prompts.renderer import PromptRenderer


def main() -> None:
    pack_dir = Path("character_packs/powdur")
    pack = DollPack.load(pack_dir)

    renderer = PromptRenderer()
    system = renderer.render(
        "scaffolding",
        identity=pack.identity,
        available_skills=[],
    )

    sample_user_msgs = [
        "你好",
        "今天天氣怎樣？",
        "I made a hair plugin in Blender, want to see?",
        "look at this twitter thread mob",
    ]

    bar = "=" * 78
    print(bar)
    print("SYSTEM PROMPT (rendered from scaffolding.jinja + Powdur identity)")
    print(bar)
    print(system)

    for i, msg in enumerate(sample_user_msgs):
        memory_block = "[Memory context]\n(no relevant memory)\n\n"
        first_user = (
            memory_block
            + "[Now]\n2026-05-15 14:32:00\n\n"
            + "[Active monitors]\n(none)\n\n"
            + "[Pending events]\n(none)\n\n"
            + f"[Message]\n{msg}"
        )
        print()
        print(bar)
        print(f"USER MESSAGE #{i} (assembled)")
        print(bar)
        print(first_user)


if __name__ == "__main__":
    main()
