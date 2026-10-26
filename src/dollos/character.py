"""Doll pack: directory-as-pack character config (TOML manifest).

A pack is a directory containing `doll.toml` with `[meta]` (id, name) and
`[identity]` (self / personality / taboos Markdown blobs). Renderer-side,
identity is laid out by `scaffolding.jinja` — TOML supplies structure,
Markdown supplies content.

Note on the `Identity.self` field name: it shadows Python's `self`
keyword in method context. Pydantic v2 handles this fine for data fields
(field is accessed by name on the instance dict). Don't add methods to
Identity that would need to reference the field as `self.self`; access
via attribute on a different instance handle (`obj.self` from outside)
or via `instance.__dict__["self"]` if needed.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# 2026-10-25 base-persona-template spec §4: customer-service phrases every
# pack bans by default, per base-persona language. Merged into the pack's own
# ``banned_substrings`` at load (see ``DollPack.load``); a pack opts out of
# individual entries with ``[enforcement].base_exempt``.
BASE_BANNED: dict[str, list[str]] = {
    "zh-TW": ["好問題", "很高興為你服務", "有什麼我可以幫", "希望這對你有幫助", "非常抱歉造成"],
    "en": ["Great question", "How can I help", "I hope this helps", "As an AI"],
}


class PackMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    # D1(b), 2026-07-06 self-learned-aliases spec §3.4/§7: optional extra
    # permanent seed names (e.g. a CJK proper name alongside the ASCII
    # ``name``) unioned into the L0 wake-eligible set alongside ``name`` —
    # kernel.py's alias_provider treats {name} ∪ set(aliases) as an
    # un-prunable seed floor. Still subject to the SAME load-time mechanical
    # guard as any other seed/floor token (a denylisted/too-short entry here
    # is dropped + logged, not silently trusted just because it's a pack
    # author's declared name).
    aliases: list[str] = Field(default_factory=list)
    # Base-persona spec §3.1: picks the base persona template and the
    # BASE_BANNED list, and is the language she talks in. Not the same as
    # ``[memory].primary_language`` (what she writes notes/diary in).
    language: Literal["zh-TW", "en"] = "en"


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    self: str
    personality: str
    taboos: str


class Enforcement(BaseModel):
    """Mechanical, code-checked persona rules (spec §2, P8 persona hardening).

    Additive and opt-in: an absent ``[enforcement]`` table in a pack's
    ``doll.toml`` produces an empty-default instance (no banned substrings,
    no exclaim-run cap). ``banned_substrings`` are literal substrings, not
    regex — cheap, no ReDoS/injection surface.

    After ``DollPack.load``, ``banned_substrings`` is the *effective* list:
    ``BASE_BANNED[language]`` + the pack's own entries − ``base_exempt``
    (base-persona spec §4), so consumers need no merge logic of their own.
    """

    model_config = ConfigDict(extra="forbid")

    banned_substrings: list[str] = Field(default_factory=list)
    max_exclaim_run: int | None = None
    base_exempt: list[str] = Field(default_factory=list)


class DollPack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meta: PackMeta
    identity: Identity
    enforcement: Enforcement = Field(default_factory=Enforcement)

    @classmethod
    def load(cls, pack_dir: Path) -> "DollPack":
        manifest = pack_dir / "doll.toml"
        if not manifest.exists():
            raise FileNotFoundError(
                f"doll.toml not found in pack dir: {pack_dir}"
            )
        with manifest.open("rb") as f:
            data = tomllib.load(f)
        pack = cls.model_validate(data)
        base = BASE_BANNED[pack.meta.language]
        enf = pack.enforcement
        # A base_exempt entry that isn't a base phrase is a typo: fail loudly
        # rather than silently exempting nothing.
        unknown = [s for s in enf.base_exempt if s not in base]
        if unknown:
            raise ValueError(
                f"{manifest}: [enforcement].base_exempt entries not in "
                f"BASE_BANNED[{pack.meta.language!r}]: {unknown}"
            )
        exempt = set(enf.base_exempt)
        # dict.fromkeys: de-duplicate, keeping first occurrence order.
        enf.banned_substrings = [
            s for s in dict.fromkeys([*base, *enf.banned_substrings])
            if s not in exempt
        ]
        return pack
