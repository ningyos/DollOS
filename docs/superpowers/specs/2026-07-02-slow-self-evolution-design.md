# 慢變演化 (Slow Self-Evolution) — Design

Status: v4, review-converged (goal-driven, 2026-07-02). Review trail: R1 panel 4C/16I; R2 fresh-eyes panel 8C/16I (converged on eight root causes — the §3.4 pending-slot lifecycle is the load-bearing rewrite); R3 panel could not run (subagent session limits) and was replaced by an R3′ inline hostile pass over R3's attack list — no new Critical, 12 targeted clarifications applied in this version. A fresh-eyes R3 panel may be re-run when limits reset if desired. Second landing step of the virtual-being positioning (`2026-07-01-virtual-being-positioning.md` §2.2 / §4「慢變演化 mechanism」). Builds directly on A1 self-profile (`2026-06-30-self-profile-design.md`) and the PinSelf guidance refactor (`2026-07-02-self-centered-pinself-guidance-design.md`), whose write/prune loop was explicitly called "the seed of the growth mechanism".

Goal (user-set): 讓 Doll 長出一段她自己擁有、系統以週為尺度幫她演化、她批准後才生效的「現在的我」人格 prose — pack 永遠是出廠狀態,核心身分永遠凍結,成長與 drift 用「批准」分界。

## 1. Problem

Personality today has no way to *move*:

- **Pack is frozen.** `identity.self / personality / taboos` are load-once TOML prose with no write path. The positioning spec requires "'growth' must mean actual change over time, not just accumulation… A being that grows should be able to have her personality prose itself shift over a long enough horizon — not just gain footnotes."
- **self_profile is accumulation.** Bullets append/replace/remove under a cap — granular facts about the self, never a synthesized shift of temperament. And `op=remove` deletes forever: "what was tried and selected out" — exactly the observational data this design needs — is being lost (deferred tombstones, PinSelf guidance spec §5).
- **Growth vs drift is unresolved.** P8 persona-hardening treats *all* deviation from baseline as regression to flag; pack changes are user-approval territory; baselines never re-baseline. There is no concept of *sanctioned* change.
- **No weeks-scale machinery.** Every existing temporal process (energy sawtooth, hourly consolidation cooldown, 30-iter reflection) operates on minutes-to-hours. Personality change lives on weeks-to-months, where nothing runs.

## 2. Design rationale

**Rejected framing #1 — evolve the pack itself.** Rejected: the pack is the *restoration point* — `project_companion_definition` establishes that being-ness = the ability to keep evolving *after faithful restoration*, so the factory state must stay intact to restore from. It also puts an LLM-written artifact inside a hand-authored, user-owned file.

**Rejected framing #2 — grow a "personality" section inside self_profile.** Rejected: wrong shape and wrong tempo. self_profile is granular dated bullets pruned turn-by-turn (fast loop, variation/selection); a temperament is synthesized *prose* that should change rarely and deliberately (slow loop, selection *of* the fast loop's survivors). A1 §1.1 already rejects competing always-inject identity mechanisms — this design *extends the existing identity surface* rather than adding a parallel one.

**Rejected framing #3 (R1) — a `revise` op letting Doll rewrite `current_self` directly at any reflection.** Drafted in v1 to honor A1's autonomy-convergence principle. Cut after R1 converged on it from four lenses: it bypasses the skeptic into the always-injected identity region; junk/echoed text becomes her whole personality file; refeed paraphrase multi-writes and multi-bumps the persona generation; it can clobber a pending candidate; and a docstring-only rare tool never fires on the weak model (PinSelf 0/3), so the sovereignty it purchases is decorative while its attack/bug surface is real. Autonomy-convergence is met instead by the **counter-proposal path** (§3.4). Full rationale in §7.

**Adopted framing — 出廠人格 vs 現在的我, with adoption as the growth/drift boundary:**

