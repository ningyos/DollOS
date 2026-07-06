# `dollosctl` live-smoke checklist

**This is a HUMAN checklist, not a test suite.** It exercises real
`systemd --user` unit start/stop, a real Discord bot token, and a private
test server. None of that exists in CI (no user login session, no systemd,
no bot token) — CI only covers unit-file *generation* (`tests/test_ctl_units.py`),
`dollosctl` argv *construction* (`tests/test_ctl_systemctl.py`,
`tests/test_ctl_cli.py`, `tests/test_ctl_install.py`), and the
`ServiceSupervisor`/kernel-wiring unit tests (`tests/test_service_supervisor.py`,
`tests/test_discord_bridge_fatal_exit.py`, `tests/test_kernel_bridge_wiring.py`)
against fake subprocesses. Run this checklist by hand, on your own machine,
before dogfooding DollOS as a running service.

**2026-07-06 single-service update**: the Discord bridge is no longer a
second systemd unit. The daemon now internalizes it as a supervised child
subprocess via the generic `ServiceSupervisor`
(`src/dollos/service_supervisor.py` — spec
`docs/superpowers/specs/2026-07-06-bridge-internalization-design.md`).
`dollosctl` manages exactly **one** unit, `dollos-daemon.service`; the
bridge comes up and goes down with it.

## Prerequisites

**Run `dollosctl install` from the DollOS repo root.** `WorkingDirectory` in
the generated unit is captured from your current directory at install time
(`units.resolve_params` → `Path.cwd()`), and the daemon resolves its `data/`
tree (memory, traces, pid) against a *relative* `Path("data")` — never
`.resolve()`d — so it ends up at `{WorkingDirectory}/data`. Installing from
elsewhere (e.g. `$HOME`) does not fail; it silently starts a fresh, empty
data store and orphans your real data. `install` now prints the resolved
`python` / `working_dir` / `data_root` absolutes to stdout — check them
before proceeding.

1. **`uv sync`** — installs the `dollosctl` console script (declared in
   `pyproject.toml` `[project.scripts]`) into the project venv.
2. **A ready daemon config** — copy `config.example.toml` → `config.toml`
   and point `[llm].base_url` at a running `llama-server` (or other
   configured provider). See the main `CLAUDE.md` Build/Run section. To
   also bring up the bridge, set:

   ```toml
   [bridge]
   enabled = true
   config  = "bridge.toml"   # path to the file from step 3, resolved relative to cwd
   ```

   `enabled = false` (the default) means the daemon never spawns a bridge
   at all — skip straight to the daemon-only parts of this checklist if
   you're not testing Discord today.
3. **A ready bridge config** (`bridge.toml`, referenced by `[bridge].config`
   above — copy `bridge.example.toml` → `bridge.toml`) — a TOML file with a
   `[discord]` table:

   ```toml
   [discord]
   token = "..."                    # bot token — on-device only, NEVER commit this file
   owner_discord_id = "123456789012345678"   # your numeric Discord user id
   ```

   `channel_allowlist` no longer exists (2026-07-06 owner-guild-only spec
   §4.3, Part B / B3) — it never gated forwarding. `owner_guild_only`
   (default `true`) is the real gate now: only guilds you've joined + your
   own DMs reach Doll. An optional `backfill_channels = [...]` list can be
   added for reconnect-gap history replay; see `bridge.example.toml`.

   Name-wake is no longer a bridge config concern (2026-07-06
   self-learned-aliases spec §3.6, Part A A5) — the bridge forwards every
   message and the daemon's `AttentionGate` decides admission from the
   character pack's `[meta] name`/`[meta] aliases` seed, an optional
   `[attention]` admin floor in `config.toml`, and names Doll learns herself
   via `LearnName`.

   Keep this file out of git (same rule as `config.toml` — secrets never
   committed). Point the bridge at a **private test server** you own — do
   not point it at a public/stranger-facing server (external-safety
   hardening is P1e; this task only proves the plumbing works).

## Upgrading from the two-unit install

If you installed before 2026-07-06, you have a leftover
`dollos-bridge.service` on disk (possibly `enabled`, i.e. it auto-starts on
boot). Left alone, it runs a second bridge process sharing the same
Discord bot token as the daemon-internalized one — Discord's gateway
rejects the second login, so this is an active hazard, not just dead
weight.

**A bare `dollosctl restart` does NOT clean this up** — only `install` and
`uninstall` run the legacy-unit migration (`_cleanup_legacy_bridge_unit`).
Re-run:

```bash
uv run dollosctl install --daemon-config config.toml --data-root data
```

