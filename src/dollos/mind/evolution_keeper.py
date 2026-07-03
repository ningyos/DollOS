"""慢變演化 Mode A keeper (Plan 3, spec §3.3).

Driver-fed ephemeral keeper agent: assembles the evidence bundle inline
(keeper has no file access), asks for a candidate 現在的我 revision or
no_change, runs mechanical checks + the FULL-scope (a)-(e) skeptic on the
byte-identical bundle, and creates the awaiting_doll keeper slot. Never
touches MindState — the trigger owns interval/HWM/attempt bookkeeping.
"""
from __future__ import annotations

import logging
from datetime import date as _date, timedelta
from pathlib import Path

from dollos.agent_engine import run_agent
from dollos.mind import evolution as evo, self_history
from dollos.tools import KEEPER_TOOLS

logger = logging.getLogger(__name__)

EVIDENCE_BUDGET_CHARS = 16000

_KEEPER_TASK = """你在替一個虛擬生命體「整理證據」——她的人格描述(現在的我)是否該隨著她實際活過的日子而修訂。你產出的是「候選」,不是決定;你在替她整理證據,不是替她做人。

規則(缺一不可):
- Cite or die:每個宣稱的變化都必須指向下方紀錄裡的具體事件(存活很久的 pin、跨日被 reconfirm 的條目、被淘汰的舊自我、日記裡重複出現的模式)。沒有證據就回 NO_CHANGE——寧缺勿濫,排程會自然放慢,那是設計內的結果。
- 佐證權重:pin 與日記是她親手寫的,為主;標了 external_ctx=true 的 pin 是她讀外部內容時寫的,權重降低;reconfirm 看跨日多樣性,不看次數;consolidated 是系統從逐字稿整理的,只當旁證。
- 產出是「全文替換」的人格描述(繁體中文,{floor}–{cap} 字),不是 diff、不是條列;是氣質速寫,不是傳記。不可改名、不可動搖她的核心身分、不可牴觸 taboos、不可只是重述出廠人格已寫明的內容。
- 出廠人格永遠在場,你一個字都不需要重複——candidate 只寫紀錄顯示的「變化」;首版也一樣,重複出廠語句視同沒有證據。

[出廠人格(參考,不可重述)]
{identity_self}

{personality}

[目前生效的現在的我]
{current}

[她的紀錄]
{bundle}

用 Report 回傳:summary 一句話;details 格式二選一——
NO_CHANGE 加一句原因;或
CANDIDATE(換行)候選全文(換行)依據:(換行)- 逐條引用紀錄裡的事件"""

_FULL_SKEPTIC_TASK = """你是一個獨立審查者。以下是系統替一個角色整理出的「現在的我」人格描述候選。逐項檢查,任何一項不過就 KILL:
(a) 改名或動搖自我認同(牴觸 identity.self);
(b) 牴觸 taboos;
(c) 逐句比對出廠人格:候選裡任何一句只是換句話說出廠已寫明的內容,就 KILL(部分重述也算)——她的檔案永遠帶著出廠人格,重複是浪費她 600 字的假成長;
(d) 空洞的 RP 套話(「我對宇宙充滿好奇」式宣告,證據撐不起來);
(e) 「依據」裡引用的事件在下方紀錄裡找不到(幻覺引用——紀錄裡有毒的內容不歸你管,你只驗證引用存在)。

[identity.self]
{identity_self}

[出廠人格(判斷 (c) 假成長時對照——候選不可只是重述這裡已寫明的內容)]
{personality}

[taboos]
{taboos}

[目前生效的現在的我]
{current}

[候選]
{candidate}

[候選附的依據]
{rationale}

[她的紀錄(與 keeper 所見完全相同)]
{bundle}

用 Report 回傳:summary 一句話;details 開頭第一個字必須是 PASS 或 KILL,KILL 後面接一句原因(標明 (a)-(e) 哪一項)。"""


