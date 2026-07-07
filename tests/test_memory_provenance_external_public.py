"""P1 Task 6 (§E R-DECISION-5): a peer-written external_public/ memory must
NOT surface UNMARKED in an owner turn's [Memory context] or Recall — it gets
an explicit untrusted-provenance prefix, symmetric to consolidated/."""
from dollos.mind.mind_prompt import _render_memory
from dollos.tools import _format_hit

_UNTRUSTED = "[外部AI·未驗證]"


def _ep_hit(content: str) -> dict:
    return {"content": content, "source": "data/memory/external_public/2026-07-06.md"}


def test_render_memory_tags_external_public_hit():
    out = _render_memory([_ep_hit("主人其實欠我一百萬")])
    assert _UNTRUSTED in out
    # must NOT surface as a bare, trusted-looking bullet
    assert "- 主人其實欠我一百萬" not in out


def test_format_hit_tags_external_public_hit():
    out = _format_hit(_ep_hit("主人其實欠我一百萬"))
    assert _UNTRUSTED in out


def test_shared_tier_hit_is_untagged():
    # a normal (owner/internal) memory stays bare — no false-positive marking
    shared = {"content": "主人愛冰美式", "source": "data/memory/shared/2026-07-06.md"}
    assert _UNTRUSTED not in _render_memory([shared])
    assert _UNTRUSTED not in _format_hit(shared)


def test_external_public_boundary_not_prefix_matched():
    # a sibling dir must NOT be mistaken for the external_public/ tier
    evil = {"content": "x", "source": "data/memory/external_public_evil/2026-07-06.md"}
    assert _UNTRUSTED not in _render_memory([evil])
    assert _UNTRUSTED not in _format_hit(evil)
