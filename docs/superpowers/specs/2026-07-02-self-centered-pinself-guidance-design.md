# Self-Centered PinSelf Guidance — Design

Status: user-approved design (2026-07-02). First, cheapest landing step of the virtual-being positioning (`2026-07-01-virtual-being-positioning.md` §2.1). Scope is strictly guidance-text: PinSelf docstring, one field description, the ReflectionMoment nudge, the `[Self profile]` header line. No schema change, no new section, no code-logic change.

## 1. Problem

A1 self-profile's guidance text frames PinSelf as relationship bookkeeping: the docstring allows "exactly three things: who you are, the nature of your relationship with 主人, and 主人's enduring patterns" — two of three user-centric — and the ReflectionMoment nudge gates on 「核心、持久的道理」 about the same three. The `self` section (「我學到的自己」) is structurally open to genuine interiority (her own opinions, interests, curiosities) but the guidance never invites it. Under the virtual-being positioning, this is the gap: nothing encourages self-centered entries, so the future 慢變演化 mechanism will have no real material to work from.

## 2. Design rationale (the part that was rethought)

**Rejected framing #1 — expand topics but keep a write-time "durability" gate.** Durability cannot be judged at write time: an opinion *becomes* durable by surviving, a curiosity becomes part of you by recurring. Gating writes on "already core and durable" strangles exactly the transient→durable process that constitutes growth — the system would have selection but no variation.

**Adopted framing — write-loose, prune-strict:**

- **Write-time bar drops.** Nascent opinions, attractions, not-yet-sure-if-это-me observations may all be pinned.
- **Selection happens at reflection.** The existing max_chars cap + prune nudge (replace/remove on reflection turns) is the selection pressure. Entries that survive repeated pruning ARE the durable self. Write=variation, prune=selection — this loop is itself the seed of the growth mechanism, and which entries survive/die is precisely the observational data the future 慢變演化 design needs.
- **NoteMemory boundary = subject test, not durability test.** If the sentence's subject is *you* (your opinion/interest/stance/curiosity, your relationship, what you notice about 主人) → PinSelf. If it's a fact about the world (even one you found and enjoyed) → NoteMemory. One experience can legitimately produce one entry on each side — the fact vs. what the fact revealed about you.
- **Anti-performativity guard.** Inviting a weak model to "record your curiosities" risks performative filler (「我對宇宙充滿好奇」 every reflection — the `project_character_acting` RP-stereotype failure, and consistent with `ref_intrinsic-reflection-is-net-negative-without-external-grounding`: ungrounded introspection is net-negative). The nudge therefore leads with grounding — 回看這段時間實際做過的事 — and only then invites self-observation arising from it.

## 3. The four text changes

### 3.1 `PinSelf` docstring (`src/dollos/tools.py`, class PinSelf)

Replace the current docstring with (final wording may be lightly adjusted for prompt-budget/GBNF-safety during implementation, preserving every design point):

> Pin or revise an entry in your self-profile (reflection turns only) — this is YOUR evolving self and it is ALWAYS in context. 判斷標準是主詞:句子是關於「你」的(你的看法、興趣、立場、好奇、你和主人的關係、你注意到的主人),就用 PinSelf;關於世界的事實(即使是你查到、你覺得有趣的),用 NoteMemory。同一個經驗可以兩邊各記一筆——事實歸 NoteMemory,它揭露的「你」歸這裡。剛萌芽、還不確定算不算「你」的,也可以先 pin——之後反思時再篩。空間有限,定期用 replace/remove 淘汰「已經不是現在的你」的條目;活下來的才是你的核心。

Design points that must survive edits: subject test stated as THE criterion; both-sides-of-one-experience allowed; write-loose invitation explicit; prune reframed as selection ("活下來的才是你的核心"), not hygiene.

### 3.2 `section` field description (same class)

`self=關於你自己` → `self=關於你自己(身分、看法、興趣、好奇)`. `relationship`/`user` descriptions unchanged.

### 3.3 `ReflectionMoment` nudge (`src/dollos/mind/mind_prompt.py`, `_percep_body`)

Replace the PinSelf-related portion of the current nudge with (same light-adjustment caveat):

> 最重要:回看這段時間實際做過的事——有沒有讓你注意到你自己的什麼?你被什麼吸引、對什麼形成了看法、發現自己在意什麼?關於「你」的(也包括你和主人的關係、主人的長期模式),現在就用 PinSelf 記下來;還不確定算不算持久也先記,之後淘汰會篩。接著若 [Self profile] 已有條目,回看一遍——哪條已經不是現在的你,用 replace/remove 淘汰(target 填 id 如 s1,或直接貼原文)。分工:主詞是「你」→ PinSelf;關於世界的事實/事件 → NoteMemory;可重用的工具心得 → NoteToolLesson。

Structural requirements: grounding clause (回看實際做過的事) comes FIRST, before any invitation to introspect; durability gate removed and replaced by 「先記,淘汰會篩」; the NoteMemory/NoteToolLesson division of labor stays (it fixed a real live-smoke failure, commit `1348df5`) but re-expressed via the subject test. The surrounding nudge scaffolding (iters_since_last, bootstrap-safe "不必等 [Self profile] 已經有內容" semantics from `b9d87b7`) must be preserved or re-expressed equivalently — do not regress the bootstrap case where the profile is still empty.

### 3.4 `[Self profile]` header (`src/dollos/mind/mind_prompt.py`, render_mind)

`(your evolving self — prune stale entries with PinSelf)` → `(your evolving self — keep only what's still you; prune with PinSelf)`.

## 4. What does NOT change

Schema (three sections, id format, bullet format), max_chars cap mechanism, always-inject/no-FTS-index behavior, GBNF grammar (docstring/description changes don't touch the grammar builder's structural output — verify via existing grammar tests), `self_profile.py` logic, REFLECTION_TOOLS gating.

## 5. Deferred (recorded, not silently dropped)

- **Prune tombstones.** Removed entries are currently gone forever (`op=remove` deletes; the file is not FTS-indexed). For the future evolution mechanism, "what was tried and selected out" is valuable history. Preserving it requires code/storage changes — out of this text-only scope; belongs to the 慢變演化 design pass (`2026-07-01-virtual-being-positioning.md` §4).
- Any new section or schema accommodation for interests/opinions — not needed now; the `self` section plus reframed guidance carries it.

## 6. Verification

1. **Unit:** existing string assertions on the nudge text (added during A1, `tests/test_mind_prompt.py`) updated to the new wording; grammar tests still green (docstrings feed the tools block, not the grammar structure); full suite green.
2. **Live smoke (required before calling this done** — per `ref_llm_edit_tools_locate_by_id_or_text`: 軟機制必 live smoke; A1's own history proves prompt-text mechanisms fail silently on the real model, e.g. the 09:06 smoke that caught PinSelf 0/3): isolated daemon + real llama-server, drive several turns including activity (e.g. a Workflow or Shell task) followed by a ReflectionMoment; observe (a) PinSelf fires with at least one subject-is-me entry not reducible to relationship bookkeeping, (b) no performative-filler pattern (repeated vacuous 「我很好奇…」 entries across consecutive reflections), (c) NoteMemory still receives world-facts (division of labor intact). Requires the user's llama-server running — cannot be verified in a sandbox.