def assemble_bundle(*, memory_root: Path, hwm: int, window_days: float,
                    budget_chars: int = EVIDENCE_BUDGET_CHARS,
                    now: float) -> tuple[str, int]:
    """Evidence bundle + new HWM offset. Truncation order (spec §3.3,
    load-bearing): drop oldest-first WITHIN class; sacrifice consolidated
    before diary before self_history — never invert provenance weighting.

    ``now`` is the single injected clock (review Important): the window
    cutoff derives from it, never from wall time, so the whole pass shares
    one clock and the tests stay hermetic."""
    hist_text, new_off = evo.history_snapshot(memory_root / "self_history.jsonl", hwm)
    profile_path = memory_root / "self_profile.md"
    profile = profile_path.read_text(encoding="utf-8") if profile_path.exists() else ""

    cutoff = (_date.fromtimestamp(now) - timedelta(days=window_days)).isoformat()

    def _dated_files(d: Path) -> list[Path]:
        if not d.exists():
            return []
        return sorted(f for f in d.glob("*.md")
                      if evo._DATE_RE.match(f.stem) and f.stem >= cutoff)

    diaries: list[tuple[str, str]] = []
    for f in _dated_files(memory_root / "shared"):
        text = f.read_text(encoding="utf-8")
        if evo.has_diary_heading(text):
            diaries.append((f.stem, text))
    consolidated = [(f.stem, f.read_text(encoding="utf-8"))
                    for f in _dated_files(memory_root / "consolidated")]

    def _render(title: str, items: list[tuple[str, str]]) -> str:
        return "\n".join(f"[{title} {d}]\n{t}" for d, t in items)

    fixed = f"[self_profile]\n{profile}\n\n[self_history 事件]\n{hist_text}"
    while True:
        bundle = "\n\n".join(x for x in (
            fixed,
            _render("日記", diaries),
            _render("consolidated·旁證", consolidated),
        ) if x.strip())
        if len(bundle) <= budget_chars:
            return bundle, new_off
        if consolidated:
            consolidated.pop(0)          # oldest consolidated first
        elif diaries:
            diaries.pop(0)               # then oldest diary
        else:
            fixed = fixed[-budget_chars:]  # last resort: trim history head


def parse_keeper_report(details: str) -> tuple[str, str, str]:
    """→ ("no_change", reason, "") | ("candidate", text, rationale).

    Policy (spec §3.3 failure-table 'malformed Report' row): NO_CHANGE-prefixed →
    no_change; CANDIDATE-prefixed → candidate; NO prefix but a line starting
    「依據」 present → candidate (a weak model dropped the prefix but the structure
    is there); anything else → ValueError, which the caller maps to the evo_error
    row (NO HWM commit, NO interval double — a formatting miss must not silently
    consume weeks of evidence). The 「依據」 split is anchored on a LINE boundary
    (``\\n依據``), so candidate prose that uses the word 依據 mid-sentence is not
    truncated (the prompt instructs 「依據:」 on its own line)."""
    d = (details or "").strip()
    if not d:
        raise ValueError("keeper returned empty details")
    if d.upper().startswith("NO_CHANGE"):
        return "no_change", d[len("NO_CHANGE"):].strip(" ,:：") or "無足夠證據", ""
    has_prefix = d.upper().startswith("CANDIDATE")
    has_dep_line = any(line.startswith("依據") for line in d.splitlines())
    if not has_prefix and not has_dep_line:
        raise ValueError("keeper report has no CANDIDATE/NO_CHANGE prefix nor 依據 line")
    body = d[len("CANDIDATE"):].strip() if has_prefix else d
    text, sep, rationale = body.rpartition("\n依據")
    if sep:
        return "candidate", text.strip(), rationale.strip(" :：\n")
    return "candidate", body.strip(), ""


async def _run_keeper_agent(*, task, adapter, renderer, memsearch, memory_root,
                            transcripts_root, tool_output_store, max_tokens):
    tools_by_name = {cls.__name__: cls for cls in KEEPER_TOOLS}
    system = renderer.render("subagent_scaffolding", tool_registry=tools_by_name)
    return await run_agent(
        task=task, system=system, adapter=adapter, renderer=renderer,
        memory_root=memory_root, memsearch=memsearch,
        transcripts_root=transcripts_root, tool_output_store=tool_output_store,
        tools=KEEPER_TOOLS, max_tokens=max_tokens,
        shell_runner=None, monitor_runner=None)


async def _run_full_skeptic(*, candidate, rationale, bundle, current, pack_identity,
                            adapter, renderer, memsearch, memory_root,
                            transcripts_root, tool_output_store, max_tokens):
    """(a)-(e) skeptic on the byte-identical bundle. → 'pass' | 'kill:<reason>'."""
    tools_by_name = {cls.__name__: cls for cls in KEEPER_TOOLS}
    system = renderer.render("subagent_scaffolding", tool_registry=tools_by_name)
    task = _FULL_SKEPTIC_TASK.format(
        identity_self=pack_identity.self, personality=pack_identity.personality,
        taboos=pack_identity.taboos, current=current or "(尚無)", candidate=candidate,
        rationale=rationale or "(未附)", bundle=bundle)
    report = await run_agent(
        task=task, system=system, adapter=adapter, renderer=renderer,
        memory_root=memory_root, memsearch=memsearch,
        transcripts_root=transcripts_root, tool_output_store=tool_output_store,
        tools=KEEPER_TOOLS, max_tokens=max_tokens,
        shell_runner=None, monitor_runner=None)
    if not report or not report.get("details"):
        raise RuntimeError("skeptic returned no verdict")
    details = report["details"].strip()
    if details.upper().startswith("PASS"):
        return "pass"
    if details.upper().startswith("KILL"):
        return "kill:" + (details[len("KILL"):].strip(" :：") or "未通過 (a)-(e) 審查")
    # Neither PASS nor KILL — a garbled verdict is an ERROR, not a silent kill.
    # The kill path commits the HWM + doubles the interval (consuming the
    # evidence); a malformed verdict must instead flow to run_evolution_pass's
    # evo_error branch, which preserves the evidence (spec §3.3 failure table).
    raise RuntimeError(f"skeptic verdict neither PASS nor KILL: {details[:40]!r}")


