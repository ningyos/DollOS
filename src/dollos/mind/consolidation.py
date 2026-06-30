"""B2 sleep-time consolidation — driver + trigger.

Driver reads a target day's transcript, feeds it inline to a memory-keeper
agent (KEEPER_TOOLS only), and writes the returned bullets to
consolidated/{date}.md. Candidate facts are pull-only (see mind_loop gating).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from dollos.agent_engine import run_agent
from dollos.tools import KEEPER_TOOLS

logger = logging.getLogger(__name__)

_KEEPER_TASK = """讀以下逐字稿,提取去重成簡潔的中性 candidate 事實——主人的穩定偏好/習慣、你們關係的進展、值得長期記住的模式。陳述為觀察(『主人偏好X』),不要自我宣告(『我是X』)。重複合併、過時捨棄。不確定就不寫,寧缺勿濫。準不要多。把結果用 Report 工具的 details 欄回傳,每條一行 markdown bullet。

逐字稿:
{transcript}
"""


async def run_consolidation(
    *,
    target_date: str,
    adapter,
    renderer,
    memsearch,
    memory_root: Path,
    transcripts_root: Path,
    tool_output_store,
    consolidated_dir: Path,
    max_tokens: int = 2048,
    agent_timeout_s: int = 120,
    transcript_tail_chars: int = 8000,
) -> bool:
    """Consolidate one day's transcript into consolidated/{date}.md.

    Returns True on success (file written + indexed), False otherwise.
    Raises CancelledError through (caller treats as cancel → no write).
    """
    src = transcripts_root / f"{target_date}.md"
    if not src.exists():
        logger.info("consolidation: no transcript for %s; skip", target_date)
        return False
    transcript = src.read_text(encoding="utf-8")[-transcript_tail_chars:]

    # Render subagent scaffolding system prompt with KEEPER_TOOLS only.
    tools_by_name = {cls.__name__: cls for cls in KEEPER_TOOLS}
    system = renderer.render("subagent_scaffolding", tool_registry=tools_by_name)

    report = await run_agent(
        task=_KEEPER_TASK.format(transcript=transcript),
        system=system,
        adapter=adapter,
        renderer=renderer,
        memory_root=memory_root,
        memsearch=memsearch,
        transcripts_root=transcripts_root,
        tool_output_store=tool_output_store,
        tools=KEEPER_TOOLS,
        max_tokens=max_tokens,
        shell_runner=None,
        monitor_runner=None,
    )
    if not report or not report.get("details"):
        logger.warning("consolidation: keeper returned no report for %s", target_date)
        return False

    consolidated_dir.mkdir(parents=True, exist_ok=True)
    out = consolidated_dir / f"{target_date}.md"
    out.write_text(report["details"].strip() + "\n", encoding="utf-8")
    await memsearch.index_file(out)
    logger.info("consolidation: wrote %s", out)
    return True
