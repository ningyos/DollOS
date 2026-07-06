"""Tests for `dollosctl install`/`uninstall` (P1g Task 3; single-service Task D/5).

Writes real unit files to a tmp_path directory (no real systemd
touched — `systemctl.daemon_reload`/`stop`/`disable_now` are mocked).
Focuses on: idempotency (re-running install overwrites, doesn't stack;
re-running uninstall on an already-torn-down state doesn't crash), that
no secret value from the bridge config path leaks into unit content, and
(single-service migration, spec §7) that BOTH `install` and `uninstall`
actively clean up a legacy standalone `dollos-bridge.service` — the
daemon now internalizes the bridge as a supervised subprocess, so a
leftover *enabled* bridge unit would auto-start on boot and race the
internalized bridge for the same Discord token.
"""

import sys
from pathlib import Path
from unittest.mock import patch

from dollos.ctl.cli import BRIDGE_UNIT, DAEMON_UNIT, install, uninstall
from dollos.ctl.systemctl import SystemctlError


def _install_kwargs(tmp_path: Path, **overrides) -> dict:
    defaults = dict(
        unit_dir=tmp_path / "units",
        daemon_config=tmp_path / "daemon.toml",
        data_root=tmp_path / "data",
    )
    defaults.update(overrides)
    return defaults


def test_install_writes_only_daemon_unit_with_absolute_paths_and_reloads(tmp_path):
    kwargs = _install_kwargs(tmp_path)
    with (
        patch("dollos.ctl.cli.systemctl.daemon_reload") as mock_reload,
        patch("dollos.ctl.cli.systemctl.disable_now"),
    ):
        install(**kwargs)

    unit_dir = kwargs["unit_dir"]
    daemon_unit = unit_dir / DAEMON_UNIT
    bridge_unit = unit_dir / BRIDGE_UNIT
    assert daemon_unit.exists()
    assert not bridge_unit.exists()

    files = sorted(p.name for p in unit_dir.iterdir())
    assert files == [DAEMON_UNIT]

    daemon_content = daemon_unit.read_text()
    assert str(kwargs["daemon_config"].resolve()) in daemon_content

    mock_reload.assert_called_once_with()


def test_install_is_idempotent_overwrites_not_appends(tmp_path):
    kwargs = _install_kwargs(tmp_path)
    with (
        patch("dollos.ctl.cli.systemctl.daemon_reload"),
        patch("dollos.ctl.cli.systemctl.disable_now"),
    ):
        install(**kwargs)
        first_daemon_content = (kwargs["unit_dir"] / DAEMON_UNIT).read_text()
        install(**kwargs)
        second_daemon_content = (kwargs["unit_dir"] / DAEMON_UNIT).read_text()

    files = sorted(p.name for p in kwargs["unit_dir"].iterdir())
    assert files == [DAEMON_UNIT]
    assert first_daemon_content == second_daemon_content


def test_install_creates_unit_dir_if_missing(tmp_path):
    kwargs = _install_kwargs(tmp_path, unit_dir=tmp_path / "nested" / "units")
    with (
        patch("dollos.ctl.cli.systemctl.daemon_reload"),
        patch("dollos.ctl.cli.systemctl.disable_now"),
    ):
        install(**kwargs)
    assert (kwargs["unit_dir"] / DAEMON_UNIT).exists()


def test_install_cleans_up_legacy_bridge_unit_file_and_disables_it(tmp_path):
    """A pre-migration install left `dollos-bridge.service` on disk and
    enabled. A fresh `install` must actively `disable --now` it and
    delete the file — otherwise it auto-starts on boot and races the
    daemon-internalized bridge for the same Discord token (spec §7)."""
    kwargs = _install_kwargs(tmp_path)
    unit_dir = kwargs["unit_dir"]
    unit_dir.mkdir(parents=True)
    legacy_bridge = unit_dir / BRIDGE_UNIT
    legacy_bridge.write_text("[Unit]\nDescription=legacy\n")

    with (
        patch("dollos.ctl.cli.systemctl.daemon_reload"),
        patch("dollos.ctl.cli.systemctl.disable_now") as mock_disable_now,
    ):
        install(**kwargs)

    mock_disable_now.assert_called_once_with(BRIDGE_UNIT)
    assert not legacy_bridge.exists()
    assert (unit_dir / DAEMON_UNIT).exists()


