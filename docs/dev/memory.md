# Memory

How the doll's long-term memory is stored, indexed, found, strengthened, corrected and audited.
Code: `Sources/DollCore/Memory.swift`, `Store.swift` (index tables), `Embedding.swift`,
`Registry.swift`, `Select.swift`, `Consolidate.swift`, `MemoryFix.swift`, `Audit.swift`, and the
tool handlers `Doll.remember`, `Doll.recall`, `Doll.attachMemories`, `Doll.memoryFix` in `Doll.swift`.
What the doll herself is told about it: `dolldocs/memory.md`.

## Mechanism

### Files are the truth, SQLite is an index

Every memory is one markdown file `<[memory] dir>/<folder>/<id>.md`. The SQLite tables in
`<[core] data_dir>/doll.db` (`memory_index`, `memory_fts` with the FTS5 `trigram` tokenizer,
`memory_vectors`, `memory_entity_refs`, and the `registry_*` tables) are only an index. `Memory.rebuild`
drops every row and re-reads the files. `Memory.syncOnBoot` rebuilds the whole index when the
stored `Memory.indexVersion` differs, or when the count of indexable plain files or registry files
differs from the index. Bump `Memory.indexVersion` whenever the same files would index differently
(new vector model, new row shape); boot then rebuilds.

Folders (`MemoryFolder`, directory names from config):

| case | `[memory]` key | holds |
|---|---|---|
| `secret` | `secret_dir` | the owner's secrets |
| `own` | `own_dir` | her own secrets: passwords, accounts, tokens |
| `public` | `public_dir` | everything else |

`[memory] registry_dir` holds the entity registry (below). The four names must be distinct
(`Config` validates it).

### Front-matter

`MemoryFile.text` writes, and `Memory.parseFile` reads, a `+++` block then the body verbatim:

```
+++
kind = "intended"            # "" | happened | intended | uncertain (MemoryKind)
confidence = 0.9
created_ts = 1763000000.0
source_event_seq = 412       # the timeline event the content came from
source_tier = "owner"        # SourceTier
deep = true                  # optional: never fades
evidence = [{"seq":412,"ts":…},…]   # optional: written on first confirm
settled_ts = …               # optional: strength reached the settled line
audit_pending = "<segment>"  # optional: not yet audited
entities = [{"id":"e1a2b3c4","name":"TextEdit"}]   # optional
+++
body text
```

Also optional: `app`/`app_version` (a record about one app version), `task` plus `turns`,
`seconds`, `attached_followed`, `attached_success`, `attached_failed` (an experience,
`ExperienceStats`), and `retired_ts`/`superseded_by`/`retired_reason` (`Retirement`). `parseFile`
rejects a malformed `entities`, `deep`, `evidence`, `settled_ts` or empty `audit_pending`. Unknown
keys are ignored. The round trip is byte-identical (`aMemoryFileRendersBackByteIdentical`).

The id is `<source_event_seq>-<6 hex>` (`Memory.write`).

### Source tier and confidence

`SourceTier` records where the content came from; it fixes the starting confidence
(`[memory] confidence_owner`, `confidence_developer`, `confidence_tool`, `confidence_stranger`,
`confidence_doll`, `confidence_program`, `confidence_self_reported`, `confidence_consolidated`).
`Memory.level` maps confidence to 確定 (sure) / 大概 (probably) / 聽說 (hearsay) with
`[memory] high_confidence` and `medium_confidence`.

For `remember`, the program decides the tier, never her: `Doll.remember` takes the last event before
her reply that is a person's line, a watch wake, or a result of `Doll.observingTools`. Owner →
`owner`, developer → `developer`, tool/watch → `tool`, none → `doll` (and `source_event_seq` is her
reply). She chooses only the text, folder, `kind` and `deep`. A bad `kind` fails that item only.
Her memories carry no entities.

### Audiences and visible folders

`Memory.visible(_:sawSecret:)` decides which folders a turn reads:

- `Audience.stranger`: `public` only.
- `Audience.developer`: `public`, `own`.
- `Audience.owner`, `Audience.doll`: `public`, `own`, plus `secret` only when the turn already saw a
  secret event.

`Doll.audience` derives the audience from the timeline (developer spoke last → developer; else the
last owner line or watch wake). Nothing yields `.stranger` yet. A secret hit makes the tool result a
secret event; a hit from an encrypted folder makes it stored encrypted.

### Embedding

