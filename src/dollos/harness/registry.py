"""ToolRegistry: the tool classes a loop can offer (harness spec §6.4, plan
2026-10-27 Task 4 / S5).

It holds the built-in registry it was given (MindLoop: ``MAIN_TOOLS``) plus
the extras a turn kind adds (``turn_policy.Extra``: NoteToolLesson, PinSelf,
SelfRevision, LearnName — never registry members themselves). ``offered`` is
the tier's fixed list the model is shown (``turn_policy.offered_tools``)
with its tool JSON; ``bound`` is what one turn may run
(``turn_policy.bound_tools``). ``register`` is where more tools come in
(MCP tools, plan Task 7).

Each tool declares ``effectful`` (mind-loop spec §11): true unless the class
sets ``effectful: ClassVar[bool] = False`` (read-only tools: Recall,
ReadToolOutput, GrepToolOutput, InvokeSkill). Recorded only; nothing reads
it yet.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from pydantic import BaseModel

from dollos.harness.permissions import Principal
from dollos.harness.turn_policy import TurnKind, bound_tools, offered_tools
from dollos.llm.tool_schema import tool_schemas
from dollos.tools import LearnName, NoteToolLesson, PinSelf, SelfRevision

# The extra tools' classes (turn_policy.Extra names only the tool).
EXTRA_TOOL_CLASSES: dict[str, type[BaseModel]] = {
    c.__name__: c for c in (NoteToolLesson, PinSelf, SelfRevision, LearnName)
}


@dataclass(frozen=True)
class Offered:
    """A tier's fixed tool list: name → class in name order, and the tool
    JSON the model is sent (``tool_schemas``)."""
    tools: dict[str, type[BaseModel]]
    schemas: list[dict]


class ToolRegistry:
    def __init__(self, tools: Mapping[str, type[BaseModel]]) -> None:
        self._tools = dict(tools)

    def register(self, name: str, cls: type[BaseModel]) -> None:
        """Add a tool. A name already taken (registry or extra) is an error."""
        if name in self._tools or name in EXTRA_TOOL_CLASSES:
            raise ValueError(f"tool {name!r} is already registered")
        self._tools[name] = cls

    def __getitem__(self, name: str) -> type[BaseModel]:
        return self.classes([name])[name]

    def classes(self, names: Iterable[str]) -> dict[str, type[BaseModel]]:
        """``names`` as registry entries, in the order given; an extra tool
        wins over a same-named registry entry, as it always has."""
        classes = {**self._tools, **EXTRA_TOOL_CLASSES}
        return {n: classes[n] for n in names}

    def effectful(self, name: str) -> bool:
        return getattr(self[name], "effectful", True)

    def offered(
        self, principal: Principal, *, self_profile: bool, evolution: bool,
    ) -> Offered:
        tools = self.classes(sorted(offered_tools(
            principal, self._tools, self_profile=self_profile, evolution=evolution,
        )))
        return Offered(tools, tool_schemas(tools.values()))

    def bound(
        self, kind: TurnKind, principal: Principal, *,
        self_profile: bool, evolution: bool,
    ) -> dict[str, type[BaseModel]]:
        return self.classes(sorted(bound_tools(
            kind, principal, self._tools,
            self_profile=self_profile, evolution=evolution,
        )))
