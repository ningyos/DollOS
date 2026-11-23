# Local API, dashboard, dollctl

`dollcore` serves two hand-written HTTP/1.1 listeners (Network.framework, one request per
connection). The local API (`[api] port`) serves `dollctl` (the developer) and the Discord bridge
(the owner). The dashboard (`[dashboard] port`) is the owner's web page showing the doll's state,
with a few operations and edits.

## What the doll sees and does

- **The developer** speaks through `dollctl chat`. His lines are tagged
  `[HH:MM 開發者｜終端機｜文字]`, distinct from the owner's `主人`. She answers him only with
  `dollctl.reply`; `discord.dm` goes to the owner. With no chat open, `dollctl.reply` fails with
  `沒有開著的 dollctl chat` (see [tool-failures.md](tool-failures.md)).
- **Memory in developer turns** is `public` + `own`, never `secret`; memories from his words get
  tier `developer` (see [memory.md](memory.md)). A developer line never cancels her model call in
  flight.
- **The dashboard** shows the owner everything: her window reads, actions and results, memories
  (secret ones decrypted), calendar, signals, goals, diary and self-knowledge (sealed entries
  opened), saved tools, boot reports and change history.
- **His dashboard edits reach her only as program notices, never as his words** (calendar: a
  【行事曆】 notice with no turn, see [calendar.md](calendar.md); memory reverts, undone changes,
  ideal-self and goal edits: see [memory.md](memory.md), [history.md](history.md),
  [drives-and-goals.md](drives-and-goals.md)). An interrupt cancels her model call; that turn gets no
  reply. Pause stops new turns; events keep queueing.

## How it works

**Local API** (`LocalAPI.swift`). Binds `127.0.0.1:<[api] port>` only (`requiredLocalEndpoint`,
not configurable); `LocalAPI.accept` drops any non-loopback peer with a WARN. Port 0 picks a free
port (tests). `LocalAPI.ownerKey` is 32 random bytes (hex), new per launch, passed only to the bridge
as `DOLL_BRIDGE_TOKEN`; owner routes need `Authorization: Bearer <key>` (`LocalAPI.authorized`) or
get 401.

| Route | Auth | Answer |
|---|---|---|
| `GET /api/status` | none | `ApiStatus` JSON |
| `POST /api/developer {"text":…}` | none | `{"queued":true}`; `Doll.developerSaid` |
| `POST /api/owner {"text":…}` | owner | `{"queued":true}` only after `Doll.ownerLine` wrote the line sealed to the inbox; else 500 `owner line not recorded: …` |
| `GET /api/listen?to=developer` | none | NDJSON stream; any number |
| `GET /api/listen?to=owner` | owner | NDJSON stream; one at a time, else 409 `an owner stream is already open` |
| `GET /api/voice` | owner | WebSocket; 503 with voice off; 409 while a live one is open (one that misses a ping is replaced) |

Bad bodies are 400, unknown paths 404, requests before `start` 503. Requests are capped at 256 KiB
(`LocalAPI.maxRequest`), WebSocket messages at 1 MiB (`LocalAPI.maxVoiceMessage`). The inbox and
secret check after an owner line are in [secrets.md](secrets.md).

Stream lines: `{"type":"hello"}`, `{"type":"say","text":…}` (owner stream may add `"image"`, an
absolute screenshot path), `{"type":"typing","on":…}`. `LocalAPI.deliver` returns why a `say` went
nowhere when no stream is open; that becomes her tool's failure. Typing (`LocalAPI.followTurns`)
follows `Doll.answering()`. One owner stream only: two bridges would split her words.

`ApiStatus`: `name`, `footer` (her 【狀況】), `state` (`thinking|acting|idle|maintenance`),
`maintenance`, `connection` (`Doll.connectionLine`), `paused`, `queued`, `turn`, `answering`,
`bridge` (`LocalAPI.bridge`), owner/developer stream counts. `ApiStatus.text` renders it.

**dollctl** (`Sources/DollCtl/main.swift`): `dollctl [--config <file.toml>] chat | status`. Without
`--config` it uses `ApiConfig().port`; with it, it loads the config (strictly) for `[api] port`.
`chat` prints each `say` as `[HH:MM <name>] …`, typing as `（<name> 在想…）`, posts each non-empty
stdin line, and prints `（沒送出：…）` on a refused post. Use it for local testing; owner routes are
reserved for the bridge.

**Dashboard** (`Dashboard.swift`, page in `DashboardPage.swift`). One HTML page with inline CSS/JS
plus JSON routes. `ChangeFeed` (`Doll.feed`) is notified on `event`, `turn`, `queue`, `pause`,
`interrupt`, `calendar`, `maintenance`, `drives`, `history`, `connection`; `GET /events` streams
`event: change` (after a `hello`), coalescing bursts, and the page refetches every route on each
change. `Dashboard.respond` only routes; logic lives in `Doll.dashboard…` methods.