`Embedder` is the protocol; `MLXEmbedder` runs Qwen3-Embedding (`[memory] embedding_model`, a
Hugging Face id or an absolute directory) on MLX in-process. Last-token pooling over the text plus an
appended `<|endoftext|>`, L2-normalized in float32, so a dot product is the cosine. Queries get
`Instruct: <[memory] embedding_query_instruction>\nQuery:`; documents (memories, registry lines,
today's events, experience task lines) get no prefix. Texts are cut at `[memory] embedding_max_tokens`
and embedded 16 per batch. A lock serializes calls (MLX state is not thread-safe). Load or download
failure is a boot error; `load` refuses a model whose pooling is not last-token.

### Entity registry

`Registry` keeps one file per concrete thing (`RegistryEntry`: `type` (`EntityType`), canonical
`name`, `aliases`, description body) in `<[memory] dir>/<[memory] registry_dir>/<id>.md`; ids are
`e` + 8 hex. Exact matching uses `Registry.key` (NFKC-style compatibility mapping, lowercased,
whitespace removed). `Registry.nearby` returns exact matches first, then RRF over FTS and cosine of
`RegistryEntry.line`.

Writers: the program for things it knows exactly (`Selector.programApp`, keyed by bundle id or exact
name; `Selector.programShortcut`), and consolidation's reply (`NewEntity`, plus aliases added via
`Registry.addAlias`). `remember` writes none. A memory's `entities` must name existing entries
(`Memory.write` and `Memory.rebuild` throw otherwise). Two memories sharing an entry are neighbours
for the hop channel.

### Today index

`TodayIndex` is an in-memory SQLite (FTS5 trigram + vectors) over the current timeline segment, built
at start and fed by `TodayIndex.add` on every append. It indexes the owner's and developer's lines,
what she said per channel, her thought, tool results except `recall` (`TodayIndex.unindexedResults`),
watch wakes and calendar events. It is never on disk and never a source of truth.

### Finding memories: `Memory.related` and `Selector.run`

The 【相關記憶】 ("related memories") attachment and her `recall` share one pipeline with no model in
it. `Memory.related` collects every candidate in the visible folders, each with the channels that
found it (`Candidate.Channel`):

- `fts`: FTS5 trigram hit for `Memory.queryPhrases` (CJK runs as 3-character windows, Latin/digit
  runs of 3+), or a substring hit for `Memory.shortTerms` (2-character words a trigram index cannot
  match, via `Store.memoryContaining` / `TodayIndex.containing`).
- `embedding`: `.atLeast(floor)` for attach, `.best(n)` for recall.
- `entity`: has one of the trigger's registry entries (only the front app's, attach only).
- `hop`: shares a registry entry with an fts/embedding hit other than itself (one hop).

`Selector.run` then applies a `Selector.Rule`:

- `Rule.attach` (program, on a new owner line or a front-app change; `Doll.attachMemories`): keeps
  candidates with cosine ≥ `[select] similarity` and, if the memory fades, strength ≥
  `[select] strength_floor`. Score = cosine + `[select] entity_weight` × trigger entities +
  `[select] strength_weight` × min(0, strength). At most `[select] attach_max`. Only experiences
  (`selfReported`), consolidated memories and deep memories are attachable (`Memory.attachable`);
  experiences of one task are grouped (`Memory.experienceGroups`, `[memory]
  experience_group_similarity`) and only the best (`Memory.ranked`) competes. Already-attached ids
  are skipped. Today's events are not attached.
- `Rule.recall` (her `recall`): the `[select] recall_candidates` best by cosine whatever their cosine,
  ranked by RRF (`[memory] rrf_k`) over a text list (count of query words contained) and a cosine
  list; the `[select] recall_limit` best. No relevance gate and no strength: a faded memory is found
  as easily as a fresh one. The line she is answering is excluded from today's hits.

`Memory.search` is an older RRF ranker (FTS + cosine, `[memory] max_candidates`, plus
`[memory] confidence_weight` × confidence); only consolidation's `gather` uses it.

`SelectionReport.line` is logged per run. In a turn that saw a secret, the recall query is kept out
of the log.

### What she sees: ids and labels

`RecallHit.rendered` gives 〔確定的打算｜public｜記憶 12-ab34cd〕 (level + kind label, folder, id) for a
memory and 〔今天 14:05｜主人說〕 for one of today's events. The id is what she passes to `memory.fix`.
An attached experience gets its stats line (`ExperienceStats.line`) before the body.

### Strength and fading

