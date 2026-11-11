"""dollcore's local API over HTTP (aiohttp, which py-cord already depends on)."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import aiohttp


class CoreAPI:
    def __init__(self, base: str, owner_key: str, session: aiohttp.ClientSession) -> None:
        self.base = base.rstrip("/")
        self.session = session
        self.auth = {"Authorization": f"Bearer {owner_key}"}

    async def post_owner(self, text: str) -> None:
        async with self.session.post(f"{self.base}/api/owner", json={"text": text}, headers=self.auth,
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                raise RuntimeError(f"dollcore answered {r.status}: {await r.text()}")

    async def listen(self) -> AsyncIterator[dict[str, Any]]:
        """The owner stream: one dict per line, until dollcore closes it."""
        async with self.session.get(f"{self.base}/api/listen", params={"to": "owner"}, headers=self.auth,
                                    timeout=aiohttp.ClientTimeout(total=None, sock_read=None)) as r:
            if r.status != 200:
                raise RuntimeError(f"dollcore refused the owner stream: {r.status} {await r.text()}")
            async for raw in r.content:
                line = raw.strip()
                if line:
                    yield json.loads(line)
