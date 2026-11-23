# Configuration

`dollcore --config <file.toml>` reads one TOML file with a small strict parser (`TOML.swift`) into a typed `Config` (`Config.swift`). Unknown, mistyped or out-of-range settings stop the boot; omitted optional keys take their Swift defaults. The character pack (`doll.toml`) uses the same parser but is read loosely. Each section's keys are listed in the file that owns the behavior (table below).

## What the doll sees and does

The doll neither reads nor changes the config. Some numbers appear in her prompt (the context warning percentage, compaction turns), so changing them changes her prompt bytes ([testing.md](testing.md)).

## How it works

**Parser** (`TOML.parse` → `[section: [key: TOMLValue]]`). Accepted: bare `[section]` headers (letters, digits, `_`, `-`); `key = value` inside a section; `#` comments; basic strings with escapes `\" \\ \n \t` only; multi-line `"""…"""` (a newline right after the opening quotes is trimmed); integers; floats; `true`/`false`; single-line arrays of those. `\r\n` becomes `\n`. Everything else throws `TOMLError` (`line N: …`): literal strings, inline tables, dates, dotted or `[[array]]` headers, duplicate sections or keys, unknown escapes, unterminated strings, a key outside a section. A strict subset turns a typo into a boot error instead of an ignored setting.

**`Config.load`.** Defaults are the Swift initial values; the file overrides them.

- Unknown section → `unknown sections: …`. Known (23): `llm core context computer files idle memory dashboard api bridge consolidate select voice calendar maintenance drives owner_sleep goals audit claims log history reflection`.
- Unknown key → `[section] unknown keys: …`: the private `Section` helper removes each key it reads and `finish()` throws on leftovers.
- Wrong type → `[section] key must be …`. An integer is accepted for a `Double`, never a float for an `Int`.
- Out of range → one `guard` per section, whose message lists all its constraints.
- Required: `[llm] base_url` and `model`; `[memory] confidence_<tier>` (0…1) for each `SourceTier` (`owner developer tool stranger doll program self_reported consolidated`).
- Cross-key: `[core] priority` and `[drives] order` list each `Priority` / `Signal` case exactly once (a new case breaks every config until listed); `[api] port` ≠ `[dashboard] port` when the dashboard is on; enabled `[bridge] command` starts with an absolute path; `retry_max_s` ≥ `retry_first_s` > 0; efforts are in `Effort.levels` (`none medium high`); `[memory]` folder names are distinct and `high_confidence` > `medium_confidence`; `[maintenance] time` and `[owner_sleep] start`/`end` are `HH:MM`, start ≠ end; `[audit] mode` is `change`/`flag`; `[voice] tts_precision` is `int8`/`bf16`; `[reflection]` (`Config.readReflection`) counts and caps ≥ 1, `recall_max` ≥ 0, `weakness_quiet_days` > 0; `[claims] reach` ≥ 0 with no empty words.

**At boot** `Doll.init` requires `README.md` in `[core] docs` (`[core] docs: …/README.md does not exist`), parses `[core] pack`, and fails on a missing `[history] git` ([history.md](history.md)). Reflection runs only with `[maintenance]` and `[drives]` enabled.

**Relative paths.** `[core] data_dir`, `pack`, `docs` and `[memory] dir` resolve against the process's working directory (`root` in `Sources/DollCLI/main.swift`), not the config's folder. `[history] dir` and `[voice] data_dir` are under `[core] data_dir`. `dollctl --config <file>` reads only `[api] port` (default 8791).

**Character pack** (`[core] pack`, default `character_packs/template/doll.toml`; unknown keys ignored):

| Key | Required | Use |
|---|---|---|
| `[meta] name` | yes | her name |
| `[meta] aliases` | no | other names she answers to |
| `[identity] self`, `personality`, `taboos` | yes | identity part of the system prompt |
| `[identity] examples` | no | dialogues after `Doll.examplesHeader` |
| `[identity] ideal` | with `[drives]`, until an ideal-self file exists | first ideal self ([reflection.md](reflection.md)) |

The template has no personality; the owner copies and fills it. Test fixture: `Tests/DollCoreTests/Fixtures/xiaodeng/doll.toml`.

**`dollcore.example.toml`** has every key with a Traditional Chinese reason comment (measurement, decision, or 初始值理由). It ships with bridge and voice off and placeholder `[llm]` values. Only `[drives]` is tested to equal the defaults; keep the others equal by hand.

**Adding a key:** add a `public var` with default and doc comment; `try <section>.read("key", &field)` before `finish()` (otherwise every config setting it fails); extend the `guard`; add it to the example with its reason; update the owning doc; add a test (example value present, bad value throws `TOMLError`). A new section also needs its name in the known set and a `Config` property. Removing a key breaks configs that set it; no aliases exist.

**Section owners:**

| Section | Doc |
|---|---|
| `llm`, `context`, `idle`, `core` `priority` | [turns-and-todo.md](turns-and-todo.md) |
| `claims` | [tool-failures.md](tool-failures.md) |
| `computer`, `files` | [computer.md](computer.md) |
| `memory`, `select`, `consolidate`, `audit` | [memory.md](memory.md) |
| `history` | [history.md](history.md) |
| `maintenance` | [maintenance.md](maintenance.md) |
| `reflection` | [reflection.md](reflection.md) |
| `drives`, `goals` | [drives-and-goals.md](drives-and-goals.md) |
| `calendar`, `owner_sleep` | [calendar.md](calendar.md) |
| `voice`, `bridge` | [voice-and-bridge.md](voice-and-bridge.md) |
| `log` | [boot-and-logs.md](boot-and-logs.md) |
| `dashboard`, `api` | [local-api-and-dashboard.md](local-api-and-dashboard.md) |

The Discord bridge reads its own strict `bridge/discord.toml`; `dollcore` never reads it because it holds the bot token.

## Config

- `[core] data_dir` — `data`; all her state.
- `[core] pack` — `character_packs/template/doll.toml`.
- `[core] docs` — `docs`; this manual.
- `[core] mlx_cache_limit_mb` — `256`; one MLX buffer cache limit for embedding and voice.

## Invariants

- Unknown sections and keys, wrong types and out-of-range values fail the boot.

## Known limitations

- An omitted optional key silently takes its default; only a misspelled one fails.
- A misspelled optional pack key is silently unused.
- Arrays must fit on one line.

## Tests

`configRejectsUnknownKeysAndBadPriority`, `apiAndBridgeConfigAreValidated`, `dashboardConfigRejectsUnknownKeysAndBadValues`, `consolidateConfigIsValidated`, `selectConfigIsValidated`, `ownerSleepConfigIsStrict`, `oldDataDirsBootAndTheGoalsConfigIsStrict`, `theExampleDrivesSectionIsTheDefaultsAndBadValuesAreRefused`, `theComputerSwitchIsReadStrictlyAndOnByDefault`, `aPacksExampleDialoguesFollowItsIdentityUnderAFixedHeader`.