Only `Memory.strengthTiers` (= `Consolidation.revisable`: owner, developer, tool, stranger, doll,
consolidated) have a strength; experiences and program records do not. `Memory.activation` is ACT-R
base-level activation, `ln Σ (1 + age_k / unit)^-d` over the evidence, age in days
(`[memory] strength_decay`, `strength_unit_days`). The `1 +` makes a fresh piece count 1, so n pieces
give at most ln n.

Evidence is the writing event plus each consolidation `confirm` (`Memory.confirm`): at most one per
local calendar day; her repeating it, `recall` and attaching never count. When strength right after a
confirm reaches `[memory] strength_settled`, `settled_ts` is stored and the memory never fades again,
even if config changes (0.75 sits between ln 2 and ln 3: three distinct days are needed).
`Memory.fadingStrength` returns nil (full strength) for deep, settled or tierless memories. She marks
`deep` on `remember`; the owner toggles it on the dashboard (`Memory.setDeep`). Strength affects only
attaching, never recall. It is independent of confidence.

### Replaced, never deleted

A memory is never removed from what she can recall without something in its place. `Memory.retire`
writes the `Retirement` into the front-matter and removes the index rows; the file stays.
`Memory.replace` writes a new memory in the **same folder** (a correction never moves content out of
`secret`), keeps the old one's kind, entities, evidence, deep mark and settled time, then retires the
old one with `superseded_by`. `Memory.unretire` is the dashboard/history revert. Consolidation deletes
files retired longer than `[consolidate] retired_keep_days` (`Memory.deleteRetired`). This is the only
deletion of a strength-tier memory.

### `memory.fix` (`MemoryFix.swift`)

`Doll.memoryFix(id, text, basis, why)` replaces a memory at once:

1. The id must exist and be in a folder the turn's audience can read (`Memory.visible` with
   `sawSecret: true`; whose turn it is decides, not whether a secret was seen). Otherwise the same
   "no such memory" refusal.
