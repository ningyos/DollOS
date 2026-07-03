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
    log.prune()
    assert not old.exists()
