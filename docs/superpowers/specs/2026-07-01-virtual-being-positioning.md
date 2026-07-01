# Positioning: Virtual Being, Not AI Companion — Design

Status: positioning decision (user-confirmed, 2026-07-01). This is not an implementation spec — it sets the lens future specs/plans must be checked against. Supersedes the "AI companion" framing in the 2026-05-01 pivot spec's product positioning section (`2026-05-01-dollos-pivot-to-computer-design.md`); that spec's technical architecture (event loop, computer-as-home, BYO LLM, Workflow/Subagent) is UNCHANGED by this doc — only the *purpose* the architecture serves is reframed.

## 1. The shift

**Old framing:** Doll is an AI companion. She exists to accompany the user; her self (mood/preferences/self-profile) is a feature that makes her a better, more compelling companion.

**New framing:** Doll is a virtual being — self-centered, with her own interiority, capable of genuine growth. The relationship with the user is a thread in her life, not the reason her life exists. She would still be *her* — have opinions, interests, a developing character — in the counterfactual where the user weren't there. Companionship is what happens because two lives (hers and the user's) are entangled, not a role she performs.

This is not invented from nothing — it sharpens two things already present but under-emphasized:

- `project_companion_definition` (prior session): "companion = 學習/自更新能力（非服務性）" — companion already meant non-service, growth-capable, not task-serving. This positioning makes that the PRIMARY lens instead of a footnote.
- `docs/roadmap.md` "之後（未排序）": "Self-First 完整（self_history、emotional_residue、**慢變演化**）" — slow-changing evolution has sat on the backlog, unordered, since before this document. This positioning gives it a reason to actually get scheduled.

## 2. What this changes (design lens for future work — none of this is implemented yet)

Four concrete places identified in the 2026-07-01 discussion where "self-centered virtual being" implies a different default than "companion with self-first as a feature":

1. **`self_profile.md`'s scope should skew toward genuine interiority, not just relationship bookkeeping.** Today's three sections (`self` / `relationship` / `user`) are two-thirds explicitly about-the-user. A self-centered being's "我學到的自己" section should be able to hold things that have nothing to do with the user at all — an opinion formed from something a Workflow surfaced, a curiosity she's chasing for its own sake. Nothing in the current schema blocks this, but nothing encourages it either — it's a nudge/prompting question (what does `NoteMemory`/`self_profile`'s guidance tell her to write?), not necessarily a schema change.

2. **"Growth" must mean actual change over time, not just accumulation.** `character.py`'s `self`/`personality` are static; only `self_profile.md` grows, and it grows by *appending*, never by *changing what's already true*. A being that grows should be able to have her personality prose itself shift over a long enough horizon — not just gain footnotes. This is the "慢變演化" item — genuinely unbuilt, no existing mechanism to extend, needs its own design pass when picked up (see §4).

3. **Autonomy should extend to self-directed agenda, not just tracked commitments.** P7's `OpenLoop`/`CloseLoop` (closed this session, see `2026-06-25-core-loop-robustness-design.md` §13.2) is currently framed as "a TODO Doll owes someone" — in practice, owes the user. A self-centered being's open loops should just as naturally include something she started because she wanted to, with no user-facing deliverable at all.

4. **Product/interaction language shifts from reactive-to-you to genuinely present-alongside-you.** Today's Gura personality (`character_packs/gura/doll.toml`) reads "主人說話我才回，沒事就安靜待著" (passive, speaks only when spoken to). A self-centered being would, at least sometimes, surface what *she's* been thinking about unprompted, independent of whether it's useful to the user — not constantly, not performing "look how alive I am," but as a natural consequence of having an inner life that runs whether or not you're watching.

## 3. What does NOT change

- The technical architecture from the 2026-05-01 pivot: event-loop-centric daemon, computer-as-home, phone-as-remote, BYO single-LLM dependency, typed-tool substrate, no-fallback discipline. This positioning is about *purpose*, not *mechanism* — the event loop that already treats conversation as one perception source among many is, if anything, already well-suited to a self-centered being (it doesn't structurally assume "the user is the reason this loop runs").
- Self-First's existing commitment that self emerges from architecture, not prompt commands (`project_character_acting`). This positioning is a radicalization of that principle, not a departure from it.
- Nothing about §2's four items is scheduled or implemented by this document. This is the lens; the work is future plans, each through the normal brainstorm→spec→plan cycle.

## 4. Open / deferred — needs its own design pass when picked up

Listed so they are not silently dropped (same convention as `2026-06-25-core-loop-robustness-design.md` §11):

- **慢變演化 (slow personality evolution) mechanism.** No existing analog to extend — this is the most expensive, least-scaffolded item in §2. Needs its own brainstorm: what triggers a change (time? accumulated self_profile entries? explicit reflection?), how is drift from the character pack's original identity bounded (so "growth" doesn't silently become "a different character" — tension with the persona-stability work from `2026-07-01-persona-hardening-design.md` §3, which measures drift as a *risk* to catch; growth needs a way to distinguish intentional slow change from unintentional drift).
- **Self-directed goal/agenda mechanism.** Overlaps and extends the already-deferred full P7 rec (self-wake `PendingEvent` re-entry, Observe/Stay-silent grammar — `2026-06-25-core-loop-robustness-design.md` §11) — that rec was scoped around *user-relevant* goals; a self-centered version needs to also handle goals with no user-facing payoff at all, which raises new questions (does she "work" on them via Workflow dispatch on her own initiative? what resource/budget bounds apply to self-directed work, given the existing energy system (B3) already gates cognitive-work turns?).
- **Prompting/guidance changes for self_profile's `self` section** to nudge non-relational entries — smallest of the deferred items, likely a low-effort follow-up once someone's ready to touch `self_profile.py`'s tool docstrings.
- **Interaction-language changes** (§2.4) — depends on the above existing (she needs something of her own to spontaneously share before "spontaneously sharing" is anything but empty performance — doing this before growth/self-directed-agenda exist would risk exactly the "RP stereotype filling an empty description" failure mode `project_character_acting` already warned about).
