"""AmbientLog — full capture + msg_id dedup + retention (spec §3.3 + carry I-2)."""
import json

from dollos.discord_bridge.ambient_log import AmbientLog


def test_append_writes_jsonl(tmp_path):
    log = AmbientLog(tmp_path, retention_days=30)
    assert log.append("g1", "c1", {"msg_id": "m1", "content": "hi", "date": "2026-07-03"}) is True
    p = tmp_path / "discord" / "g1" / "c1" / "2026-07-03.jsonl"
    assert json.loads(p.read_text().splitlines()[0])["msg_id"] == "m1"


def test_dedup_same_msg_id(tmp_path):
    log = AmbientLog(tmp_path, retention_days=30)
    log.append("g1", "c1", {"msg_id": "m1", "content": "hi", "date": "2026-07-03"})
    assert log.append("g1", "c1", {"msg_id": "m1", "content": "hi", "date": "2026-07-03"}) is False
    p = tmp_path / "discord" / "g1" / "c1" / "2026-07-03.jsonl"
    assert len(p.read_text().splitlines()) == 1     # not duplicated


def test_prune_deletes_old(tmp_path):
    log = AmbientLog(tmp_path, retention_days=1)
    old = tmp_path / "discord" / "g1" / "c1" / "2020-01-01.jsonl"
    old.parent.mkdir(parents=True); old.write_text('{"msg_id":"x"}\n')
    log.prune(today="2026-07-03")     # injected today; module never calls date.today()
    assert not old.exists()


def test_prune_retention_boundary(tmp_path):
    """Files exactly retention_days old are KEPT; one day older is DELETED.

    Hermetic: `today` is injected as a fixed ISO string, so the boundary is
    computed from data, not the wall clock. cutoff = today - retention_days;
    a file is deleted only when file_date < cutoff (strictly older).
    """
    log = AmbientLog(tmp_path, retention_days=7)
    today = "2026-07-10"
    # cutoff = 2026-07-03. Boundary file dated exactly cutoff is KEPT (not < cutoff).
    keep = tmp_path / "discord" / "g1" / "c1" / "2026-07-03.jsonl"
    # One day older than cutoff is DELETED.
    delete = tmp_path / "discord" / "g1" / "c1" / "2026-07-02.jsonl"
    keep.parent.mkdir(parents=True)
    keep.write_text('{"msg_id":"keep"}\n')
    delete.write_text('{"msg_id":"del"}\n')

    log.prune(today=today)

    assert keep.exists()          # exactly retention_days old → kept
    assert not delete.exists()    # one day older → deleted


def test_restart_safe_dedup(tmp_path):
    """A fresh instance dedups against msg_ids already on disk (lazy load).

    Simulates a daemon restart / reconnect backfill: instance A logs m1, then a
    brand-new instance B (empty in-memory set) sees m1 again and must return
    False by loading the seen id from the file, not double-write the corpus.
    """
    a = AmbientLog(tmp_path, retention_days=30)
    assert a.append("g1", "c1", {"msg_id": "m1", "content": "hi", "date": "2026-07-03"}) is True

    b = AmbientLog(tmp_path, retention_days=30)     # fresh instance, cold cache
    assert b.append("g1", "c1", {"msg_id": "m1", "content": "hi", "date": "2026-07-03"}) is False

    p = tmp_path / "discord" / "g1" / "c1" / "2026-07-03.jsonl"
    assert len(p.read_text().splitlines()) == 1     # B loaded m1 from disk, no dup
