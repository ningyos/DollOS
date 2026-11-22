# Local API, dashboard, dollctl

dollcore serves two HTTP listeners, both hand-written on Network.framework (HTTP/1.1, one
request per connection, `Connection: close`, `Cache-Control: no-store`):

| listener | file | config | who uses it |
|---|---|---|---|
| local API | `Sources/DollCore/LocalAPI.swift` | `[api] port` (8791) | `dollctl` (developer), the Discord bridge (owner) |
| dashboard | `Sources/DollCore/Dashboard.swift`, `DashboardPage.swift` | `[dashboard] enabled`, `port` (8790), `bind`, `timeline_items`, `boot_reports` | the owner, in a browser |

`dollctl` is `Sources/DollCtl/main.swift`. The bridge side is in
[voice-and-bridge.md](voice-and-bridge.md).

## Local API (`LocalAPI.swift`)

**Loopback only, not configurable.** `LocalAPI.start` binds `127.0.0.1:<[api] port>`
(`requiredLocalEndpoint`), and `LocalAPI.accept` closes any connection whose peer is not
loopback (`LocalAPI.isLoopback`) without answering, with a WARN line. Port 0 picks a free port
(tests). A bind failure is a boot failure.

**Owner key.** `LocalAPI.ownerKey` is 32 random bytes (hex), generated per launch and handed only
to the bridge as `DOLL_BRIDGE_TOKEN`. Owner routes need `Authorization: Bearer <key>`
(`LocalAPI.authorized`, constant-time compare); without it they answer 401. Nothing else on the
machine can speak as the owner. Developer routes need no key.

Routes:

| route | auth | answer |
|---|---|---|
| `GET /api/status` | none | `ApiStatus` JSON (snake_case) |
| `POST /api/developer {"text":…}` | none | `{"queued":true}`; a developer line (`Doll.developerSaid`) |
| `POST /api/owner {"text":…}` | owner key | `{"queued":true}` only after the line is on disk; 500 `owner line not recorded: …` otherwise |
| `GET /api/listen?to=developer` | none | NDJSON stream; any number of listeners |
| `GET /api/listen?to=owner` | owner key | NDJSON stream; **one at a time** (a second gets 409 `an owner stream is already open`) |
| `GET /api/voice` | owner key | WebSocket upgrade; 503 when `[voice] enabled = false`; 409 while a live one is open |

Empty or non-`{"text": …}` bodies are 400; unknown paths 404; requests before `start` 503.
Request size is capped at 256 KiB (`LocalAPI.maxRequest`); WebSocket messages at 1 MiB
(`LocalAPI.maxVoiceMessage`).

**Durable before ack.** `POST /api/owner` calls `Doll.ownerLine`, which writes the line sealed to
the inbox (`Inbox.swift`) before returning; only then does the API answer 200. The secret check
and queueing happen afterwards; the bridge is not kept waiting for the check. A write failure is
a 500 and the bridge tells the owner in the DM. See [secrets.md](secrets.md) and
[turns-and-todo.md](turns-and-todo.md).

**Streams.** Lines are `{"type":"hello"}` first, then `{"type":"say","text":…}` (optionally
`"image": "<absolute screenshot path>"`, owner stream only) and `{"type":"typing","on":true|false}`.
`LocalAPI.deliver` queues a `say` on every open stream for the addressee and returns why it went
nowhere when none is open — this is how her `discord.dm` / `dollctl.reply` learn the message was
not delivered (`Discord 送不出去：bridge <state>` / `沒有開著的 dollctl chat`), and she is told in
her tool result. Typing (`LocalAPI.followTurns`) follows `Doll.answering()`: on for whoever's line
the running turn follows, off when the turn ends. A stream ends when either side closes; the
client never sends after its request, so any completed read means it closed.

Reason for one owner stream: two bridges would split her words between them.

**`ApiStatus`**: name, footer (her 【狀況】 status footer), state
(`thinking|acting|idle|maintenance`), maintenance, connection (model server unreachable line),
paused, queued, turn, answering, bridge (state string from `Bridge`), owner/developer stream
counts. `ApiStatus.text` renders it for `dollctl status`.

## dollctl (`Sources/DollCtl/main.swift`)

```
dollctl [--config <file.toml>] chat     # chat as the developer
dollctl [--config <file.toml>] status   # ApiStatus.text
```

Without `--config` it uses the default `[api] port` (`ApiConfig().port`); with it, it loads the
config (strictly, like dollcore) only to read `[api] port`. It talks to `http://127.0.0.1:<port>`.

`chat` opens `GET /api/listen?to=developer`, prints each `say` as `[HH:MM <name>] …` and `typing`
on as `（<name> 在想…）` ("… is thinking"), and posts each non-empty stdin line to
`POST /api/developer`. A refused post prints `（沒送出：…）` ("not sent"). Ctrl-D/Ctrl-C ends it; the
stream closing exits with an error.

### The developer identity

A developer line is a distinct speaker, not the owner:

- In her context it is tagged `[HH:MM 開發者｜終端機｜文字]` (developer | terminal | text) by
  `Doll.speakerTag`, versus `主人` (owner) for owner lines; the event's channel is
  `Channel.dollctl`.
- She answers with `dollctl.reply` (`Tools.Name.reply`), which reaches only open `chat`
  sessions; `discord.dm` goes to the owner.
- Memory visibility: turns answering the developer use `Audience.developer`, which
  `Memory.visible` maps to `public` + `own` — never `secret`, even if the owner's secrets would be
  visible in an owner turn. A turn woken later by a watch the developer's task set up still
  counts as the developer's.
