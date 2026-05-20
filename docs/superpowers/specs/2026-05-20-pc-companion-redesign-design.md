# PC Companion Redesign

**Status**: Approved direction, ready for implementation plan
**Pivot**: Supersedes parts of `2026-05-01-dollos-pivot-to-computer-design.md` (the "Doll lives on your computer, phone is optional body" framing)

## Goal

Lock the PC-side interaction model for Doll: she's a **passive same-living AI** that lives on an always-on machine, surfaces on the PC as a Live2D tray companion, talks by click-to-talk voice, only opens her mouth when reminders fire or you assign work, and runs file/computer-use tasks with destructive-action consent gating.

Phone app is explicitly out of scope for this round. Wake word, screen monitoring, ambient mic — all out of scope.

## Why this redesign

Previous direction was muddy. Three concrete shifts:

1. **Brain is no longer "on the PC"** — it runs on whatever machine the user keeps on (PC, NAS, home server). PC is just a client. This means Doll survives PC reboots and "PC asleep" doesn't kill her.
2. **No KWS, no always-on mic.** Click Live2D to talk. The wake-word stack stays in repo for future phone work, but is not part of the PC client.
3. **Doll is passive, not observant.** She does not watch the screen, does not listen ambiently, does not proactively probe data sources. She only speaks on **schedule/reminder triggers** or when the user initiates.

## Architecture

```
┌─ Brain (daemon) ────────────────────────────┐
│  Python, runs on always-on machine          │
│  Event loop / Memory / LLM / Cascade        │
│  Skills / Tools / Schedule / Reminders      │
│  IPC server (WS, localhost or LAN)          │
└──────────────────────────────────────────────┘
                   ▲ WS
                   ▼
┌─ PC Client (Tauri + Cubism Web) ─────────────┐
│  Tray icon (persistent)                       │
│  Live2D overlay (toggleable)                  │
│  Chat window (hotkey-summoned)                │
│  Voice I/O (click-to-talk, VAD endpoint)     │
│  File access + computer use actuators        │
│  Consent gate for destructive actions        │
└──────────────────────────────────────────────┘
```

**Deployment flexibility**: daemon binds to a network port. Common cases:
- Brain box == PC (single machine, daemon as user service)
- Brain box == NAS / home server (LAN connection via Tailscale / mDNS / explicit host)

Client is identical in both cases. No "embedded" mode.

### Critical architectural consequence — actuators move client-side

Because the brain box can be a different machine from the PC, **all actuators that touch the user's PC must live in the client, not the daemon**:

- File access on the user's PC — client-side tool (daemon calls `client.fs.read(path)` over WS)
- Mouse/keyboard simulation — client-side only (NAS can't move PC's mouse)
- Window management, app launch, OS notification — client-side only
- Screen observation (future) — client-side only

The daemon retains:
- Its own filesystem (memory store, character packs, logs) — daemon-side
- Network calls (web fetch) — daemon-side, no user-PC dependency
- Subagent / shell against the daemon's own host — daemon-side (renamed from "Shell" to "BrainShell" to disambiguate)

This means the existing daemon-side Shell tool needs to split into two tools: **BrainShell** (daemon's host) and **HostShell** (user's PC, via client). The client must implement these tools as IPC-callable actuators. This is a real restructure, not a cosmetic rename — implementation plan must own it.

## Components

### Daemon (no major changes from current architecture)

Existing event loop + memory + cascade + skills stays. The only daemon-side surface change is:
- IPC server must accept non-localhost connections (config flag) for the NAS case
- Authentication for non-localhost (shared secret / TLS) — design TBD in implementation plan, not blocking

### PC Client

New deliverable. Tauri (Rust shell) + Web frontend with Cubism Web SDK rendering Live2D.

**Tray icon (persistent)**
- Right-click menu: Show/Hide Live2D · Open Chat · Settings · Quit
- Left-click: toggle Live2D visible/hidden (same effect as menu)
- Tray icon reflects state: idle / listening / talking / running-task

**Live2D overlay**
- Two-state visibility: **visible** (overlay at user-configured screen corner) / **hidden**
- Toggle via tray click, tray menu, or hotkey. State persists across sessions.
- Idle behavior when visible: breathing, occasional blink, optional look-at-cursor (Settings toggle, default on)
- Does NOT track screen content. Look-at-cursor is local cursor coords only, never sent to daemon.
- Click Live2D → activates voice input
- Win/Mac only. Transparent overlay (click-through outside Live2D bounds). Linux is out of scope — X/Wayland transparency / click-through is too unreliable to commit to, and the user base lives on Win/Mac.

**Future**: "look at this region of my screen" — user can voice-command Doll to observe a screen region (e.g. cursor area). Triggers screen capture of that region only, sent to daemon as a one-shot Perception event. Out of scope this round; mention here so the IPC schema doesn't preclude it.

**Chat window**
- Summoned via hotkey or tray menu — a single regular window, not an overlay
- Contains:
  - Conversation transcript (voice turns transcribed + her replies)
  - Text input box (the "text aux" channel)
  - **Task panel** (side tab) — see Task Execution below
- Closing the window doesn't end the conversation; reopening shows the same transcript

**Settings**
- Default Live2D visibility on launch (visible / hidden)
- Look-at-cursor (on / off)
- Hotkeys (toggle visibility, summon chat, push-to-talk override)
- Daemon connection (host:port, secret)
- Task panel default (visible / silent)
- Reply-window duration (default 3s)
- Reminder voice (TTS engine selection — already in `.doll` pack)

### Voice I/O

