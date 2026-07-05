# `dollosctl` live-smoke checklist

**This is a HUMAN checklist, not a test suite.** It exercises real
`systemd --user` unit start/stop, a real Discord bot token, and a private
test server. None of that exists in CI (no user login session, no systemd,
no bot token) — CI only covers unit-file *generation* (`tests/test_ctl_units.py`)
and `dollosctl` argv *construction* (`tests/test_ctl_systemctl.py`,
`tests/test_ctl_cli.py`). Run this checklist by hand, on your own machine,
before dogfooding DollOS as a running service.

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
   configured provider). See the main `CLAUDE.md` Build/Run section.
3. **A ready bridge config** — a TOML file with a `[discord]` table:

   ```toml
   [discord]
   token = "..."                    # bot token — on-device only, NEVER commit this file
   owner_discord_id = "123456789012345678"   # your numeric Discord user id
   name_aliases = ["gura", "古拉"]           # names that trigger wake in a channel
   channel_allowlist = ["111111111111111111"]  # channel ids the bridge will join
   always_wake_channels = []          # optional; channels where every message wakes her
   ```

   Keep this file out of git (same rule as `config.toml` — secrets never
   committed). Point the bridge at a **private test server** you own — do
   not point it at a public/stranger-facing server (external-safety
   hardening is P1e; this task only proves the plumbing works).

## Steps

1. **Install the units**

   ```bash
   uv run dollosctl install --daemon-config config.toml --bridge-config <bridge>.toml --data-root data
   ```

   Expected: writes `~/.config/systemd/user/dollos-daemon.service` and
   `~/.config/systemd/user/dollos-bridge.service`, then runs
   `systemctl --user daemon-reload` (this happens automatically inside
   `install` — no separate manual daemon-reload step is needed). Re-running
   the same command is safe (idempotent — overwrites the two files in place).

2. **Start both services**

   ```bash
   uv run dollosctl start
   uv run dollosctl status
   ```

   Expected: `status` prints `systemctl --user status` output for both
   `dollos-daemon.service` and `dollos-bridge.service`, each showing
   `Active: active (running)`.

3. **Watch the logs come up**

   ```bash
   uv run dollosctl logs daemon -f
   ```

   Expected: the daemon's event-loop startup log lines (WS server binding,
   character pack load, etc.). `Ctrl-C` to stop following (this is a real
   `journalctl -f`, it streams forever otherwise).

   ```bash
   uv run dollosctl logs bridge -f
   ```

   Expected: the bridge connecting to the daemon's WS server, then
   connecting to Discord and registering the allowlisted channel(s).

4. **End-to-end conversation, in the private test server**

   - **@-mention her** (or say her name-alias, or DM her) → she replies in
     the same channel. This proves the full path: Discord → bridge →
     daemon WS → event loop → LLM → `AddressedText` reply → bridge → Discord.
   - **A tagless follow-up message right after** → she continues the
     conversation without needing another @-mention (engagement).
   - **Unrelated chatter from another user, untagged** → she stays silent
     (ambient-logged only, no reply) — this is the L0 attention gate, not a
     bug.

5. **Restart, confirm reconnect**

   ```bash
   uv run dollosctl restart
   ```

   Expected: both units restart (daemon first, then bridge). The bridge's
   own reconnect loop (soft `Wants=`/`After=` dependency, not `Requires=`)
   picks the daemon back up without manual intervention — @-mention her
   again in the test server and confirm she still replies.

6. **Stop and uninstall**

   ```bash
   uv run dollosctl stop
   uv run dollosctl uninstall
   ```

   Expected: `stop` stops the bridge, then the daemon. `uninstall` stops
   both (tolerating "not loaded" if already stopped), deletes both unit
   files from `~/.config/systemd/user/`, and daemon-reloads. Confirm with
   `systemctl --user list-unit-files | grep dollos` → no output.

## Resilience checks

- **Daemon crash auto-restart**: with both services running, find the
  daemon PID and `kill` it:

  ```bash
  systemctl --user show -p MainPID dollos-daemon.service
  kill <pid>
  uv run dollosctl status
  ```

  Expected: `Restart=on-failure` (baked into the unit — see
  `src/dollos/ctl/units.py`) brings the daemon back up on its own within a
  few seconds (`RestartSec=3`); `status` shows `active (running)` again
  without you running `start` yourself.

- **Bridge crash does not affect the daemon**: kill the bridge's PID the
  same way. Expected: the daemon keeps running untouched (no shared
  process, no hard unit dependency); the bridge unit restarts on its own
  and reconnects (step 5's reconnect behavior).

- **Auto-start on boot / without an active login session**: `dollosctl`
  does not enable the units by default (`install` only writes + reloads).
  To make both services survive a reboot and run even when you are not
  logged in interactively:

  ```bash
  systemctl --user enable dollos-daemon.service dollos-bridge.service
  loginctl enable-linger $USER
  ```

  `enable` sets `WantedBy=default.target` (already declared in both unit
  files) to actually start them at boot; `enable-linger` is what lets
  `--user` services run without an active login session at all (otherwise
  systemd tears down the user's service manager when the last session
  logs out).

## Scope note

CI's job ends at "the unit files render correctly and `dollosctl` builds
the right `systemctl`/`journalctl` argv." Everything above — real process
lifecycle, real Discord token, a real human @-mentioning her — only proves
itself on a machine with an actual user systemd session and an actual bot
in an actual (private) server. Run this checklist once per machine setup,
and again after any change to `src/dollos/ctl/units.py` or
`src/dollos/discord_bridge/__main__.py`'s CLI surface.
