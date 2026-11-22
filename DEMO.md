# Running a demo locally

This file used to be the developer log of the rewrite (in Chinese). Its contents now live in
[AGENTS.md](AGENTS.md) and [docs/dev/](docs/dev/); the measurements behind the decisions are kept
privately. Comments in the code and in `dollcore.example.toml` that say "see DEMO.md" refer to the
sections listed at the end of this file.

## Run her

Install first ([SETUP.md](SETUP.md)): Xcode 27, the Metal toolchain, a character pack, a model
server with tool calling.

```bash
cp dollcore.example.toml dollcore.toml          # then set [llm], [core] pack, …
swift run dollcore --config dollcore.toml       # runs until Ctrl-C (SIGINT) or SIGTERM; no stdin
# in another terminal:
swift run dollctl --config dollcore.toml chat   # talk to her as the developer
swift run dollctl --config dollcore.toml status # her state, the bridge, 【狀況】
```

The dashboard is at `http://127.0.0.1:<[dashboard] port>/` (default 8790). Without `--config`,
`dollctl` uses `127.0.0.1:8791` (the default `[api] port`).

To talk to her on Discord, set up a bot and the bridge
([docs/dev/voice-and-bridge.md](docs/dev/voice-and-bridge.md)) and set `[bridge] enabled = true`.

To try something without touching your own doll, run a throwaway doll with its own data dir,
ports and Keychain service ([docs/dev/testing.md](docs/dev/testing.md#running-a-throwaway-doll)).

`swift run dollcore --config dollcore.toml consolidate <data_dir>` consolidates a stopped doll's
day into memory by hand; her running process does this in its daily maintenance.

## Where the old sections went

| Old section | Now |
|---|---|
| 跑起來 (running, toolchain, embedding at boot, config format) | this file, [testing.md](docs/dev/testing.md), [config.md](docs/dev/config.md) |
| 前端: local API, `dollctl`, speaker tags | [local-api-and-dashboard.md](docs/dev/local-api-and-dashboard.md), [turns-and-todo.md](docs/dev/turns-and-todo.md) |
| Traditional conversion, claims check, where she speaks, tool-name and `tools` measurements | [turns-and-todo.md](docs/dev/turns-and-todo.md) |
| Discord bridge, real bot setup, voice channel, voice protocol | [voice-and-bridge.md](docs/dev/voice-and-bridge.md) |
| Boot notice, log file, boot report | [boot-and-logs.md](docs/dev/boot-and-logs.md) |
| 怎麼運作: events, todo, compaction, context | [turns-and-todo.md](docs/dev/turns-and-todo.md) |
| Calendar, owner sleep | [calendar.md](docs/dev/calendar.md) |
| System maintenance | [maintenance.md](docs/dev/maintenance.md) |
| Concerns, signals, wander, goals, ideal self | [drives-and-goals.md](docs/dev/drives-and-goals.md) |
| Memory, registry, selection, strength, `memory.fix`, audit | [memory.md](docs/dev/memory.md) |
| Change history | [history.md](docs/dev/history.md) |
| Secret encryption, secret check | [secrets.md](docs/dev/secrets.md) |
| 電腦 (the computer): driver, reads, `act`, safety, auth windows, own tools | [computer.md](docs/dev/computer.md) |
| Staying alive without the model, the inbox | [turns-and-todo.md](docs/dev/turns-and-todo.md) |
| 驗證 (verification), suite flakiness, a real disconnect | [testing.md](docs/dev/testing.md) |