Routes (`Dashboard.routes`; a path ending in `/` matches by prefix):

| Route | Does |
|---|---|
| `GET /` | the page |
| `GET /api/status` | `DashboardStatus`: state, todo, plan, estimates, effort, queue, paused, last interrupt, `computer` |
| `GET /api/timeline` | newest `[dashboard] timeline_items` `DashboardItem`s, newest first |
| `GET /api/vision` | `DashboardVision`: last screenshot + element list |
| `GET /shots/<name>` | screenshot file (`Dashboard.validShotName`: plain ASCII name only) |
| `GET /api/memory` | `DashboardMemory`: folders, strengths, deep, attached, corrections |
| `GET /api/calendar`, `/api/drives`, `/api/goals`, `/api/tools`, `/api/history` | read views |
| `GET /api/reflection` | `DashboardReflection`: newest `[reflection] dashboard_days` diary entries (sealed opened), self items with stats and goal, `habitsMax`, `quietDays`; `enabled: false` unless `[reflection]`, `[maintenance]` and `[drives]` are all on |
| `GET /api/boots` | newest `[dashboard] boot_reports` boot reports ([boot-and-logs.md](boot-and-logs.md)) |
| `GET /api/history/diff/<id>` | one change's diff, secrets included |
| `POST /api/interrupt` | cancel the model call in flight (409 if none) |
| `POST /api/pause`, `/api/resume` | stop / resume starting turns |
| `POST /api/memory/deep/<id>`, `/undeep/<id>` | mark a memory deep or not |
| `POST /api/memory/revert/<fix id>` | revert a memory correction |
| `POST /api/calendar/add\|change\|delete?…` | calendar edit with her tool's parameters |
| `POST /api/ideal/edit?text=`, `/revert?id=` | edit / revert her ideal self |
| `POST /api/goals/add\|change\|done\|drop?…` | goal tree edit |
| `POST /api/record/eval?id=&kind=good\|bad\|none` | the owner's evaluation of a record |
| `POST /api/tools/delete?name=` | delete a saved tool |
| `POST /api/history/undo/<id>`, `/restore/<id>` | undo one change / return to right after one |

Unknown path 404, wrong method 405. `MemoryError`, `CalendarError`, `ToolError`, `HistoryError`
map to 409 (a missing diff 404).

Page sections: 視覺, 動作, 她怎麼想, 記憶, 行事曆, 她的狀態, 她的方向, 她的日記、長處和短處, 她的工具,
開機紀錄, 改動紀錄. 視覺 and 她的工具 are hidden when `[computer] enabled = false`. The diary
section lists strengths, weaknesses (with habit count against `[reflection] habits_max` and the quiet-days mark),
past items, then the diary entries, each marked when stored sealed; it has no edit controls (the
owner undoes through 改動紀錄).

## Config

- `[api] port` (8791) — local API port; the address is always 127.0.0.1.
- `[dashboard] enabled` (true) — serve the dashboard.
- `[dashboard] port` (8790) — 1…65535.
- `[dashboard] bind` ("127.0.0.1") — listen address; anything else exposes her secrets.
- `[dashboard] timeline_items` (200) — items `/api/timeline` returns.
- `[dashboard] boot_reports` (10) — reports `/api/boots` returns.
- `[reflection] dashboard_days` (30) — diary entries `/api/reflection` returns.

## Invariants

- Local API is loopback-only in code; owner routes need the per-launch key.
- At most one owner stream and one live voice socket.
- `POST /api/owner` answers 200 only after the line is sealed on disk.
- Dashboard POST routes are exactly the listed operations; none puts text into her conversation as
  a person, and none writes a memory's text.

## Known limitations

- The dashboard has no authentication and no loopback check.
- `dollctl` without `--config` assumes port 8791.
- HTTP parsing is minimal: no keep-alive, no chunked bodies; clients must send `Content-Length`.
- `DashboardItem` still has a `watch` type for old timelines; nothing writes watch events now.

## Tests

`localAPIIsLoopbackOnlyAndOnlyTheBridgeSpeaksAsTheOwner`,
`theBridgeMustComeUpAndItsExitIsASystemError`, `dashboardEndpointsShowAScriptedRunAndChangeNothing`,
`dashboardConfigRejectsUnknownKeysAndBadValues`, `theOwnersDashboardEditIsAProgramEventWithoutATurn`,
`theDashboardShowsAndEditsHerDirectionAndSheIsTold`,
`theOwnerRevertsACorrectionOnTheDashboardAndSheIsTold`,
`theOwnerUndoesAChangeOfHersOnTheDashboardAndSheIsToldInPlainWords`.
