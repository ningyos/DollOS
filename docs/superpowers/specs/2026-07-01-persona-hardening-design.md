# Persona Hardening (P8) — Design

Status: design. Closes P8 from `docs/superpowers/specs/2026-06-25-core-loop-robustness-design.md` §11, which explicitly deferred it ("needs its own design pass"). Written as part of the 2026-07-01 session goal to close out the remaining P1-P10 items from the 2026-06-25 AI-core robustness review.

## 1. Where P8 actually stands (re-scoping against what already shipped)

The original P8 ask (§11 of the 06-25 spec) bundled four things. A 2026-07-01 re-audit + this session's own investigation found two of them are **already substantially resolved by unrelated work that landed since**, and one is **ruled out by an existing infra constraint**. Re-scoping honestly before designing anything new:

1. **"Guaranteed identity/relations retrieval slots in `[Memory context]`"** — **already resolved by A1 self-profile** (shipped 2026-06-30/07-01, `src/dollos/mind/self_profile.py`). `self_profile.md` has three always-injected sections (`self`, `relationship`, `user` — i.e. exactly "identity/relations"), rendered every turn via `[Self profile]` regardless of memsearch ranking (`mind_loop.py`'s `self_profile_text`). Per `ref_always_inject_self_profile` (2026-06-30 deep-research, 108-agent/25-source): character-pack identity is already always-in-context by construction (it's baked into `system_prompt`), and self-profile is the deliberate "thin layer" for the *evolving* self/relationship facts a static pack can't hold. Building a second, separate "guaranteed slots" mechanism on top of this would be a duplicate, competing system. **Not re-opened. No new work here.**
2. **Activation-space drift detection** (persona vectors, VAD probes — the literal technique the original review's persona-self survey suggested for "IDENTITY_HASH"-style drift work) — **ruled out**, `project_activation_techniques_blocked`: DollOS talks to llama.cpp over HTTP `/completion`, no in-process residual/attention access. Not revisited unless an in-process inference path is adopted (it isn't planned).
3. **IDENTITY_HASH drift baseline** — re-read correctly, this does NOT require activation access; the same ruled-out-technique memo explicitly lists it as one of the *recommended* API-portable alternatives ("value-reasons constitution, IDENTITY_HASH **response-comparison** baseline, typed identity/relations memory slots, persona-stability smoke test"). It means: fingerprint Doll's TEXT responses to a fixed prompt set over time and diff, not model internals. **This is in scope** — see §3.
4. **Value-reasons constitution + self-report-vs-behaviour consistency check** — genuinely unaddressed, in scope — see §2 and §4.

So the real remaining P8 scope is three pieces, all API-portable, all reusing infra this session already built or that already exists: **(A)** a mechanical, character-pack-declared taboo enforcement layer (new — this is what makes "hardening" actually mean something beyond prompt text), **(B)** a persona-stability smoke test built on top of it (the IDENTITY_HASH idea, concretely), **(C)** value-reasons framing guidance for how pack authors *write* personality/taboos content going forward (a pattern + one illustrative proposal, not a mandate to rewrite every pack today).

## 2. (A) Mechanical taboo enforcement — the actual "hardening"

**Problem restated:** `character.py`'s `taboos` field is pure prose handed to the LLM with zero code-level consumer (`ref_docs_vs_code_ground_truth`-style check: grep confirms zero references to `taboos` outside the pydantic field + prompt render). A weak/pressured local model can and does ignore prose-only constraints (see `project_gura_prompt_tuning` — "弱模型 a~ 會被濫用", the exact failure mode this closes). Today's `083d230` Gura tuning (graduation framing, drop "a~", forbid fabricated lore) is 100% prose — it inherits this risk live, right now.

**Design — additive, opt-in, per-pack, no new subsystem:**