Expected: in addition to (re-)writing `dollos-daemon.service`, this
`disable --now`s and deletes `dollos-bridge.service` if present. Confirm
with `systemctl --user list-unit-files | grep dollos` → only
`dollos-daemon.service` remains.

## Steps

1. **Install the unit**

   ```bash
   uv run dollosctl install --daemon-config config.toml --data-root data
   ```

   Expected: writes **only** `~/.config/systemd/user/dollos-daemon.service`
   (no `--bridge-config` flag anymore — the bridge path lives in
   `config.toml`'s `[bridge].config`), then runs `systemctl --user
   daemon-reload` (happens automatically inside `install` — no separate
   manual daemon-reload step needed). Re-running the same command is safe
   (idempotent — overwrites the file in place).

2. **Start the service**

   ```bash
   uv run dollosctl start
   uv run dollosctl status
   ```

   Expected: `status` prints `systemctl --user status` output for
   `dollos-daemon.service` showing `Active: active (running)`. There is no
   separate bridge unit to check.

3. **Watch the logs come up**

   ```bash
   uv run dollosctl logs daemon -f
   ```

   Expected: the daemon's event-loop startup log lines (WS server binding,
   character pack load, etc.), **and**, if `[bridge].enabled = true`, the
   bridge's own log lines interleaved in the same stream — the bridge
   subprocess inherits the daemon's stdout/stderr fds, so its output lands
   in the daemon's journal, not a separate one. Look for the bridge
   connecting to the daemon's WS server, then to Discord. `Ctrl-C` to stop
   following (this is a real `journalctl -f`, it streams forever
   otherwise). There is no `dollosctl logs bridge` — `logs` only takes
   `daemon`.

4. **Confirm the bridge is actually running, as a subprocess of the daemon**

   ```bash
   pgrep -af dollos.discord_bridge
   ```

   Expected: one matching process, spawned by the daemon after `[bridge]`
   is enabled and its config file exists (`kernel.py` registers the
   `ServiceSpec` only under that condition, and starts the supervisor
   after the WS server is up).

5. **End-to-end conversation, in the private test server**

   - **@-mention her** (or say her name-alias, or DM her) → she replies in
     the same channel. This proves the full path: Discord → bridge →
     daemon WS → event loop → LLM → `AddressedText` reply → bridge → Discord.
   - **A tagless follow-up message right after** → she continues the
     conversation without needing another @-mention (engagement).
   - **Unrelated chatter from another user, untagged** → she stays silent
     (ambient-logged only, no reply) — this is the L0 attention gate, not a
     bug.

6. **Restart, confirm the bridge comes back with it**

   ```bash
   uv run dollosctl restart
   ```

   Expected: the unit stops and restarts as one; the new daemon process
   re-spawns a fresh bridge child on startup (`service_supervisor.start()`
   re-registers and re-spawns since the old supervisor/child died with the
   old daemon). Re-run `pgrep -af dollos.discord_bridge` — a new PID.
   @-mention her again in the test server and confirm she still replies.

   Note: the unit sets `KillMode=mixed` (`src/dollos/ctl/units.py`), so
   `systemctl --user restart` SIGTERMs only the daemon's main process, not
   the whole cgroup. The daemon's own SIGTERM handler (`kernel.py`) then
   drives the same graceful shutdown sequence as a direct signal —
   `service_supervisor.stop()` → `SIGINT`-to-bridge — before the process
   exits, so `systemctl restart` itself gives a clean gateway close, not
   just a directly-targeted `kill -SIGINT`. See the resilience check below
   for how to verify that.

7. **Stop and uninstall**

   ```bash
   uv run dollosctl stop
   uv run dollosctl uninstall
   ```

   Expected: `stop` SIGTERMs the daemon's main process only (`KillMode=mixed`,
   same as the restart note above); the daemon's SIGTERM handler drives
   the graceful shutdown (bridge gateway closes cleanly, then the bridge
   child exits, then the daemon itself stops) rather than a cgroup-wide
   hard kill. `uninstall` stops the daemon (tolerating "not loaded" if already
   stopped), deletes the unit file from `~/.config/systemd/user/`,
   daemon-reloads, and (as in the upgrade section above) cleans up any
   leftover legacy `dollos-bridge.service`. Confirm with `systemctl --user
   list-unit-files | grep dollos` → no output. Confirm `pgrep -af
   dollos.discord_bridge` → no output either.

## Resilience checks

- **Daemon crash auto-restart**: with the service running (`[bridge]`
  enabled), find the daemon PID and `kill` it:

  ```bash
  systemctl --user show -p MainPID dollos-daemon.service
  kill <pid>
  uv run dollosctl status
  ```

  Expected: `Restart=on-failure` (baked into the unit — see
  `src/dollos/ctl/units.py`) brings the daemon back up on its own within a
  few seconds (`RestartSec=3`); `status` shows `active (running)` again
  without you running `start` yourself, and a fresh bridge child comes up
  with it.

- **Graceful stop closes the bridge's gateway cleanly (not just process
  death)**: `uv run dollosctl stop` (or `restart`) now gives this directly
  — `KillMode=mixed` means `systemctl --user stop/restart` SIGTERMs only
  the daemon's main process, and the daemon's own SIGTERM handler drives
  `service_supervisor.stop()` → `SIGINT`-to-bridge (the supervisor sends
  `SIGINT`, not `SIGTERM`, because the bridge only traps
  `KeyboardInterrupt`). A direct `kill -SIGINT <daemon_pid>` against the
  systemd-managed PID exercises the identical handler path and works too.
  Either way, confirm in the journal that the bridge's Discord gateway
  connection closes cleanly (its `_connect_and_run` `finally` block runs),
  then confirm:

  ```bash
  pgrep -f dollos.discord_bridge   # expect: empty
  ```

- **`kill -9` the daemon → no orphaned bridge (PDEATHSIG anti-orphan)**:
  this is the scenario the old two-unit design couldn't protect against
  (bridge reconnecting forever to whatever daemon comes back up, risking a
  second bridge process on the same Discord token). Find the daemon PID
  and force-kill it (simulating an OOM-kill or a bug the graceful path
  can't run for):

  ```bash
  systemctl --user show -p MainPID dollos-daemon.service
  kill -9 <pid>
  pgrep -f dollos.discord_bridge   # expect: empty, no zombie
  ```

  Expected: the kernel died before it could send anything, but
  `PR_SET_PDEATHSIG` (set in the bridge child's `preexec_fn` before exec)
  makes the *kernel* deliver `SIGINT` to the bridge the instant its parent
  (the daemon) dies — including via `SIGKILL`. `systemd`'s
  `Restart=on-failure` then brings the daemon back up, and the new daemon
  spawns its own fresh bridge child; there is never a window where two
  bridge processes exist.

- **Bad token → crash-loop → `giving up` + `BridgeDown` perception**:
  temporarily break `bridge.toml`'s `token` (e.g. append garbage), then
  start the daemon:

  ```bash
  uv run dollosctl restart
  uv run dollosctl logs daemon -f
  ```

  Expected: the bridge process now exits **non-zero** on the bad token
  (Task B2 — `discord.LoginFailure` is classified fatal and re-raised
  instead of hanging in `wait_until_ready()` or being silently retried by
  the bridge's own reconnect loop) instead of hanging or retrying forever
  — this is what makes the crash-loop detection able to see it at all.
  The supervisor restarts it with exponential backoff; after 6 consecutive
  crashes within the healthy-uptime window (more than `_MAX_CONSECUTIVE = 5`
  in `service_supervisor.py`) the journal shows a `giving up` log line and
  the daemon stops retrying until its own next restart. Doll should
  perceive a `BridgeDown` event in this same window (spec §9-3) — she
  can't fix a config typo, but she should know she lost an online
  channel. **Restore the real token afterward** and restart again.

- **Auto-start on boot / without an active login session**: `dollosctl`
  does not enable the unit by default (`install` only writes + reloads).
  To make the service survive a reboot and run even when you are not
  logged in interactively:

  ```bash
  systemctl --user enable dollos-daemon.service
  loginctl enable-linger $USER
  ```

  `enable` sets `WantedBy=default.target` (already declared in the unit
  file) to actually start it at boot; `enable-linger` is what lets
  `--user` services run without an active login session at all (otherwise
  systemd tears down the user's service manager when the last session
  logs out).

## Scope note

CI's job ends at "the unit file renders correctly, `dollosctl` builds the
right `systemctl`/`journalctl` argv, and the `ServiceSupervisor`/kernel
wiring behave correctly against fake subprocesses." Everything above —
real process lifecycle, a real Discord token, a real human @-mentioning
her, a real `kill -9` — only proves itself on a machine with an actual
user systemd session and an actual bot in an actual (private) server. Run
this checklist once per machine setup, and again after any change to
`src/dollos/ctl/units.py`, `src/dollos/service_supervisor.py`,
`src/dollos/kernel.py`'s bridge-wiring, or
`src/dollos/discord_bridge/__main__.py`'s CLI/exit-code surface.
