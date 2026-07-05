"""systemd user-unit file generation for DollOS services.

Pure string-template + path-resolution — no systemd interaction, no I/O.
`dollosctl` (a later task) writes these rendered strings to
`~/.config/systemd/user/` and shells out to `systemctl --user`.

Two units:
- ``dollos-daemon.service`` — the DollOS event-loop daemon (WS server).
- ``dollos-bridge.service`` — the Discord bridge, which talks to the
  daemon over its WS server.

The bridge unit uses a SOFT ordering dependency on the daemon
(``Wants=`` + ``After=``), never ``Requires=``. A hard dependency would
drag the bridge down whenever the daemon restarts; the bridge already
auto-reconnects to the daemon's WS server, so a soft dependency (start
order only, no propagated stop/restart) is strictly better here. Do not
"fix" this to ``Requires=`` — see the assertion in
tests/test_ctl_units.py::test_bridge_unit_soft_deps_daemon_not_hard.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class UnitParams:
    """Parameters interpolated into the rendered unit-file templates.

    All path-like fields are plain ``str`` (not ``Path``) because they
    are interpolated directly into unit-file text; construct via
    `resolve_params` to guarantee they are absolute — systemd has no
    shell PATH or relative-path convention, so a relative path here
    would resolve against systemd's own cwd, not the caller's.
    """

    python: str
    working_dir: str
    daemon_config: str
    bridge_config: str
    data_root: str
    daemon_ws: str = "ws://127.0.0.1:9876"
    retention_days: int = 30
    restart_sec: int = 3


def render_daemon_unit(p: UnitParams) -> str:
    """Render the `dollos-daemon.service` unit-file content."""
    return f"""[Unit]
Description=DollOS daemon (event loop + memory + IPC WS server)
After=network.target

[Service]
Type=simple
WorkingDirectory={p.working_dir}
ExecStart="{p.python}" -m dollos --config "{p.daemon_config}"
Restart=on-failure
RestartSec={p.restart_sec}

[Install]
WantedBy=default.target
"""


def render_bridge_unit(p: UnitParams) -> str:
    """Render the `dollos-bridge.service` unit-file content.

    Soft-depends on the daemon via `Wants=` + `After=` only — see the
    module docstring for why this must never become `Requires=`.
    """
    return f"""[Unit]
Description=DollOS Discord bridge
After=dollos-daemon.service network.target
Wants=dollos-daemon.service

[Service]
Type=simple
WorkingDirectory={p.working_dir}
ExecStart="{p.python}" -m dollos.discord_bridge --daemon {p.daemon_ws} --config "{p.bridge_config}" --data-root "{p.data_root}" --retention-days {p.retention_days}
Restart=on-failure
RestartSec={p.restart_sec}

[Install]
WantedBy=default.target
"""


def resolve_params(
    *,
    daemon_config: Path,
    bridge_config: Path,
    data_root: Path,
    python: str | None = None,
    working_dir: Path | None = None,
) -> UnitParams:
    """Build a `UnitParams` with every path absolutized.

    `python` defaults to `sys.executable` (the current venv's
    interpreter) so the unit runs the right interpreter without relying
    on a PATH lookup at service-start time. `working_dir` defaults to
    the current working directory. All paths are expanded (`~`) and
    resolved to absolute strings.
    """
    resolved_working_dir = (working_dir if working_dir is not None else Path.cwd()).expanduser().resolve()
    return UnitParams(
        python=python if python is not None else sys.executable,
        working_dir=str(resolved_working_dir),
        daemon_config=str(daemon_config.expanduser().resolve()),
        bridge_config=str(bridge_config.expanduser().resolve()),
        data_root=str(data_root.expanduser().resolve()),
    )
