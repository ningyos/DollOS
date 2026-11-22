# Configuration

How `dollcore` reads its settings: one TOML file passed as `dollcore --config <file.toml>`, parsed by
a small strict parser (`Sources/DollCore/TOML.swift`) into a typed `Config`
(`Sources/DollCore/Config.swift`). The character pack (`doll.toml`) goes through the same parser.

Related: [testing.md](testing.md) (test configs and throwaway dolls), [secrets.md](secrets.md)
(`[memory] keychain_service`), [history.md](history.md) (`[history]`).

## The parser: `TOML.parse`

`TOML.parse(_:)` returns `[section: [key: TOMLValue]]`. It accepts only this subset:

- `[section]` headers with a bare name (ASCII letters, digits, `_`, `-`). No dotted or quoted
  names, no `[[array tables]]`.
- `key = value` lines inside a section. A key before the first header is an error.
- `#` comments, on their own line or after a value.
- Values (`TOMLValue`): basic strings `"..."` (escapes `\"`, `\\`, `\n`, `\t` only), multi-line
  basic strings `"""..."""` (a newline right after the opening quotes is trimmed), integers,
  floats (a word containing `.`), `true`/`false`, and single-line arrays of those (no nested
  arrays).
- `\r\n` is normalized to `\n`.

Everything else throws `TOMLError` with `line N: …`: literal strings `'...'`, inline tables, dates,
duplicate sections, duplicate keys, unknown escapes, unterminated strings.

Why a hand-written subset: the files only need these forms, and a strict parser turns a typo into a
boot error instead of a silently ignored setting.

## `Config.load`

`Config.load(_:)` parses the file and builds one struct per section. The struct defaults are the
Swift initial values in `Config.swift`; the file overrides them.

### Strictness (invariants)

- **Unknown section** → `TOMLError("unknown sections: …")`. The known list is the literal set in
  `Config.load` (22 sections: `llm core context computer files idle memory dashboard api bridge
  consolidate select voice calendar maintenance drives owner_sleep goals audit claims log history`).
- **Unknown key** → `TOMLError("[section] unknown keys: …")`. Each section is read through the
  private `Section` helper: every `read` removes its key, and `Section.finish()` throws if anything
  is left.
- **Wrong type** → `"[section] key must be a string / true or false / an integer / a number /
  an array of strings"`. An integer is accepted where a `Double` is expected; a float is never
  accepted where an `Int` is expected.
- **Out-of-range values** → one `guard` per section after `finish()`, with a message listing every
  constraint of that section (for example `[context] limit at least 1000, …`).
- **Required keys**: `[llm] base_url` and `[llm] model` (via `Section.required`), and one
  `[memory] confidence_<tier>` per `SourceTier` case (`owner developer tool stranger doll program
  self_reported consolidated`), each 0…1. A missing `[llm]` section is an error.
- **Every other key is optional** and falls back to its Swift default. "Strict" here means unknown,
  mistyped or out-of-range values fail; it does not mean every key must be present.

Any `TOMLError` from `Config.load` stops the boot (`dollcore` prints it and exits). There is no
"best effort" load.

### Cross-key validations worth knowing

- `[core] priority` must list each `Priority` raw value exactly once: `owner_voice`, `owner_text`,
  `developer_text`, `calendar`, `watch`, `wander`. Adding a `Priority` case therefore breaks every
  existing config file until its `priority` lists the new kind (including `dollcore.example.toml`).
  `watch` has no producer any more but stays so old config files keep booting.
- `[drives] order` must list each `Signal` (`drive`, `stress`, `closeness`) exactly once
  (`Config.readDrives`).
- `[api] port` must differ from `[dashboard] port` when the dashboard is enabled.
- `[bridge] command`, when `[bridge] enabled`, must be an array whose first element is an absolute
  path.
- `[llm] retry_first_s` > 0 and `retry_max_s` ≥ `retry_first_s`.
- `[llm] default_effort`, `[llm] task_turn_effort`, `[context] talk_effort`,
  `[consolidate] effort` must be one of `Effort.levels` (`none`, `medium`, `high`).
- `[memory]`: the four folder names (`secret_dir`, `own_dir`, `public_dir`, `registry_dir`) must be
  distinct; `high_confidence` > `medium_confidence`.
- `[maintenance] time`, `[owner_sleep] start`/`end` must parse as `TimeOfDay` (`HH:MM`);
  `start` ≠ `end` (an end before the start runs over midnight).
- `[audit] mode` is `change` or `flag`; `[voice] tts_precision` is `int8` or `bf16`.

### Checks that happen at boot, not in `Config.load`

`Doll.init` validates paths the config names:

- `[core] docs`: the folder must contain `README.md`, else
  `TOMLError("[core] docs: …/README.md does not exist")`.
