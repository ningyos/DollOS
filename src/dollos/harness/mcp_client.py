"""MCP client: outside MCP servers' tools as Doll's tools (harness spec §5,
plan 2026-10-27 Task 7 / S7).

``mcp_clients.toml`` (see ``mcp_clients.example.toml``) lists stdio servers.
At boot each one is spawned, ``initialize``d and listed ONCE; its tools
become tool classes named ``<server>__<tool>`` that the ToolRegistry offers
and binds like built-ins (``ToolRegistry.register_mcp``).

- Permissions (plan decision 6): ``min_principal`` is REQUIRED — a server
  without it fails boot, there is no default (stricter than spec §5's
  "default owner", which would bind straight away on owner turns). It names
  the lowest principal that may run the server's tools; ``owner`` covers
  owner_dm too (owner DM = owner, user decision 2026-10-27). That set is the
  tools' permission row. Turn kinds: only a conversation binds MCP tools
  (``TurnPolicy.mcp_tools``).
- ``untrusted_output = true``: calling one of its tools taints the turn
  (``taints``; ToolExecutor narrows the rest of the turn, spec §6.5 point 2,
  and MindLoop every later turn on the same conversation page). A result
  too long for the tool message is stored under an ``ext-`` id, and reading
  it back taints the reading turn the same way.
- The tool list is fixed for the run (plan decision 4: the offered tool JSON
  must not change, or the server cache is recomputed). A server's
  ``tools/list_changed`` is logged and ignored until the next boot.
- No fallback (plan decision 7): a server that cannot be reached at boot
  registers no tools — one log line with the reason, the daemon boots — and
  is not retried. A server that dies mid-run is not restarted; its calls
  answer with a clear failure and the turn goes on.

Process handling: the mcp SDK's ``stdio_client``, not ``ServiceSupervisor``.
The supervisor hands its children the daemon's own stdout (no pipes to give
the SDK's transport), restarts a crashed child (decision 7 says never), and
its pre-exec hook is Linux-only ``prctl``. ``stdio_client`` owns the pipes,
starts the child in its own session, and on exit closes its stdin, waits,
then terminates the process tree.
"""
from __future__ import annotations

import asyncio
import logging
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from mcp import ClientSession, McpError, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from dollos.harness.executor import ToolResult
from dollos.harness.permissions import Principal
from dollos.harness.runner import TOOL_MESSAGE_MAX_CHARS

if TYPE_CHECKING:
    from dollos.mind.mind_ctx import MindCtx

logger = logging.getLogger(__name__)

# Separator between server and tool name in the registry (spec §5).
PREFIX_SEP = "__"

# min_principal → its permission row. "owner" includes owner_dm (user
# decision 2026-10-27); owner_dm itself is not a value, so a config cannot
# grant the DM without the local owner or the other way round.
MinPrincipal = Literal["owner", "internal", "external_public"]
MIN_PRINCIPAL_ROWS: dict[str, frozenset[Principal]] = {
    "owner": frozenset({"owner", "owner_dm"}),
    "internal": frozenset({"owner", "owner_dm", "internal"}),
    "external_public": frozenset({"owner", "owner_dm", "internal", "external_public"}),
}

# Boot handshake (spawn + initialize + list_tools) per server, and one call.
# Module constants, not config (YAGNI, as service_supervisor's).
BOOT_TIMEOUT_S = 20.0
CALL_TIMEOUT_S = 120.0
# The SDK's stdio_client waits 2 s for the child after closing its stdin and
# then terminates it; this bounds the whole stop on top of that.
STOP_TIMEOUT_S = 10.0

# A result longer than one tool message (the Turn Runner cuts there) goes to
# the ToolOutputStore and the model gets these first lines plus its id to
# page with ReadToolOutput, as a Shell result does.
PREVIEW_LINES = 10


class McpConfigError(ValueError):
    """mcp_clients.toml is invalid; boot fails."""


class McpServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    # stdio only for now; an HTTP ``url`` comes later (plan Task 7).
    command: list[str] = Field(min_length=1)
    min_principal: MinPrincipal
    untrusted_output: bool = False
    # Allowlist of the server's tool names; None = all of them.
    tools: list[str] | None = None

    @field_validator("name")
    @classmethod
    def _no_separator(cls, v: str) -> str:
        if PREFIX_SEP in v:
            raise ValueError(f"must not contain {PREFIX_SEP!r} (the tool name prefix separator)")
        return v


def _describe(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '(entry)'}: {e['msg']}"
        for e in exc.errors()
    )


