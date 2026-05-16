"""Layer 3 A/B eval: Doll responses with IV-filtered memory vs raw memsearch top-K.

Reuses Layer 2 corpus at /tmp/iv_test_memory + /tmp/iv_test_milvus.db. Same 10
queries from `scripts/eval_inner_voice.py`. For each query, run Doll (big LLM)
twice — once with IV.recall(query) output as `[Memory context]`, once with raw
memsearch top-15 bullet list. Capture both responses, save JSON + markdown,
print to stdout.

Run:
    uv run python scripts/eval_inner_voice_ab.py
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from memsearch import MemSearch

from dollos.character import DollPack
from dollos.inner_voice import InnerVoice
from dollos.llm.composed import ComposedLLMAdapter
from dollos.llm.templates import Qwen3PlainTemplate, Qwen3ThinkingTemplate
from dollos.llm.transport import LlamaCppProvider
from dollos.prompts import PromptRenderer

import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parent))

# Reuse Layer 2 query list + corpus paths
from eval_inner_voice import (  # noqa: E402
    MEMORY_ROOT,
    MILVUS_PATH,
    QUERIES,
    SHARED_ROOT,
    setup_memory_dir,
)


INNER_VOICE_URL = "http://127.0.0.1:8003"
BIG_LLM_URL = "http://127.0.0.1:8001"
BIG_LLM_TIMEOUT_S = 120.0
TOP_K = 15

GURA_PACK = Path(__file__).resolve().parent.parent / "character_packs" / "gura"

RESULTS_JSON = Path("/tmp/iv_ab_results.json")
RESULTS_MD = Path("/tmp/iv_ab_report.md")

NOW_STR = "2026-05-15 14:30:00"

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def strip_think(text: str) -> str:
    """Remove <think>...</think> blocks. Stream prompt starts inside <think>
    (Qwen3ThinkingTemplate opens it), so output usually starts with content
    then `</think>`. Handle both shapes.
    """
    # If output begins inside think (no opening <think>) but has closing tag:
    if "<think>" not in text and "</think>" in text:
        # Drop everything up to and including the first </think>
        idx = text.find("</think>")
        rest = text[idx + len("</think>") :]
        return rest.lstrip()
    # Otherwise strip any properly-paired blocks
    return _THINK_RE.sub("", text).lstrip()


def build_user_message(mem_block: str, query: str) -> str:
    return (
        f"{mem_block}"
        f"[Now]\n"
        f"{NOW_STR}\n\n"
        f"[Active monitors]\n"
        f"(none)\n\n"
        f"[Pending events]\n"
        f"(none)\n\n"
        f"[Message]\n"
        f"{query}"
    )


def format_raw_bullets(hits: list[dict[str, Any]]) -> str:
    """Format raw memsearch hits as a bullet list of content lines."""
    if not hits:
        return "(no relevant memory)"
    lines: list[str] = []
    for h in hits:
        content = h.get("content", "").strip()
        if content:
            lines.append(f"- {content}")
    return "\n".join(lines) if lines else "(no relevant memory)"


async def doll_complete(
    adapter: ComposedLLMAdapter, system: str, user: str
) -> tuple[str, float]:
    """Stream a full response from the big LLM. Returns (text, latency_s)."""
    t0 = time.perf_counter()
    chunks: list[str] = []
    async for chunk in adapter.stream_completion(
        system=system, user=user, prefill="", max_tokens=2048
    ):
        if chunk.text:
            chunks.append(chunk.text)
        if chunk.done:
            break
    latency = time.perf_counter() - t0
    return "".join(chunks), latency


def classify_diff(resp_a: str, resp_b: str) -> str:
    a = resp_a.strip()
    b = resp_b.strip()
    if not a and not b:
        return "both empty"
    if not a:
        return "A empty, B non-empty"
    if not b:
        return "B empty, A non-empty"
    if a == b:
        return "identical"
    # Crude char-level similarity
    short, long = sorted([a, b], key=len)
    ratio = len(short) / max(len(long), 1)
    len_diff = abs(len(a) - len(b))
    if ratio > 0.85 and len_diff < 30:
        return "near-identical wording"
    if abs(len(a) - len(b)) > 200:
        longer = "A" if len(a) > len(b) else "B"
        return f"materially different — {longer} much longer"
    return "materially different"


async def run_one(
    iv: InnerVoice,
    ms: MemSearch,
    adapter: ComposedLLMAdapter,
    system: str,
    q: dict[str, str],
) -> dict[str, Any]:
    query = q["query"]

    # Run A: IV-filtered
    t_iv0 = time.perf_counter()
    iv_text = await iv.recall(query, top_k=TOP_K)
    iv_latency = time.perf_counter() - t_iv0
    if iv_text.strip():
        mem_block_a_body = iv_text
    else:
        mem_block_a_body = "(no relevant memory)"
    mem_block_a = f"[Memory context]\n{mem_block_a_body}\n\n"
    user_a = build_user_message(mem_block_a, query)
    raw_a, lat_a = await doll_complete(adapter, system, user_a)
    resp_a = strip_think(raw_a)

    # Run B: raw top-K passthrough
    hits = await ms.search(query, top_k=TOP_K)
    bullets = format_raw_bullets(hits)
    mem_block_b = f"[Memory context]\n{bullets}\n\n"
    user_b = build_user_message(mem_block_b, query)
    raw_b, lat_b = await doll_complete(adapter, system, user_b)
    resp_b = strip_think(raw_b)

    return {
        "category": q["category"],
        "query": query,
        "mem_block_A": mem_block_a_body,
        "mem_block_B": bullets,
        "mem_len_A_chars": len(mem_block_a_body),
        "mem_len_B_chars": len(bullets),
        "response_A_raw": raw_a,
        "response_B_raw": raw_b,
        "response_A": resp_a,
        "response_B": resp_b,
        "iv_latency_s": round(iv_latency, 2),
        "latency_A_s": round(lat_a, 2),
        "latency_B_s": round(lat_b, 2),
        "diff_signal": classify_diff(resp_a, resp_b),
    }


def render_markdown(results: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append("# Layer 3 A/B Eval — Doll responses: IV-filtered vs raw memsearch top-K")
    lines.append("")
    lines.append(f"- Inner Voice: `{INNER_VOICE_URL}` (small model)")
    lines.append(f"- Big LLM (Doll): `{BIG_LLM_URL}` (Qwen3.6 / unsloth)")
    lines.append(f"- top_k = {TOP_K}")
    lines.append(f"- Character pack: `character_packs/gura/`")
    lines.append(f"- Synthetic `[Now]` = `{NOW_STR}`")
    lines.append(f"- {len(results)} queries")
    lines.append("")

    n = len(results)
    avg_iv = sum(r["iv_latency_s"] for r in results) / n
    avg_a = sum(r["latency_A_s"] for r in results) / n
    avg_b = sum(r["latency_B_s"] for r in results) / n
    avg_total_a = avg_iv + avg_a

    lines.append("## Aggregate latency")
    lines.append("")
    lines.append(f"- avg IV.recall latency: **{avg_iv:.2f} s**")
    lines.append(f"- avg Doll latency (A, with IV memory block): **{avg_a:.2f} s**")
    lines.append(f"- avg Doll latency (B, raw bullets memory block): **{avg_b:.2f} s**")
    lines.append(f"- avg total pipeline (IV + Doll A): **{avg_total_a:.2f} s**")
    lines.append(f"- avg total pipeline (B): **{avg_b:.2f} s**")
    lines.append("")

    # Per-query summary
    lines.append("## Per-query summary")
    lines.append("")
    lines.append("| # | Category | Query | mem_len A/B | latency A/B (s) | diff |")
    lines.append("|---|---|---|---|---|---|")
    for i, r in enumerate(results, 1):
        lines.append(
            f"| {i} | {r['category']} | {r['query']} | "
            f"{r['mem_len_A_chars']}/{r['mem_len_B_chars']} | "
            f"{r['latency_A_s']}/{r['latency_B_s']} | {r['diff_signal']} |"
        )
    lines.append("")

    # Per-query detail
    lines.append("## Per-query detail")
    lines.append("")
    for i, r in enumerate(results, 1):
        lines.append(f"### Q{i}: {r['query']}")
        lines.append("")
        lines.append(f"**Category**: {r['category']} — **Diff signal**: {r['diff_signal']}")
        lines.append("")
        lines.append(f"**Memory context (IV-filtered, {r['mem_len_A_chars']} chars)**:")
        lines.append("")
        lines.append("```")
        lines.append(r["mem_block_A"])
        lines.append("```")
        lines.append("")
        lines.append(f"**Memory context (raw top-{TOP_K}, {r['mem_len_B_chars']} chars)**:")
        lines.append("")
        lines.append("```")
        lines.append(r["mem_block_B"])
        lines.append("```")
        lines.append("")
        lines.append(f"**Doll response (with IV) — {r['latency_A_s']}s**:")
        lines.append("")
        lines.append("```")
        lines.append(r["response_A"] or "(empty)")
        lines.append("```")
        lines.append("")
        lines.append(f"**Doll response (raw) — {r['latency_B_s']}s**:")
        lines.append("")
        lines.append("```")
        lines.append(r["response_B"] or "(empty)")
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def ensure_corpus() -> None:
    """Regenerate Layer 2 corpus if missing."""
    if SHARED_ROOT.exists() and MILVUS_PATH.exists():
        if any(SHARED_ROOT.glob("*.md")):
            print(f"[setup] reusing existing corpus at {MEMORY_ROOT}")
            return
    print(f"[setup] corpus missing/incomplete, regenerating at {MEMORY_ROOT}")
    setup_memory_dir()


async def main() -> None:
    ensure_corpus()

    print("[setup] constructing memsearch")
    ms = MemSearch(
        paths=[str(SHARED_ROOT)],
        embedding_provider="onnx",
        milvus_uri=str(MILVUS_PATH),
        collection="iv_eval",
    )
    print("[setup] indexing memsearch...")
    n = await ms.index()
    print(f"[setup] indexed {n} chunks")

    # Inner Voice (small model)
    iv_provider = LlamaCppProvider(base_url=INNER_VOICE_URL, timeout_s=60.0)
    iv_llm = ComposedLLMAdapter(provider=iv_provider, template=Qwen3PlainTemplate())
    iv = InnerVoice(
        memsearch=ms, llm=iv_llm, renderer=PromptRenderer(), default_top_k=TOP_K
    )

    # Big LLM (Doll)
    big_provider = LlamaCppProvider(
        base_url=BIG_LLM_URL, timeout_s=BIG_LLM_TIMEOUT_S
    )
    big_adapter = ComposedLLMAdapter(
        provider=big_provider, template=Qwen3ThinkingTemplate()
    )

    # System prompt: scaffolding + Gura identity, no skills, no tools list
    # (we intentionally skip tool grammar — capturing raw response shape).
    pack = DollPack.load(GURA_PACK)
    renderer = PromptRenderer()
    system = renderer.render(
        "scaffolding",
        identity=pack.identity,
        available_skills=[],
    )

    print(f"[setup] system prompt: {len(system)} chars")
    print(f"[setup] {len(QUERIES)} queries; expecting 2x Doll calls each")
    print()

    wall_t0 = time.perf_counter()
    results: list[dict[str, Any]] = []
    for i, q in enumerate(QUERIES, 1):
        print(f"[run] Q{i}/{len(QUERIES)} [{q['category']}] {q['query']}")
        try:
            r = await run_one(iv, ms, big_adapter, system, q)
        except Exception as e:
            print(f"  ERROR: {e!r}")
            r = {
                "category": q["category"],
                "query": q["query"],
                "mem_block_A": "",
                "mem_block_B": "",
                "mem_len_A_chars": 0,
                "mem_len_B_chars": 0,
                "response_A_raw": "",
                "response_B_raw": "",
                "response_A": "",
                "response_B": "",
                "iv_latency_s": 0.0,
                "latency_A_s": 0.0,
                "latency_B_s": 0.0,
                "diff_signal": "error",
                "error": repr(e),
            }
        print(
            f"  -> iv={r['iv_latency_s']}s A={r['latency_A_s']}s B={r['latency_B_s']}s "
            f"diff={r['diff_signal']}"
        )
        results.append(r)
    wall_total = time.perf_counter() - wall_t0
    print(f"\n[done] wall time: {wall_total:.1f}s")

    RESULTS_JSON.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md = render_markdown(results)
    md += f"\n\n_Wall time: {wall_total:.1f}s_\n"
    RESULTS_MD.write_text(md, encoding="utf-8")

    print()
    print("=" * 80)
    print(md)
    print("=" * 80)
    print(f"[done] results: {RESULTS_JSON}")
    print(f"[done] report:  {RESULTS_MD}")


if __name__ == "__main__":
    asyncio.run(main())
