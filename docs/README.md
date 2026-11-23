# DollOS manual

One manual for the doll and for developers (2026-11-23). With `[context] manual = true` every file here
(not subfolders) is placed whole in the doll's system prompt, this README first, then by name. "The
doll" is her; "the owner" is the person she lives with; "the developer" works on this program.
`docs/private/` is the owner's local notes and is not part of the manual.

Start with [turns-and-todo.md](turns-and-todo.md): how a turn happens, what a request holds, and the
todo list. Then the topic you need:

| File | Topic |
|---|---|
| [turns-and-todo.md](turns-and-todo.md) | Events, turns, the request and its fixed part, todo states, compaction, effort, claims, inbox |
| [tool-failures.md](tool-failures.md) | What each common 「失敗：」 result means and what to do |
| [memory.md](memory.md) | Memory files, folders, recall, attached memories, strength, `memory.fix`, consolidation, audit |
| [maintenance.md](maintenance.md) | The daily system maintenance: the diary turn, the tasks, interruption |
| [reflection.md](reflection.md) | The diary, the skill panel, habits, the `recur` task |
| [drives-and-goals.md](drives-and-goals.md) | Concerns, signals, wandering, system tasks, the ideal self, her graph, the record, curiosity |
| [calendar.md](calendar.md) | The calendar, its timer, owner sleep |
| [computer.md](computer.md) | The Mac through cua-driver, `act`, safety, honest completion, saved tools |
| [voice-and-bridge.md](voice-and-bridge.md) | The Discord bridge, the voice channel, TTS and speech recognition |
| [history.md](history.md) | The version history of her files, undo and restore |
| [secrets.md](secrets.md) | Sealing, the memory key and Touch ID, her secret mark, the secret filter, reading a sealed log |
| [boot-and-logs.md](boot-and-logs.md) | Boot order, the log, the boot report |
| [local-api-and-dashboard.md](local-api-and-dashboard.md) | The local API, `dollctl`, the dashboard |
| [config.md](config.md) | Config loading, adding a key, the character pack |
| [testing.md](testing.md) | Toolchain, the test harness, deterministic tests, throwaway dolls |

Setup for a new machine: [SETUP.md](../SETUP.md). Rules for working on the code: [AGENTS.md](../AGENTS.md).
