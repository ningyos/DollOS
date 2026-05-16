"""One-off eval: Inner Voice (recall filter) vs raw memsearch top-K.

Throw-away script. Generates synthetic memory, runs 10 probe queries, captures
side-by-side raw memsearch hits vs IV.recall output, and emits JSON + markdown
reports to /tmp/.

Run:
    uv run python scripts/eval_inner_voice.py
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from memsearch import MemSearch

from dollos.inner_voice import InnerVoice
from dollos.llm.composed import ComposedLLMAdapter
from dollos.llm.templates import Qwen3PlainTemplate
from dollos.llm.transport import LlamaCppProvider
from dollos.prompts import PromptRenderer


MEMORY_ROOT = Path("/tmp/iv_test_memory")
SHARED_ROOT = MEMORY_ROOT / "shared"
MILVUS_PATH = Path("/tmp/iv_test_milvus.db")
RESULTS_JSON = Path("/tmp/iv_eval_results.json")
RESULTS_MD = Path("/tmp/iv_eval_report.md")

INNER_VOICE_URL = "http://127.0.0.1:8003"
INNER_VOICE_MODEL = "qwen3.5-0.8b"  # informational; provider uses base_url
TOP_K = 15


# -----------------------------------------------------------------------------
# Synthetic memory corpus
# -----------------------------------------------------------------------------

# Each entry is (date, fact). Dates are spread across ~15 days so memsearch
# sees a realistic directory layout.
FACTS: list[tuple[str, str]] = [
    # === USER PREFERENCES (30) ===
    ("2026-04-01", "User likes pour-over coffee in the morning, brewed with a V60 dripper and Ethiopian beans."),
    ("2026-04-01", "User hates sweetened drinks — no sugar, no syrup, no flavored creamers."),
    ("2026-04-01", "User prefers dark mode in all editors and terminals — light mode gives them eye strain."),
    ("2026-04-02", "User is vegetarian on weekdays but eats meat on weekends."),
    ("2026-04-02", "User likes spicy food, especially Sichuan peppercorn dishes."),
    ("2026-04-02", "User prefers tea over coffee in the afternoon — usually oolong or genmaicha."),
    ("2026-04-03", "User prefers tabs over spaces in code, and 4-space indent width when forced to use spaces."),
    ("2026-04-03", "User uses Neovim as primary editor, with telescope and treesitter."),
    ("2026-04-03", "User dislikes auto-formatters that reorder imports without asking."),
    ("2026-04-04", "User prefers Python type hints with `from __future__ import annotations`."),
    ("2026-04-04", "User likes Rust for systems work but finds the borrow checker exhausting some days."),
    ("2026-04-04", "User dislikes JavaScript ecosystem fatigue, but tolerates TypeScript."),
    ("2026-04-05", "User prefers Linux (Ubuntu) on desktop, refuses to use macOS for development."),
    ("2026-04-05", "User runs i3wm with a custom keybind layout — disabled the Windows key on purpose."),
    ("2026-04-05", "User listens to lo-fi or Vocaloid while coding, never lyrics-heavy English songs."),
    ("2026-04-06", "User prefers mechanical keyboards with tactile switches (Boba U4T)."),
    ("2026-04-06", "User uses a Logitech MX Master 3 mouse but finds the side scroll wheel useless."),
    ("2026-04-06", "User has an ultra-wide monitor (34 inch) plus a vertical secondary."),
    ("2026-04-07", "User prefers cold weather over hot — sets aircon to 24°C in summer."),
    ("2026-04-07", "User dislikes phone calls, strongly prefers async text communication."),
    ("2026-04-07", "User prefers face masks (KF94) when in crowded public transit even post-pandemic."),
    ("2026-04-08", "User does not drink alcohol, mainly because it makes them sleepy."),
    ("2026-04-08", "User likes dark chocolate (≥85%) but dislikes milk chocolate as too sweet."),
    ("2026-04-08", "User prefers reading on a Kindle Oasis over physical books or phones."),
    ("2026-04-09", "User prefers concise, direct communication — dislikes corporate-speak emails."),
    ("2026-04-09", "User likes anime in the slice-of-life and CGDCT genre."),
    ("2026-04-09", "User dislikes shounen battle anime — finds power-creep arcs boring."),
    ("2026-04-10", "User prefers writing specs before code, and dislikes ad-hoc patches without design."),
    ("2026-04-10", "User likes minimal websites with no JavaScript when possible."),
    ("2026-04-10", "User prefers cycling over driving for trips under 5km."),

    # === PAST EVENTS (35) ===
    ("2026-04-10", "On 2026-04-10 user mentioned their cat Mochi got a urinary tract infection; vet prescribed antibiotics for 10 days."),
    ("2026-04-11", "User completed a long Blender session sculpting Powdur's hair groom on 2026-04-11; took 6 hours."),
    ("2026-04-12", "User reported a CUDA OOM during a fine-tuning run on 2026-04-12, had to drop batch size from 8 to 4."),
    ("2026-04-13", "User said on 2026-04-13 they had insomnia, slept only 3 hours, drank extra coffee next morning."),
    ("2026-04-14", "User's mother visited from Kaohsiung on 2026-04-14 and stayed for the weekend."),
    ("2026-04-15", "User had dinner with old high-school friend Ken-Wei at a hotpot restaurant on 2026-04-15."),
    ("2026-04-16", "User finished reading 'Project Hail Mary' on 2026-04-16, rated it 9/10."),
    ("2026-04-17", "User bought a new SSD (Samsung 990 Pro 2TB) on 2026-04-17 for the DollOS rig."),
    ("2026-04-18", "User had a dental cleaning appointment on 2026-04-18 morning; the dentist said all clear."),
    ("2026-04-19", "User encountered a torch.compile crash on 2026-04-19 when training fish-tts."),
    ("2026-04-20", "User defined the Bridge/Drone architecture for DollOS on 2026-04-20."),
    ("2026-04-21", "On 2026-04-21 user's cat Mochi finished antibiotics course; UTI cleared up."),
    ("2026-04-22", "User completed the Blender hair groom for Powdur on 2026-04-22 (final version, hair-card export)."),
    ("2026-04-23", "User pivoted DollOS away from Android-only on 2026-04-23 — computer-first design."),
    ("2026-04-24", "On 2026-04-24 user got stuck on a llama.cpp grammar bug, debugged for 4 hours."),
    ("2026-04-25", "User attended a hardware conference in Taipei Nangang on 2026-04-25."),
    ("2026-04-26", "User skipped lunch on 2026-04-26 because they were deep in a coding flow state."),
    ("2026-04-27", "User reported headache on 2026-04-27, blamed dehydration and skipped meals."),
    ("2026-04-28", "User got a haircut on 2026-04-28; the barber gave a slightly shorter cut than asked."),
    ("2026-04-29", "On 2026-04-29 user's fish Yuki (a betta) died after 2 years — they were sad for a few days."),
    ("2026-04-30", "User picked up two parcels from the convenience store on 2026-04-30 — keyboard and SSD."),
    ("2026-05-01", "User wrote the DollOS pivot spec on 2026-05-01: computer-as-home, phone-as-remote."),
    ("2026-05-02", "User went to a ramen shop in Da'an district on 2026-05-02 for dinner."),
    ("2026-05-03", "On 2026-05-03 user did a long brainstorming session with Doll about Self-First design."),
    ("2026-05-04", "User did a system update on the rig on 2026-05-04 — Ubuntu 24.04 LTS HWE stack."),
    ("2026-05-05", "User decided on emoji gating via GBNF grammar on 2026-05-05 — default deny, tool to unlock."),
    ("2026-05-06", "User had a video call with their mother on 2026-05-06 to check in."),
    ("2026-05-07", "On 2026-05-07 user finished the memsearch pivot — Milvus Lite + bge-m3."),
    ("2026-05-08", "User pivoted the VoM wire format on 2026-05-08 from <think>-prefill to [Memory context] block."),
    ("2026-05-09", "User hit a sherpa-onnx libonnxruntime symlink bug on 2026-05-09; fixed by manual ln -s."),
    ("2026-05-10", "User's cat Mochi knocked over a glass of water on 2026-05-10 onto the desk."),
    ("2026-05-11", "User went hiking on Yangmingshan on 2026-05-11 — first outdoor day in two weeks."),
    ("2026-05-12", "On 2026-05-12 user filed a bug against fish-tts for a streaming deadlock."),
    ("2026-05-13", "User had a long dinner with Ken-Wei again on 2026-05-13 at a beef noodle shop."),
    ("2026-05-14", "User watched a Vtuber stream on 2026-05-14 — Gawr Gura archive replay."),

    # === SHARED KNOWLEDGE (35) ===
    ("2026-04-01", "DollOS runs on a dual-RTX-4060-Ti rig (16GB each), 96GB DDR5 RAM, Ryzen 9 7950X."),
    ("2026-04-01", "User's home is in Taipei, Da'an district, near Daan Forest Park."),
    ("2026-04-01", "User's mother lives in Kaohsiung and visits about once a month."),
    ("2026-04-02", "User has one cat, Mochi — a brown tabby, 4 years old, indoor only."),
    ("2026-04-02", "User used to keep a betta fish named Yuki, but Yuki died in April 2026."),
    ("2026-04-02", "User does not own a dog and is not planning to get one — apartment is too small."),
    ("2026-04-03", "User's primary big LLM is a self-hosted llama.cpp running Qwen3.6-35B-A3B."),
    ("2026-04-03", "User's small inner-voice model is Qwen3-0.8B running on port 8003."),
    ("2026-04-03", "User uses ONNX bge-m3 for memsearch embeddings."),
    ("2026-04-04", "DollOS uses Milvus Lite as the vector store, file-backed."),
    ("2026-04-04", "Doll's default character pack is gura.doll, a Gawr-Gura-styled persona."),
    ("2026-04-04", "User has a younger sister named Mei-Ling who lives in Tainan."),
    ("2026-04-05", "User's father passed away when they were 14; rarely brought up."),
    ("2026-04-05", "User is in their early 30s and works as an indie ML engineer."),
    ("2026-04-05", "User's apartment is 22 ping (~73 m²), east-facing."),
    ("2026-04-06", "User commutes mostly by U-bike + Taipei MRT; does not own a car."),
    ("2026-04-06", "User has a fully retired indoor plant collection — only fake plants now."),
    ("2026-04-06", "Taipei's monsoon rainy season usually peaks in May and early June."),
    ("2026-04-07", "DollOS's IPC server listens on port 9876 (WebSocket)."),
    ("2026-04-07", "User's local llama-server for the big model runs on port 8001."),
    ("2026-04-07", "User's local llama-server for embeddings runs on port 8002."),
    ("2026-04-08", "Mochi the cat has a favorite spot — top of the bookshelf next to the router."),
    ("2026-04-08", "Mochi the cat eats wet food twice a day and one scoop of dry kibble at night."),
    ("2026-04-08", "User uses Pi-Hole at home for ad-blocking, runs on a Raspberry Pi 4."),
    ("2026-04-09", "User's gym is a 24-hour gym 200m from the apartment but they rarely go."),
    ("2026-04-09", "User's preferred convenience store chain is 7-Eleven (closer than FamilyMart)."),
    ("2026-04-10", "User's bank is CTBC; they keep most savings in NTD."),
    ("2026-04-10", "Powdur is one of the user's original character designs — a fluffy pastel mascot."),
    ("2026-04-11", "Gura is the user's primary Doll character, modeled after Gawr Gura."),
    ("2026-04-11", "User's friend Ken-Wei works as a backend engineer at a Taipei fintech."),
    ("2026-04-12", "User's friend Mei-Ling (sister) is a clinical psychologist in Tainan."),
    ("2026-05-01", "User's bali trip was planned for late 2025 but was cancelled — no future Bali plans."),
    ("2026-05-02", "DollOS daemon is configured at character_packs/gura by default."),
    ("2026-05-03", "User keeps a wake-word ONNX model per character (e.g., gura.onnx)."),
    ("2026-05-04", "User's preferred restaurant near home is a small ramen shop called Mensho Tokyo (Da'an branch)."),
]


# -----------------------------------------------------------------------------
# Test queries
# -----------------------------------------------------------------------------

QUERIES: list[dict[str, str]] = [
    # 2x Topical
    {"category": "topical", "query": "What does the user like to drink in the morning?"},
    {"category": "topical", "query": "What editor and OS does the user use for development?"},
    # 3x Adjacent / synthesis
    {"category": "adjacent", "query": "Is the user vegetarian?"},
    {"category": "adjacent", "query": "Does the user own a dog?"},
    {"category": "adjacent", "query": "Has the user been to Bali recently?"},
    # 3x Ambiguous / multiple candidates
    {"category": "ambiguous", "query": "Tell me about the user's pets."},
    {"category": "ambiguous", "query": "What family does the user have?"},
    {"category": "ambiguous", "query": "What hardware does DollOS run on?"},
    # 2x Unrelated / should return empty
    {"category": "empty", "query": "What is the user's favorite Pokemon?"},
    {"category": "empty", "query": "Has the user ever been skydiving?"},
]


# -----------------------------------------------------------------------------
# Setup helpers
# -----------------------------------------------------------------------------

def setup_memory_dir() -> None:
    if MEMORY_ROOT.exists():
        shutil.rmtree(MEMORY_ROOT)
    SHARED_ROOT.mkdir(parents=True, exist_ok=True)
    if MILVUS_PATH.exists():
        MILVUS_PATH.unlink()

    by_date: dict[str, list[str]] = defaultdict(list)
    for d, fact in FACTS:
        by_date[d].append(fact)

    for d, facts in by_date.items():
        path = SHARED_ROOT / f"{d}.md"
        body = "\n".join(f"- {f}" for f in facts) + "\n"
        path.write_text(body, encoding="utf-8")


def build_memsearch_obj() -> MemSearch:
    return MemSearch(
        paths=[str(SHARED_ROOT)],
        embedding_provider="onnx",
        milvus_uri=str(MILVUS_PATH),
        collection="iv_eval",
    )


def build_inner_voice_obj(ms: MemSearch) -> InnerVoice:
    provider = LlamaCppProvider(base_url=INNER_VOICE_URL, timeout_s=60.0)
    llm = ComposedLLMAdapter(provider=provider, template=Qwen3PlainTemplate())
    return InnerVoice(memsearch=ms, llm=llm, renderer=PromptRenderer(), default_top_k=TOP_K)


# -----------------------------------------------------------------------------
# Metrics
# -----------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z一-鿿][A-Za-z0-9一-鿿'-]*")


def _normalize(s: str) -> str:
    return s.lower()


def _keywords(s: str, min_len: int = 4) -> set[str]:
    """Pull non-trivial content words for additive-chars heuristic."""
    return {w.lower() for w in _WORD_RE.findall(s) if len(w) >= min_len}


def compute_metrics(
    iv_output: str, raw_hits: list[dict[str, Any]]
) -> dict[str, Any]:
    iv_norm = _normalize(iv_output)
    contents = [h.get("content", "") for h in raw_hits]
    total_raw_len = sum(len(c) for c in contents)

    # kept: a hit is "kept" if a substantial chunk of its words appear in IV output.
    # Use a sliding 6-word prefix match OR ≥60% keyword overlap.
    kept_flags: list[bool] = []
    for c in contents:
        c_kw = _keywords(c)
        if not c_kw:
            kept_flags.append(False)
            continue
        iv_kw = _keywords(iv_output)
        overlap = len(c_kw & iv_kw) / len(c_kw)
        # Or check for any 6-consecutive-word phrase match
        words = [w.lower() for w in _WORD_RE.findall(c)]
        phrase_hit = False
        if len(words) >= 6:
            for i in range(len(words) - 5):
                phrase = " ".join(words[i : i + 6])
                if phrase in iv_norm:
                    phrase_hit = True
                    break
        kept_flags.append(overlap >= 0.5 or phrase_hit)

    kept_count = sum(kept_flags)
    dropped_count = len(contents) - kept_count

    # added_chars: keywords in IV output not present in any raw hit content
    raw_kw_union: set[str] = set()
    for c in contents:
        raw_kw_union |= _keywords(c)
    iv_kw = _keywords(iv_output)
    added_kw = iv_kw - raw_kw_union - _keywords("user")
    # estimate added chars by summing their lengths
    added_chars = sum(len(k) for k in added_kw)

    compression_ratio = (len(iv_output) / total_raw_len) if total_raw_len else 0.0
    empty_response = iv_output.strip() == ""

    diverged = (kept_count <= 8) or (added_chars > 100)

    return {
        "kept_count": kept_count,
        "dropped_count": dropped_count,
        "added_chars": added_chars,
        "added_keywords_sample": sorted(added_kw)[:20],
        "compression_ratio": round(compression_ratio, 3),
        "empty_response": empty_response,
        "diverged": diverged,
    }


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

async def run_one(
    iv: InnerVoice, ms: MemSearch, q: dict[str, str]
) -> dict[str, Any]:
    query = q["query"]
    raw_hits = await ms.search(query, top_k=TOP_K)
    t0 = time.perf_counter()
    iv_output = await iv.recall(query, top_k=TOP_K)
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    metrics = compute_metrics(iv_output, raw_hits)
    return {
        "category": q["category"],
        "query": query,
        "raw_hits": [
            {
                "rank": i + 1,
                "score": round(float(h.get("score", 0.0)), 4),
                "content": h.get("content", ""),
                "source": h.get("source", ""),
            }
            for i, h in enumerate(raw_hits)
        ],
        "iv_output": iv_output,
        "latency_ms": latency_ms,
        "metrics": metrics,
    }


def render_markdown(results: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append("# Inner Voice .recall() vs Raw memsearch top-K — Eval Report")
    lines.append("")
    lines.append(f"- Inner Voice server: `{INNER_VOICE_URL}` (model: `{INNER_VOICE_MODEL}`)")
    lines.append(f"- top_k = {TOP_K}")
    lines.append(f"- corpus: {len(FACTS)} synthetic facts across {len({d for d,_ in FACTS})} daily files")
    lines.append(f"- {len(results)} probe queries")
    lines.append("")

    # Aggregate
    n = len(results)
    avg_kept = sum(r["metrics"]["kept_count"] for r in results) / n
    avg_dropped = sum(r["metrics"]["dropped_count"] for r in results) / n
    avg_added_chars = sum(r["metrics"]["added_chars"] for r in results) / n
    avg_latency = sum(r["latency_ms"] for r in results) / n
    pct_diverged = 100.0 * sum(1 for r in results if r["metrics"]["diverged"]) / n
    pct_empty = 100.0 * sum(1 for r in results if r["metrics"]["empty_response"]) / n
    avg_compression = sum(r["metrics"]["compression_ratio"] for r in results) / n

    lines.append("## Aggregate")
    lines.append("")
    lines.append(f"- avg kept_count (of {TOP_K}): **{avg_kept:.2f}**")
    lines.append(f"- avg dropped_count: **{avg_dropped:.2f}**")
    lines.append(f"- avg added_chars (paraphrasing heuristic): **{avg_added_chars:.1f}**")
    lines.append(f"- avg compression_ratio (IV_len / sum(raw_lens)): **{avg_compression:.3f}**")
    lines.append(f"- avg latency: **{avg_latency:.1f} ms**")
    lines.append(f"- % queries materially diverged (kept ≤ 8 or added_chars > 100): **{pct_diverged:.0f}%**")
    lines.append(f"- % empty IV responses: **{pct_empty:.0f}%**")
    lines.append("")

    # Per-query summary table
    lines.append("## Per-query summary")
    lines.append("")
    lines.append("| # | Category | Query | kept/dropped | added_chars | latency (ms) | empty? | diverged? |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(results, 1):
        m = r["metrics"]
        lines.append(
            f"| {i} | {r['category']} | {r['query']} | {m['kept_count']}/{m['dropped_count']} | {m['added_chars']} | {r['latency_ms']} | {'Y' if m['empty_response'] else 'N'} | {'Y' if m['diverged'] else 'N'} |"
        )
    lines.append("")

    # Per-query detail (top-3 raw + IV)
    lines.append("## Per-query detail")
    lines.append("")
    for i, r in enumerate(results, 1):
        lines.append(f"### Q{i} [{r['category']}] {r['query']}")
        lines.append("")
        lines.append("**Raw memsearch top-3:**")
        lines.append("")
        for h in r["raw_hits"][:3]:
            lines.append(f"  {h['rank']}. (score={h['score']}) {h['content']}")
        lines.append("")
        lines.append("**IV.recall output:**")
        lines.append("")
        if r["iv_output"].strip():
            for ln in r["iv_output"].splitlines():
                lines.append(f"  {ln}")
        else:
            lines.append("  *(empty)*")
        lines.append("")
        lines.append(f"_metrics_: kept={r['metrics']['kept_count']}/{TOP_K}, added_chars={r['metrics']['added_chars']}, latency={r['latency_ms']}ms")
        lines.append("")

    # Diverged spotlight
    diverged = [r for r in results if r["metrics"]["diverged"]]
    if diverged:
        lines.append("## Diverged queries — full diff")
        lines.append("")
        for r in diverged[:3]:
            lines.append(f"### {r['query']}")
            lines.append("")
            lines.append("**All 15 raw hits:**")
            lines.append("")
            for h in r["raw_hits"]:
                lines.append(f"  {h['rank']}. (score={h['score']}) {h['content']}")
            lines.append("")
            lines.append("**IV.recall output:**")
            lines.append("")
            if r["iv_output"].strip():
                for ln in r["iv_output"].splitlines():
                    lines.append(f"  {ln}")
            else:
                lines.append("  *(empty)*")
            lines.append("")

    return "\n".join(lines)


async def main() -> None:
    print("[setup] building synthetic memory at", MEMORY_ROOT)
    setup_memory_dir()

    print("[setup] constructing memsearch")
    ms = build_memsearch_obj()
    print("[setup] indexing...")
    n_indexed = await ms.index(force=True)
    print(f"[setup] indexed {n_indexed} chunks")

    iv = build_inner_voice_obj(ms)

    results: list[dict[str, Any]] = []
    for i, q in enumerate(QUERIES, 1):
        print(f"[run] Q{i}/{len(QUERIES)} [{q['category']}] {q['query']}")
        try:
            r = await run_one(iv, ms, q)
        except Exception as e:
            print(f"  ERROR: {e!r}")
            r = {
                "category": q["category"],
                "query": q["query"],
                "raw_hits": [],
                "iv_output": "",
                "latency_ms": 0.0,
                "metrics": {
                    "kept_count": 0, "dropped_count": 0, "added_chars": 0,
                    "added_keywords_sample": [], "compression_ratio": 0.0,
                    "empty_response": True, "diverged": True,
                },
                "error": repr(e),
            }
        print(
            f"  -> kept={r['metrics']['kept_count']}/{TOP_K} "
            f"added_chars={r['metrics']['added_chars']} "
            f"latency={r['latency_ms']}ms "
            f"empty={r['metrics']['empty_response']}"
        )
        results.append(r)

    RESULTS_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    md = render_markdown(results)
    RESULTS_MD.write_text(md, encoding="utf-8")

    print()
    print("=" * 80)
    print(md)
    print("=" * 80)
    print(f"[done] results: {RESULTS_JSON}")
    print(f"[done] report:  {RESULTS_MD}")


if __name__ == "__main__":
    asyncio.run(main())
