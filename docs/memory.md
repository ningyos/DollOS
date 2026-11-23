# Memory

The doll's long-term memory: one markdown file per memory, indexed in SQLite, found by one selection
pipeline for both her `recall` and the program's 【相關記憶】 ("related memories") attachment. Memories
are never deleted from what she can find without a replacement; corrections replace them. Nightly
maintenance audits new memories and consolidates the day into new ones.

## What the doll sees and does

**Folders.** Each memory lives in one of three folders:

- `secret`: the owner's secrets. Visible only in the owner's turns and hers, and only once the turn has
  seen a secret event. Never in a developer's turn.
- `own`: her own secrets (passwords, accounts, tokens). Visible in the owner's, the developer's and her
  own turns.
- `public`: everything else.

`secret` and `own` are encrypted on disk ([secrets.md](secrets.md)); she uses them exactly as `public`.

**`remember(items, folder?)`.** Each item is `{text, folder, kind?, deep?}`; a `folder` beside `items`
applies to every item that names none. `folder` is `secret`, `own` or `public` (when unsure whether it
is the owner's secret: `secret`). `kind` is `happened`, `intended`, `uncertain`, or omitted for facts and
preferences; an unknown `kind` fails that item only. `deep: true` marks something especially important:
it never fades. She chooses text, folder, kind and deep; the program sets the source and confidence.
Her memories link to no registry entity, so she should write names in full (「林美玲」, not only
「我媽」): they are found again only by words or meaning.

**`recall(query)`.** Searches memories and today's timeline and returns the 3 nearest, nearest first.
There is no relevance floor: unrelated queries still return the nearest, and she judges whether they
help. `<query>：0 條` only when there is nothing to search. After the memories come up to 2 diary
passages ([reflection.md](reflection.md)), marked as her own words. Faded memories are found as easily
as fresh ones.

**Labels.** A memory shows as 〔確定的打算｜public｜記憶 12-ab34cd〕: confidence level (確定 sure ≥
0.70, 大概 probably ≥ 0.40, else 聽說 hearsay), kind (omitted when none: 〔確定｜public｜記憶 12-ab34cd〕),
folder, id. Today's events show time and source: 〔今天 14:05｜主人說〕, 〔今天 14:06｜你說｜Discord 私訊〕,
〔今天 14:06｜你想〕, 〔今天 14:07｜click 的結果〕. `recall` results are never indexed as today's events.

**【相關記憶】.** The program attaches memories when the owner or developer says a new line and when
the computer's front app changes. Query: the last owner line plus the front app's name; the front app's
registry entry also counts. Only experiences, consolidated memories and deep memories are attached, at
most 5, each with cosine ≥ 0.55; ones already attached after the same line are skipped; faded ones are
not attached. Nothing is attached when nothing is close enough in meaning.

**Experiences.** When she finishes a computer task and says it succeeded, the program stores her actions
as an experience. Attached, it starts with a stats line (turns, seconds, how often it was followed and
succeeded). Experiences whose owner lines are ≥ 0.9 similar form one group; only the best is attached.
Old `program` menu-shortcut records describe the former VM's apps, not this Mac.

**Strength and fading.** Content never changes; only how easily a memory surfaces by itself does.
Seen once: it stops being attached after about 24 days. Seen on two different days: about four months.
Seen on three different days within about three weeks: settled, never fades. Only consolidation counts a
new sighting, at most once per day, when another day shows the same thing true again. Her repeating it,
`recall` hits and attachment never count. Strength is not confidence (how likely it is true). Deep,
settled, experience and program memories never fade. The owner can set or clear deep on the dashboard.

**`memory.fix(id, text, basis, why)`.** When the owner corrects her, or what she reads contradicts a
memory, she fixes that memory instead of adding a new `remember`.

- `id`: from the 〔…｜記憶 <id>〕 label.
- `text`: the whole corrected memory; required. Without it: 「記憶不會刪掉，只能換成新的內容」. If the
  right content is unknown, she writes what she knows: 「之前記的…是錯的（依據…）；正確的還不知道」.
- `basis`: a verbatim quote, at least 4 letters/digits, of the owner's words or of a window, file or tool
  result she read. Her own replies, thoughts, memories and call arguments never count. Without a basis
  she checks first (reads the file, looks at the window) or asks the owner.
- Only a memory she wrote herself with no source in front of her (tier `doll`) may be overwritten
  without a basis.
- Effective at once. The new memory stays in the old one's folder and keeps its kind, strength and deep
  mark. A secret basis cannot fix a non-`secret` memory; she uses `remember` into `secret` instead.
- Experiences and program records cannot be fixed.
- Every fix is recorded; the owner can undo it from the dashboard, and she then sees
  【主人退回了一個改動】 ([history.md](history.md)).

## How it works

### Files and index

Each memory is `<[memory] dir>/<folder>/<id>.md`, id `<source_event_seq>-<6 hex>` (`Memory.write`).
`MemoryFolder` maps `secret`/`own`/`public` to `[memory] secret_dir`/`own_dir`/`public_dir`;
`registry_dir` holds the entity registry; the four names must differ. Files are the truth. The SQLite
tables in `<[core] data_dir>/doll.db` (`memory_index`, `memory_fts` with FTS5 `trigram`,
`memory_vectors`, `memory_entity_refs`, `registry_*`) are an index. `Memory.rebuild` re-reads all files;
`Memory.syncOnBoot` rebuilds when `Memory.indexVersion` or the file counts differ. Bump
`Memory.indexVersion` whenever the same files would index differently.

`MemoryFile.text` writes and `Memory.parseFile` reads a `+++` TOML front-matter block, then the body
verbatim (byte-identical round trip). Keys: `kind`, `confidence`, `created_ts`, `source_event_seq`,
`source_tier`; optional `deep`, `evidence` (`[{seq, ts}]`, from the first confirm), `settled_ts`,
`audit_pending` (segment), `entities` (`[{id, name}]`), `app`/`app_version`, experience stats (`task`,
`turns`, `seconds`, `attached_*`), `retired_ts`/`superseded_by`/`retired_reason`. Malformed optional
keys are rejected; unknown keys ignored.

### Source tier and audience

`SourceTier` sets the starting confidence (`[memory] confidence_<tier>`); `Memory.level` maps it to
確定/大概/聽說. In `Doll.remember` the tier comes from the last event before her reply that is a person's
line, a `watch` event, or a result of `Doll.observingTools` (`open`, `read_window`, `act`, `tool.run`,
`read_file`, `edit_file`): owner → `owner`, developer → `developer`, tool/watch → `tool`, none → `doll`
(then `source_event_seq` is her reply). No tool writes `watch` events any more; old timelines may hold
them.

`Doll.audience`: developer spoke last → `.developer`; else last owner line → `.owner`; else `.doll`.
`Memory.visible(_:sawSecret:)` applies the folder rules above (`.stranger` → `public` only). A secret
hit makes the result a secret event; a sealed-folder hit makes it stored sealed.

### Embedding, registry, today index

`MLXEmbedder` runs `[memory] embedding_model` (Qwen3-Embedding) in process: last-token pooling,
L2-normalized, so a dot product is the cosine. Queries get the `Instruct: … Query:` prefix; documents
none. Load failure is a boot error.

`Registry` keeps one file per concrete thing (`RegistryEntry`: `type`, `name`, `aliases`, description),
id `e` + 8 hex; same name, different thing → two entries. `Registry.nearby`: exact `Registry.key`
matches, then RRF over FTS and cosine. Writers: `Selector.programApp` (bundle id or exact name) and
consolidation (`NewEntity`, `Registry.addAlias`). A memory's `entities` must exist.

`TodayIndex`: in-memory FTS5 + vectors over the current segment: people's lines, her speech and
thoughts, tool results except `recall` (`TodayIndex.unindexedResults`), watch and calendar events.

### Selection (`Memory.related`, `Selector.run`)

No model in the pipeline. `Memory.related` collects candidates in the visible folders with their
channels (`Candidate.Channel`): `fts` (FTS5 trigram over `Memory.queryPhrases`, plus substring match of
2-character words from `Memory.shortTerms`), `embedding` (`.atLeast(floor)` for attach, `.best(n)` for
recall), `entity` (the front app's entry, attach only), `hop` (shares an entry with an fts/embedding
hit).

- `Rule.attach` (`Doll.attachMemories`): `Memory.attachable` memories with cosine ≥ `[select]
  similarity` and, if fading, strength ≥ `strength_floor`; score = cosine + `entity_weight` × trigger
  entities + `strength_weight` × min(0, strength); best `attach_max`. Experiences grouped by
  `Memory.experienceGroups`, best by `Memory.ranked`.
- `Rule.recall` (`Doll.recall`): the `recall_candidates` best by cosine, RRF (`[memory] rrf_k`) over a
  word-count list and a cosine list, the `recall_limit` best. No floor, no strength. The line she is
  answering is excluded.

`Memory.search` (older RRF ranker) serves only consolidation. `SelectionReport.line` is logged; a turn
that saw a secret logs no recall query.

### Strength

`Memory.strengthTiers` (owner, developer, tool, stranger, doll, consolidated) have a strength.
`Memory.activation` = `ln Σ (1 + age_k / unit)^-d`, age in days (`strength_decay`,
`strength_unit_days`), over the evidence: the writing event plus each consolidation `confirm`
(`Memory.confirm`, at most one per local day). When strength right after a confirm reaches
`strength_settled`, `settled_ts` is stored and never cleared. `Memory.fadingStrength` is nil for deep,
settled or tierless memories. `Memory.setDeep` is the dashboard toggle. Strength affects only attaching.

### Replacement

`Memory.retire` writes the `Retirement` and drops the index rows; the file stays. `Memory.replace`
writes a new memory in the same folder, keeps kind, entities, evidence, deep and `settled_ts`, then
retires the old one with `superseded_by`. `Memory.unretire` reverts. Consolidation deletes files retired
longer than `[consolidate] retired_keep_days` (`Memory.deleteRetired`), the only deletion of a
strength-tier memory.

### `memory.fix` (`MemoryFix.swift`)

`Doll.memoryFix` checks in order: the id exists in a folder the turn's audience may read
(`Memory.visible` with `sawSecret: true`), else "no such memory"; retired → names the replacement;
experience/program → refused; `text` present and not `Doll.sameText`; `basis` passes `Doll.quoted`
against `Doll.citable(before:)` (people's lines, window and file reads, computer/file tool results with
her argument values cut, calendar, the 【狀況】 footer, the open nodes of her graph) with ≥
`[computer] plan_min_chars` by `Doll.plainKey`; a basis found in a secret event cannot fix a
non-`secret` memory; an empty basis only when `Doll.hers`. The replacement takes the basis's tier
(`doll` stays `doll`, `consolidated` stays `consolidated`) and `audit_pending`. Each change appends a
`MemoryFixRecord` (`by`: `her`, `audit`, `flag`) to `<data_dir>/memory-fixes.json` and commits history;
`Doll.dashboardMemoryRevert` undoes via `Doll.undoRef("fix#N")`; flags cannot be reverted.

### Audit (`Audit.swift`) and consolidation (`Consolidate.swift`)

Maintenance tasks, audit first ([maintenance.md](maintenance.md)). Each sends one persona-free request
and writes nothing unless the whole reply validates.

Audit: candidates are non-retired memories with `audit_pending` (`Doll.auditCandidates`; those shown
when the owner corrected her first, then oldest, at most `max_memories`). Source is
`Audit.sourceLines`: people's lines, gated tool results without her arguments, watch and calendar
events, never her words; cut to `max_source_chars` by `Audit.select`. `Audit.refusal` accepts a `wrong`
verdict only with a verbatim quote from the memory's source and no dropped source run of
`guard_chars` letters. `mode = change` replaces via `Memory.replace`; `flag` only records. Every checked
memory loses `audit_pending`.

Consolidation: `Consolidation.gather` → `ask` → `parse` (ops `add`, `update`, `retire`, `confirm`) →
`apply` (adds are tier `consolidated` with `audit_pending`; then a new timeline segment starting with
【系統維護】). The CLI `consolidate` runs `Consolidation.run` offline. Own-words rule: her lines are
tagged `｜自己的話` and are never a fact source; something only she said may be written only as
「她說過…（沒查證）」, because a stored guess of hers was once repeated as fact.

### Encryption

`Memory.sealedFolders` = `secret`, `own`: whole-file `SecretBox` texts via `Memory.writeFile` /
`Memory.contents`; their index rows live only in memory (`Store.sealedFolders`); fix records store their
texts sealed (`MemoryFixRecord.sealed`). Registry files are plain. See [secrets.md](secrets.md).

## Config

All keys and validation: [config.md](config.md).

- `[memory] dir`, `secret_dir`, `own_dir`, `public_dir`, `registry_dir` (`memory`, `secret`, `own`,
  `public`, `entities`): folder names.
- `[memory] confidence_<tier>`: required starting confidence per tier (example file: owner 0.9,
  developer 0.8, tool 0.85, stranger 0.5, doll 0.3, program 0.95, self_reported 0.4, consolidated 0.6).
- `[memory] high_confidence` (0.70), `medium_confidence` (0.40): 確定/大概 thresholds.
- `[memory] embedding_model` (`Qwen/Qwen3-Embedding-0.6B`).
- `[memory] experience_group_similarity` (0.9).
- `[memory] strength_decay` (0.5), `strength_unit_days` (7), `strength_settled` (0.75: three distinct
  days).
- `[select] similarity` (0.55), `attach_max` (5), `recall_limit` (3), `recall_candidates` (5),
  `strength_floor` (-0.75).
- `[consolidate] retired_keep_days` (30).
- `[audit] mode` (`flag`; `change` once removed a correct detail only a cut-off part of the source
  supported), `max_memories` (20), `max_source_chars` (30000), `guard_chars` (2).
- `[computer] plan_min_chars` (4): minimum basis/quote length.

## Invariants

- Markdown is the source of truth; the index is rebuildable from files alone.
- No fallback: embedder failure, unknown entity id, bad front-matter are errors.
- The tier comes from the timeline, never from her claim.
- Her own words are never a fact source (consolidation, audit, `memory.fix`).
- A memory is replaced, never removed without a replacement; a correction never changes folder.
- `settled_ts` is never cleared.
- Consolidation and audit write nothing unless the whole reply validates.

## Known limitations

- `[memory] today_confidence` is parsed but unused.
- Nothing writes `program`-tier shortcut records now; `Memory.learnedApps`, `Memory.forgetApp`,
  `Selector.programShortcut` have no production caller.
- `Audience.stranger` is never produced.
- `Memory.search` and `Memory.related` rank differently.
- Registry files are plaintext even when a sealed memory names them.

## Tests

`aMemoryFileRendersBackByteIdentical`, `theRegistryAndEntityRefsRoundTripAndTheIndexRebuildsFromFiles`,
`rememberWritesNoEntitiesAndNoRegistryEntries`, `recallReturnsTheRecallLimitBestWithNoFloorTodayIncluded`,
`attachGivesAtMostAttachMaxAboveTheCosineFloorBestCheapScoreFirst`,
`ownMemoriesShowInTheOwnersTurnsAndHersButNeverAStrangers`, `secretMemoryStaysOutOfTurnsWithoutSecrets`,
`endToEndUnconfirmedMemoriesFadeConfirmedAndDeepOnesStayRecallFindsAll`,
`herCorrectionWithTheOwnersWordsReplacesTheMemoryKeepsTheOldRetiredAndRecallGivesOnlyTheNew`,
`withoutABasisOnlyHerOwnMemoryMayBeOverwritten`, `aCorrectionNeverLeaksASecretAndASecretMemoryIsUnknownToTheDeveloper`,
`consolidationAppliesAddUpdateRetireAndTheNextStartIsANewDay`,
`theAuditChangesAMemoryOnlyWithAVerifiedContradictingQuoteAndSaysSoAtTheEnd`,
`inFlagModeTheAuditOnlyListsItForTheOwner`.
