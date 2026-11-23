"""The bridge against a fake Discord (messages, the owner's DM channel) and a fake dollcore."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace as NS
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from dollbridge.__main__ import intents, load_config
from dollbridge.api import CoreAPI
from dollbridge.core import DISCORD_LIMIT, Bridge, owner_text, pump, split_message
from dollbridge.voice import VoiceConfig

OWNER, OTHER, BOT = 111, 222, 999


class FakeCore:
    def __init__(self, fail: Exception | None = None) -> None:
        self.posted: list[str] = []
        self.strangers: list[tuple[int, str, str | None, str]] = []
        self.fail = fail

    async def post_owner(self, text: str) -> None:
        if self.fail:
            raise self.fail
        self.posted.append(text)

    async def post_stranger(self, user_id: int, name: str, channel: str | None, text: str) -> None:
        if self.fail:
            raise self.fail
        self.strangers.append((user_id, name, channel, text))


class FakeDM:
    def __init__(self) -> None:
        self.sent: list[str | None] = []
        #: The file each send carried (None for none), beside `sent`.
        self.files: list[Any] = []
        self.typing = 0

    async def send(self, content: str | None = None, *, file: Any = None) -> None:
        self.sent.append(content)
        self.files.append(file)

    async def trigger_typing(self) -> None:
        self.typing += 1


def message(author: int, content: str = "", guild: int | None = None, files: tuple[str, ...] = (),
            channel: int = 5, mentions: tuple[int, ...] = ()) -> Any:
    return NS(author=NS(id=author, display_name=f"user{author}"), content=content, guild=NS(id=guild) if guild else None,
              channel=NS(id=channel, name="聊天"), attachments=[NS(filename=f) for f in files],
              mentions=[NS(id=m) for m in mentions])


def bridge(core: FakeCore | None = None, typing_every_s: float = 8.0) -> tuple[Bridge, FakeCore, FakeDM]:
    core = core or FakeCore()
    b = Bridge(OWNER, core, self_id=lambda: BOT, attach=fake_file, typing_every_s=typing_every_s)
    b.dm = FakeDM()
    return b, core, b.dm


async def test_owner_dm_is_forwarded() -> None:
    b, core, _ = bridge()
    await b.on_message(message(OWNER, "今天好累"))
    assert core.posted == ["今天好累"]


async def test_guild_messages_outside_her_channel_or_without_a_mention_are_dropped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    b, core, dm = bridge()
    b.channel_id = 7
    with caplog.at_level(logging.INFO, logger="dollbridge"):
        await b.on_message(message(OWNER, "在伺服器講的", guild=42))
        await b.on_message(message(OTHER, "伺服器", guild=42, channel=5, mentions=(BOT,)))
        await b.on_message(message(OTHER, "沒叫她", guild=42, channel=7))
    assert core.posted == [] and core.strangers == [] and dm.sent == []
    dropped = [r.getMessage() for r in caplog.records if "dropped" in r.getMessage()]
    assert len(dropped) == 3 and "guild 42" in dropped[0]
    # The content never reaches the log.
    assert not any("伺服器" in m or "沒叫她" in m for m in dropped)


async def test_a_strangers_dm_and_a_mention_in_her_channel_go_to_her_as_a_strangers_line() -> None:
    b, core, _ = bridge()
    b.channel_id = 7
    await b.on_message(message(OTHER, "嗨"))
    await b.on_message(message(OTHER, f"<@{BOT}> 你好", guild=42, channel=7, mentions=(BOT,)))
    assert core.strangers == [(OTHER, "user222", None, "嗨"), (OTHER, "user222", "聊天", "你好")]
    assert core.posted == []


async def test_her_words_to_a_stranger_go_to_the_channel_or_their_dm() -> None:
    b, _, owner_dm = bridge()
    ch, their = FakeDM(), FakeDM()
    b.channel = ch

    async def open_dm(user_id: int) -> FakeDM:
        assert user_id == OTHER
        return their

    b.open_dm = open_dm
    await b.on_core({"type": "send", "to": "channel", "text": "大家好"})
    await b.on_core({"type": "send", "to": f"dm:{OTHER}", "text": "私下說"})
    assert ch.sent == ["大家好"] and their.sent == ["私下說"] and owner_dm.sent == []


async def test_her_own_messages_are_ignored_silently(caplog: pytest.LogCaptureFixture) -> None:
    b, core, _ = bridge()
    with caplog.at_level(logging.INFO, logger="dollbridge"):
        await b.on_message(message(BOT, "我自己說的"))
    assert core.posted == [] and caplog.records == []


async def test_attachments_become_lines() -> None:
    b, core, _ = bridge()
    await b.on_message(message(OWNER, "看這個", files=("cat.png", "notes.txt")))
    await b.on_message(message(OWNER, "", files=("only.pdf",)))
    assert core.posted == ["看這個\n[附件：cat.png]\n[附件：notes.txt]", "[附件：only.pdf]"]
    assert owner_text("  ", []) == ""


async def test_undelivered_owner_dm_is_told_to_the_owner() -> None:
    b, _, dm = bridge(FakeCore(fail=RuntimeError("dollcore answered 503")))
    await b.on_message(message(OWNER, "在嗎"))
    assert dm.sent == ["[系統] 這則沒送到她那裡：dollcore answered 503"]


def fake_file(path: str) -> tuple[str, str]:
    """What `attach` makes of a screenshot path in these tests; a missing one fails like discord.File."""
    if path.startswith("/missing"):
        raise FileNotFoundError(f"No such file: {path}")
    return ("file", path)


async def test_a_say_with_an_image_carries_it_on_its_last_piece() -> None:
    b, _, dm = bridge()
    await b.on_core({"type": "say", "text": "算好了，是 1,702", "image": "/shots/a.jpg"})
    assert dm.sent == ["算好了，是 1,702"] and dm.files == [("file", "/shots/a.jpg")]
    long = "\n".join(f"第 {i} 行" + "魚" * 90 for i in range(60))
    await b.on_core({"type": "say", "text": long, "image": "/shots/b.jpg"})
    assert dm.files[1:] == [None, None, ("file", "/shots/b.jpg")]
    # Without an image nothing is attached.
    await b.on_core({"type": "say", "text": "好"})
    assert dm.files[-1] is None


async def test_an_image_that_cannot_be_attached_still_sends_her_words_and_says_so() -> None:
    b, _, dm = bridge()
    await b.on_core({"type": "say", "text": "做好了", "image": "/missing/c.jpg"})
    assert dm.sent == ["做好了", "[系統] 截圖沒附上：No such file: /missing/c.jpg"]
    assert dm.files == [None, None]


async def test_say_goes_to_the_owner_dm_split_at_2000() -> None:
    b, _, dm = bridge()
    long = "\n".join(f"第 {i} 行" + "魚" * 90 for i in range(60))   # about 5.8k characters
    await b.on_core({"type": "say", "text": long})
    assert len(dm.sent) == 3
    assert all(len(p) <= DISCORD_LIMIT for p in dm.sent)
    assert "\n".join(dm.sent) == long          # cut only at line ends, nothing lost
    await b.on_core({"type": "say", "text": "短的"})
    assert dm.sent[-1] == "短的"


def test_split_cuts_inside_a_line_only_when_it_must() -> None:
    assert split_message("a" * 4500) == ["a" * 2000, "a" * 2000, "a" * 500]
    assert split_message("x\n" + "b" * 2001) == ["x", "b" * 2000, "b"]
    assert split_message("") == [] and split_message("\n \n") == []
    assert split_message("一\n\n二") == ["一\n\n二"]


async def test_typing_shows_while_her_turn_runs() -> None:
    b, _, dm = bridge(typing_every_s=0.02)
    await b.on_core({"type": "typing", "on": True})
    await asyncio.sleep(0.07)
    shown = dm.typing
    assert shown >= 2                          # re-triggered while the turn runs
    await b.on_core({"type": "typing", "on": False})
    await asyncio.sleep(0.05)
    assert dm.typing == shown                  # stopped
    await b.on_core({"type": "typing", "on": True})
    await asyncio.sleep(0.01)
    await b.on_core({"type": "say", "text": "好了"})   # her say ends the indicator too
    after = dm.typing
    await asyncio.sleep(0.05)
    assert dm.typing == after and dm.sent == ["好了"]


def test_config(tmp_path: Any) -> None:
    f = tmp_path / "discord.toml"
    f.write_text('token = "abc"\nowner_id = 123\n')
    c = load_config(f)
    assert (c.token, c.owner_id, c.voice) == ("abc", 123, VoiceConfig())
    f.write_text('token = "abc"\nowner_id = "123"\n')
    assert (load_config(f).token, load_config(f).owner_id) == ("abc", 123)
    for bad in ['token = ""\nowner_id = 1\n', 'token = "a"\n', 'token = "a"\nowner_id = 1\nx = 2\n']:
        f.write_text(bad)
        with pytest.raises(ValueError):
            load_config(f)


def test_intents() -> None:
    i = intents()
    assert i.dm_messages and i.message_content and i.guild_messages and not i.members and not i.presences
    assert i.voice_states


# The HTTP contract against a fake dollcore -------------------------------------------------------

async def fake_dollcore(events: list[dict[str, Any]]) -> tuple[web.AppRunner, str, list[Any]]:
    seen: list[Any] = []

    async def owner(req: web.Request) -> web.Response:
        if req.headers.get("Authorization") != "Bearer k":
            return web.json_response({"error": "owner key required"}, status=401)
        seen.append(await req.json())
        return web.json_response({"queued": True})

    async def listen(req: web.Request) -> web.StreamResponse:
        if req.headers.get("Authorization") != "Bearer k" or req.query.get("to") != "owner":
            return web.json_response({"error": "no"}, status=401)
        r = web.StreamResponse()
        await r.prepare(req)
        for e in [{"type": "hello"}] + events:
            await r.write((json.dumps(e) + "\n").encode())
        return r

    app = web.Application()
    app.router.add_post("/api/owner", owner)
    app.router.add_get("/api/listen", listen)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}", seen


async def test_core_api_round_trip_through_the_bridge() -> None:
    runner, base, seen = await fake_dollcore([{"type": "typing", "on": True}, {"type": "say", "text": "主人好"}])
    try:
        async with aiohttp.ClientSession() as s:
            core = CoreAPI(base, "k", s)
            b = Bridge(OWNER, core, self_id=lambda: BOT, attach=fake_file)
            b.dm = dm = FakeDM()
            await b.on_message(message(OWNER, "早安"))
            assert seen == [{"text": "早安"}]
            with pytest.raises(ConnectionError, match="closed the stream"):
                await pump(core.listen, b.on_core)
            assert dm.sent == ["主人好"]
            with pytest.raises(RuntimeError, match="401"):
                await CoreAPI(base, "wrong", s).post_owner("x")
    finally:
        await runner.cleanup()