- Memories from developer words get tier `developer` with `[memory] confidence_developer`.
- Priority: `developer_text` in `[core] priority` (example: after `owner_text`). A developer
  line does not cancel a model call in flight; an owner voice line does.

Use `dollctl chat` for local testing instead of faking owner input: the owner routes are reserved
for the bridge by design.

## Dashboard (`Dashboard.swift`, `DashboardPage.swift`)

One page (`DashboardPage.html`, inline CSS and JS, served from the binary at `/`) plus JSON
routes, live-updated over Server-Sent Events.

**Bind.** `[dashboard] bind` (example `127.0.0.1`) and `port`. Unlike the local API there is no
loopback check in code: the page shows the owner's secret memories (decrypted) and her own
passwords, so exposing it on another interface is the operator's explicit, risky choice.
There is no authentication.

**Live updates.** `ChangeFeed` (owned by `Doll`, `Doll.feed`) is notified on events, turns, queue
changes, pause/resume, interrupts, calendar, maintenance, drives, history and connection changes
(`ChangeFeed.Change`). `GET /events` streams `event: change` / `data: {"what": …}` with a first
`hello` on connect; bursts coalesce (a slow client gets only the newest), and the page refetches
everything on each change.

**Routes** (`Dashboard.routes`; a path ending in `/` matches by prefix):

| method, path | does |
|---|---|
| `GET /` | the page |
| `GET /api/status` | `DashboardStatus`: state, todo phase/items, plans and estimates, effort, queue, paused, last interrupt, `[computer] enabled` |
| `GET /api/timeline` | newest `[dashboard] timeline_items` `DashboardItem`s (turns with calls/results, owner/developer lines, watch wakes, program events) |
| `GET /api/vision` | `DashboardVision`: last window screenshot + element list |
| `GET /shots/<name>` | a screenshot file (`Dashboard.validShotName` rejects anything but a plain ASCII name) |
| `GET /api/memory` | `DashboardMemory`: folders, strengths, deep marks, attached experiences, corrections |
| `GET /api/calendar`, `/api/drives`, `/api/goals`, `/api/tools`, `/api/history` | read views |
| `GET /api/boots` | newest `[dashboard] boot_reports` boot reports |
| `GET /api/history/diff/<id>` | one change's diff, secret memories included |
| `POST /api/interrupt` | cancel the model call in flight (409 if none) |
| `POST /api/pause`, `/api/resume` | stop / resume starting turns; events keep queueing |
| `POST /api/memory/deep/<id>`, `/undeep/<id>` | mark a memory deep or not |
| `POST /api/memory/revert/<fix id>` | revert a memory correction; she is told |
| `POST /api/calendar/add|change|delete?…` | calendar edit (her tool's parameters in the query); a 【行事曆】 program event, no turn |
| `POST /api/ideal/edit?text=`, `/api/ideal/revert?id=` | edit/revert her ideal self |
| `POST /api/goals/add|change|done|drop?…` | goal tree edit |
| `POST /api/record/eval?id=&kind=good|bad|none` | the owner's evaluation of a record |
| `POST /api/tools/delete?name=` | delete one of her saved tools |
| `POST /api/history/undo/<id>`, `/api/history/restore/<id>` | undo one change / return to the state right after one |

Unknown path → 404, known path with the wrong method → 405. Domain errors (`MemoryError`,
`CalendarError`, `ToolError`, `HistoryError`) map to 409 (404 for a missing diff) with their text.

Page sections (`<h2>` titles): 視覺 (vision), 動作 (actions/timeline), 她怎麼想 (plans, estimate
vs actual, failure causes), 記憶 (memory), 行事曆 (calendar), 她的狀態 (signals, concerns),
她的方向 (ideal self, goal tree, record), 她的工具 (her tools), 開機紀錄 (boot reports),
改動紀錄 (change history). Vision and tools are hidden when `[computer] enabled = false`.

**Rule: the dashboard cannot speak to her or write a memory's text.** The only POST routes are
system operations and owner edits; each edit that concerns her is delivered as a program event
(told, not a turn), never as an owner line. Domain logic lives in `Doll` (`Doll.dashboard…`
methods in the respective files); `Dashboard.respond` only routes.

## Invariants

- Local API: loopback-only, enforced in code; owner routes require the per-launch key.
- At most one owner stream and one voice socket.
- `POST /api/owner` answers 200 only after the line is durably (sealed) on disk.
- Dashboard POST routes are exactly the listed operations; no route injects text into her
  conversation as a person.

## Known limitations / pitfalls

- The doc comment on `DashboardPage` still says the page can only send interrupt and
  pause/resume; the page also posts all the edit routes above. Trust `Dashboard.routes`.
- The dashboard has no auth and no loopback check; changing `[dashboard] bind` exposes secrets.
- `dollctl` without `--config` assumes port 8791; a non-default `[api] port` needs `--config`.
- HTTP parsing is minimal (no keep-alive, no chunked bodies, headers lower-cased into a
  dictionary); clients must send `Content-Length`.
- Tests: `localAPIIsLoopbackOnlyAndOnlyTheBridgeSpeaksAsTheOwner`,
  `theBridgeMustComeUpAndItsExitIsASystemError`, `dashboardEndpointsShowAScriptedRunAndChangeNothing`
  in `Tests/DollCoreTests/DollCoreTests.swift` (see [testing.md](testing.md)).