def load_mcp_clients(path: Path) -> list[McpServerConfig]:
    """The servers in ``path``; no file → none (MCP tools are opt-in).

    Every error names the server, so a bad entry is found at boot."""
    if not path.exists():
        return []
    with path.open("rb") as f:
        data = tomllib.load(f)
    unknown = set(data) - {"server"}
    if unknown:
        raise McpConfigError(f"{path}: unknown keys {sorted(unknown)}; only [[server]]")
    servers: list[McpServerConfig] = []
    for i, entry in enumerate(data.get("server", [])):
        label = entry.get("name", f"#{i + 1}") if isinstance(entry, dict) else f"#{i + 1}"
        try:
            server = McpServerConfig.model_validate(entry)
        except ValidationError as e:
            raise McpConfigError(f"{path}: server {label!r}: {_describe(e)}") from e
        if any(s.name == server.name for s in servers):
            raise McpConfigError(f"{path}: server {server.name!r} is listed twice")
        servers.append(server)
    return servers


class McpServer:
    """One stdio MCP server for the daemon's run: ``start`` once at boot,
    ``call`` from turns, ``stop`` at shutdown. Never restarted.

    The SDK's transport and session are anyio task groups, which must be
    entered and left in one task, so ``_hold`` keeps them open in its own
    task until ``stop``; ``call`` runs on that session from the turn's task.
    """

    def __init__(
        self, config: McpServerConfig, *,
        boot_timeout_s: float = BOOT_TIMEOUT_S, call_timeout_s: float = CALL_TIMEOUT_S,
    ) -> None:
        self.config = config
        self._boot_timeout_s = boot_timeout_s
        self._call_timeout_s = call_timeout_s
        self._session: ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Set once a call finds the server gone; every later call fails at once.
        self._gone: str | None = None
        self._list_changed_logged = False

    @property
    def name(self) -> str:
        return self.config.name

    async def start(self) -> list[types.Tool]:
        """Spawn, initialize and list the tools. Raises if any step fails or
        takes longer than the boot timeout (the child is then stopped)."""
        ready: asyncio.Future[list[types.Tool]] = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._hold(ready), name=f"mcp:{self.name}")
        try:
            return await asyncio.wait_for(asyncio.shield(ready), self._boot_timeout_s)
        except BaseException:
            # The handshake never finished, so nothing waits on _stop yet:
            # cancel the holder; stdio_client's exit stops the child.
            self._stop.set()
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            raise

    async def _hold(self, ready: asyncio.Future) -> None:
        command, *args = self.config.command
        params = StdioServerParameters(command=command, args=args)
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, message_handler=self._on_message) as session:
                    await session.initialize()
                    tools: list[types.Tool] = []
                    cursor: str | None = None
                    while True:
                        page = await session.list_tools(
                            params=types.PaginatedRequestParams(cursor=cursor),
                        )
                        tools.extend(page.tools)
                        cursor = page.nextCursor
                        if not cursor:
                            break
                    self._session = session
                    ready.set_result(tools)
                    await self._stop.wait()
        except BaseException as e:
            booted = ready.done()
            if not booted:
                # start() logs the boot failure.
                ready.set_exception(e if isinstance(e, Exception) else RuntimeError(repr(e)))
            if isinstance(e, asyncio.CancelledError):
                raise
            if booted and not self._stop.is_set():
                # Mid-run death of the transport (e.g. a write to a dead
                # child); calls see the task done. Not restarted (decision 7).
                logger.error("mcp server %s: connection lost (%s); not restarted", self.name, e)

    async def _on_message(self, message: Any) -> None:
        """Server notifications. ``tools/list_changed`` is logged once and
        ignored: the offered list stays fixed for the run (plan decision 4)."""
        if (
            isinstance(message, types.ServerNotification)
            and isinstance(message.root, types.ToolListChangedNotification)
            and not self._list_changed_logged
        ):
            self._list_changed_logged = True
            logger.warning(
                "mcp server %s: tool list changed; ignored until the next daemon start",
                self.name,
            )

    def _mark_gone(self, reason: str) -> str:
        if self._gone is None:
            self._gone = reason
            logger.error("mcp server %s is gone (%s); not restarted", self.name, reason)
        return self._gone

    async def call(self, tool: str, arguments: dict) -> types.CallToolResult:
        """Call one tool. Raises ``McpServerGone`` when the server has died
        (now or earlier), ``TimeoutError`` after the call timeout."""
        if self._gone is not None:
            raise McpServerGone(self.name, self._gone)
        if self._session is None or self._task is None or self._task.done():
            raise McpServerGone(self.name, self._mark_gone("connection lost"))
        call = asyncio.ensure_future(self._session.call_tool(tool, arguments))
        # Race the call against the holder task: a write to a dead child ends
        # the holder, and the call itself would then never be answered.
        done, _ = await asyncio.wait(
            {call, self._task}, timeout=self._call_timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if call in done:
            try:
                return call.result()
            except McpError as e:
                if e.error.code == types.CONNECTION_CLOSED:
                    raise McpServerGone(self.name, self._mark_gone("connection closed")) from e
                raise
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        if self._task.done():
            raise McpServerGone(self.name, self._mark_gone("connection lost"))
        raise TimeoutError(f"no answer within {self._call_timeout_s:g}s")

    async def stop(self) -> None:
        """Close the session and stop the child (idempotent)."""
        self._stop.set()
        task = self._task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), STOP_TIMEOUT_S)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class McpServerGone(Exception):
    def __init__(self, server: str, reason: str) -> None:
        super().__init__(f"MCP server {server!r} is gone ({reason}); it is not restarted")


