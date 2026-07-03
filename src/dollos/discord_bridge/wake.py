"""L0 wake rules + self-filter (spec §3.3/§3.4 L0 + C3).

`l0_wake` is a pure function: given a plain-dict Discord message event and
the bridge's static config, it decides whether the message should wake
Doll's attention (become a `ChannelEvent` → `ChannelMessage` perception) or
stay ambient-log-only (still captured by `AmbientLog`, just never surfaced).

Self-filter (spec §3.3 C3) runs FIRST and unconditionally: the bot's own
messages must never wake her, no matter what else is true about them (e.g.
her own name appearing in her own message). The caller is responsible for
still writing self-authored events to the ambient log — this function only
decides the wake/no-wake question.
"""
from __future__ import annotations


def l0_wake(
    event: dict,
    *,
    bot_id: str,
    owner_id: str,
    name_aliases: list[str],
    always_wake_channels: set[str],
) -> bool:
    """Return True if `event` should wake Doll's attention.

    Args:
        event: plain-dict message event with at least author_id, is_dm,
            mentioned, content, channel_id.
        bot_id: Doll's own Discord user id — self-filter target.
        owner_id: accepted for interface stability with the rest of P1b's
            author_is_owner plumbing (computed by the caller when building
            the ChannelMessage perception); L0 itself does not gate on it.
        name_aliases: substrings of `content` that count as being addressed
            by name (spec §3.4 L0).
        always_wake_channels: channel_ids that always wake regardless of
            content (e.g. a DM-like "always listening" channel).

    Returns:
        False if the message is self-authored (hard self-filter, checked
        first). Otherwise True if the message is a DM, mentions the bot,
        contains a name alias substring, or is in an always-wake channel.
        False otherwise (ambient-log-only public chatter).
    """
    if event["author_id"] == bot_id:
        return False

    if event["is_dm"]:
        return True
    if event["mentioned"]:
        return True
    if any(alias in event["content"] for alias in name_aliases):
        return True
    if event["channel_id"] in always_wake_channels:
        return True

    return False