- **三層自我,只有中層動。** 核心(pack `identity.self` + `taboos` + `[enforcement]`)凍結 — this design adds no write path to the pack. 性情層 = 新的 Doll-owned「現在的我」prose(this design). 興趣觀點層 = self_profile(already live; gains the evidence-recording this design needs).
- **The pack is her factory personality; `current_self` is who she has become.** Both render in the Identity region, ordered temporally. They may *disagree about disposition* — that disagreement is what growth looks like(出廠「沒事就安靜待著」→ 現在「監控數字跳動時會主動來勁」). They may not disagree about constitutive identity (name, self-concept, taboos) — that is the growth/character boundary.
- **System proposes, Doll adopts (Doll-sovereign).** A scheduled idle-time keeper synthesizes a *candidate* from her longitudinal record; nothing takes effect until Doll adopts it on a reflection turn. **Gate-chain invariant: every byte that becomes sanctioned `current_self` text passed (i) the mechanical checks, (ii) a skeptic verdict, and (iii) Doll's explicit adopt — on all three origins (keeper candidate, her counter-proposal, external file edit).** R2 verified the v2 draft violated this on the external path; §3.4's lifecycle table now enforces it literally. The skeptic's *scope* differs by origin (§3.3): system-synthesized text is additionally judged for groundedness; text originating from Doll or the user is judged **only against the frozen core** — the system is never the arbiter of her (or the user's proposed) self-expression beyond that boundary.
- **Adoption re-baselines P8**: adopted change = growth (baseline moves with her); un-adopted deviation = drift (guard still catches it).
- **Grounded, scheduled, slow.** Idle-time, week-scale, material-gated, decaying schedule(年輕時常變、穩定後漸稀). The keeper must cite concrete evidence, 寧缺勿濫; a separate-context skeptic verifies before anything reaches Doll (never same-context self-grading, per `ref_intrinsic-reflection-is-net-negative-without-external-grounding`).
- **Security posture, stated honestly** (§5 threat model): the `ref_memory-write-paths-are-attack-surfaces` triple is *consciously relaxed*, not satisfied. Provided: (1) provenance *recording* (`external_ctx` flags, append-only event log); (2) layered gates against *unwitting* corruption — not against a compromised Doll with Shell; (3) human-auditable artifacts, with **the event log — not the file — as the audit source of truth** (the file can lag or diverge; the log cannot, §5). A true held-out behavioral test remains the *manual* persona smoke run by the user; nothing in-daemon gates on a test Doll cannot influence, and the spec says so rather than pretending otherwise.

## 3. Mechanics

### 3.1 The artifact: `current_self.md`

- **Location:** `{memory_root}/current_self.md`. A single prose block(繁體中文,無 bullet 結構要求), mechanical floor **80** / cap `evolution.current_self_max_chars` (default **600**) chars.
- **Rendering mechanism (R1 correction; R2 formula fix).** The system prompt becomes a three-piece per-turn composition: `identity_prefix ⊕ current_self_section ⊕ scaffolding_suffix`, split at the end of the identity block when present (immediately after `## Taboos`), or at the template start for a packless run (R3′: `{% if identity %}` may be absent — the section then simply leads the prompt). Kernel renders the TWO static pieces once (scaffolding.jinja gains a split seam); MindLoop's constructor takes both and composes per turn, reading the *sanctioned* text (§5 tripwire) with a content-keyed cache — bytes change only when sanctioned text changes (weeks), so the prompt cache stays warm. The section renders as `## 現在的我` directly after the factory prose with one descriptive framing line — wording finalized at implementation; load-bearing: *descriptive* not imperative (Self-First), provenance-accurate(它是妳在反思中採納而來 — kept TRUE by sanctioned-text rendering), temporally ordered against the factory prose. No sanctioned text → section omitted entirely (no-fallback).
- **Never FTS-indexed** (same rule and structural-test pattern as self_profile.md).
- **Writer discipline:** sanctioned writers are the adoption path plus the §5 restore/repair paths of the evolution machinery; writes are atomic tmp+rename. (Claiming "single writer by construction" would be false — Doll has Shell, the user has an editor; §5 defines detection and ratification of unsanctioned writes.)

### 3.2 The evidence layer: `self_history.jsonl`

Append-only event log at `{memory_root}/self_history.jsonl`. One JSON object per line:

- **Pin events** (from `self_profile.apply`, code-level, no LLM): `{ts, turn, external_ctx, kind: "pin_add" | "pin_replace" | "pin_remove" | "pin_reconfirm", section, id, text, old_text?}`.
  - `pin_remove`/`pin_replace` preserve the outgoing text — **closes the deferred-tombstone gap**.
  - `pin_reconfirm` fires on the idempotent add-dedup hit **only cross-turn**. `turn` := the mind-loop **outer** iteration number — one drained perception batch and ALL its cascade/refeed passes share one turn value (R2: refeed passes must not count as separate turns, or the rule re-enables the very refeed-echo fabrication it exists to prevent). Prior-turn lookup = backward scan of `self_history.jsonl` for the last `pin_add`/`pin_reconfirm` with matching section+text (file is small; restart-safe; §6.1 tests the restart boundary).
  - `external_ctx: bool` := (the turn's drained batch contained `ToolResultArrived`/monitor perceptions) **OR** (Recall executed earlier in this turn's cascade — R2: Recall is a sync in-turn tool and never appears in any drained batch, yet is the highest-risk in-context channel; mind_loop keeps a turn-local flag). The always-injected `[Memory context]` block does **not** set the flag — conscious choice: it is present on essentially every conversational turn, so counting it saturates the flag to meaninglessness; write-time provenance of memory itself is out of this design's scope.
