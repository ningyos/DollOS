import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from dollos.config import (
    CharacterConfig,
    DataConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemsearchConfig,
    Settings,
    TraceSettings,
)
from dollos.kernel import build_memsearch
from dollos.mind.trace import TraceWriter


def _read_lines(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_envelope_written_once_with_nested_passes(tmp_path):
    w = TraceWriter(tmp_path)
    ts = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC).timestamp()
    tt = w.begin_turn(
        turn_id="t1",
        ts=ts,
        origin_channel="discord:42",
        situation="external",
        model_id="unsloth/Qwen3.6",
        perception_batch=[{"kind": "ChannelMessage", "data": {"text": "hi"}}],
        static_prefix={
            "identity_hash": "abc",
            "current_self_text": "我是 Gura",
            "situational_template_id": None,
        },
        dynamic_blocks={
            "memsearch_hits": [{"source": "s", "text": "m"}],
            "mood": {"valence": 0.1},
            "energy": 0.9,
        },
    )
    tt.add_pass(
        pass_idx=0,
        input_messages_delta=[{"role": "user", "content": "PROMPT"}],
        raw_assistant_emit="<think>SEEN: hi</think> hello",
        tool_calls=[{"name": "Recall", "args": {"query": "x"}}],
        results=[{"tool_name": "Recall", "success": True, "detail": "FULL DETAIL " * 100}],
        active_tools=["Recall", "Say"],
        is_reflection=False,
        safe_mode=False,
        external=True,
        latency_ms=1234,
    )
    tt.finish(speech="hello", silence=False)

    out = tmp_path / "2026-07-03.jsonl"
    lines = _read_lines(out)
    assert len(lines) == 1
    env = lines[0]
    assert env["schema_version"] == "1"
    assert env["turn_id"] == "t1"
    assert env["origin_channel"] == "discord:42"
    assert env["situation"] == "external"
    assert env["model_id"] == "unsloth/Qwen3.6"
    assert env["perception_batch"][0]["kind"] == "ChannelMessage"
    # static_prefix: current_self VERBATIM, identity as hash
    assert env["static_prefix"]["current_self_text"] == "我是 Gura"
    assert env["static_prefix"]["identity_hash"] == "abc"
    # dynamic_blocks store ACTUAL values, not hashes (T-C2)
    assert env["dynamic_blocks"]["memsearch_hits"][0]["text"] == "m"
    assert env["speech"] == "hello"
    assert env["silence"] is False
    # passes nested, per-pass full content
    assert len(env["passes"]) == 1
    p = env["passes"][0]
    assert p["pass_idx"] == 0
    assert p["raw_assistant_emit"] == "<think>SEEN: hi</think> hello"  # verbatim, not parsed
    # tool result stored FULL, not truncated to 500 (T-C2)
    assert len(p["results"][0]["detail"]) == len("FULL DETAIL " * 100)
    assert p["active_tools"] == ["Recall", "Say"]
    assert p["latency_ms"] == 1234
    assert p["tokens"] is None  # deferred, retokenize offline


def test_date_bucket_uses_ts_not_wallclock(tmp_path):
    # ts on 2026-07-01 must land in 2026-07-01.jsonl regardless of wall-clock.
    w = TraceWriter(tmp_path)
    ts = datetime(2026, 7, 1, 23, 59, 0, tzinfo=UTC).timestamp()
    tt = w.begin_turn(
        turn_id="t", ts=ts, origin_channel="", situation="internal", model_id=None,
        perception_batch=[], static_prefix={}, dynamic_blocks={},
    )
    tt.finish(speech="", silence=True)
    assert (tmp_path / "2026-07-01.jsonl").exists()
    assert not (tmp_path / "2026-07-02.jsonl").exists()


def test_write_failure_is_logged_not_raised(tmp_path, monkeypatch):
    w = TraceWriter(tmp_path)
    ts = datetime(2026, 7, 3, 0, 0, 0, tzinfo=UTC).timestamp()
    tt = w.begin_turn(
        turn_id="t", ts=ts, origin_channel="", situation="internal", model_id=None,
        perception_batch=[], static_prefix={}, dynamic_blocks={},
    )

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", boom)
    # must NOT raise — loud but turn-safe
    tt.finish(speech="", silence=True)


def test_silence_turn_flagged(tmp_path):
    w = TraceWriter(tmp_path)
    ts = datetime(2026, 7, 3, 0, 0, 0, tzinfo=UTC).timestamp()
    tt = w.begin_turn(
        turn_id="t", ts=ts, origin_channel="", situation="internal", model_id=None,
        perception_batch=[], static_prefix={}, dynamic_blocks={},
    )
    tt.finish(speech="", silence=True)
    env = _read_lines(tmp_path / "2026-07-03.jsonl")[0]
    assert env["silence"] is True
    assert env["passes"] == []


# ── Task 6: never-FTS structural guard (mirrors self_profile's
# test_memory_root_not_in_fts_paths in test_self_profile_integration.py) ──


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
        trace=TraceSettings(root=str(tmp_path / "data" / "traces")),
    )


def test_traces_root_not_in_fts_paths(tmp_path: Path):
    """Structural guard: data/traces/ is the finetune training-layer corpus
    and must NEVER enter the memsearch conversation-layer index — same
    invariant as self_profile.md.

    Mechanism found (investigated, not guessed): NATURAL ISOLATION, not a
    deny-list. ``kernel.build_memsearch`` builds FtsMemory with an explicit
    ALLOWLIST of exactly three paths — ``data.root/memory/{shared,
    transcripts,skills}`` (kernel.py:128-134) — nothing else is ever walked.
    ``data/traces`` (settings.trace.root) sits at ``data.root`` itself, a
    disjoint sibling of ``data.root/memory``, not nested under any indexed
    subdir (in fact not even nested under ``data.root/memory`` at all — an
    even shallower separation than self_profile.md, which merely sits
    outside the three indexed subdirs while still living inside
    ``data.root/memory``). No exclusion logic was added; this test pins the
    existing structural fact using the REAL production wiring
    (``kernel.build_memsearch``), not a hand-rolled path list, so it tracks
    kernel.py's actual behavior instead of a guess that could silently drift.
    """
    settings = _make_settings(tmp_path)
    traces_root = Path(settings.trace.root).resolve()

    mem = build_memsearch(settings)
    try:
        # Core assertion: every FtsMemory-indexed path is disjoint from
        # traces_root — neither equal to it, nor an ancestor, nor a
        # descendant of it.
        for p in mem._paths:
            resolved = p.resolve()
            assert resolved != traces_root
            assert traces_root not in resolved.parents
            assert resolved not in traces_root.parents

        # Behavioral corollary: drop an indexable markdown file INSIDE
        # traces_root (simulating a stray/misplaced file) and confirm a full
        # index() never picks it up — the directory itself is not a scan
        # root, so content placed there is structurally invisible to FTS
        # regardless of extension.
        traces_root.mkdir(parents=True, exist_ok=True)
        (traces_root / "leaked.md").write_text(
            "## leaked\n- unique-marker-never-indexed-trace\n"
        )
        asyncio.run(mem.index())
        hits = asyncio.run(mem.search("unique-marker-never-indexed-trace", top_k=5))
        assert hits == []
    finally:
        mem.close()
