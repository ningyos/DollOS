"""Trace — finetune 級語意層語料底盤(spec §3.6)。

每 turn 一筆 JSONL envelope(passes nested),落 data/traces/{date}.jsonl。
與 cascade_log 從同一 per-pass tuple 衍生(superset),但獨立序列化。
存實際內容非 hash(T-C2);think 逐字;按日輪替不設上限;
永不進 memsearch 索引;寫失敗 loud 但不斷 turn。

不變式(結構性,非 deny-list):kernel.build_memsearch 只對
data.root/memory/{shared,transcripts,skills} 三個路徑白名單掃描索引;
data/traces 是 data.root 底下與 memory/ 平行的獨立目錄,天然不在任何掃描根
之下,故永不被索引 —— 機制與 self_profile.md(存在 memory_root 但不在三個
索引子目錄內)相同精神,但層級更淺(data/traces 連 memory_root 都不在裡面)。
見 tests/test_trace.py::test_traces_root_not_in_fts_paths(結構守衛,pin 住
此不變式)。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TurnTrace:
    """單一 turn 的可變 envelope builder。turn 尾 finish() 寫一次。"""

    def __init__(self, root: Path, schema_version: str, envelope: dict[str, Any]):
        self._root = root
        self._schema_version = schema_version
        self._envelope = envelope  # turn-level fields already populated
        self._ts: float = envelope["ts"]
        self._passes: list[dict] = []

    def add_pass(
        self,
        *,
        pass_idx: int,
        input_messages_delta: list[dict],
        raw_assistant_emit: str,
        tool_calls: list[dict],
        results: list[dict],
        active_tools: list[str],
        is_reflection: bool,
        safe_mode: bool,
        external: bool,
        latency_ms: int | None,
    ) -> None:
        """追加一個已完成 pass。tokens 明文 drop(存 null):per-pass usage
        不從既有 StreamChunk 掉出來(R2 T-token),離線 retokenize
        input_messages_delta + raw_assistant_emit 可精確還原,不新接 transport。"""
        self._passes.append(
            {
                "pass_idx": pass_idx,
                "input_messages_delta": input_messages_delta,
                "raw_assistant_emit": raw_assistant_emit,  # 逐字全文,非 _parse_think
                "tool_calls": tool_calls,
                "results": results,  # 全文,非 detail[:500]
                "active_tools": active_tools,
                "is_reflection": is_reflection,
                "safe_mode": safe_mode,
                "external": external,
                "latency_ms": latency_ms,
                "tokens": None,
            }
        )

    def finish(self, *, speech: str, silence: bool) -> None:
        """序列化整筆 envelope,append 到 root/{date}.jsonl。失敗 loud 不拋。"""
        self._envelope["passes"] = self._passes
        self._envelope["speech"] = speech
        self._envelope["silence"] = silence
        date = datetime.fromtimestamp(self._ts, UTC).strftime("%Y-%m-%d")
        out = self._root / f"{date}.jsonl"
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            line = json.dumps(self._envelope, ensure_ascii=False, default=str)
            with out.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            logger.exception("trace finish() write failed; continuing (turn not broken)")


class TraceWriter:
    """建構每 turn 的 TurnTrace。無狀態(單 event loop,一次一 turn)。"""

    def __init__(self, root: Path, *, schema_version: str = "1"):
        self._root = Path(root)
        self._schema_version = schema_version

    def begin_turn(
        self,
        *,
        turn_id: str,
        ts: float,
        origin_channel: str,
        situation: str,
        model_id: str | None,
        perception_batch: list[dict],
        static_prefix: dict,
        dynamic_blocks: dict,
    ) -> TurnTrace:
        envelope: dict[str, Any] = {
            "schema_version": self._schema_version,
            "turn_id": turn_id,
            "ts": ts,
            "origin_channel": origin_channel,
            "situation": situation,
            "model_id": model_id,
            "perception_batch": perception_batch,
            "static_prefix": static_prefix,
            "dynamic_blocks": dynamic_blocks,
        }
        return TurnTrace(self._root, self._schema_version, envelope)