**Trigger**: click anywhere on the Live2D sprite → mic opens.

**Endpoint**: VAD detects user pause → mic closes → turn submitted to daemon. User can also re-click Live2D mid-turn to force-close.

**Mic default state**: OFF. The client only opens the OS audio capture stream when click-to-talk, push-to-talk, or a reply-window triggers it; it's closed again as soon as the turn ends. No KWS process. No "hot mic." (Software gate, not hardware — but the gate is the single code path that opens the stream, so there's no second way in.)

**Proactive-speech reply window**: when Doll opens her mouth on her own (reminder, scheduled trigger, task interrupt for consent), her TTS finishes → mic auto-opens for 3 seconds (default, configurable in Settings) → if user says nothing, mic closes. If user replies, turn proceeds as a normal voice turn.

**Push-to-talk hotkey**: configurable global hotkey as a backup for when Live2D is hidden or the user doesn't want to mouse over.

### Reminders & Proactive Speech

Doll only speaks unprompted in these cases:
1. **Scheduled reminder fires** — entry added by the user via voice ("提醒我 5 點開會") or future calendar sync (out of scope this round).
2. **Long-running task needs destructive-action consent** (see below).
3. **Long-running task completes** (status report).

No ambient observations. No "I noticed you've been idle for an hour."

### Task Execution & Computer Use

**Scope of "computer use"** (all client-side actuators):
- **Tier 0 — Shell + filesystem on user's PC**: read, list, write, move, delete, run scripts
- **Tier 1 — App-level**: launch applications, open URLs in browser, send OS notifications
- **Tier 2 — GUI simulation**: mouse move/click, keyboard input — **always gated, every action**
- **Tier 3 — Window management**: move, resize, focus, switch workspace

She can do all four tiers, but Tier 2 (mouse/keyboard simulation) is treated specially: **every single click/keystroke must be consent-prompted**, not just destructive ones. The reasoning: synthetic input is the highest-risk capability and the user must remain the agent of last resort on their own GUI.

**Permission model**:
- ✅ Free (no prompt): read, list, search, open-for-view, web fetch, app launch (Tier 1), OS notification, window move/resize (Tier 3 non-destructive)
- 🛑 Gated (per-action voice + UI prompt): file write/move/delete/overwrite, script with side-effects, package install
- 🛑🛑 Always-gated (Tier 2, every action): mouse simulation, keyboard simulation

**Consent prompt surfacing**: when a gated action needs approval, Doll **forces Live2D visible** (overriding the user's hidden toggle), opens her mouth, and asks ("I want to move `X` to `Y` — OK?"). Mic auto-opens (3s reply window) for yes/no. Live2D returns to its prior state after the user responds. Consent prompts are non-suppressible — even silent task mode pops them.

**Task progress visibility** (Task panel):
- Default mode: **visible** — panel shows current step (`step 3/8: moved 5 files`), recent actions, time elapsed
- Alt mode: **silent** — panel hidden; Live2D shows a busy indicator only; result reported on completion
- Mode toggle is per-session via panel UI; default set in Settings
- Destructive-action prompts surface in BOTH modes (consent gate is non-suppressible)

**Task lifecycle**:
1. User assigns: "幫我整理 PDF..."
2. Doll acknowledges (short voice reply), spawns task
3. Task runs in background (subagent / shell / computer-use, same as current daemon model)
4. Panel updates (or stays silent based on mode)
5. On destructive action → pause, prompt, wait for consent
6. On completion → Doll opens mouth → reports result → reply window opens

### What stays out of scope

- Phone app (Android `VoiceInteractionService`) — future
- Wake word KWS on PC — future, possibly never
- Screen observation / accessibility tree reading — future, opt-in
- Always-on mic / ambient listening — explicitly rejected
- Multi-user / household — single user
- Calendar sync — future
- Drone (persistent agent) — already planned separately
- **Linux PC client** — Win/Mac only; the brain (daemon) still runs fine on Linux (NAS / home server case), but no Linux client UI

## Dependencies on existing work

This redesign assumes (and waits on):
- Voice Phase C (local audio bridge + real WebRTC E2E) — for the daemon-side mic/speaker plumbing that the PC client connects to
- Existing IPC server (step 16) extended for non-localhost connection

This redesign does NOT depend on:
- Drone (orthogonal)
- Memory pivots beyond current memsearch

## Open questions deferred to implementation plan

- Auth model for non-localhost daemon connection (shared secret vs TLS vs Tailscale-only)
- Tauri IPC bridge details (Rust ↔ Web ↔ daemon WS)
- Cubism Web SDK licensing path (Live2D Cubism SDK has a free-tier license — verify before shipping)
- BrainShell / HostShell tool split — naming, schema, how existing Shell call-sites migrate
- Cross-platform actuator implementation (mouse/kbd sim, window mgmt) — Win + Mac only, likely one Rust crate per OS

## Success criteria

We'll know this redesign worked when:
1. User can install daemon on a chosen machine and PC client on their PC, point client at daemon, and Doll appears in tray.
2. Click Live2D, say something, get a voice reply — no wake word, no setup beyond Settings.
3. Set a voice reminder, wait, hear Doll speak at the scheduled time, reply within the auto-open window.
4. Assign a multi-step file task, watch task panel, get consent prompts for destructive actions, get final report.
5. (NAS deployment only) PC sleeps, daemon keeps running on NAS, PC wakes, client reconnects, Doll picks up where she was. (Co-located deployment: PC sleep == daemon sleep — that's expected.)
6. Doll asked to move the mouse → prompts before every click → user can decline and she stops.
