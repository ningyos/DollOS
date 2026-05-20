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
- Default: visible, transparent overlay at user-configured screen corner
- Idle behavior: breathing, occasional blink, look at cursor — but does NOT track screen content
- Click Live2D → activates voice input
- Display modes (configurable in Settings):
  - **Always visible** (default) — overlay sits on top, never hides
  - **Auto-hide on focus** — fades when another window is active
  - **Hidden** — only appears when speaking or when summoned
- Win/Mac get transparent overlay (click-through outside Live2D bounds); Linux gets a normal toplevel window (X/Wayland transparency is unreliable enough that "normal window" is the contract).

**Chat window**
- Summoned via hotkey or tray menu — a single regular window, not an overlay
- Contains:
  - Conversation transcript (voice turns transcribed + her replies)
  - Text input box (the "text aux" channel)
  - **Task panel** (side tab) — see Task Execution below
- Closing the window doesn't end the conversation; reopening shows the same transcript

**Settings**
- Display mode (3 modes above)
- Hotkeys (toggle visibility, summon chat, push-to-talk override)
- Daemon connection (host:port, secret)
- Task panel default (visible / silent)
- Reminder voice (TTS engine selection — already in `.doll` pack)

### Voice I/O

**Trigger**: click anywhere on the Live2D sprite → mic opens.

**Endpoint**: VAD detects user pause → mic closes → turn submitted to daemon. User can also re-click Live2D mid-turn to force-close.

**Mic default state**: OFF. Mic is hardware-gated to never run unless a click-to-talk or reply-window opened it. No KWS process. No "hot mic."

**Proactive-speech reply window**: when Doll opens her mouth on her own (reminder, scheduled trigger, task interrupt for consent), her TTS finishes → mic auto-opens for N seconds (default 5s, configurable) → if user says nothing, mic closes. If user replies, turn proceeds as a normal voice turn.

**Push-to-talk hotkey**: configurable global hotkey as a backup for when Live2D is hidden or the user doesn't want to mouse over.

### Reminders & Proactive Speech

Doll only speaks unprompted in these cases:
1. **Scheduled reminder fires** — entry added by the user via voice ("提醒我 5 點開會") or future calendar sync (out of scope this round).
2. **Long-running task needs destructive-action consent** (see below).
3. **Long-running task completes** (status report).

No ambient observations. No "I noticed you've been idle for an hour."

### Task Execution & Computer Use

Doll has full file system + computer-use capabilities, gated by consent on destructive actions.

**Permission model**:
- ✅ Free: read, list, search, open-for-view, web fetch, anything reversible
- 🛑 Gated: write, move, delete, overwrite, install, run-script-with-side-effects
- Each gated action surfaces as: voice prompt + chat notification ("I want to move `X` to `Y` — OK?") → user replies yes/no via voice or chat button.

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
- Voice Phase C local-audio-bridge — separate plan, but this redesign depends on it being done first

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
- File access surface: does the client expose a "file access tool" that the daemon calls back into, or does the daemon access files directly when daemon is co-located? Different in the NAS case vs PC-co-located case.
- Computer use surface: same question — client-side actuator or daemon-side? Computer-use only makes sense client-side (the daemon on a NAS can't move the user's mouse).

The last two are the meatiest — the implementation plan needs to nail down the client/daemon split for actuators.

## Success criteria

We'll know this redesign worked when:
1. User can install daemon on a chosen machine and PC client on their PC, point client at daemon, and Doll appears in tray.
2. Click Live2D, say something, get a voice reply — no wake word, no setup beyond Settings.
3. Set a voice reminder, wait, hear Doll speak at the scheduled time, reply within the auto-open window.
4. Assign a multi-step file task, watch task panel, get consent prompts for destructive actions, get final report.
5. PC sleeps, daemon keeps running, PC wakes, client reconnects, Doll picks up where she was.
