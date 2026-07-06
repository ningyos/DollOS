"""`dollosctl` — install/uninstall of the DollOS systemd `--user` unit.

This module currently holds the install/uninstall logic plus the
constants shared with the argparse dispatch. It consumes `units.py`
(unit-file generation) and `systemctl.py` (subprocess wrappers) — it
does no template rendering or subprocess work of its own.

Single-service migration (spec `2026-07-06-bridge-internalization-design.md`
§7): the Discord bridge used to be a second systemd unit
(`dollos-bridge.service`) installed/started/stopped alongside the daemon.
The daemon now internalizes the bridge as a supervised subprocess
(`ServiceSupervisor`, wired via `[bridge].config` in the daemon's own
config file), so `install` writes ONLY `dollos-daemon.service`, and
`start`/`stop`/`restart`/`status`/`logs` all operate on that one unit.

`BRIDGE_UNIT` is kept as a constant purely for legacy cleanup: a
pre-migration install may have left `dollos-bridge.service` on disk and
*enabled*. Left alone, that legacy unit auto-starts on boot and runs a
second bridge process sharing the same Discord bot token as the
daemon-internalized one — Discord's gateway rejects the second session
for the same token, so this is an active hazard, not just dead weight.
`_cleanup_legacy_bridge_unit` actively `disable --now`s and deletes it;
BOTH `install` and `uninstall` call it (Review 2 I4) — a user who
upgrades and only ever runs `uninstall` (never re-running `install`)
must not be left with an enabled legacy unit either.

Idempotency contract:
- `install` always overwrites the daemon unit file (never appends /
  stacks) and always ends with `daemon_reload()` so systemd picks up
  the new content. Write failures are not caught — surface them.
- `uninstall` is the ONE place that tolerates a `SystemctlError`: a
  `stop` on a unit that is not loaded (e.g. never installed, or
  already stopped) raises `SystemctlError`, and since we are tearing
  down anyway that failure carries no actionable information — the
  end state ("not running") is what we wanted. Deleting the unit file
  uses `missing_ok=True` for the same reason. No other function in
  `dollosctl` is allowed this leniency — this is teardown, not steady
  -state operation, and the "no fallback mechanisms" project rule
  still applies everywhere else. `_cleanup_legacy_bridge_unit` follows
  this same teardown-leniency pattern (tolerates `SystemctlError` from
  `disable_now`, `missing_ok=True` unlink) since it is teardown of a
  unit that, by definition, may never have existed on this machine.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dollos.ctl import systemctl
from dollos.ctl.systemctl import SystemctlError
from dollos.ctl.units import render_daemon_unit, resolve_params

logger = logging.getLogger(__name__)

DAEMON_UNIT = "dollos-daemon.service"
BRIDGE_UNIT = "dollos-bridge.service"  # legacy-cleanup only — no longer installed


def _user_unit_dir() -> Path:
    """Default systemd `--user` unit directory: `~/.config/systemd/user`."""
    return Path.home() / ".config" / "systemd" / "user"


def _cleanup_legacy_bridge_unit(unit_dir: Path) -> None:
    """Actively disable + delete a pre-migration `dollos-bridge.service`.

    Migration note (spec §7): the bridge is now a daemon-managed
    subprocess, not a systemd unit. A legacy unit left on disk and
    *enabled* would auto-start on boot and run a second bridge process
    sharing the same Discord bot token as the daemon-internalized one —
    Discord's gateway rejects the second session for that token, so this
    is a hazard, not just dead weight.

    Idempotent / failure-tolerant like `uninstall` (see module
    docstring): `disable --now` on a unit that was never installed
    raises `SystemctlError`, which carries no actionable information
    here — "not present" is the desired end state, not a failure.
    """
    try:
        systemctl.disable_now(BRIDGE_UNIT)
    except SystemctlError as exc:
        logger.info(
            "legacy bridge unit cleanup: disable --now %s failed, continuing: %s",
            BRIDGE_UNIT,
            exc,
        )
    (unit_dir / BRIDGE_UNIT).unlink(missing_ok=True)


def install(
    *,
    unit_dir: Path,
    daemon_config: Path,
    data_root: Path,
    python: str | None = None,
    working_dir: Path | None = None,
) -> None:
    """Render and write the daemon unit to `unit_dir`, then daemon-reload.

    Also actively cleans up a legacy `dollos-bridge.service` (see
    `_cleanup_legacy_bridge_unit` / module docstring) — the daemon now
    internalizes the bridge as a supervised subprocess, so a leftover
    enabled bridge unit is a token-clash hazard, not just dead weight.

    Idempotent: re-running overwrites the daemon unit file in place
    (does not append or stack). Write failures are not caught — no
    fallback.
    """
    params = resolve_params(
        daemon_config=daemon_config,
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
    _cleanup_legacy_bridge_unit(unit_dir)
    (unit_dir / DAEMON_UNIT).write_text(render_daemon_unit(params))
    systemctl.daemon_reload()


def uninstall(*, unit_dir: Path) -> None:
    """Stop the daemon unit, delete its unit file, then daemon-reload.

    Also actively cleans up a legacy `dollos-bridge.service` (see
    `_cleanup_legacy_bridge_unit` / module docstring) — this runs
    unconditionally so a user who upgrades and only ever runs
    `uninstall` (never re-running `install`) doesn't leave an enabled
    legacy bridge unit behind.

    Idempotent: safe to call when the unit was never installed, is
    already stopped, or the file is already gone. `stop` on a unit
    that isn't loaded raises `SystemctlError` — that is a tolerated
    error in this codebase (see module docstring): we are tearing
    down, so "already not running" is a success, not a failure, and
    must not abort the rest of the teardown.
    """
    _cleanup_legacy_bridge_unit(unit_dir)

    try:
        systemctl.stop(DAEMON_UNIT)
    except SystemctlError as exc:
        logger.info("uninstall: stop(%s) failed, continuing teardown: %s", DAEMON_UNIT, exc)

    (unit_dir / DAEMON_UNIT).unlink(missing_ok=True)
    systemctl.daemon_reload()


def _build_parser() -> argparse.ArgumentParser:
    """Build the `dollosctl` argparse parser (pure — no dispatch logic)."""
    parser = argparse.ArgumentParser(
        prog="dollosctl", description="Manage DollOS systemd --user services."
    )
    subparsers = parser.add_subparsers(dest="command")

    install_parser = subparsers.add_parser(
        "install",
        help="Render + write the daemon systemd --user unit "
        "(and clean up a legacy dollos-bridge.service, if present).",
    )
    install_parser.add_argument("--daemon-config", type=Path, required=True)
    install_parser.add_argument("--data-root", type=Path, default=Path("data"))
    install_parser.add_argument("--unit-dir", type=Path, default=None)

    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="Stop and remove the daemon systemd --user unit "
        "(and clean up a legacy dollos-bridge.service, if present).",
    )
    uninstall_parser.add_argument("--unit-dir", type=Path, default=None)

    subparsers.add_parser("start", help="Start the daemon unit.")
    subparsers.add_parser("stop", help="Stop the daemon unit.")
    subparsers.add_parser("restart", help="Restart the daemon unit.")
    subparsers.add_parser("status", help="Show systemd status for the daemon unit.")

    logs_parser = subparsers.add_parser(
        "logs", help="Show or follow the daemon unit's journal (the bridge runs inside it)."
    )
    logs_parser.add_argument("which", choices=["daemon"])
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
                data_root=args.data_root,
            )
        elif args.command == "uninstall":
            uninstall(unit_dir=args.unit_dir if args.unit_dir is not None else _user_unit_dir())
        elif args.command == "start":
            systemctl.start(DAEMON_UNIT)
        elif args.command == "stop":
            systemctl.stop(DAEMON_UNIT)
        elif args.command == "restart":
            systemctl.restart(DAEMON_UNIT)
        elif args.command == "status":
            print(systemctl.status(DAEMON_UNIT))
        elif args.command == "logs":
            # `which` choices are restricted to ["daemon"] — the bridge is a
            # supervised subprocess now, its output lives in this same journal.
            systemctl.journal(DAEMON_UNIT, follow=args.follow, lines=args.lines)
    except SystemctlError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0
