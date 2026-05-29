# DollOS Tauri Spike — Notes

Date: 2026-05-20
Host: Ubuntu 24.04 (noble), kernel 6.8, x86_64.

## TL;DR

- **System deps**: installed by user (apt line below). `pkg-config --exists webkit2gtk-4.1 gtk+-3.0` → OK.
- **Tauri shell**: `cargo check` and full `cargo build` both **pass cleanly** (1m 18s first build, 493 crates). The binary launches but **panics on `Failed to initialize GTK`** because this session has no `DISPLAY` and no `WAYLAND_DISPLAY` (SSH/tty-only). No Xvfb is installed and no sudo to add it. Cannot render the webview in this environment.
- **One additional Cargo fix during this run**: had to add the `tray-icon` Cargo feature to `tauri` (Tauri 2.x gates `tauri::tray::*` behind it). Patch: `Cargo.toml` `tauri = { version = "2", features = ["tray-icon"] }`. Build green after that.
- **Frontend** (vanilla TS + Vite + canvas character + WebSocket client): complete, type-checks clean, `vite build` produces a 6 KB JS bundle.
- **Dummy daemon** (`dummy_daemon.py`, `websockets` lib, port 9876): **verified working again this run** — connected from a probe client, received 3 `hello` frames, sent a `ping`, got `echo` back.
- The `spike-result.png` in this directory is still the original **mock composite**. A real runtime screenshot is not possible without a display server. The runtime evidence captured this round is the build success line + GTK-init panic in `/tmp/tauri-dev.log`, preserved at `runtime-evidence.txt` in this directory.

## Step-by-step status

| # | Step                       | Status         |
| - | -------------------------- | -------------- |
| 1 | Tauri skeleton (`npm create tauri-app`) | Scaffolded, `npm install` OK; **`cargo check` + `cargo build` now pass cleanly** after adding `tray-icon` feature. Binary panics on launch due to no DISPLAY (headless SSH). |
| 2 | Static character render    | Code written (canvas 2D, SVG asset); cannot run — no display |
| 3 | Idle animation (breathing + blink) | Code written; cannot run — no display |
| 4 | Tray icon (Tauri 2 built-in `tauri::tray::TrayIconBuilder`) | Code written; `tray-icon` feature added to `tauri` dep; cannot run — no display |
| 5 | WS echo loop               | Daemon **re-verified end-to-end** with a probe client (`hello` x3 + `ping`→`echo`). Frontend WS code written; never instantiated inside Tauri (webview never started). |
| 6 | Screenshot + NOTES         | NOTES updated. `spike-result.png` remains the mock — real runtime screenshot impossible without a graphical session. `runtime-evidence.txt` captures the build success + GTK-init panic. |

## Where I stopped (resumed 2026-05-20, second session)

System deps now installed. `pkg-config --exists webkit2gtk-4.1 gtk+-3.0 libdbus-1-dev` → OK.

Second blocker, fixed in this session: `cargo check` failed with `unresolved import 'tauri::tray'` — Tauri 2.x gates `tauri::tray::*` behind a `tray-icon` Cargo feature that the scaffold does NOT enable by default. Patched `src-tauri/Cargo.toml`:

```toml
tauri = { version = "2", features = ["tray-icon"] }
```

After that, `cargo check` is clean and a full `cargo build` (via `npm run tauri dev`) finished in **1m 18s** (493 crates). The binary then launches and panics:

```
thread 'main' panicked at tao-0.35.2/src/platform_impl/linux/event_loop.rs:217:53:
Failed to initialize gtk backend!: BoolError { message: "Failed to initialize GTK", ... }
```

Root cause: no `DISPLAY` and no `WAYLAND_DISPLAY` in this session (`loginctl` shows only `pts/2`, no graphical seat). GTK can't connect to any display server. Xvfb is not installed and there is no passwordless sudo to add it. So the spike still cannot capture a real runtime screenshot — but the build + launch path is now fully de-risked. The frontend was de-risked separately last session: `npx tsc --noEmit` clean, `npx vite build` succeeds, all 5 modules transform, bundle ~6 KB gzipped.

Dummy daemon **re-verified this round** from a probe client: 3 unsolicited `hello` frames received, `ping` → `echo` round-trip OK. The WS contract is sound; only the webview-side instantiation is unproven.

## Required system deps (Ubuntu 24.04)

Single apt line — the user needs to run this to unblock the spike runtime:

```bash
sudo apt install \
    libwebkit2gtk-4.1-dev \
    libgtk-3-dev \
    libayatana-appindicator3-dev \
    librsvg2-dev \
    libdbus-1-dev \
    libsoup-3.0-dev \
    libjavascriptcoregtk-4.1-dev \
    build-essential curl wget file \
    pkg-config
```