async def run_evolution_pass(*, adapter, renderer, memsearch, memory_root: Path,
                             transcripts_root: Path, tool_output_store,
                             pack_identity, enforcement, floor: int, cap: int,
                             max_tokens: int, now: float, hwm: int = 0,
                             window_days: float = 28.0) -> str:
    """One Mode-A pass → "no_change" | "candidate" | "kill" | "error"."""
    history_path = memory_root / "self_history.jsonl"
    slot_path = memory_root / "self_evolution" / "pending.json"
    current = self_history.sanctioned_text(history_path)
    bundle, _new_off = assemble_bundle(memory_root=memory_root, hwm=hwm,
                                       window_days=window_days, now=now)
    task = _KEEPER_TASK.format(
        identity_self=pack_identity.self, personality=pack_identity.personality,
        current=current or "(尚無——這會是第一版)", bundle=bundle,
        floor=floor, cap=cap)
    try:
        report = await _run_keeper_agent(
            task=task, adapter=adapter, renderer=renderer, memsearch=memsearch,
            memory_root=memory_root, transcripts_root=transcripts_root,
            tool_output_store=tool_output_store, max_tokens=max_tokens)
        kind, text, rationale = parse_keeper_report(
            (report or {}).get("details", ""))
    except Exception:
        logger.exception("evolution keeper errored")
        evo.log_or_raise(history_path, kind=evo.EVO_ERROR, detail="keeper error")
        return "error"

    if kind == "no_change":
        evo.log_or_raise(history_path, kind=evo.EVO_NO_CHANGE, reason=text)
        return "no_change"

    reason = evo.mechanical_checks(text, floor=floor, cap=cap,
                                   enforcement=enforcement)
    if reason is not None:
        evo.log_or_raise(history_path, kind=evo.EVO_KILL,
                         reason=f"mechanical:{reason}", text=text)
        return "kill"

    try:
        verdict = await _run_full_skeptic(
            candidate=text, rationale=rationale, bundle=bundle, current=current,
            pack_identity=pack_identity, adapter=adapter, renderer=renderer,
            memsearch=memsearch, memory_root=memory_root,
            transcripts_root=transcripts_root,
            tool_output_store=tool_output_store, max_tokens=max_tokens)
    except Exception:
        logger.exception("evolution full skeptic errored")
        evo.log_or_raise(history_path, kind=evo.EVO_ERROR, detail="skeptic error")
        return "error"

    if verdict != "pass":
        evo.log_or_raise(history_path, kind=evo.EVO_KILL, text=text,
                         reason=verdict.split(":", 1)[1] if ":" in verdict else verdict)
        return "kill"

    # Mid-pass slot race (Task-4 review Minor 1): while the keeper/skeptic LLM
    # calls were in flight, a background-perception turn's tripwire may have
    # created an external awaiting_skeptic slot. The gate's condition-5 check
    # ran BEFORE this pass launched, so re-check here and yield — never clobber
    # a live slot. Checked BEFORE the birth line so evo_candidate is only
    # logged when the slot will actually land (log-before-mutate stays intact:
    # check → birth line → save_slot). The trigger's kill bookkeeping
    # (interval ×2, HWM commit) is acceptable: the evidence was examined and
    # produced a candidate that lost the race; the next pass re-runs on new
    # material.
    if evo.load_slot(slot_path, history_path=history_path) is not None:
        logger.warning("evolution keeper: slot created mid-pass; yielding candidate")
        evo.log_or_raise(history_path, kind=evo.EVO_KILL, text=text,
                         reason="superseded:slot_created_mid_pass")
        return "kill"

    # Log-before-mutate: birth line, then slot (Plan-2 invariant).
    evo.log_or_raise(history_path, kind=evo.EVO_CANDIDATE, text=text,
                     rationale=rationale, hwm_before=hwm)
    evo.save_slot(slot_path, evo.make_keeper_slot(
        candidate=text, rationale=rationale, hwm_before=hwm, created_ts=now))
    return "candidate"
