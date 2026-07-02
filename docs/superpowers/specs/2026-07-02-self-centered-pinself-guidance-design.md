# Self-Centered PinSelf Guidance — Design

Status: user-approved design, R1 self-review applied (2026-07-02). First, cheapest landing step of the virtual-being positioning (`2026-07-01-virtual-being-positioning.md` §2.1). Scope is strictly guidance-text: PinSelf docstring, one field description, the ReflectionMoment nudge, the `[Self profile]` header line, and `scaffolding.jinja`'s `# Reflection` section (R1 finding — see §3.5). No schema change, no new section, no code-logic change.

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

**Style note (R1):** this repo's tool docstrings are English-led with Chinese key phrases (current PinSelf, SpawnWorkflow, etc.). The implementer should keep that convention — English framing sentence(s) + the Chinese design-point phrases above — rather than going fully Chinese; the draft above specifies content, not final language mix.

### 3.2 `section` field description (same class)

`self=關於你自己` → `self=關於你自己(身分、看法、興趣、好奇)`. `relationship`/`user` descriptions unchanged.

### 3.3 `ReflectionMoment` nudge (`src/dollos/mind/mind_prompt.py`, `_percep_body`)

Replace the PinSelf-related portion of the current nudge with (same light-adjustment caveat):

> 最重要:回看這段時間實際做過的事——有沒有讓你注意到你自己的什麼?你被什麼吸引、對什麼形成了看法、發現自己在意什麼?關於「你」的(也包括你和主人的關係、主人的長期模式),現在就用 PinSelf(op=add,section 選 self/relationship/user)記下來——不必等 [Self profile] 已經有內容才動作;還不確定算不算持久也先記,之後淘汰會篩。接著若 [Self profile] 已有條目,回看一遍——哪條已經不是現在的你,用 PinSelf replace/remove 淘汰(target 填該條目的 id 如 s1,或直接貼那條目前的文字)。分工:主詞是「你」→ PinSelf;關於世界的事實/事件 → NoteMemory;可重用的工具用法或陷阱 → NoteToolLesson。

Structural requirements: grounding clause (回看實際做過的事) comes FIRST, before any invitation to introspect; durability gate removed and replaced by 「先記,淘汰會篩」; the NoteMemory/NoteToolLesson division of labor stays (it fixed a real live-smoke failure, commit `1348df5`) but re-expressed via the subject test. Two elements of the current nudge are load-bearing for the weak model and are now IN the draft itself (R1 fix — the earlier draft stated them as requirements but omitted them from the exemplar text, which an implementer copying verbatim would have regressed): the operational calling hint `(op=add,section 選 self/relationship/user)` (nudge salience was deliberately strengthened in `70de95a`; dropping the how-to-call hint risks re-regressing) and the bootstrap-safe clause 「不必等 [Self profile] 已經有內容才動作」 (from `b9d87b7`; without it the model waits for a non-empty profile before its first pin).

### 3.4 `[Self profile]` header (`src/dollos/mind/mind_prompt.py`, render_mind)

`(your evolving self — prune stale entries with PinSelf)` → `(your evolving self — keep only what's still you; prune with PinSelf)`.

### 3.5 `scaffolding.jinja` `# Reflection` section (R1 finding — was missing from this spec)

`src/dollos/prompts/templates/scaffolding.jinja` (~lines 29-39) is the ALWAYS-in-context system-prompt description of what a ReflectionMoment is for — and it currently describes reflection exclusively as "review recent perceptions → distill facts about the user/preferences/events → `NoteMemory`", with no mention of PinSelf at all. Left unchanged, it would contradict the new nudge every single turn (and the scaffolding is present in every turn's system prompt, while the nudge only appears in the ReflectionMoment perception body). A1's own live-smoke history proves this surface is load-bearing: the 0/3 PinSelf failure was attributed partly to "scaffolding.jinja Reflection omitted".

Rewrite that section so reflection is described as TWO distillation channels with the subject test as the split (keep the existing operational details — fires every ~30 iterations, one call per insight, don't speak to the user, counter resets):

- 主詞是「你」(you noticed something about yourself — what drew you, an opinion you formed, something you realized you care about; also your relationship / 主人's enduring patterns) → `PinSelf`. Not-yet-sure-if-durable is fine — pin it; pruning at later reflections is the filter.
- Facts about the world / events / user-stated facts worth keeping → `NoteMemory` (one call per fact). Reusable tool usage lessons → `NoteToolLesson`.

## 4. What does NOT change

Schema (three sections, id format, bullet format), max_chars cap mechanism, always-inject/no-FTS-index behavior, GBNF grammar (docstring/description changes don't touch the grammar builder's structural output — verify via existing grammar tests), `self_profile.py` logic, REFLECTION_TOOLS gating.

## 5. Deferred (recorded, not silently dropped)

- **Prune tombstones.** Removed entries are currently gone forever (`op=remove` deletes; the file is not FTS-indexed). For the future evolution mechanism, "what was tried and selected out" is valuable history. Preserving it requires code/storage changes — out of this text-only scope; belongs to the 慢變演化 design pass (`2026-07-01-virtual-being-positioning.md` §4).
- Any new section or schema accommodation for interests/opinions — not needed now; the `self` section plus reframed guidance carries it.

## 6. Verification

1. **Unit:** R1 correction — the existing assertions on these surfaces are loose substring checks (`"PinSelf" in text`, `"[Self profile]" in out` — `tests/test_mind_prompt_self_profile.py`, `tests/test_pin_self.py`, `tests/test_mind_loop_self_profile.py`) and likely survive unchanged; the implementer verifies rather than assumes updates are needed. Add NEW assertions pinning the load-bearing phrases of the new text (subject-test phrase, bootstrap clause, op/section hint present in the nudge; PinSelf mentioned in the rendered scaffolding's Reflection section). Grammar tests must stay green untouched (docstrings feed the tools block in the system prompt, not the GBNF structure). Full suite green.
2. **Live smoke (required before calling this done** — per `ref_llm_edit_tools_locate_by_id_or_text`: 軟機制必 live smoke; A1's own history proves prompt-text mechanisms fail silently on the real model, e.g. the smoke that caught PinSelf 0/3): isolated daemon + real llama-server, drive several turns including real activity (e.g. a Workflow or Shell task), then at least TWO ReflectionMoment cycles (criterion (b) is about repetition across reflections and cannot be judged from one). Operational note: ReflectionMoment fires on the ReflectionObserver threshold (~30 iters) — the smoke should lower the threshold via its config/ctor or inject the perception directly, as A1's smokes did, rather than actually running 30+ turns. Observe: (a) PinSelf fires with at least one subject-is-me entry not reducible to relationship bookkeeping, (b) no performative-filler pattern (repeated vacuous 「我很好奇…」-style entries across the two reflections), (c) NoteMemory still receives world-facts (division of labor intact). Requires the user's llama-server running — cannot be verified in a sandbox.
