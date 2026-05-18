"""Smoke test: write a few NoteMemory entries with different moods, then run
associative_search with a current state matching one of them.

Run:  uv run python scripts/smoke_associative_recall.py
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime
from pathlib import Path

import memsearch  # type: ignore

from dollos.mind.associative_search import associative_search
from dollos.mind.context_tags import build_heading
from dollos.mind.mind_state import MindState, Mood, Perception


async def _note(memroot: Path, ms, state: MindState, text: str, when: datetime) -> None:
    """Mimic NoteMemory.run but with an injected timestamp."""
    path = memroot / f"{when:%Y-%m-%d}.md"
    heading = build_heading(state, when)
    with path.open("a") as f:
        f.write(f"\n## {heading}\n\n{text}\n")
    await ms.index_file(path)
    print(f"  wrote: {heading}")
    print(f"         → {text}")


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        memroot = root / "shared"
        memroot.mkdir(parents=True)

        ms = memsearch.MemSearch(
            paths=[str(memroot)],
            embedding_provider="onnx",
            milvus_uri=str(root / "milvus.db"),
            collection="smoke_" + os.urandom(4).hex(),
        )
        await ms.index()

        # 3 entries with different moods + dates
        print("=== writing memories ===")
        s_anx = MindState(mood=Mood(emotion="anxious"), focus="powdur voice tuning")
        await _note(
            memroot, ms, s_anx,
            "powdur 的聲音在 evening 試出來不太對，調 EQ 之後好多了",
            datetime(2026, 4, 10, 19, 30),
        )
        s_calm = MindState(mood=Mood(emotion="calm"), focus="reading")
        await _note(
            memroot, ms, s_calm,
            "今天讀了一本關於海洋的書，感覺很平靜",
            datetime(2026, 4, 12, 14, 0),
        )
        s_curi = MindState(mood=Mood(emotion="curious"), focus="cooking")
        await _note(
            memroot, ms, s_curi,
            "嘗試了新的義大利麵食譜，效果不錯",
            datetime(2026, 4, 13, 11, 0),
        )

        # Re-index so all chunks visible
        await ms.index()

        # Current state matches the anxious / evening / mon entry
        now = datetime(2026, 5, 18, 19, 45)  # Mon, evening
        current = MindState(mood=Mood(emotion="anxious"), focus="powdur voice tuning")
        current.recent_perceptions.append(
            Perception(kind="UserSpoke", t=0.0, data={"text": "powdur 的聲音"})
        )

        print("\n=== current state ===")
        print(f"  mood = anxious, focus = powdur voice tuning")
        print(f"  now  = {now}  (Mon, evening)")

        print("\n=== associative_search top 4 ===")
        hits = await associative_search(ms, current, top_k=4, now=now)
        if not hits:
            print("  (no hits)")
        for h in hits:
            print(f"  [{h['_axis']}={h['_axis_value']}] {h['content'].strip()[:100]}")

        # Assert the anxious one ranks
        contents = " ".join(h["content"] for h in hits)
        assert "powdur" in contents, "expected the anxious/powdur memory to appear"
        print("\n=== OK ===  matched anxious/powdur memory")

        try:
            ms.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