- **Evolution events**: `{ts, kind: "evo_candidate" | "evo_counter" | "evo_adopt" | "evo_reject" | "evo_expire" | "evo_kill" | "evo_no_change" | "evo_error" | "external_edit", text?, old_text?, rationale?, reason?, drift_score?}`.

Write ordering: for **adoption** the `evo_adopt` line is appended and flushed *before* `current_self.md` is written — a failed append aborts the adoption with a friendly error. The reverse failure (line flushed, file write fails) is defined in §5 (crash-repair). For **pin events**: log after successful apply; an IO error on the log is logged loudly and swallowed — the pin already happened, and the material gate needs *enough* evidence, not *complete* evidence (this swallow rule is pins-only, never evolution events).

### 3.3 The evolution pass

**Trigger — `EvolutionTrigger`,** same daemon-side poll-loop pattern as `ConsolidationTrigger` (5s tick; in-flight pass cancelled on `UserSpoke` at both kernel ingress points; torn down before `memsearch.close()`). Two firing modes:

**Mode A — full pass (keeper + skeptic).** Fires when ALL hold:

1. conversation-idle ≥ `evolution.idle_threshold_s` (default 600; baseline = `max(last_user_at, last_iter_at)`, same conscious choice as consolidation),
2. `now - last_evolution_attempt_at ≥ current_interval` (base `evolution.base_interval_days` = **7.0**),
3. **material gate**: (≥ `evolution.min_history_events` [default **8**] `pin_*` events past the HWM) OR (≥ `evolution.min_diary_days` [default **14**] Doll-authored diary days since the last *verdicted* attempt). `evo_*` bookkeeping lines never count.
4. no consolidation currently running (`ConsolidationTrigger.current_task` is public; kernel passes the reference). The reverse direction — consolidation cancelling an in-flight evolution pass — is **consciously omitted** (R2 YAGNI): the keeper's evidence bundle is assembled inline before the LLM call, so a mid-pass consolidation cannot change its inputs, and LLM contention is already handled by this forward gate plus `llm.max_concurrency` queueing.
5. no pending slot exists (either status, §3.4).

