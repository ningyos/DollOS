import json
import os
import time
from pathlib import Path

from dollos.mind.mind_state import Perception
from dollos.wal.perception_log import PerceptionWAL


def test_wal_append_and_read(tmp_path: Path):
    wal = PerceptionWAL(tmp_path / "wal" / "perceptions.jsonl")
    p1 = Perception(kind="UserSpoke", t=time.time(), data={"text": "hi"})
    seq1 = wal.append(p1)
    p2 = Perception(kind="ScheduledMoment", t=time.time(), data={"text": "alarm"})
    seq2 = wal.append(p2)
    assert seq2 == seq1 + 1

    pending = list(wal.iter_pending())
    assert len(pending) == 2
    assert pending[0][0] == seq1
    assert pending[0][1].kind == "UserSpoke"
    assert pending[0][1].data == {"text": "hi"}
    assert pending[1][0] == seq2
    assert pending[1][1].kind == "ScheduledMoment"


def test_wal_truncate_through(tmp_path: Path):
    wal = PerceptionWAL(tmp_path / "wal" / "perceptions.jsonl")
    s1 = wal.append(Perception(kind="UserSpoke", t=1.0, data={}))
    s2 = wal.append(Perception(kind="UserSpoke", t=2.0, data={}))
    s3 = wal.append(Perception(kind="UserSpoke", t=3.0, data={}))
    wal.truncate_through(s2)  # ack s1, s2

    pending = list(wal.iter_pending())
    assert len(pending) == 1
    # s3 entry remains; verify by data ordering
    assert pending[0][0] == s3


def test_wal_truncate_through_all(tmp_path: Path):
    wal = PerceptionWAL(tmp_path / "wal" / "perceptions.jsonl")
    s1 = wal.append(Perception(kind="UserSpoke", t=1.0, data={}))
    wal.truncate_through(s1)
    assert list(wal.iter_pending()) == []


def test_wal_creates_directory(tmp_path: Path):
    path = tmp_path / "deep" / "nested" / "wal.jsonl"
    wal = PerceptionWAL(path)
    wal.append(Perception(kind="UserSpoke", t=1.0, data={}))
    assert path.exists()


def test_wal_handles_corrupt_line(tmp_path: Path):
    """Malformed JSONL lines (e.g. partial write before crash) are skipped, not crashed on."""
    path = tmp_path / "wal.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"seq": 1, "kind": "UserSpoke", "t": 1.0, "data": {}}\n'
        '{"seq": 2, "ki\n'
        '{"seq": 3, "kind": "UserSpoke", "t": 3.0, "data": {}}\n'
    )

    wal = PerceptionWAL(path)
    pending = list(wal.iter_pending())
    seqs = [s for s, _ in pending]
    assert 1 in seqs
    assert 3 in seqs
    assert 2 not in seqs


def test_wal_persists_across_instances(tmp_path: Path):
    path = tmp_path / "wal.jsonl"
    w1 = PerceptionWAL(path)
    w1.append(Perception(kind="UserSpoke", t=1.0, data={}))
    w1.append(Perception(kind="UserSpoke", t=2.0, data={}))

    w2 = PerceptionWAL(path)
    pending = list(w2.iter_pending())
    assert len(pending) == 2
    s = w2.append(Perception(kind="UserSpoke", t=3.0, data={}))
    pending = list(w2.iter_pending())
    assert pending[-1][0] == s


def test_wal_truncate_is_atomic_via_tmpfile(tmp_path: Path):
    """truncate_through uses tmpfile + os.replace; no partial-write window."""
    path = tmp_path / "wal.jsonl"
    wal = PerceptionWAL(path)
    s1 = wal.append(Perception(kind="UserSpoke", t=1.0, data={}))
    s2 = wal.append(Perception(kind="UserSpoke", t=2.0, data={}))
    wal.truncate_through(s1)

    # No stale .tmp file left behind
    assert not (path.with_suffix(".tmp")).exists()
    pending = [s for s, _ in wal.iter_pending()]
    assert pending == [s2]
