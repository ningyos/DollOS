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

import argparse
import logging
import sys
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
    # Visibility for the data-dir/cwd footgun (P1g whole-branch review,
    # Important #1): WorkingDirectory is captured from cwd at install time,
    # and the daemon resolves its data/ tree (memory, traces, pid) relative
    # to it — installing from the wrong directory silently starts a fresh,
    # empty data store. Echo the resolved absolutes so the operator can
    # catch that before it happens.
    print(f"dollosctl install: python={params.python}")
    print(f"dollosctl install: working_dir={params.working_dir}")
    print(f"dollosctl install: data_root={params.data_root}")
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


def _build_parser() -> argparse.ArgumentParser:
    """Build the `dollosctl` argparse parser (pure — no dispatch logic)."""
    parser = argparse.ArgumentParser(
        prog="dollosctl", description="Manage DollOS systemd --user services."
    )
    subparsers = parser.add_subparsers(dest="command")

    install_parser = subparsers.add_parser(
        "install", help="Render + write the daemon and bridge systemd --user units."
    )
    install_parser.add_argument("--daemon-config", type=Path, required=True)
    install_parser.add_argument("--bridge-config", type=Path, required=True)
    install_parser.add_argument("--data-root", type=Path, default=Path("data"))
    install_parser.add_argument("--unit-dir", type=Path, default=None)

    uninstall_parser = subparsers.add_parser(
        "uninstall", help="Stop and remove the daemon and bridge systemd --user units."
    )
    uninstall_parser.add_argument("--unit-dir", type=Path, default=None)

    subparsers.add_parser("start", help="Start the daemon unit, then the bridge unit.")
    subparsers.add_parser("stop", help="Stop the bridge unit, then the daemon unit.")
    subparsers.add_parser("restart", help="Restart the daemon unit, then the bridge unit.")
    subparsers.add_parser("status", help="Show systemd status for both units.")

    logs_parser = subparsers.add_parser("logs", help="Show or follow the journal for one unit.")
    logs_parser.add_argument("which", choices=["daemon", "bridge"])
    logs_parser.add_argument("-f", "--follow", action="store_true")
    logs_parser.add_argument("-n", "--lines", type=int, default=200)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `dollosctl` console script.

    Unknown subcommands / missing required args exit non-zero via
    argparse's own `SystemExit` (default behavior, not caught here). A
    `SystemctlError` raised by any wrapper is caught here and turned
    into a clean non-zero return + stderr message rather than a
    traceback — the one place this module deliberately does NOT let an
    error propagate raw, because this is the CLI/process boundary.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    try:
        if args.command == "install":
            install(
                unit_dir=args.unit_dir if args.unit_dir is not None else _user_unit_dir(),
                daemon_config=args.daemon_config,
                bridge_config=args.bridge_config,
                data_root=args.data_root,
            )
        elif args.command == "uninstall":
            uninstall(unit_dir=args.unit_dir if args.unit_dir is not None else _user_unit_dir())
        elif args.command == "start":
            systemctl.start(DAEMON_UNIT)
            systemctl.start(BRIDGE_UNIT)
        elif args.command == "stop":
            systemctl.stop(BRIDGE_UNIT)
            systemctl.stop(DAEMON_UNIT)
        elif args.command == "restart":
            systemctl.restart(DAEMON_UNIT)
            systemctl.restart(BRIDGE_UNIT)
        elif args.command == "status":
            print(systemctl.status(DAEMON_UNIT))
            print(systemctl.status(BRIDGE_UNIT))
        elif args.command == "logs":
            unit = DAEMON_UNIT if args.which == "daemon" else BRIDGE_UNIT
            systemctl.journal(unit, follow=args.follow, lines=args.lines)
    except SystemctlError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0
