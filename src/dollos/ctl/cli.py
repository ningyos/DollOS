"""`dollosctl` — install/uninstall of DollOS systemd `--user` units.

This module currently holds the install/uninstall logic plus the
constants shared with the (not-yet-written) argparse dispatch. It
consumes `units.py` (unit-file generation) and `systemctl.py`
(subprocess wrappers) — it does no template rendering or subprocess
work of its own.

Idempotency contract:
- `install` always overwrites the two unit files (never appends /
  stacks) and always ends with `daemon_reload()` so systemd picks up
  the new content. Write failures are not caught — surface them.
- `uninstall` is the ONE place that tolerates a `SystemctlError`: a
  `stop` on a unit that is not loaded (e.g. never installed, or
  already stopped) raises `SystemctlError`, and since we are tearing
  down anyway that failure carries no actionable information — the
  end state ("not running") is what we wanted. Deleting the unit files
  uses `missing_ok=True` for the same reason. No other function in
  `dollosctl` is allowed this leniency — this is teardown, not steady
  -state operation, and the "no fallback mechanisms" project rule
  still applies everywhere else.
"""

from __future__ import annotations

import logging
from pathlib import Path

from dollos.ctl import systemctl
from dollos.ctl.systemctl import SystemctlError
from dollos.ctl.units import render_bridge_unit, render_daemon_unit, resolve_params

logger = logging.getLogger(__name__)

DAEMON_UNIT = "dollos-daemon.service"
BRIDGE_UNIT = "dollos-bridge.service"


def _user_unit_dir() -> Path:
    """Default systemd `--user` unit directory: `~/.config/systemd/user`."""
    return Path.home() / ".config" / "systemd" / "user"


def install(
    *,
    unit_dir: Path,
    daemon_config: Path,
    bridge_config: Path,
    data_root: Path,
    python: str | None = None,
    working_dir: Path | None = None,
) -> None:
    """Render and write both unit files to `unit_dir`, then daemon-reload.

    Idempotent: re-running overwrites the two files in place (does not
    append or stack). Write failures are not caught — no fallback.
    """
    params = resolve_params(
        daemon_config=daemon_config,
        bridge_config=bridge_config,
        data_root=data_root,
        python=python,
        working_dir=working_dir,
    )
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / DAEMON_UNIT).write_text(render_daemon_unit(params))
    (unit_dir / BRIDGE_UNIT).write_text(render_bridge_unit(params))
    systemctl.daemon_reload()


def uninstall(*, unit_dir: Path) -> None:
    """Stop both units, delete their unit files, then daemon-reload.

    Idempotent: safe to call when the units were never installed, are
    already stopped, or the files are already gone. `stop` on a unit
    that isn't loaded raises `SystemctlError` — that is the ONE
    tolerated error in this codebase (see module docstring): we are
    tearing down, so "already not running" is a success, not a
    failure, and must not abort the rest of the teardown.
    """
    for unit in (BRIDGE_UNIT, DAEMON_UNIT):
        try:
            systemctl.stop(unit)
        except SystemctlError as exc:
            logger.info("uninstall: stop(%s) failed, continuing teardown: %s", unit, exc)

    (unit_dir / DAEMON_UNIT).unlink(missing_ok=True)
    (unit_dir / BRIDGE_UNIT).unlink(missing_ok=True)
    systemctl.daemon_reload()
