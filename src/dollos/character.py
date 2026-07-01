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

from pydantic import BaseModel, ConfigDict, Field


class PackMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    self: str
    personality: str
    taboos: str


class Enforcement(BaseModel):
    """Mechanical, code-checked persona rules (spec §2, P8 persona hardening).

    Additive and opt-in: an absent ``[enforcement]`` table in a pack's
    ``doll.toml`` produces an empty-default instance (no banned substrings,
    no exclaim-run cap), i.e. zero behavior change for packs that don't
    opt in. ``banned_substrings`` are literal substrings, not regex —
    cheap, no ReDoS/injection surface.
    """

    model_config = ConfigDict(extra="forbid")

    banned_substrings: list[str] = Field(default_factory=list)
    max_exclaim_run: int | None = None


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
        return cls.model_validate(data)
