"""systemctl/journalctl `--user` subprocess wrappers for DollOS services.

Every invocation goes through `--user` — never system-level. Argv
construction is split out into pure functions (`build_systemctl_argv`,
`build_journal_argv`) so tests can assert the exact command shape
without mocking anything; `_run` is the single place that actually
calls `subprocess.run`, so tests that need to fake a systemd result
mock exactly one function.

No-fallback: action verbs (start/stop/restart/enable --now/disable
--now/daemon-reload) raise `SystemctlError` on non-zero exit, carrying
the argv + stderr — callers must see the failure, not have it silently
swallowed. `status` and `is_active` are queries, not actions: systemd
uses non-zero exit to *report* "inactive", which is a valid answer, not
a failure — they tolerate non-zero and return normally.
"""

from __future__ import annotations

import os
import subprocess


class SystemctlError(RuntimeError):
    """Raised when a systemctl/journalctl action verb exits non-zero."""

    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"command {argv!r} failed with exit code {returncode}: {stderr.strip()}"
        )


def build_systemctl_argv(verb: str, unit: str | None = None) -> list[str]:
    """Build the argv for a `systemctl --user <verb> [unit]` invocation.

    Pure — no subprocess. `verb` may itself contain multiple words
    (e.g. ``"enable --now"``) for verbs systemd treats as a single
    action with a flag.
    """
    argv = ["systemctl", "--user", *verb.split()]
    if unit:
        argv.append(unit)
    return argv


def build_journal_argv(unit: str, *, follow: bool = False, lines: int = 200) -> list[str]:
    """Build the argv for a `journalctl --user -u <unit> [-f] -n <lines>` invocation.

    Pure — no subprocess. `-f` (if present) precedes `-n`.
    """
    argv = ["journalctl", "--user", "-u", unit]
    if follow:
        argv.append("-f")
    argv += ["-n", str(lines)]
    return argv


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    """The single subprocess call site. Tests mock this, not `subprocess` directly."""
    return subprocess.run(argv, capture_output=True, text=True)


def _run_action(argv: list[str]) -> subprocess.CompletedProcess:
    """Run an action-verb argv; raise `SystemctlError` on non-zero exit (no-fallback)."""
    result = _run(argv)
    if result.returncode != 0:
        raise SystemctlError(argv, result.returncode, result.stderr)
    return result


def daemon_reload() -> None:
    _run_action(build_systemctl_argv("daemon-reload"))


def start(unit: str) -> None:
    _run_action(build_systemctl_argv("start", unit))


def stop(unit: str) -> None:
    _run_action(build_systemctl_argv("stop", unit))


def restart(unit: str) -> None:
    _run_action(build_systemctl_argv("restart", unit))


def enable_now(unit: str) -> None:
    _run_action(build_systemctl_argv("enable --now", unit))


def disable_now(unit: str) -> None:
    _run_action(build_systemctl_argv("disable --now", unit))


def status(unit: str) -> str:
    """Return `systemctl --user status <unit>` stdout.

    `status` is a query: systemd exits non-zero for an inactive/dead
    unit even though the query itself succeeded. Do not raise on
    non-zero here — return whatever stdout it produced.
    """
    result = _run(build_systemctl_argv("status", unit))
    return result.stdout


def is_active(unit: str) -> bool:
    """Return whether `unit` is active.

    `is-active` is a query: it exits non-zero for inactive/failed
    units. Do not raise on non-zero — just report False.
    """
    result = _run(build_systemctl_argv("is-active", unit))
    return result.stdout.strip() == "active"


def journal(unit: str, *, follow: bool = False, lines: int = 200) -> str | None:
    """Run `journalctl --user -u <unit> ...` and return output (or stream it).

    `follow=True` is an interactive tail — it execs `journalctl`
    directly (no capture) so it streams straight to the terminal and
    never returns. `follow=False` runs it, prints the captured output,
    and returns it as a string.
    """
    argv = build_journal_argv(unit, follow=follow, lines=lines)
    if follow:
        os.execvp(argv[0], argv)  # noqa: S606 — intentional exec, this is a CLI tool
    result = _run(argv)
    print(result.stdout)
    return result.stdout
