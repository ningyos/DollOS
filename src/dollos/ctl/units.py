"""systemd user-unit file generation for DollOS services.

Pure string-template + path-resolution — no systemd interaction, no I/O.
`dollosctl` writes the rendered string to `~/.config/systemd/user/` and
shells out to `systemctl --user`.

Single unit:
- ``dollos-daemon.service`` — the DollOS event-loop daemon (WS server).

Single-service migration (spec `2026-07-06-bridge-internalization-design.md`
§7): the Discord bridge used to be a second unit
(``dollos-bridge.service``) started/stopped independently. The daemon now
internalizes the bridge as a supervised subprocess (``ServiceSupervisor``,
config'd via ``[bridge].config`` in the daemon's own config file), so
there is no bridge-specific unit-file content left to render here —
``render_bridge_unit`` was deleted, not deprecated. `dollos/ctl/cli.py`
still references a `BRIDGE_UNIT` constant, but only to actively clean up
a *legacy* pre-migration unit; see that module's docstring.
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
    data_root: str
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


def resolve_params(
    *,
    daemon_config: Path,
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
    cwd = working_dir if working_dir is not None else Path.cwd()
    resolved_working_dir = cwd.expanduser().resolve()
    return UnitParams(
        python=python if python is not None else sys.executable,
        working_dir=str(resolved_working_dir),
        daemon_config=str(daemon_config.expanduser().resolve()),
        data_root=str(data_root.expanduser().resolve()),
    )