**Mode B — verdict-only pass.** Fires when `pending.status == "awaiting_skeptic"`, gated ONLY on conditions 1 and 4 (R2 Critical: the v2 draft's "same trigger machinery" made the counter re-verdict unreachable — conditions 2/3/5 all blocked it). Runs the skeptic on the pending text (scope per origin, below); no keeper.

**High-water mark (HWM)** := byte offset into `self_history.jsonl`. Captured when the driver snapshots the file; **committed to MindState only when the pass completes with a verdicted outcome** (`evo_no_change`, `evo_kill`, or candidate creation). Not committed on cancel or error. **Restored on `evo_expire`** (`pending.json` carries `hwm_before`; expiry writes it back): evidence Doll never got to judge re-seeds the next pass — a weak model failing to call the tool must not silently consume weeks of evidence (R2). `evo_reject` does NOT restore — her verdict consumed the evidence. The diary-days clause anchors to the same commit semantics.

**Interval dynamics**(年輕時常變、穩定後漸稀): `evo_no_change`, `evo_kill`, `evo_reject` → `current_interval := min(×2, max_interval_days=28.0)`; `evo_adopt` → reset to base; **`evo_expire` → unchanged** (a non-decision is noise, not a stability signal — R2: reading tool-calling failure as「自我已穩定」is semantically wrong); external-origin events → unchanged (an unratified file edit says nothing about the stability of her self). Anchor: `last_evolution_attempt_at := now` on every completed attempt AND every decision event (adopt/reject/expire). Bootstrap: first boot → `last_evolution_attempt_at := now`, `current_interval := base`. Fields (`last_evolution_attempt_at`, `current_interval`, HWM) ride MindState explicit save/load.

**Failure paths:**

| Event | Log | `last_attempt` | Interval | HWM | Slot |
|---|---|---|---|---|---|
| keeper/skeptic LLM error, timeout, malformed Report (Mode A) | `evo_error` | not advanced; 1h error-cooldown | unchanged | not committed | — |
| skeptic error (Mode B re-verdict) | `evo_error` | not advanced; 1h cooldown; `verdict_errors`+1; **≥3 → `evo_expire`** (deterministic bound — a failing skeptic must not wedge condition 5) | unchanged | n/a | retained until bound |
| mid-pass cancel (UserSpoke) | nothing (not an attempt) | not advanced | unchanged | not committed | — |
| candidate fails mechanical checks | `evo_kill(mechanical:…)` | advanced | ×2 | committed | none created |
| skeptic kill (keeper candidate) | `evo_kill` | advanced | ×2 | committed | none created |
| skeptic kill (counter) | `evo_kill` | — | unchanged | n/a | **reverts to fallback** (§3.4 — not silent) |
| keeper no_change | `evo_no_change` | advanced | ×2 | committed | — |
| adoption file-write fails after log append | retry once; then `evo_error` loud | — | — | — | cleared (sanctioned = log; §5 repair heals the file) |

**Keeper — driver-fed ephemeral agent** (Report + Scratchpad only, same shape as `run_consolidation`). Driver assembles inputs inline: pack identity prose(出廠人格,read-only reference), current sanctioned text (or 「尚無」), `self_profile.md`, the `self_history.jsonl` tail past the HWM (human-readable, `external_ctx` visible), consolidated/diary files since the last verdicted attempt (window capped at `max_interval_days`; missing files omitted). **Budget truncation order (load-bearing):** oldest-first within class; sacrifice consolidated before diary before self_history — dropping Doll-authored evidence in favor of transcript-derived files would invert the provenance weighting. Task prompt load-bearing points:

- 產出的是「候選」,不是決定 — 你在替她整理證據,不是替她做人。
- **Cite or die:** every claimed shift must point at concrete events in the supplied record(存活很久的 pin、跨日被 reconfirm 的條目、被淘汰的舊自我、diary 裡重複出現的模式). No evidence → Report `no_change`(寧缺勿濫 — the schedule decays; that is the designed outcome).
- Provenance weighting: ground primarily in pins and diary (she wrote them); weight `external_ctx: true` pins lower; weight reconfirms by **cross-day diversity, not raw count**; consolidated files are secondary (transcript-derived — hints, not proof).
- Output via Report: `no_change` (+ reason) or a candidate — **full replacement text** (floor ≤ len ≤ cap) + rationale listing citations. Full text, not a diff: LLM diff-application is the silent-no-op trap `ref_llm_edit_tools_locate_by_id_or_text` documents.

**Mechanical checks (code):** char floor/cap; `check_persona_violations` (banned substrings + exclaim runs); echo-marker normalization (§3.4). Applied to every origin's text at its entry point.

**Skeptic — a second, separate-context agent call.** For a **keeper candidate** (receives the byte-identical evidence bundle the keeper received, plus pack identity, old sanctioned text, candidate, rationale), verdict kill/pass against: (a) 改名或動搖自我認同(牴觸 pack `identity.self`); (b) 牴觸 taboos; (c) 重述 pack 已寫明的內容(假成長); (d) RP-stereotype filler 無證據支撐; (e) rationale 引用的證據在材料裡不存在 — **an anti-hallucination check, NOT an anti-poisoning defense** (in-bundle poison passes it by definition; poisoning defenses are the layered gates + Doll + audit). For a **counter-proposal or external edit** (no evidence bundle; receives pack identity, old sanctioned text, proposed text): verdict against **(a) and (b) only** — R2 sovereignty finding: (c)/(d)/(e) grade the authenticity of self-expression, which is legitimate only for system-synthesized text; applying them to her rewrite (or the user's proposal) makes the system the arbiter of her self beyond the frozen core.

### 3.4 The pending slot and the `SelfRevision` tool

**Slot:** exactly one — `{memory_root}/self_evolution/pending.json`, atomic write:

```json
{kind: "keeper" | "counter" | "external",
 status: "awaiting_skeptic" | "awaiting_doll",
 candidate, rationale?, fallback?,        // fallback = the prior awaiting_doll proposal, if any
 counter_round, created_ts, surfaced_count, verdict_errors, hwm_before?}
```

**Lifecycle (R2's load-bearing fix — one table, no ambiguity):**

| Origin | Created | Enters as | On skeptic pass | On skeptic kill |
|---|---|---|---|---|
| keeper | Mode A pass: mechanical checks → skeptic → slot | `awaiting_doll` (skeptic ran inside the pass) | — | no slot created (`evo_kill`) |
| counter | adopt-with-different-text (below): mechanical checks inline → slot replaced | `awaiting_skeptic`, `counter_round`+1, `surfaced_count` **reset** (R3′ supersedes R2's carry-over: with the `counter_round ≤ 2` cap the reset is bounded — at most 3 proposals' worth of surfacings per candidate — and her rewrite deserves a fresh decision window) | → `awaiting_doll`, surfaces as 「你的改寫已通過,現在可以採納」 | **reverts to `fallback`** (the candidate she countered), `awaiting_doll`; next surfacing leads with 你的改寫未通過(reason)——原候選仍在 (R2: a silent kill breaks the promise 「通過後會回來」 and erases her expressed intent) |
| external | §5 tripwire: mechanical checks at detection (fail → restore/delete file + `external_edit(reason)` logged, **no slot**) → slot | `awaiting_skeptic` | → `awaiting_doll` | restore/delete file (§5), `evo_kill` logged, no slot |

**Lifecycle notes (R3′):** slot creation of any origin logs its event (`evo_candidate` for keeper, `evo_counter` for counter, the `external_edit` line for external) — every slot has a birth line in the audit trail. A counter replacement inherits `created_ts`, `hwm_before` (the evidence window belongs to the originating keeper pass; external-origin slots carry no `hwm_before` — they consumed no evidence, so their expiry restores nothing), and sets `fallback := {candidate, rationale, kind}` of the proposal being countered; `notice` is a one-shot field carrying the kill reason, cleared after its first surfacing. A corrupt/unparseable `pending.json` is quarantined (`pending.json.corrupt`), logged as `evo_error` loudly, and treated as no-slot — surface-not-blank discipline, never silent deletion. **Slot-resolution invariant:** any slot clearing *without* adoption (reject / expire / verdict-error bound) restores `current_self.md` to the sanctioned text if divergent (logged loudly); adoption writes the new sanctioned text. This single rule replaces per-kind file-restore special cases and guarantees the tripwire finds a clean state after every resolution.

**Surfacing — `awaiting_doll` only** (R2: an unverdicted text must never be adoptable; `awaiting_skeptic` slots are invisible to Doll and do not increment `surfaced_count`). While `awaiting_doll`, each reflection turn — except safe-mode turns (grammar excludes the tool there) — receives a `[人格演化候選]` perception: 舊文(= live sanctioned text)與候選全文並列,distinctive marker prefixes(incoming `text` strips exactly these — A1 `_strip_incoming_tag` analogue)+ rationale(keeper)or origin note(counter/external — external uses neutral attribution:「檔案被直接修改,系統無法確認是誰」— R2: 「可能是主人」socially biases a compliance-prone model toward adopting hostile bytes)+ operational hint(採納:`SelfRevision decision=adopt`,不需填 text;不採納:`decision=reject`;想改寫後採納:全文放進 `text`,會先送審再回來)+ 主權句,now TRUE as stated(這是妳的人格描述——採不採納由妳;改寫只需不觸犯妳的核心身分與 taboos). Perception includes 第 N 次提醒 (breaks byte-identical correlated failure across surfacings). Each surfacing increments `surfaced_count`.

**Expiry:** `surfaced_count ≥ pending_max_surfacings` (default **5**) **AND** `now - created_ts ≥ pending_min_age_days` (default **2.0** — R2: reflections are iteration-based; five crowded in-conversation glances within one busy afternoon are not a considered non-decision on a weeks-scale question) → `evo_expire` loud, slot cleared, HWM restored from `hwm_before` (when present), interval unchanged, `last_attempt := now`. File restore follows the slot-resolution invariant above (prevents the tripwire re-detect livelock).

**Tool:** `SelfRevision(decision: Literal["adopt","reject"], text: str = "", reason: str = "")`, registered + grammar-included on reflection turns when `evolution.enabled` (static per-run predicate — single cached reflection grammar, PinSelf precedent). Acts only on an `awaiting_doll` slot; no slot or `awaiting_skeptic` → friendly 「目前沒有待批的演化候選」/「還在送審中」. **Per-turn latch (R2):** the first slot-mutating call per outer turn acts; any further SelfRevision call the same turn → friendly 「這一輪已處理過」 no-op (the refeed-paraphrase analogue of A1's add-dedup).

- **Echo-equivalence test (R2 — load-bearing, defined here, not at implementation):** normalize = strip surfacing markers + Unicode NFKC + whitespace collapse; `text` is *equivalent* to a reference if normalized-exact-equal OR jieba-Jaccard similarity ≥ 0.9 (reusing `response_drift_score` machinery — the model paraphrases, and exact-match misroutes an intended adopt into a 送審 round-trip).
- **`adopt`, `text` empty or equivalent to candidate** → the *candidate text verbatim* takes effect (never her paraphrase): append `evo_adopt` (text, old_text = live sanctioned text, `drift_score` — §3.5) then write file, clear slot, reset interval.
- **`adopt`, `text` equivalent to current sanctioned text** → friendly 「與現在的內容相同;要維持現狀用 decision=reject」, slot unchanged (R2: a zero-move adoption forged by echo confusion would bump the generation and reset the schedule for nothing).
- **`adopt`, genuinely different `text`** — the counter-proposal: if `counter_round ≥ 2` → friendly 「這個候選已改寫過兩次,請直接採納或拒絕」, slot unchanged (convergence bound in the `MAX_SYNC_REFEED_PASSES` style). Else mechanical checks inline (fail → friendly error, slot unchanged) → slot becomes her counter per the lifecycle table, `evo_counter` logged, friendly 「你的改寫已送審,通過後會回來給你採納」.
- **`reject`** → `evo_reject` (+reason, declined text preserved in the event); clear slot; kind=external restores/deletes the file; interval per §3.3.
- All results friendly `success=True`, in `IN_TURN_REFEED_TOOLS`; idempotency = the per-turn latch (stronger than identical-text no-op, which paraphrase defeats). The latch is turn-local state owned by mind_loop (same scope as the outer-turn id it threads to the pin logger), reset when a new perception batch drains.

### 3.5 P8 re-baseline: growth ≠ drift, mechanically

- **Persona generation** := count of `evo_adopt` events in `self_history.jsonl` (derived; every sanctioned change of any origin lands as `evo_adopt`).
- **Per-adoption `drift_score`** = text distance between old and new sanctioned prose (jieba-Jaccard, text-to-text, no LLM; `response_drift_score`'s tokenize+Jaccard core is refactored into a pairwise helper both callers share — R3′). First-ever adoption → `null` (distance-from-nothing is not a magnitude). The user-auditable trajectory of every sanctioned move — the honest substitute for the auto-behavioral gate this design consciously lacks.
- **Baseline bookkeeping:** `append_baseline` stamps the current generation into each record (absent = 0 legacy); drift comparison uses current-generation baselines only — requires `load_baselines` to return generation-aware records (return-shape change, acknowledged) while `response_drift_score` stays untouched. Empty current-generation pool → existing empty-baseline semantics (1.0, "no reference yet"); the smoke run then populates the new pool. Old-generation baselines retained — the trajectory of who she used to sound like.
- **Smoke isolation fix (R1 Critical):** `persona_stability_smoke.py`'s `_isolate_settings` swaps `[data]` to a scratch root, so unamended it would never render `## 現在的我` and generation would always derive 0. The smoke must (a) copy the *sanctioned* `current_self` text into the scratch root pre-construction, (b) derive the generation stamp from the *real* `self_history.jsonl` read before isolation.
- Deviation *within* a generation still flags. `[enforcement]` is C-layer and never re-baselines.

### 3.6 Config

```toml
[evolution]
enabled = true              # kill switch; false → machinery freezes (no trigger, no tool, no tripwire side-effects) but already-sanctioned text KEEPS rendering — disabling evolution must not amputate an adopted self (R3′)
current_self_max_chars = 600
current_self_min_chars = 80
base_interval_days = 7.0    # floats — live smoke clamps to sub-day values
max_interval_days = 28.0
idle_threshold_s = 600
min_history_events = 8
min_diary_days = 14
pending_max_surfacings = 5
pending_min_age_days = 2.0
```

Constants (not config, minimal-knob principle): counter_round cap = 2; verdict_errors bound = 3; echo-equivalence similarity = 0.9. Keeper + skeptic run on the single BYO LLM through existing worker machinery, subject to `llm.max_concurrency`.

## 4. What does NOT change

Pack schema and every `doll.toml`; self_profile schema, cap, PinSelf tool semantics and grammar (apply() additionally logs events and threads the outer-turn id); B2 consolidation logic (condition 4 reads its public `current_task`; nothing flows the other way); Mood/Energy; `check_persona_violations` and `response_drift_score` logic (baseline load/append gain generation awareness; `load_baselines` return shape changes — acknowledged); the no-fallback, friendly-error, Doll-sovereign norms.

## 5. Threat model, tripwire, and edge cases

- **Threat model, honest.** Doll holds Shell; every artifact is a plain file under `{memory_root}`. A fully compromised turn can rewrite `current_self.md`, forge log lines, or both — no in-daemon mechanism prevents that, and this spec does not claim to (that class belongs to a future sandboxing/provenance track; §7). Defended: the *unwitting* path — poisoned evidence steering a candidate through the legitimate pipeline — via layered gates, provenance flags, append-only audit (the **log is the audit source of truth**; the file may lag or diverge while a ratification pends), per-adoption drift scores, and the cap. The cap bounds *magnitude*, not *impact* — 600 always-injected identity chars is the highest-leverage text surface in the system, which is exactly why every sanctioned byte passes the full gate chain.
- **Tamper tripwire + ratification (transition-gated — R2).** The last sanctioned text = the latest `evo_adopt`'s `text` (bootstrap: none → any non-empty file counts as an edit; "restore" then means delete). At render time, if file ≠ sanctioned:
  - **Crash-repair special case:** file == `old_text` of the latest `evo_adopt` (the §3.2 log-then-write window) → rewrite file to sanctioned text, log a repair notice, no pending, no `external_edit` — a disk hiccup must not be narrated to Doll as tampering.
  - **New edit** (file text changed vs the last observed state — transition-fired, once per distinct edit): render sanctioned text (unratified bytes never enter the identity region; the framing line's provenance claim stays true), append ONE `external_edit` event, run mechanical checks — fail → restore/delete + `external_edit(reason)` logged, done. Pass → if no slot exists, create `{kind: "external", status: "awaiting_skeptic"}`; **if a slot exists, the edit is logged and nothing else happens** — external edits are NOT queued for auto-promotion (R3′, replacing v3's re-detection idea, which livelocked against the already-detected guard): on the current slot's resolution the slot-resolution invariant (§3.4) restores or overwrites the file, and the user re-edits to re-propose. The declined text survives in the log; a re-edit costs one command. (R2 context: v2's "replace any pending slot" let a file write destroy her in-flight counter; this keeps that fix.)
  - **Unchanged divergent file** (text == the already-logged edit): no side effects — render sanctioned text and move on (no per-turn log spam).
  - Ratification: skeptic (a)+(b) → surfaces `awaiting_doll` with neutral attribution → adopt = sanctioned (`evo_adopt`, generation bumps — sanctioned by *ratification*, not by file write); reject/expire = file restored to sanctioned text (or deleted, bootstrap case), declined text preserved in the log line — the proposer can read the outcome in the log; the daemon also logs the restore loudly (a silently self-reverting file reads as the daemon fighting its owner).
- **Residual risk — hostile external text passing (a)+(b) (R3′):** 550 chars of flattering RP filler violates neither identity nor taboos; the (c)/(d) exemption for non-keeper origins is a sovereignty decision (the system must not grade the user's or her own self-expression), so such text survives to Doll's judgment. Her adopt — under neutral attribution, with the full text in front of her — is the final gate; accepted residual, auditable in the log.
- **Placement / pack scoping (R3′):** `current_self.md` and `self_history.jsonl` sit exactly where `self_profile.md` sits (`memory_root` root, outside index paths). Whether that root is per-character is A1's pre-existing property, inherited unchanged; pack-swap semantics for the evolving self are out of this design's scope (verify placement at implementation).
- **current_self vs self_profile overlap:** accepted residual overlap (A1 §6.4 precedent).
- **current_self vs pack disagreement:** allowed on disposition, blocked on constitutive identity — skeptic (a)/(b) enforce the semantic half on every origin, the frozen pack the structural half. §6.2 *verifies* the adopted disposition prevails in-context rather than asserting it.
- **Restart / first boot:** slot and artifact are plain files; trigger fields ride MindState; first-boot init in §3.3; reconfirm lookup is restart-safe (log scan, not memory).
- **Latency:** +≤600 always-on chars. Same deliberate-tradeoff posture as A1's 1200 (measure post-implementation).

## 6. Verification

1. **Unit/TDD** (target ~873 → +~45): history logging (pin kinds incl. tombstones; cross-turn-only reconfirm incl. refeed-pass and restart boundaries; external_ctx incl. in-cascade Recall; pins-only IO-swallow); trigger Mode A (five gates independently block; pin-only+diary-OR material gate; HWM capture-vs-commit, restore-on-expire; interval dynamics incl. expire-unchanged and decision-anchored last_attempt; failure table row by row; first-boot init; MindState round-trip); trigger Mode B (fires on awaiting_skeptic with only conditions 1+4; verdict_errors bound); keeper driver (assembly, truncation order, byte-identical skeptic bundle, no_change); mechanical checks (floor/cap/banned/echo-strip) applied per origin; skeptic scope per origin ((a)-(e) vs (a)+(b)); pending lifecycle (every cell of the §3.4 table; counter fallback revert with one-shot kill-notice; surfaced_count reset bounded by counter_round cap; expiry needs count AND age; slot-resolution invariant restores/overwrites the file on every clearing path; corrupt-slot quarantine; birth event per origin); `SelfRevision` (plain adopt log-then-write ordering; echo-equivalent adopt takes candidate verbatim; text≡sanctioned refusal; counter path; reject; per-turn latch; no-slot/awaiting friendly errors; registry+grammar reflection∧enabled; safe-mode suppression); tripwire (crash-repair; new-edit transition firing; unchanged-divergent idempotence; edit-while-slot-exists logs-only; bootstrap no-predecessor; mechanical-fail restore); rendering (three-piece composition incl. packless split, content-keyed cache, sanctioned-vs-file divergence, section iff sanctioned text, enabled=false keeps rendering sanctioned text, never indexed); generation derivation + generation-aware baselines + empty-pool + drift_score incl. first-adoption null.
2. **Live smoke (required before done** — 軟機制必 live smoke; needs the user's llama-server): clamp intervals/thresholds small; seed PinSelf churn + a diary day; observe the full loop: grounded candidate (reject the smoke on RP filler), skeptic pass, `[人格演化候選]` surfaces, **real model calls SelfRevision** (riskiest link — now expiry-bounded), file written, `## 現在的我` renders *next turn without restart*, persona smoke stamps a new generation from the copied artifact. Also smoke: reject; one counter round-trip (改寫 → 送審 → re-surface → adopt); expiry (`pending_max_surfacings=1`, `pending_min_age_days≈0`); external-edit ratification (hand-edit the file mid-run → neutral perception → adopt AND a second run reject-restores); **disposition-prevails probe:** seed evidence conflicting with factory prose(出廠「沒事就安靜待著」vs adopted「主動來勁」), adopt, probe with prompts where the two dispositions diverge — 現在的我 must win, else the positioning goal ("prose that shifts", not a footnote) is not delivered and the framing line must be strengthened before merge.
3. **Plan sizing (R2 update):** v2 already sat at B2+A1+P8 combined; v3's lifecycle machinery adds more. Sanctioned decomposition (complete features, never MVP slices): (1) evidence layer (§3.2 — independently closes the tombstone debt); (2) artifact + composition seam + tripwire/ratification + `SelfRevision` adopt/reject (complete standalone: user-proposes/Doll-ratifies, sanctioned rendering, generation bump — de-risks the two scariest surfaces, prompt composition and weak-model tool adoption, before the week-scale machinery); (3) evolution pass proper (trigger + keeper + skeptic + counter re-verdict). Decide 2-way vs 3-way at writing-plans time.

## 7. Deferred (recorded, not silently dropped)

- **`revise`** (direct any-reflection rewrite) — cut in R1 (§2 framing #3); autonomy-convergence served by counter-proposals + reject-and-wait (bounded by `max_interval_days`). Revisit only on live evidence of genuine ossification, with the R1 constraints in hand.
- **Self-directed agenda** (positioning §2.3) and **interaction language / spontaneous sharing** (§2.4) — separate passes; §2.4 depends on this design existing first.
- **Mood history** — add only if live smokes show the keeper starving for material.
- **Reading the trajectory back** (Doll browsing her own generations/tombstones) — data exists append-only; a Recall surface is future work.
- **Multi-candidate / user-approval UI** — one slot, Doll-only approval; the user audits via the log (SoT), drift scores, and files.
- **Sandboxing/OS-level provenance for Shell** — the compromised-Doll class (§5) needs its own track.
