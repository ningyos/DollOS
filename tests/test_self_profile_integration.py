"""A1 self-profile integration validation (Task 8, final).

Two structural guarantees the unit-level tests (Tasks 1-7) don't directly
exercise together:

1. Structural guard — ``self_profile.md`` lives at ``memory_root`` root,
   which FtsMemory never scans (it only indexes the ``shared`` /
   ``transcripts`` / ``skills`` subdirs kernel.py wires up). That's what
   makes the "no duplicate injection" property hold: the file can never be
   picked up by ``FtsMemory.index()`` / ``index_file()`` and re-surfaced via
   memsearch on top of the always-injected ``[Self profile]`` block.
2. e2e — ``self_profile.apply(add)`` -> ``render_block`` -> ``render_mind``
   produces a prompt containing the pinned text (the full pin -> always
   -inject path works end to end).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from dollos.config import (
    CharacterConfig,
    DataConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemsearchConfig,
    Settings,
)
from dollos.kernel import build_memsearch
from dollos.mind import self_profile as sp
from dollos.mind.mind_prompt import render_mind
from dollos.mind.mind_state import MindState


def _make_settings(tmp_path: Path) -> Settings:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "doll.toml").write_text(
        '[meta]\n'
        'id = "doll"\n'
        'name = "Doll"\n'
        '\n'
        '[identity]\n'
        'self = "You are Doll."\n'
        'personality = "- chill"\n'
        'taboos = "- no LARP"\n'
    )
    return Settings(
        llm=LLMConfig(
            provider="llamacpp",
            template="qwen3-thinking",
            base_url="http://test.local:8001",
            model_alias="big",
        ),
        ipc=IPCConfig(host="127.0.0.1", port=0),
        log=LogConfig(level="WARNING"),
        data=DataConfig(root=tmp_path / "data"),
        memsearch=MemsearchConfig(top_k=7),
        character=CharacterConfig(pack=pack_dir),
    )


def test_memory_root_not_in_fts_paths(tmp_path: Path):
    """Structural guard: memory_root root is not among FtsMemory's indexed
    paths, so self_profile.md (which lives at that root) can never be
    scanned into the FTS index.

    Uses the REAL production wiring — ``kernel.build_memsearch`` — rather
    than a hand-rolled paths list, so this test tracks kernel.py's actual
    behavior (kernel.py:91-116 builds paths=[shared, transcripts, skills],
    all children of memory_root, never memory_root itself) instead of a
    guess that could silently drift out of sync.
    """
    settings = _make_settings(tmp_path)
    memory_root = settings.data.root / "memory"

    mem = build_memsearch(settings)
    try:
        # Core assertion: the root itself is not, and none of the indexed
        # paths equal or sit above it (i.e. every indexed path is a proper
        # descendant of root, never the root itself).
        assert memory_root.resolve() not in {p.resolve() for p in mem._paths}
        for p in mem._paths:
            resolved = p.resolve()
            assert resolved != memory_root.resolve()
            assert memory_root.resolve() in resolved.parents

        # Behavioral corollary: put self_profile.md at the root (not inside
        # any indexed subdir) and confirm a full index() never picks it up,
        # even though its content is otherwise indexable markdown.
        profile = memory_root / "self_profile.md"
        profile.write_text("## 我和主人\n- [r1·2026-06-30] unique-marker-never-indexed\n")
        asyncio.run(mem.index())
        hits = asyncio.run(mem.search("unique-marker-never-indexed", top_k=5))
        assert hits == []
    finally:
        mem.close()


def test_pinned_self_appears_in_next_prompt(tmp_path: Path):
    """e2e: apply(add) -> render_block -> render_mind surfaces the pinned
    text in the next prompt via the always-injected [Self profile] block."""
    profile = tmp_path / "self_profile.md"
    sp.apply(
        profile,
        section="relationship",
        op="add",
        target="",
        text="我們可以直話直說",
        max_chars=1200,
        today="2026-06-30",
    )
    body = sp.render_block(profile)
    assert body is not None

    out = render_mind(MindState(), [], "SYS", self_profile_text=body)
    assert "我們可以直話直說" in out
    assert "[Self profile]" in out