Tauri 2.x docs list `libwebkit2gtk-4.1-dev` (4.1, not 4.0 — Ubuntu 24.04 only ships 4.1 anyway). `librsvg2-dev`, `libayatana-appindicator3-dev`, and `libsoup-3.0-dev` are the non-obvious ones; `libdbus-1-dev` came up later in the crate dep tree (`libdbus-sys` is pulled in transitively).

`pkg-config` is already installed but listed for completeness.

## How to run once deps are installed

```bash
# Toolchain (already installed under user home this session):
#   PATH=~/.local/opt/node-v20.18.0-linux-x64/bin:~/.cargo/bin:$PATH
# rustc 1.95.0, node 20.18.0, npm 10.8.2 — no sudo needed.

# Terminal 1 — daemon
cd experiments/tauri-spike
uv run --with websockets python dummy_daemon.py

# Terminal 2 — app
cd experiments/tauri-spike/dollos-spike
export PATH=~/.local/opt/node-v20.18.0-linux-x64/bin:~/.cargo/bin:$PATH
npm run tauri dev
```

Expected: a 600×800 dark-grey window with the pastel placeholder doll centred, a log box top-right streaming `SYS open` → `IN {"type":"hello",...}` every 5s, and a tray icon (default Tauri icon) reachable from left-click toggle / right-click menu.

## What worked

- Toolchain setup without sudo: portable Node 20 tarball under `~/.local/opt/`, rustup default profile minimal under `~/.cargo/`. Both took < 1 minute combined.
- `create-tauri-app` with `--template vanilla-ts` — generated a clean, current Tauri 2.x layout (no Tauri 1.x cruft). `npm install` finished in 12s with zero vulnerabilities.
- `cargo check` resolved & downloaded ~280 crates from registry over a fresh user-local Cargo home, before failing on the C linker step. No Cargo manifest / lockfile issues.
- `websockets` library (Python) `serve()` + asyncio handler pattern → trivially testable from a one-shot probe client. **End-to-end round-trip verified** via the probe.
- Vite + vanilla TS: zero friction. `tsc --noEmit` clean on first pass, SVG-as-asset import works with a one-liner `*.svg` module declaration.

## What didn't work / pain points

- **`tauri-plugin-tray` doesn't exist on Tauri 2.x.** The brief mentioned it — that crate is Tauri 1.x. On 2.x, the tray API moved into core as `tauri::tray::TrayIconBuilder` (and `tauri::menu` for the menu). No extra Cargo dependency or capability change needed; default core capability covers it. **Update the Plan accordingly.**
- **Linux system deps for Tauri are non-trivial and the failure mode is opaque** — `cargo check` panics deep inside a build script of a transitive dep with a long stack trace, not at the top level. Production install docs should pin the apt line above.
- **AppIndicator on Wayland**: Ubuntu 24.04 default is GNOME on Wayland, and GNOME has no built-in system tray. Even with `libayatana-appindicator3-dev` installed at build time, the tray icon won't show without the user installing the "AppIndicator and KStatusNotifierItem Support" GNOME extension. This is documented but easy to miss. **Tray-first design needs a Linux footnote** — though per `CLAUDE.md` the production target is Win/Mac, so this only bites devs.
- The ImageMagick CLI in this environment cannot rasterise the placeholder SVG correctly (no gradient support in this build, eats the viewBox). Used Python `cairosvg` instead — fine for one-off, but suggests the real client should never lean on ImageMagick for asset prep; use `librsvg-bin` or just ship pre-rasterised PNGs.

## Library choices / surprises

- **Tauri 2.x is on `tauri = "2"` at top level — not a pre-release tag any more.** Cargo.toml from `create-tauri-app` pins it cleanly; no funny business with `2.0.0-rc.N`.
- **`withGlobalTauri: true`** is enabled by default in the scaffold; harmless for the spike, but the real client probably wants it `false` to force IPC through the typed `@tauri-apps/api/core` import and avoid leaking globals.
- **Capabilities ACL** (`src-tauri/capabilities/default.json`) only needs `core:default` + `opener:default` for the tray. If we add filesystem actuators, we'll need `fs:allow-read-text-file` etc. with explicit scopes. Not a problem, but plan for it.
- **`devtools` feature** on `tauri` is gated; for dev iteration on the real plan, add `tauri = { version = "2", features = ["devtools"] }` in `[features]` for dev profile.

## Character abstraction — is the "static OR Live2D" split clean?

Yes, and the spike confirms it cheaply. `src/main.ts` defines:

```ts
interface CharacterRenderer {
  ready: Promise<void>;
  draw(ctx: CanvasRenderingContext2D, w: number, h: number, tMs: number): void;
}
class StaticImageCharacter implements CharacterRenderer { ... }
```

Swapping in a Cubism renderer later means a new `Live2DCharacter implements CharacterRenderer` that internally drives the Cubism Web SDK against the same canvas. The rAF loop in `main.ts` doesn't care which implementation it's calling. Two notes:

