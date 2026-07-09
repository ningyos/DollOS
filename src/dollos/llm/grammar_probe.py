"""啟動時探測 llama-server 是否支援 GBNF bounded repetition `{m,n}`。

本專案的 think 行長綁定（spec §3.1）依賴 `[^\\n]{1,64}`。過舊的 llama.cpp
不支援 `{m,n}`，會在**每一回合**的請求級吐 HTTP error——那不是 Python
build-time raise（grammar 只是字串組裝），no-fallback 章節接不到。故在啟動
時送一次最小探測，fail-closed：拒絕啟動勝過每回合對話崩潰。（spec §11 / R1-7）
"""

from __future__ import annotations

from contextlib import aclosing

from dollos.llm.adapter import LLMAdapter

_PROBE_GRAMMAR = "root ::= [a]{1,2}\n"


async def assert_bounded_repetition_supported(llm: LLMAdapter) -> None:
    """Send one minimal-cost completion using a `{m,n}` GBNF grammar.

    Returns ``None`` on success. Raises ``RuntimeError`` if the backend
    rejects the grammar or the call otherwise errors — fail-closed, so a
    too-old llama-server aborts the daemon boot instead of breaking every
    conversation turn later.
    """
    try:
        stream = llm.stream_completion(
            system="",
            user="a",
            prefill="",
            max_tokens=2,
            grammar=_PROBE_GRAMMAR,
            purpose="startup_probe",
        )
        async with aclosing(stream) as s:
            async for chunk in s:
                if getattr(chunk, "done", False):
                    break
    except Exception as e:
        raise RuntimeError(
            "llama-server 不支援 GBNF bounded repetition `{m,n}`；"
            "DollOS 的 think 行長綁定需要它。請升級 llama.cpp "
            f"（原始錯誤：{e!r}）"
        ) from e
