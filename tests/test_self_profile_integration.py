"""A1 self-profile integration validation (Task 8, final).

Two structural guarantees the unit-level tests (Tasks 1-7) don't directly
exercise together:

1. Structural guard — ``self_profile.md`` lives at ``memory_root`` root,
   which FtsMemory never scans (it only indexes the ``shared`` /
   ``transcripts`` / ``skills`` subdirs kernel.py wires up). That's what
   makes the "no duplicate injection" property hold: the file can never be
   picked up by ``FtsMemory.index()`` / ``index_file()`` and re-surfaced via
   memsearch on top of the always-injected ``[Self profile]`` block.
2. e2e — ``self_profile.apply(add)`` -> ``render_block`` -> ``render_epoch_start``
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
from dollos.mind.mind_prompt import Seen, render_epoch_start
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
            base_url="http://test.local:8001",
            model="big",
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
    """e2e: apply(add) -> render_block -> render_epoch_start surfaces the
    pinned text on the next page via the [Self profile] block."""
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

    out = render_epoch_start(MindState(), Seen(), "internal", 0.0, self_profile_text=body)
    assert "我們可以直話直說" in out
    assert "[Self profile]" in out


def _profile_with_all_sections(memory_root: Path) -> None:
    profile = memory_root / "self_profile.md"
    for section, text in (("self", "self-marker-ok"),
                          ("user", "user-marker-owner-name"),
                          ("relationship", "rel-marker-dont-call-me-master")):
        sp.apply(profile, section=section, op="add", target="", text=text,
                 max_chars=1200, today="2026-10-27")


def _prompt_text(ml) -> str:
    return "\n".join(m["content"] or "" for m in ml._llm.calls[0]["messages"]
                     if isinstance(m.get("content"), str))


def test_external_public_turn_sees_only_self_section(tmp_path: Path):
    """Live acceptance 2026-10-27: Doll repeated the owner's facts (user /
    relationship sections) to strangers in a public channel. A stranger turn's
    prompt carries the self bullet only; an owner turn carries all three."""
    import time

    from dollos.mind.mind_state import Perception
    from tests._mindloop_factory import make_mindloop

    _profile_with_all_sections(tmp_path)

    stranger = make_mindloop(memory_root=tmp_path, self_profile_enabled=True)
    asyncio.run(stranger._run_one_turn([Perception(
        kind="ChannelMessage", t=time.time(),
        data={"content": "hi", "channel_id": "disc:g1:c1",
              "author_is_owner": False, "is_dm": False},
    )]))
    assert stranger._ctx.origin_tier == "external_public"
    text = _prompt_text(stranger)
    assert "self-marker-ok" in text
    assert "user-marker-owner-name" not in text
    assert "rel-marker-dont-call-me-master" not in text

    owner = make_mindloop(memory_root=tmp_path, self_profile_enabled=True)
    asyncio.run(owner._run_one_turn([Perception(
        kind="UserSpoke", t=time.time(), data={"text": "hi"},
    )]))
    assert owner._ctx.origin_tier == "internal"
    text = _prompt_text(owner)
    assert "self-marker-ok" in text
    assert "user-marker-owner-name" in text
    assert "rel-marker-dont-call-me-master" in text