1. **Cubism Web SDK wants a WebGL context, not 2D.** That means the swap is slightly less clean than I wrote it — the interface should probably take a generic `canvas: HTMLCanvasElement` and let each implementation grab its own context. Tweak: `draw(canvas, tMs)` instead of `draw(ctx, w, h, tMs)`. Small change, do it before any code multiplies.
2. **Blink/breath state lives inside the implementation**, which is the right call. The static-image impl drives them with sin + RNG; the Live2D impl will drive ParamAngleX / ParamEyeLOpen / etc. The outer dispatcher never sees raw animation state, only high-level cues (e.g. `setEmotion('happy')`, `triggerWink()`).

## Where does the dispatcher live?

Strong recommendation from this spike: **the dispatcher belongs in TypeScript, not Rust.** The spike's `main.ts` already does the WS connection and could trivially gain a `handleEvent(msg)` switch on `msg.type` that drives the character (`triggerWink`, `setMood`), the log, and any future actuator calls.

Why TS not Rust:

- Every event from the daemon ends up affecting the canvas (animation cue) or DOM (chat bubble, settings UI). Both live in the webview. Routing through Rust adds an IPC hop and a serialization round-trip per event for no gain.
- Actuators (fs/shell/mouse/kbd) DO live in Rust (capability-gated commands), but they're called *from* the dispatcher via `invoke()`. The dispatcher is the orchestrator, the actuators are leaves.
- Hot-reload during dev: TS changes don't require a Rust rebuild. With the apt line above the cold Rust build still takes minutes; you want to minimise touching Rust in the inner loop.

Proposed structure for Plan D:

```
src/
  main.ts                 # bootstrap: canvas, rAF, WS connect
  dispatcher.ts           # WS msg -> character cues / DOM / invoke()
  character/
    index.ts              # CharacterRenderer interface
    static.ts             # StaticImageCharacter
    live2d.ts             # (later) Live2DCharacter
  ws.ts                   # reconnecting WS wrapper w/ JSON typing
  log.ts                  # debug log overlay (drop in prod)
src-tauri/src/
  lib.rs                  # entrypoint + tray
  actuators/              # one file per actuator, Tauri commands
    fs.rs
    shell.rs
    mouse.rs
    kbd.rs
```

## WebSocket lifecycle gotchas

- **Browser `WebSocket` ctor throws synchronously on some malformed URLs** but is otherwise async — wrap in try/catch around the constructor AND attach an `error` handler. The spike does both.
- **`close` event fires for both clean and unclean disconnects.** Reconnect logic should backoff (this spike uses a fixed 2.5s — fine for dev, the real client wants exponential capped at e.g. 30s).
- **Tauri webview doesn't restrict WS connections by default** under the dev CSP (`csp: null` in `tauri.conf.json`). For prod we'll need to set a CSP that explicitly allows `connect-src ws://localhost:*` — easy but don't forget.
- **No `Authorization` header support in browser WebSocket** — auth has to go in the URL query string or in a first-message handshake. Plan D should pick one; subprotocol auth is the cleanest.

## Recommendations for Plan D

1. **Document the Linux apt line in the plan, even though target is Win/Mac.** Devs on Linux (us) will trip on it.
2. **Tray plugin claim is wrong — Tauri 2.x has tray in core.** Update spec wording to "use `tauri::tray::TrayIconBuilder`". No `tauri-plugin-tray` dependency.
3. **`CharacterRenderer.draw(canvas, tMs)`, not `(ctx, w, h, tMs)`.** Lets Live2D grab its own WebGL context.
4. **Dispatcher in TypeScript.** Rust is just actuator leaves + tray + window mgmt. Keep the IPC surface narrow (one `invoke('actuate', {kind, args})` is plenty for the spike's purposes; type it properly in prod).
5. **Don't enable `withGlobalTauri`** in prod — force typed imports.
6. **GNOME-on-Wayland tray needs the AppIndicator extension.** Add a "if your tray doesn't show…" footnote to dev setup.
7. **Pre-rasterise character assets** rather than relying on browser SVG rendering at runtime. Ship `doll@1x.png`, `doll@2x.png`. The Cubism path won't use them but the static-image path should be pixel-perfect.
8. **Plan a CSP for prod.** `csp: null` is dev only.
9. The frontend bundle is **6 KB gzipped** for the entire spike (canvas + animation + WS + UI). The real client's hot path is going to stay tiny if we avoid frameworks. Reaffirms "vanilla TS, no React".

## Estimated complexity for Plan D

**Medium** — the unknowns are now small. The Rust side is plumbing (tray + a handful of actuator commands), the TS side has a clear shape (dispatcher + renderer interface, both proven in this spike), and the WS protocol is whatever daemon emits. The medium-not-low rating comes from: actuator security model (capability scopes, consent UI), prod CSP, cross-platform tray/window behaviour (Mac menu bar vs Windows notification area), and asset/icon pipeline. None individually hard, collectively a week of fiddling.