- `[core] pack`: parsed with `TOML.parse` (see below).
- `[history] git` missing on disk is a boot error ([history.md](history.md)).

### Relative paths

Relative `[core] data_dir`, `[core] pack`, `[core] docs` and `[memory] dir` resolve against the
**current working directory** of the process (`root` in `Sources/DollCLI/main.swift`), not the
config file's folder. `Config.dataDirectory(root:)` implements this for `data_dir`. `[history] dir`
and `[voice] data_dir` are relative to `[core] data_dir`. Absolute paths are taken as they are.

`dollctl --config <file>` reads only `[api] port` from the file; without `--config` it uses
`ApiConfig().port` (8791).

## The character pack: `doll.toml`

`[core] pack` names a `doll.toml` (default `character_packs/template/doll.toml`). `Doll.init`
parses it with `TOML.parse` and reads:

| Key | Required | Use |
|---|---|---|
| `[meta] name` | yes (string) | her name (`Doll.name`) |
| `[meta] aliases` | no (array) | other names she answers to |
| `[identity] self`, `personality`, `taboos` | yes (strings) | joined into the identity part of the system prompt |
| `[identity] examples` | no (string) | example dialogues, prefixed by `Doll.examplesHeader` |
| `[identity] ideal` | yes when `[drives] enabled` and no ideal-self file exists yet | the first version of her ideal self, written once |

Unlike `Config.load`, the pack is **not** checked for unknown keys or sections: other keys (for
example the template's `[meta] id`, `[meta] language`) are parsed and ignored.

- `character_packs/template/doll.toml`: the shipped template, deliberately without a personality.
  Owners copy it elsewhere, fill it in and point `[core] pack` at the copy (absolute path).
- `Tests/DollCoreTests/Fixtures/xiaodeng/doll.toml`: the test fixture (小燈) with every field
  filled; the tests' `exampleConfig()` swaps it in.

Tests pin SHA-256 digests of the tools list and the instruction texts (`enabledRequestDigests` in
`DollCoreTests.swift`); changing prompt text means re-pinning them, see
[turns-and-todo.md](turns-and-todo.md).

## Where the reasons live

`dollcore.example.toml` holds every section and every behavior number with a comment next to it
(in Traditional Chinese) giving the reason: a measurement, a decision, or an "initial value reason"
(`初始值理由`). The Swift doc comments on the config structs repeat the important ones. When you
change a number, change the comment with it; when you add a key, add the reason.

The example ships with `[bridge] enabled = false` and `[voice] enabled = false`; `[llm] base_url`
and `[llm] model` are placeholders the owner fills in (SETUP.md walks through it).

Only `[drives]` is tested for example-equals-default (`exampleConfig().drives == DrivesConfig()`
in `theExampleDrivesSectionIsTheDefaultsAndBadValuesAreRefused`). For other sections the example
value and the Swift default can drift apart unnoticed; keep them equal by hand.

## How to add a config key

1. Add a `public var` with its default to the section's struct in `Config.swift` (for example
   `ComputerConfig`), with a doc comment saying what it governs.
2. In `Config.load` (or `Config.readDrives` / `Config.readGoals`), add
   `try <section>.read("snake_case_key", &field)` **before** that section's `finish()`. If you
   forget this, the key is "unknown" and every config that sets it fails to boot.
3. Extend that section's range `guard` and its error message.
4. Add the key to `dollcore.example.toml` in its section, same value as the Swift default, with the
   reason comment.
5. If the doll is told about the behavior, update the matching `dolldocs/*.md`.
6. Add a test in `Tests/DollCoreTests/DollCoreTests.swift` following the existing pattern
   (`sequenceMaxStepsIsReadFromTheConfigAndZeroIsRefused`,
   `theComputerSwitchIsReadStrictlyAndOnByDefault`): check `exampleConfig()` has the value, write a
   temp config with a bad value and `#expect(throws: TOMLError.self) { try Config.load(file) }`.

Adding a whole section also means adding its name to the known-section set in `Config.load` and a
stored property on `Config`.

Removing a key is a breaking change for every owner's config file (it becomes "unknown"). There
are no aliases or deprecated keys; migrate the example and say so in the change.

## The bridge's own config

The Python bridge reads its own file (`bridge/discord.toml`, copy of `bridge/discord.example.toml`)
with `tomllib`; `load_config` in `bridge/src/dollbridge/__main__.py` also rejects unknown keys.
`dollcore` never reads that file (it holds the bot token). See
[voice-and-bridge.md](voice-and-bridge.md).

## Known limitations

- Missing optional keys silently take the Swift default; a misspelled key fails, but an omitted
  one does not.
- The pack parser ignores unknown keys, so a misspelled optional pack key (`example` for
  `examples`) is silently unused.
- Arrays must fit on one line.