def test_install_tolerates_disable_now_failure_on_legacy_bridge_unit(tmp_path):
    """If the legacy bridge unit was never installed (fresh machine),
    `disable --now` fails with `SystemctlError` — install must swallow
    that and proceed (idempotent, teardown-leniency, same pattern as
    `uninstall`'s existing tolerance for `stop` on a not-loaded unit)."""
    kwargs = _install_kwargs(tmp_path)
    with (
        patch("dollos.ctl.cli.systemctl.daemon_reload") as mock_reload,
        patch(
            "dollos.ctl.cli.systemctl.disable_now",
            side_effect=SystemctlError(
                ["systemctl", "--user", "disable", "--now", BRIDGE_UNIT], 5, "Unit not loaded."
            ),
        ) as mock_disable_now,
    ):
        install(**kwargs)  # must not raise

    mock_disable_now.assert_called_once_with(BRIDGE_UNIT)
    assert (kwargs["unit_dir"] / DAEMON_UNIT).exists()
    mock_reload.assert_called_once_with()


def test_uninstall_stops_daemon_unit_deletes_file_and_reloads(tmp_path):
    kwargs = _install_kwargs(tmp_path)
    unit_dir = kwargs["unit_dir"]
    with (
        patch("dollos.ctl.cli.systemctl.daemon_reload"),
        patch("dollos.ctl.cli.systemctl.disable_now"),
    ):
        install(**kwargs)

    with (
        patch("dollos.ctl.cli.systemctl.stop") as mock_stop,
        patch("dollos.ctl.cli.systemctl.disable_now"),
        patch("dollos.ctl.cli.systemctl.daemon_reload") as mock_reload,
    ):
        uninstall(unit_dir=unit_dir)

    mock_stop.assert_called_once_with(DAEMON_UNIT)
    assert not (unit_dir / DAEMON_UNIT).exists()
    mock_reload.assert_called_once_with()


def test_uninstall_missing_files_does_not_crash(tmp_path):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    with (
        patch("dollos.ctl.cli.systemctl.stop"),
        patch("dollos.ctl.cli.systemctl.disable_now"),
        patch("dollos.ctl.cli.systemctl.daemon_reload") as mock_reload,
    ):
        uninstall(unit_dir=unit_dir)  # no files present at all
    mock_reload.assert_called_once_with()


def test_uninstall_tolerates_systemctl_error_from_stop_on_not_loaded_unit(tmp_path):
    kwargs = _install_kwargs(tmp_path)
    unit_dir = kwargs["unit_dir"]
    with (
        patch("dollos.ctl.cli.systemctl.daemon_reload"),
        patch("dollos.ctl.cli.systemctl.disable_now"),
    ):
        install(**kwargs)

    with (
        patch(
            "dollos.ctl.cli.systemctl.stop",
            side_effect=SystemctlError(["systemctl", "--user", "stop", "x"], 5, "Unit not loaded."),
        ) as mock_stop,
        patch("dollos.ctl.cli.systemctl.disable_now"),
        patch("dollos.ctl.cli.systemctl.daemon_reload") as mock_reload,
    ):
        uninstall(unit_dir=unit_dir)  # must not raise

    assert mock_stop.call_count == 1
    assert not (unit_dir / DAEMON_UNIT).exists()
    mock_reload.assert_called_once_with()


def test_uninstall_cleans_up_legacy_bridge_unit_file_and_disables_it(tmp_path):
    """Spec §7 (Review 2 I4): a user who upgrades and only ever runs
    `dollosctl uninstall` (never re-running `install`) must not be left
    with an enabled legacy bridge unit — `uninstall` alone must clean it
    up too, not just `install`."""
    kwargs = _install_kwargs(tmp_path)
    unit_dir = kwargs["unit_dir"]
    unit_dir.mkdir(parents=True)
    legacy_bridge = unit_dir / BRIDGE_UNIT
    legacy_bridge.write_text("[Unit]\nDescription=legacy\n")

    with (
        patch("dollos.ctl.cli.systemctl.stop"),
        patch("dollos.ctl.cli.systemctl.disable_now") as mock_disable_now,
        patch("dollos.ctl.cli.systemctl.daemon_reload"),
    ):
        uninstall(unit_dir=unit_dir)

    mock_disable_now.assert_called_once_with(BRIDGE_UNIT)
    assert not legacy_bridge.exists()