2. Retired → refusal naming the replacement. Experiences/program records → refusal.
3. `text` is required. Retiring alone is refused: 「記憶不會刪掉，只能換成新的內容」 ("memories are not
   deleted, only replaced"). Same text as before (`Doll.sameText`) is refused.
4. `basis` must be a verbatim quote (`Doll.quoted`) from `Doll.citable(before:)`: people's lines,
   window and file reads, computer/file tool results with her own argument values cut, the calendar,
   the 【狀況】 footer, her goal-tree nodes. Her replies, thoughts, memories and call arguments never
   count; a quote of her own words gets an explicit refusal. At least `[computer] plan_min_chars`
   letters/digits (`Doll.plainKey`).
5. A basis that appears in a secret event cannot correct a non-`secret` memory.
6. Exception: an empty basis is allowed only when `Doll.hers` (tier `doll`, she wrote it with no
   source in front of her); the replacement stays `doll`.

The replacement takes its basis's tier (owner/developer/tool); a consolidated memory stays
`consolidated` so it is still attached. It is marked `audit_pending`. Each change appends a
`MemoryFixRecord` to `<data_dir>/memory-fixes.json` (`by`: `her`, `audit`, or `flag`) and a history
commit; `Doll.dashboardMemoryRevert` undoes it through the history (flags cannot be reverted).

### Consolidation (`Consolidate.swift`)

The first task of the nightly maintenance (see [maintenance.md](maintenance.md)); the CLI's
`consolidate` command runs the same steps via `Consolidation.run` while her process is stopped.

- `Consolidation.gather` (sync, writes nothing): renders the current segment with
  `Consolidation.tasks` (one `DayTask` per owner/developer line; events before the first one are task
  0), recalls related existing memories of `revisable` tiers per task query (`[consolidate]
  recall_k`), collects registry entries named by them plus `Registry.nearby` (`[consolidate]
  registry_k`), and builds a persona-free request (`Consolidation.instructions`). Empty segment is an
  error.
- `Consolidation.ask` (async, cancellable): one request with `[consolidate] effort`, `max_tokens`,
  `timeout_s`; `Consolidation.parse` validates the whole reply (exactly one ```json block; ops `add`,
  `update`, `retire`, `confirm`; ids must be among the memories shown, used once; `superseded_by` an
  add's ref or a non-retired shown id). A second clean request writes the day summary. Any failure
  writes nothing and keeps the segment.
- `Consolidation.apply` (sync): deletes long-retired files, creates only the new registry entries some
  operation names, aligns entity names as aliases, applies ops (adds are tier `consolidated`,
  `source_event_seq` = last seq of the segment, `audit_pending` = the segment), then starts a new
  timeline segment whose first event is 【系統維護】 ("system maintenance") with `doneText`,
  `summaryNote` and the summary.

**Own-words rule.** In `Consolidation.tasks` her thoughts and speech are tagged `｜自己的話` ("her own
words", `Consolidation.ownMark`). `Consolidation.ownWords` (in the instructions) and
`Consolidation.summaryOwnWords` (in the summary request) say these lines and her call arguments are
never a source of facts; something only she said may be written only as 「她說過…（沒查證）」 ("she said
… (unverified)"). Reason: a real run had her guess a disconnect time; a maintenance step stored it as a
fact and she repeated it the next day. The same rule is enforced in code for `memory.fix` (citable
sources) and the audit (`Audit.sourceLines`).

### Audit (`Audit.swift`)

A maintenance task after `review`, before `consolidate`, so corrections land before consolidation
builds on them. Candidates (`Doll.auditCandidates`): memories with `audit_pending`, not retired;
first those shown to her when the owner corrected her (`Audit.shownAtCorrections`); at most
`[audit] max_memories`, oldest first; the rest wait.

- Source: `Audit.sourceLines` of the memory's segment(s): people's lines, `Tools.gated` tool results
  without her arguments (`Doll.withoutArguments`), watch wakes, calendar. Never her words, memories
  or recall results. Windows are compacted (`Audit.compactWindow`, controls in
  `Audit.controlRoles` dropped, a repeat read collapsed to one line). Over `[audit] max_source_chars`,
  `Audit.select` keeps, per memory, lines before its writing (`[audit] origin_share`) and the most
  similar lines, then the newest; gaps are marked 「……（略過 N 行）」.
- One persona-free request; each memory gets `ok`, `unsure` or `wrong` with a `quote` and `fixed`.
- `Audit.refusal` accepts a `wrong` only if the quote has ≥ `[computer] plan_min_chars` letters, is
  verbatim in the memory's own source, `fixed` differs, a quote the memory itself repeats only allows
  removals (`Audit.removesOnly`), and no run of ≥ `[audit] guard_chars` letters that the source holds
  is dropped (`Audit.removedRuns`).
- `[audit] mode`: `change` replaces via `Memory.replace` (tier and `source_event_seq` kept, record
  `audit`, one history commit per change); `flag` records only (`flag`). An empty `fixed` is flagged
  in both modes. Default is `flag`: a real-length measurement showed `change` removing a correct
  detail that only a cut-off part of the source supported.
- Every checked memory loses `audit_pending` whatever the verdict, except ones changed meanwhile.

### Encryption

`Memory.sealedFolders` = `secret` and `own`. Their files are whole-file `SecretBox` texts
(`dollbox1:` prefix) via `Memory.writeFile` / `Memory.contents`; a plaintext file there is an error.
Their index rows live only in an attached in-memory schema (`Store.sealedFolders`) and are rebuilt
from decrypted files every boot; plaintext rows of those folders in `doll.db` fail boot. Fix records
of sealed memories store their texts sealed (`MemoryFixRecord.sealed`). Registry files are not
encrypted. Details: [secrets.md](secrets.md).

## Invariants

- Markdown is the source of truth; the index must be rebuildable from files alone.
- No fallback: embedder load failure, unknown entity id, bad front-matter → error, not degraded mode.
- Tier comes from the timeline, not from her claim.
- Her own words are never a fact source (consolidation, audit, `memory.fix`).
- A memory is replaced, never removed from recall without a replacement; a correction never changes
  folder.
- `deep` only for strength tiers; `settled_ts` never cleared.
- Consolidation and audit write nothing unless the whole reply validates.

## Known limitations

- `[memory] today_confidence` is parsed and validated (`MemoryConfig.todayConfidence`) but no code
  reads it.
- Nothing currently writes `program`-tier shortcut records; `Memory.learnedApps`,
  `Memory.forgetApp` and `Selector.programShortcut` have no production caller.
- `Audience.stranger` is never produced.
- `Memory.search` (consolidation) and `Memory.related` (recall/attach) rank differently.
- Registry files are plaintext even when a sealed memory names them.

Related: [maintenance.md](maintenance.md), [history.md](history.md), [secrets.md](secrets.md),
[config.md](config.md), [testing.md](testing.md), [turns-and-todo.md](turns-and-todo.md).