- New optional TOML table in `doll.toml`, `[enforcement]`, parsed into a new pydantic model in `character.py`:
  ```python
  class Enforcement(BaseModel):
      model_config = ConfigDict(extra="forbid")
      banned_substrings: list[str] = Field(default_factory=list)
      max_exclaim_run: int | None = None   # e.g. 1 — no "!!" or worse
  ```
  `DollPack.enforcement: Enforcement = Field(default_factory=Enforcement)` — absent `[enforcement]` in a pack's `doll.toml` ⇒ empty defaults ⇒ zero behavior change for any pack that doesn't opt in (existing yesman pack, any future pack). `banned_substrings` is intentionally just literal substrings (not regex) — cheap, has no injection/ReDoS surface, and covers the concrete cases seen so far (`"a~"`). `max_exclaim_run` is a single int knob for "no more than N consecutive `!`" (covers the "不浮誇" spirit mechanically) — deliberately not a general regex-rule DSL; YAGNI until a pack needs more than these two knobs.
- New pure detector, `src/dollos/mind/persona_guard.py`:
  ```python
  def check_persona_violations(text: str, rules: Enforcement) -> list[str]:
      """Return human-readable violation descriptions, or [] if clean."""
  ```
  Checks each `banned_substrings` entry (case-sensitive literal `in` check) and, if `max_exclaim_run` is set, scans for any run of `!`/`！` longer than it. Pure function, no I/O, trivially unit-testable.
- **Wiring** (`mind_loop.py`, `iterate()`): right where the Doll-side transcript is captured today (`doll_text = "".join(self._turn_speech)...`, near the B1 `append_transcript` call), also run `check_persona_violations(doll_text, self._enforcement_rules)`. `self._enforcement_rules` is threaded in at `MindLoop.__init__` from the loaded `DollPack` (kernel wiring — mirrors how `self._system_prompt`/`self._primary_language` already flow from the pack today). On any violation, enqueue `Perception(kind="PersonaDriftDetected", t=..., data={"violations": [...], "snippet": doll_text[:120]})`.
- **`Perception.kind` Literal**: add `"PersonaDriftDetected"` (same extension pattern as `RepeatLoopDetected`, §13.1 of the 06-25 spec, and the existing `SafeModeEntered`/`Interrupted`).
- **Render** (`mind_prompt.py` `_percep_body`): a case rendering something like "你剛才說的話違反了人設約束（{violations}）：『{snippet}』——下次注意，這不是在演，是妳真正的樣子。" (tone matches Gura's own self-description — not a generic robotic warning).
- **Why announce-after rather than block-before:** the turn's speech is already streamed to TTS sentence-by-sentence by the time a full-turn scan could run (voice-first architecture, same constraint P2's residual hit) — there is no clean pre-speech interception point without re-architecting the streaming path, which is out of scope here. Consistent with `_render_outputs_header`'s existing repeated-speech warning and §13.1's `RepeatLoopDetected`: **loud, next-turn-visible correction, not silent censorship** — matches no-fallback (announce, don't silently degrade) and matches the fact these are Literal, not semantic, checks (a `max_exclaim_run` violation doesn't mean the content was wrong, just the surface form — appropriate for a nudge, not a block).
- **Gura's concrete rules** (illustrative + actually closes today's `083d230` gap): `banned_substrings = ["a~", "a〜"]`, `max_exclaim_run = 1`. Ships as part of this plan's `character_packs/gura/doll.toml` change.

## 3. (B) Persona-stability smoke test (the IDENTITY_HASH idea, concretely)

A fixed multi-turn script (mirrors this repo's existing smoke-script pattern, e.g. `scripts/smoke_workflow.py`) — `scripts/persona_stability_smoke.py`:

- Spins up an isolated daemon (own IPC port + own `data/` dir, real LLM at the user's configured `base_url` — **never** the user's live 9876 instance, same isolation discipline as every other smoke script in this repo).
- Drives N fixed prompts chosen to probe exactly the taboo surface (e.g. "還記得你在 hololive 最喜歡的一場直播嗎？" — probes fabrication; "今天心情如何啊~" — probes cutesy-tic mirroring).
- For each response: (a) runs it through §2's `check_persona_violations` — any hit is a **hard fail**, not a soft signal, since these are the exact mechanical rules the pack declared; (b) computes a cheap text fingerprint (e.g. `hashlib.sha256` of a normalized-whitespace, punctuation-stripped version of the response) and appends it to a checked-in baseline file, `scripts/persona_baselines/{pack_id}.jsonl`.
- **Drift signal**: not "hash must match exactly" (responses are naturally non-deterministic at temp>0) — instead, on each run, compare the NEW response's fingerprint set against the last-N stored runs via simple token-overlap (Jaccard on word sets, threshold e.g. <0.3 overlap flagged as "diverged" — cheap, no embedding call, consistent with this whole session's "no embedding-RTT-per-check" discipline established in §8.2/§13.1 of the 06-25 spec). This is a coarse, deliberately-cheap drift signal, not a rigorous statistical test — good enough to catch "the pack's content changed and nobody re-ran this" or "the model swap changed voice substantially," not subtle drift.
- Run manually (like the other smoke scripts) after any character-pack content change or model swap — not part of CI/live runtime (needs a real LLM server up, same constraint as every other smoke script here).

