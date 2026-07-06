"""Fatal discord-bridge connection errors exit non-zero instead of hanging or
retrying forever (Task B2, spec §3.4a).

The bridge's #1 real failure mode is a bad/revoked/expired Discord bot
token. Before this fix it did NOT surface as a process exit at all:
`_connect_and_run` hung forever inside a bare `await
discord.wait_until_ready()` (login never reaches READY on a bad token), or
— if it somehow got past that — `run()`'s reconnect loop swallowed the
error via `except Exception` and retried every 5s forever, hammering
Discord's login endpoint. The generic `ServiceSupervisor` (Task 2, merged
c286c28) only reacts to process *exit*, so it was blind to both.

This splits FATAL (`discord.LoginFailure`) from TRANSIENT (network drops,
clean daemon-WS closes): fatal re-raises all the way out of `run()` so
`main()` returns exit code 2, giving the supervisor something to see and
its crash-loop cap something to bite on; transient stays in the existing
5s reconnect loop — a network blip must not restart the whole process.

These tests target the four load-bearing assertions directly (not full
end-to-end wiring, which `test_discord_bridge_main.py` already covers for
the happy path):
  (a) `_connect_and_run` re-raises a login failure instead of hanging in
      `wait_until_ready()`.
  (b) `run()` propagates a fatal error instead of entering the infinite
      reconnect loop.
  (c) `run()` still retries on a transient error (the fix must not turn
      every error fatal).
  (d) `main()` returns a non-zero exit code on a fatal login failure.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import discord  # py-cord; the bridge already depends on it via PycordClient
import pytest

from dollos.discord_bridge import __main__ as bridge_main
from dollos.discord_bridge.ambient_log import AmbientLog
from dollos.discord_bridge.controller import BridgeConfig


def _write_bridge_toml(tmp_path: Path) -> Path:
    path = tmp_path / "bridge.toml"
    path.write_text(
        """
[discord]
token = "tok-fatal-test"
owner_discord_id = "111"
"""
    )
    return path


def _fake_args(tmp_path: Path) -> object:
    """A real argparse.Namespace from the bridge's own parser — `run()`
    calls `_load_bridge_config(args.config)` before it ever reaches the
    reconnect loop, so the config file must actually parse."""
    return bridge_main.build_parser().parse_args(
        ["--config", str(_write_bridge_toml(tmp_path)), "--data-root", str(tmp_path)]
    )


def _fake_cfg() -> BridgeConfig:
    return BridgeConfig(owner_id="111", owner_guild_only=False, backfill_channels=[])


def _fake_ambient(tmp_path: Path) -> AmbientLog:
    return AmbientLog(tmp_path, retention_days=30)


class _FakeWS:
    """Same shape as `test_discord_bridge_main.py`'s `_FakeWS` — the daemon
    WS receive loop ends immediately (no incoming messages); it only needs
    to exist long enough for `_connect_and_run` to reach the
    wait_until_ready-vs-discord_task race."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _FakeConnect:
    def __init__(self, ws: _FakeWS) -> None:
        self._ws = ws

    async def __aenter__(self) -> _FakeWS:
        return self._ws

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _LoginFailClient:
    """Fakes the real current bug: `run()` raises `LoginFailure` right away
    (py-cord rejecting a bad token during the gateway handshake), while
    `wait_until_ready()` never completes (READY is never set because login
    never got that far) — this is exactly the hang this task fixes."""

    def __init__(self, *, token: str) -> None:
        self.token = token

    def on_message(self, cb) -> None:
        pass

    async def run(self) -> None:
        raise discord.LoginFailure("bad token")

    async def wait_until_ready(self) -> None:
        await asyncio.Event().wait()  # never resolves

    def me_id(self) -> str:
        return "bot-1"

    async def fetch_history(self, channel_id: str, limit: int) -> list[dict]:
        return []


async def test_login_failure_propagates_not_hang(monkeypatch, tmp_path):
    """discord.run() raising LoginFailure must re-raise out of
    _connect_and_run via the wait_until_ready()-vs-discord_task race, not
    hang forever in wait_until_ready(). asyncio.wait_for's timeout is the
    regression guard: if the race is ever reverted to a bare await, this
    test fails via timeout instead of hanging the whole suite."""
    monkeypatch.setattr(bridge_main, "PycordClient", _LoginFailClient)
    monkeypatch.setattr(
        bridge_main.websockets, "connect", lambda url: _FakeConnect(_FakeWS())
    )

    with pytest.raises(discord.LoginFailure):
        await asyncio.wait_for(
            bridge_main._connect_and_run(
                _fake_args(tmp_path), "tok", _fake_cfg(), _fake_ambient(tmp_path)
            ),
            timeout=2,
        )


async def test_run_exits_on_fatal_not_infinite_retry(monkeypatch, tmp_path):
    """run() must propagate a fatal LoginFailure out instead of swallowing
    it into the 5s reconnect loop forever. The outer wait_for is the
    regression guard: an infinite-retry regression times out instead of
    hanging the suite."""

    async def _boom(*a, **k):
        raise discord.LoginFailure("bad token")

    monkeypatch.setattr(bridge_main, "_connect_and_run", _boom)

    with pytest.raises(discord.LoginFailure):
        await asyncio.wait_for(bridge_main.run(_fake_args(tmp_path)), timeout=2)


async def test_run_retries_on_transient(monkeypatch, tmp_path):
    """A transient (non-LoginFailure) error must stay in the reconnect
    loop — proven by observing 2 calls into _connect_and_run rather than
    the first error propagating straight out. KeyboardInterrupt on the 2nd
    call breaks the otherwise-infinite loop deterministically (it is not
    an Exception subclass, so run()'s `except Exception` does not swallow
    it)."""
    calls: list[int] = []

    async def _drop(*a, **k):
        calls.append(1)
        if len(calls) >= 2:
            raise KeyboardInterrupt
        raise ConnectionError("transient drop")

    # Capture the REAL asyncio.sleep before patching — the replacement must
    # not call `asyncio.sleep` by name, since that name now resolves to
    # itself (bridge_main.asyncio IS the asyncio module, patched globally),
    # which would recurse.
    real_sleep = asyncio.sleep
    monkeypatch.setattr(bridge_main, "_connect_and_run", _drop)
    monkeypatch.setattr(
        bridge_main.asyncio, "sleep", lambda *_a, **_k: real_sleep(0)
    )

    with pytest.raises(KeyboardInterrupt):
        await bridge_main.run(_fake_args(tmp_path))

    assert len(calls) >= 2


def test_main_returns_nonzero_on_login_failure(monkeypatch, tmp_path):
    """main() must return a non-zero exit code on a fatal login failure so
    a supervising process (ServiceSupervisor) sees the death as abnormal,
    not clean."""

    async def _boom(_args):
        raise discord.LoginFailure("bad token")

    monkeypatch.setattr(bridge_main, "run", _boom)

    rc = bridge_main.main(
        ["--config", str(_write_bridge_toml(tmp_path)), "--data-root", str(tmp_path)]
    )
    assert rc != 0