def test_uninstall_tolerates_disable_now_failure_on_legacy_bridge_unit(tmp_path):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    with (
        patch("dollos.ctl.cli.systemctl.stop"),
        patch(
            "dollos.ctl.cli.systemctl.disable_now",
            side_effect=SystemctlError(
                ["systemctl", "--user", "disable", "--now", BRIDGE_UNIT], 5, "Unit not loaded."
            ),
        ) as mock_disable_now,
        patch("dollos.ctl.cli.systemctl.daemon_reload") as mock_reload,
    ):
        uninstall(unit_dir=unit_dir)  # must not raise

    mock_disable_now.assert_called_once_with(BRIDGE_UNIT)
    mock_reload.assert_called_once_with()


def test_install_prints_resolved_python_working_dir_and_data_root(tmp_path, capsys):
    """The data-dir-cwd footgun (P1g whole-branch review, Important #1): the
    daemon resolves its data root relative to WorkingDirectory, which is
    itself captured from cwd at install time. `install` must echo the
    resolved absolutes so the operator can catch a wrong-cwd install before
    it silently starts a fresh empty data store."""
    kwargs = _install_kwargs(tmp_path)
    with (
        patch("dollos.ctl.cli.systemctl.daemon_reload"),
        patch("dollos.ctl.cli.systemctl.disable_now"),
    ):
        install(**kwargs)

    captured = capsys.readouterr()
    assert sys.executable in captured.out
    assert str(Path.cwd().resolve()) in captured.out  # working_dir defaults to cwd
    assert str(kwargs["data_root"].resolve()) in captured.out


def test_no_secret_token_leaks_into_unit_content(tmp_path):
    """The daemon unit references the daemon config *path* only; nothing
    from a config file's contents (e.g. a Discord token living inside a
    bridge config the daemon points at) is ever interpolated into it."""
    secret_token = "sk-super-secret-discord-token-abc123"
    daemon_config = tmp_path / "daemon.toml"
    daemon_config.write_text(f'[bridge]\nconfig = "bridge.toml"\n# token = "{secret_token}"\n')

    kwargs = _install_kwargs(tmp_path, daemon_config=daemon_config)
    with (
        patch("dollos.ctl.cli.systemctl.daemon_reload"),
        patch("dollos.ctl.cli.systemctl.disable_now"),
    ):
        install(**kwargs)

    unit_dir = kwargs["unit_dir"]
    daemon_content = (unit_dir / DAEMON_UNIT).read_text()

    assert secret_token not in daemon_content
    # sanity: the path itself IS present (that's the whole point)
    assert str(daemon_config.resolve()) in daemon_content


def test_legacy_bridge_cleanup_call_order_is_disable_before_unlink(tmp_path):
    """Not strictly required for correctness, but pins the documented
    order (disable_now, then unlink) so a future edit doesn't silently
    reorder these into something systemd-unfriendly (e.g. deleting the
    file before `disable --now` can still find it)."""
    kwargs = _install_kwargs(tmp_path)
    unit_dir = kwargs["unit_dir"]
    unit_dir.mkdir(parents=True)
    legacy_bridge = unit_dir / BRIDGE_UNIT
    legacy_bridge.write_text("[Unit]\nDescription=legacy\n")

    calls = []

    def _record_disable_now(unit):
        calls.append(("disable_now", unit, legacy_bridge.exists()))

    with (
        patch("dollos.ctl.cli.systemctl.daemon_reload"),
        patch("dollos.ctl.cli.systemctl.disable_now", side_effect=_record_disable_now),
    ):
        install(**kwargs)

    assert calls == [("disable_now", BRIDGE_UNIT, True)]  # file still existed when disable ran