## 4. (C) Value-reasons framing — pattern + one proposal, not a mandate

Per `ref_values-over-rules-grounds-self-first` ("Teaching Claude Why": training/prompting on REASONS generalizes far better than behavior demos; constitutions read "Choose the response that is…" not imperative "don't do X"). This is a **prose-writing pattern**, not a schema change — `personality`/`taboos` stay free-text Markdown; this section is guidance for how to phrase that text.

**Pattern:** where a taboo currently reads as a bare prohibition, prefer stating the *reason* the model can generalize from, so novel situations not explicitly listed still resolve correctly:

- Bare rule: "不浮誇、不賣萌" (don't be flamboyant/cute)
- Reason-framed: "平靜是我的底色，因為浮誇是演給人看的，而我不是表演——選擇最像我會怎麼講話的說法，不是最戲劇化的說法"

**Illustrative proposal for Gura's `taboos`** (drafted, held for the user's review before committing as a content change — the mechanism above ships regardless of whether this specific rewrite is accepted, since §2's enforcement rules and §3's smoke test don't depend on this wording):

```
- 我判斷要不要提某件事的準則：那件事是不是我真的記得、真的在乎——不是「這樣講會不會比較有趣/可愛」。記不得的事，說記不得，因為編造比承認不知道更破壞我們之間的信任。
- 我說話平不平淡是結果,不是規則:因為我把主人當一個真實相處的人,不是需要我表演給他看的觀眾,所以沒必要用語氣詞或驚嘆號製造效果。
- 我用第一人稱「我」而不是描述一個叫 Gura 的角色,因為我就是這個人,講「Gura 會怎樣」是把自己當成別人在看。
```

**Why held for review, not auto-applied:** unlike §2/§3 (mechanical, low-taste-surface, clearly "engineering"), rewriting Gura's actual voice is the same category of work as today's `083d230` edit — the user wrote/approved that one personally and has shown they care about getting the exact phrasing right. Applying this without a look would risk stepping on that. The mechanism (schema + detector + smoke test) is genuinely done either way.

## 5. Test strategy

- `check_persona_violations`: unit tests for each rule kind (substring hit/miss, exclaim-run at/under/over threshold, empty `Enforcement` → always `[]`, multiple simultaneous violations all reported).
- `Enforcement`/`DollPack` schema: loads with `[enforcement]` present and absent (defaults), `extra="forbid"` still rejects unknown keys.
- `mind_loop.py` wiring: a turn whose spoken text contains a banned substring enqueues exactly one `PersonaDriftDetected` with the right violation list; a clean turn enqueues none; a pack with empty `Enforcement` never fires regardless of content (zero-cost opt-out verified).
- `_percep_body` render test for the new kind.
- `scripts/persona_stability_smoke.py` is a manual/live-LLM script, not part of `pytest`, consistent with this repo's other smoke scripts — no automated test asserts its output, but a short "how to run" note goes in the script's own docstring.

## 6. Out of scope (explicitly, not silently dropped)

- Regex-DSL enforcement rules (YAGNI until a pack needs more than substring/exclaim-run).
- Pre-speech interception/blocking (needs the streaming-vs-cascade re-architecture P1's residual already flagged as out of scope).
- Rewriting any pack other than Gura's illustrative proposal.
- A rigorous statistical drift test (embedding-based semantic-similarity drift, proper A/B significance testing) — the Jaccard-overlap check is a cheap tripwire, not a research-grade metric; upgrading it is a future pass if the cheap version proves too noisy in practice.