class McpTool(BaseModel):
    """Base of the generated MCP tool classes (``tool_class``). The model's
    arguments are any JSON object: the server validates them against its own
    inputSchema, which is not re-implemented here."""
    model_config = ConfigDict(extra="allow")

    server: ClassVar[McpServer]
    remote_name: ClassVar[str]
    description: ClassVar[str]
    input_schema: ClassVar[dict]
    # Permission row (ToolRegistry.register_mcp), from min_principal.
    principals: ClassVar[frozenset[Principal]]
    untrusted_output: ClassVar[bool]

    @classmethod
    def taints(cls, arguments: dict) -> bool:
        """The server's ``untrusted_output``: whatever it returns may carry
        injected instructions, so the turn is downgraded (spec §6.5 point 2)."""
        return cls.untrusted_output

    @classmethod
    def tool_json(cls) -> dict:
        """The offered tool JSON: the server's name (prefixed), description
        and inputSchema as given (``llm.tool_schema``)."""
        return {
            "type": "function",
            "function": {
                "name": cls.__name__,
                "description": cls.description,
                "parameters": cls.input_schema,
            },
        }

    async def run(self, ctx: "MindCtx") -> str | ToolResult:
        name = type(self).__name__
        try:
            result = await self.server.call(self.remote_name, dict(self.model_extra or {}))
        except (McpServerGone, TimeoutError, McpError) as e:
            return ToolResult(tool_name=name, success=False, detail=str(e))
        text = _text_of(result)
        if result.isError:
            return ToolResult(tool_name=name, success=False, detail=text or "error (no text)")
        if len(text) > TOOL_MESSAGE_MAX_CHARS:
            # An untrusted server's text gets an ext- id, so reading it back
            # later, on any page, taints that turn too (ReadToolOutput.taints).
            output_id = ctx.tool_output_store.write(text, untrusted=self.untrusted_output)
            lines = text.splitlines()
            preview = "\n".join(lines[:PREVIEW_LINES])[:TOOL_MESSAGE_MAX_CHARS]
            return (
                f"{preview}\n(output_id {output_id}, {len(lines)} lines, "
                f"{len(text)} chars; page with ReadToolOutput)"
            )
        return text


def _text_of(result: types.CallToolResult) -> str:
    """The text content, joined; other content is named, not shown."""
    parts = [
        c.text if isinstance(c, types.TextContent) else f"[{c.type} content not shown]"
        for c in result.content
    ]
    return "\n".join(parts)


def tool_class(server: McpServer, tool: types.Tool) -> type[McpTool]:
    config = server.config
    return type(f"{config.name}{PREFIX_SEP}{tool.name}", (McpTool,), {
        "__module__": __name__,
        "server": server,
        "remote_name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.inputSchema,
        "principals": MIN_PRINCIPAL_ROWS[config.min_principal],
        "untrusted_output": config.untrusted_output,
    })


class McpClients:
    """Every configured server for the daemon's run (kernel: ``start`` before
    the first turn, ``stop`` at shutdown)."""

    def __init__(
        self, configs: list[McpServerConfig], *,
        boot_timeout_s: float = BOOT_TIMEOUT_S, call_timeout_s: float = CALL_TIMEOUT_S,
    ) -> None:
        self._servers = [
            McpServer(c, boot_timeout_s=boot_timeout_s, call_timeout_s=call_timeout_s)
            for c in configs
        ]

    async def start(self) -> list[type[McpTool]]:
        """Connect to every server at once; the tool classes of those that
        answered. One that did not is logged and left out (decision 7)."""
        listed = await asyncio.gather(
            *(s.start() for s in self._servers), return_exceptions=True,
        )
        classes: list[type[McpTool]] = []
        for server, tools in zip(self._servers, listed, strict=True):
            if isinstance(tools, BaseException):
                reason = f"{type(tools).__name__}: {tools}" if str(tools) else type(tools).__name__
                logger.error(
                    "mcp server %s unreachable at boot (%s); its tools are not registered",
                    server.name, reason,
                )
                continue
            classes.extend(self._select(server, tools))
        return classes

    @staticmethod
    def _select(server: McpServer, tools: list[types.Tool]) -> list[type[McpTool]]:
        allow = server.config.tools
        if allow is not None:
            missing = sorted(set(allow) - {t.name for t in tools})
            if missing:
                logger.warning(
                    "mcp server %s does not list allowlisted tools %s", server.name, missing,
                )
            tools = [t for t in tools if t.name in allow]
        logger.info(
            "mcp server %s: %d tools (min_principal=%s, untrusted_output=%s)",
            server.name, len(tools), server.config.min_principal,
            server.config.untrusted_output,
        )
        return [tool_class(server, t) for t in tools]

    async def stop(self) -> None:
        await asyncio.gather(*(s.stop() for s in self._servers), return_exceptions=True)
