"""TurnRunner: the one tool loop behind every model turn (harness spec §9 S5,
mind-loop spec §11, plan 2026-10-27 Task 5).

A MindLoop turn and an agent (workflow worker / skeptic / synthesis,
consolidation and evolution keepers) both run ``TurnRunner.run(turn)``.
Each pass streams one reply on ``turn.thread``, runs its tool calls in order
(``run_tool_calls`` through ``turn.execute``), and appends the assistant
message verbatim (reasoning_content + tool_calls with their raw argument
strings) plus one ``tool`` message per call, so the conversation only grows
at the end and every request is a prefix of the next. ``prior_tokens`` is
the thread's last call total, so each pass is charged only for what it
appended.

What differs between the two comes from ``TurnPolicy`` (``max_passes``,
``must_report``, ``failure_rule``) and from what the Turn carries: a
MindLoop turn has ``speak`` (sentences chunked to the sink live), a
``cancel`` handle and the trace / cascade-log hooks; an agent has none of
them. MindLoop-only side effects (tool memory, the action log, the diary
flag, safe-mode entry, cascade_log) stay in MindLoop: its ``execute`` and
hooks do them, and it applies the ``TurnResult``.

How a turn ends (``TurnResult.outcome``):

- ``done``: a pass without tool calls; or, ``must_report``, a pass after
  which Report has run (alone or next to other calls).
- ``cut``: interrupted (``cancel``) at a pass boundary, mid-stream, right
  after the reply ended, or between its tool calls; or a reply cut by
  max_tokens.
- ``failed``: ``failure_rule`` tripped (``safe_mode``: the same tool failed
  3x in a row or ``SAFE_MODE_FAIL_THRESHOLD`` consecutive failures —
  ``reason`` says which, MindLoop enters safe mode; ``stuck_tool``: one
  tool's failures reached 3, counted across other tools' failures); one
  offered-but-unbound tool refused in ``UNBOUND_REJECT_LIMIT`` passes
  (parallel refusals in one pass count once); or ``max_passes`` passes that
  all called tools.
- ``brain_down``: a ``ModelError`` (``error``); the caller raises it.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from dollos.cascade.sentence_chunker import SentenceChunker
from dollos.harness.executor import ToolResult
from dollos.harness.turn_policy import POLICIES
from dollos.ipc.messages import ErrorMsg
from dollos.llm.model import (
    End,
    ModelError,
    SpeechDelta,
    ThinkDelta,
    ToolCall,
    ToolCalls,
    Usage,
    tool_message,
)
from dollos.mind.context_builder import partial_reply

if TYPE_CHECKING:
    import asyncio

    from dollos.cascade.cascade_ctx import CascadeCtx
    from dollos.harness.permissions import Principal
    from dollos.harness.turn_policy import TurnKind
    from dollos.ipc.messages import ServerMessage
    from dollos.llm.model import ModelAdapter
    from dollos.mind.mind_ctx import MindCtx

logger = logging.getLogger(__name__)

# A tool message stays in the conversation (plan 2026-10-18 Task 8), so one
# longer than this is cut. InvokeSkill is exempt: a skill is the full text
# she has to follow and cannot be paged through.
TOOL_MESSAGE_MAX_CHARS = 8000
UNCUT_TOOLS = frozenset({"InvokeSkill"})

# The tool message of a call that never ran because the turn was interrupted
# (plan 2026-10-18 Task 9): every call in a reply must have its tool message.
NOT_RUN = "not run: turn interrupted"

# A tool refused in this many passes of one turn because it is offered but
# not bound ends the turn. Refusals are not failures (no safe mode), but
# agenda / diary / pulse / safe-mode turns see e.g. Shell's schema and could
# otherwise spend every pass on refused calls. Several refusals of one tool
# in the same pass (parallel calls) count once, so she still gets the next
# pass to see them and say so (plan 2026-10-28 decision 3). The count runs
# for the whole turn; passes without a refusal do not reset it.
UNBOUND_REJECT_LIMIT = 3

# Read-only safe mode (spec §8.3). After this many CONSECUTIVE tool failures
# within a single live turn — OR the same-tool 3-strike stuck flag — Doll
# narrows to a read-only tool set and announces it. K=3 catches the
# rotating-invalid-call storm the same-tool 3-strike misses. Bounded-severity,
# announced boundary — NOT a fallback.
SAFE_MODE_FAIL_THRESHOLD = 3

Outcome = Literal["done", "cut", "failed", "brain_down"]
# Which rule ended the turn; each maps to one outcome (module doc).
EndedBy = Literal[
    "no_tool_calls", "report",                                  # done
    "cancel", "max_tokens",                                     # cut
    "safe_mode", "stuck_tool", "unbound_limit", "max_passes",   # failed
    "model_error",                                              # brain_down
]


@dataclass(frozen=True)
class ToolOutcome:
    """One executed native tool call: the call, its result, and the ``tool``
    message that answers it."""

    call: ToolCall
    result: ToolResult
    message: dict


async def run_tool_calls(
    calls: Sequence[ToolCall],
    dispatch: Callable[[str, dict], Awaitable[ToolResult | None]],
    stop: Callable[[], bool] | None = None,
) -> list[ToolOutcome]:
    """Run one reply's tool calls in index order, one ``tool`` message each,
    in the same order (the server template matches results to calls by order
    only).

    ``arguments`` is the raw JSON string from the server; it must parse to an
    object, else the call fails without running. A tool that returns None
    answers "ok"; a failure answers with its error text. A message over
    ``TOOL_MESSAGE_MAX_CHARS`` is cut (``UNCUT_TOOLS`` excepted); the
    ``ToolResult`` keeps the full text.

    ``stop()`` is checked before each call; once it is true, that call and
    the rest do not run and answer ``NOT_RUN``.
    """
    outcomes: list[ToolOutcome] = []
    for call in calls:
        if stop is not None and stop():
            result = ToolResult(tool_name=call.name, success=False, detail=NOT_RUN)
            outcomes.append(ToolOutcome(call, result, tool_message(call.id, NOT_RUN)))
            continue
        error = None
        try:
            args = json.loads(call.arguments)
        except json.JSONDecodeError as e:
            error = f"arguments are not valid JSON ({e}): {call.arguments!r}"
        else:
            if not isinstance(args, dict):
                error = f"arguments must be a JSON object, got {call.arguments!r}"
        if error is not None:
            result = ToolResult(tool_name=call.name, success=False, detail=error)
        else:
            returned = await dispatch(call.name, args)
            result = returned if returned is not None else ToolResult(
                tool_name=call.name, success=True, detail="ok"
            )
        text = result.detail if result.detail else "ok"
        if len(text) > TOOL_MESSAGE_MAX_CHARS and call.name not in UNCUT_TOOLS:
            text = f"{text[:TOOL_MESSAGE_MAX_CHARS]}\n(cut; {len(text)} chars total)"
        outcomes.append(ToolOutcome(call, result, tool_message(call.id, text)))
    return outcomes


class Thread(Protocol):
    """The message list a turn runs on. MindLoop's is a ``Conversation``
    (``mind/context_builder.py``); an agent's is an ``AgentThread``."""

    messages: list[dict]
    prior_tokens: int

    @property
    def stream_key(self) -> tuple[str, int] | None: ...

    def record_usage(self, end: End | None) -> None: ...


@dataclass
class AgentThread:
    """An agent's own message list (system first), never stored.
    ``prior_tokens`` moves only when the call reported both its prompt and
    completion tokens."""

    messages: list[dict]
    prior_tokens: int = 0

    @property
    def stream_key(self) -> None:
        return None

    def record_usage(self, end: End | None) -> None:
        if end is None:
            return
        usage = end.usage
        if usage.prompt_tokens is not None and usage.completion_tokens is not None:
            self.prior_tokens = usage.prompt_tokens + usage.completion_tokens


@dataclass(frozen=True)
class PassRecord:
    """One finished, uncancelled pass, handed to ``Turn.on_pass`` before its
    messages go into the thread. ``sent`` is ``len(messages)`` when the pass
    was sent; ``started`` its ``time.monotonic()``; ``stream_ms`` how long
    the stream took (tools not included)."""

    index: int
    reasoning: str
    speech: str
    end: End
    outcomes: list[ToolOutcome]
    sent: int
    started: float
    stream_ms: int


@dataclass
class Turn:
    """What one ``TurnRunner.run`` needs.

    ``kind`` / ``principal``: the turn's binding (``POLICIES[kind]`` is its
    policy; the executor behind ``execute`` checks every call against it).
    ``tools``: the offered tool JSON. ``execute(name, arguments)``: runs one
    call (MindLoop: ``_dispatch_bound``; an agent: ``ToolExecutor.run``).
    ``ctx``: read for Report (``must_report``).

    ``speak(sentence)``: the reply's speech is sentence-chunked live and each
    sentence goes here (MindLoop: the sink + its speech bookkeeping); None —
    an agent — keeps the speech unspoken. ``sink``: where a ``stuck_tool``
    abort is announced (None: logged). ``cancel``: the interrupt handle.
    ``on_usage``: the adapter's per-call token callback.

    Hooks: ``on_stream_end(index, reasoning, end)`` after every pass's stream
    (``end`` None when cut before it ended); ``before_tools()`` once per pass
    whose tool calls are about to run; ``on_pass(record)`` per finished,
    uncancelled pass.
    """

    kind: "TurnKind"
    principal: "Principal"
    thread: Thread
    tools: list[dict]
    execute: Callable[[str, dict], Awaitable[ToolResult | None]]
    ctx: "MindCtx"
    max_tokens: int
    think_budget: int
    purpose: str
    speak: Callable[[str], None] | None = None
    sink: "asyncio.Queue[ServerMessage | None] | None" = None
    cancel: "CascadeCtx | None" = None
    on_usage: Callable[[int | None, int | None], None] | None = None
    on_stream_end: Callable[[int, str, End | None], None] | None = None
    before_tools: Callable[[], None] | None = None
    on_pass: Callable[[PassRecord], None] | None = None


@dataclass
class TurnResult:
    """How the turn ended (module doc) and by which rule. ``said``: the sentences given to
    ``speak``; ``usage``: each pass's reported usage, in order;
    ``tool_outcomes``: every call of every pass (``NOT_RUN`` included);
    ``report``: the Report args (``must_report``), else None; ``reason``:
    why it ``failed``; ``error``: the ``ModelError`` (``brain_down``)."""

    outcome: Outcome
    ended_by: EndedBy
    said: str = ""
    usage: list[Usage] = field(default_factory=list)
    tool_outcomes: list[ToolOutcome] = field(default_factory=list)
    report: dict | None = None
    passes: int = 0
    reason: str | None = None
    error: ModelError | None = None


@dataclass(frozen=True)
class _Pass:
    """What one model call of the turn produced."""

    reasoning: str
    speech: str
    end: End | None               # None when cut (cancel / error) before the stream ended
    outcomes: list[ToolOutcome]   # the reply's tool calls, in index order
    # The assistant message as it goes into the thread: the reply verbatim,
    # or — cut short — what was actually said (None: nothing yet).
    reply: dict | None
    error: ModelError | None = None
    cancelled: bool = False


class _Failures:
    """The turn's tool-failure counters (``TurnPolicy.failure_rule``). A
    refusal of an offered-but-unbound tool counts toward neither rule, only
    toward ``UNBOUND_REJECT_LIMIT``: ``unbound`` is, per tool, how many
    passes of the turn refused it."""

    def __init__(self, rule: str) -> None:
        self._rule = rule
        self._per_tool: dict[str, int] = {}
        self._last_failed: str | None = None
        self._run = 0
        self.unbound: dict[str, int] = {}

    def add(self, results: list[ToolResult]) -> None:
        """One pass's results, in call order."""
        refused: set[str] = set()
        for r in results:
            if r.unbound:
                # Parallel refusals of one tool in a pass count once.
                if r.tool_name not in refused:
                    refused.add(r.tool_name)
                    self.unbound[r.tool_name] = self.unbound.get(r.tool_name, 0) + 1
            elif r.success:
                self._per_tool.clear()
                self._last_failed = None
                self._run = 0
            elif self._rule == "stuck_tool":
                # Per-tool counts accumulate across tool changes, so
                # alternating failing tools cannot run the agent unbounded.
                self._per_tool[r.tool_name] = self._per_tool.get(r.tool_name, 0) + 1
            else:
                # safe_mode: same-tool 3-strike (reset by another tool's
                # failure) plus the generic consecutive-failure run.
                self._run += 1
                if r.tool_name == self._last_failed:
                    self._per_tool[r.tool_name] = self._per_tool.get(r.tool_name, 1) + 1
                else:
                    self._last_failed = r.tool_name
                    self._per_tool = {r.tool_name: 1}

    def stuck(self) -> str | None:
        return next((n for n, c in self._per_tool.items() if c >= 3), None)

    def safe_mode_reason(self) -> str | None:
        stuck = self.stuck()
        if stuck is not None:
            return f"same tool {stuck} failed 3x in a row"
        if self._run >= SAFE_MODE_FAIL_THRESHOLD:
            return f"{self._run} consecutive tool failures"
        return None

    def refused(self) -> str | None:
        return next(
            (n for n, c in self.unbound.items() if c >= UNBOUND_REJECT_LIMIT), None,
        )


class TurnRunner:
    def __init__(self, model: "ModelAdapter") -> None:
        self._model = model

    async def run(self, turn: Turn) -> TurnResult:
        """Run ``turn`` to its end (module doc). Never raises a
        ``ModelError``: it comes back as ``brain_down``.

        Cut short — interrupted, or a ``transport`` error after some output —
        the reply so far goes in as said (thinking + sentences already
        spoken), and every tool call of a finished reply gets its tool
        message (``NOT_RUN`` if it never ran). After a ``server`` error
        nothing goes in."""
        policy = POLICIES[turn.kind]
        messages = turn.thread.messages
        failures = _Failures(policy.failure_rule)
        said: list[str] = []
        result = TurnResult(outcome="done", ended_by="no_tool_calls")

        def cancelled() -> bool:
            return turn.cancel is not None and turn.cancel.cancelled

        def finish(outcome: Outcome, ended_by: EndedBy, **kw) -> TurnResult:
            result.outcome = outcome
            result.ended_by = ended_by
            result.said = "".join(said)
            for k, v in kw.items():
                setattr(result, k, v)
            return result

        for index in range(policy.max_passes):
            if cancelled():
                logger.info("%s cancelled at pass boundary; exiting cleanly", turn.purpose)
                return finish("cut", "cancel")

            sent = len(messages)
            started = time.monotonic()
            p = await self._stream_one_pass(turn, said, cancelled)
            stream_ms = int((time.monotonic() - started) * 1000)
            turn.thread.record_usage(p.end)
            result.passes = index + 1
            if turn.on_stream_end is not None:
                turn.on_stream_end(index, p.reasoning, p.end)

            if p.error is not None:
                # A bad or too-long message would stay and fail every later
                # call (server): nothing goes in. Transport: kept as said.
                if p.error.kind != "server" and p.reply is not None:
                    messages.append(p.reply)
                return finish("brain_down", "model_error", error=p.error)

            result.tool_outcomes.extend(p.outcomes)
            # A cancelled pass gets no on_pass (no cascade_log / trace entry);
            # what it left in the thread reaches the trace with the next one.
            if p.cancelled:
                if p.reply is not None:
                    messages.append(p.reply)
                messages.extend(o.message for o in p.outcomes)
                return finish("cut", "cancel")

            result.usage.append(p.end.usage)
            if turn.on_pass is not None:
                turn.on_pass(PassRecord(
                    index=index, reasoning=p.reasoning, speech=p.speech,
                    end=p.end, outcomes=p.outcomes, sent=sent,
                    started=started, stream_ms=stream_ms,
                ))

            messages.append(p.reply)
            messages.extend(o.message for o in p.outcomes)

            if p.end.finish_reason == "length":
                # What was said counts; the adapter already dropped the
                # reply's tool calls (the last one is cut mid-JSON).
                logger.warning(
                    "%s reply hit max_tokens on pass %d (%d tool call(s) dropped); "
                    "ending the turn", turn.purpose, index + 1, p.end.dropped_tool_calls,
                )
                return finish("cut", "max_tokens")
            if policy.must_report and turn.ctx.agent_report is not None:
                # Report stashes its args in ctx.agent_report; once it ran
                # (alone or next to other calls in the reply), the turn is done.
                return finish("done", "report", report=turn.ctx.agent_report)
            if not p.outcomes:
                return finish("done", "no_tool_calls")  # no tool calls → nothing to send back

            failures.add([o.result for o in p.outcomes])
            if policy.failure_rule == "safe_mode":
                reason = failures.safe_mode_reason()
                if reason is not None:
                    logger.info(
                        "live cascade entering read-only safe mode (%s); "
                        "ending the turn", reason,
                    )
                    return finish("failed", "safe_mode", reason=reason)
            else:
                stuck = failures.stuck()
                if stuck is not None:
                    if turn.sink is not None:
                        turn.sink.put_nowait(ErrorMsg(
                            message=(
                                f"cascade aborted: 連續 3 次 {stuck} tool 失敗，"
                                f"停下來換思路。"
                            )
                        ))
                    else:
                        logger.info("cascade stuck on %s (3x); aborting", stuck)
                    return finish("failed", "stuck_tool", reason=f"{stuck} failed 3x")
            refused = failures.refused()
            if refused is not None:
                logger.info(
                    "%s: unbound tool %s refused in %d passes; ending the turn",
                    turn.purpose, refused, UNBOUND_REJECT_LIMIT,
                )
                return finish(
                    "failed", "unbound_limit",
                    reason=f"{refused} refused in {UNBOUND_REJECT_LIMIT} passes",
                )

        logger.warning(
            "%s still calling tools after %d passes; stopping",
            turn.purpose, policy.max_passes,
        )
        return finish("failed", "max_passes", reason=f"{policy.max_passes} passes")

    async def _stream_one_pass(
        self, turn: Turn, said: list[str], cancelled: Callable[[], bool],
    ) -> _Pass:
        """Stream ONE model call on ``turn.thread``, then run the reply's tool
        calls.

        ThinkDelta → kept raw; SpeechDelta → `SentenceChunker` →
        ``turn.speak``, live; ToolCalls / End arrive after the stream. The
        chunker is flushed before any tool runs, so the last sentence is out
        first; then the calls run in index order (`run_tool_calls`).

        Honours cancel at every event, before the tools run and between them.
        Cut short — cancelled, or a ``ModelError`` (returned in ``error``, not
        raised) — ``reply`` is what was actually said; a finished reply's
        calls that never ran answer ``NOT_RUN``.
        """
        chunker = SentenceChunker() if turn.speak is not None else None
        think: list[str] = []
        speech: list[str] = []
        spoken: list[str] = []   # sentences handed to speak this pass
        calls: tuple[ToolCall, ...] = ()
        end: End | None = None
        thread = turn.thread
        stream = self._model.stream(
            messages=thread.messages,
            tools=turn.tools,
            max_tokens=turn.max_tokens,
            think_budget=turn.think_budget,
            purpose=turn.purpose,
            on_usage=turn.on_usage,
            conversation=thread.stream_key,
            prior_tokens=thread.prior_tokens,
        )

        def speak(sentence: str) -> None:
            turn.speak(sentence)
            spoken.append(sentence)
            said.append(sentence)

        def cut(error: ModelError | None = None) -> _Pass:
            reply = partial_reply("".join(think), "".join(spoken))
            return _Pass(
                "".join(think), "".join(speech), None, [], reply, error,
                cancelled=error is None,
            )

        # aclosing() tears the HTTP stream down on any early exit (cancel,
        # exception) — not just on normal exhaustion.
        try:
            async with aclosing(stream) as events:
                async for ev in events:
                    if cancelled():
                        logger.info("%s cancelled mid-stream; exiting cleanly", turn.purpose)
                        return cut()
                    if isinstance(ev, ThinkDelta):
                        think.append(ev.text)
                    elif isinstance(ev, SpeechDelta):
                        speech.append(ev.text)
                        if chunker is not None:
                            for sentence in chunker.feed(ev.text):
                                speak(sentence)
                    elif isinstance(ev, ToolCalls):
                        calls = ev.calls
                    elif isinstance(ev, End):
                        end = ev
        except ModelError as e:
            return cut(e)

        if cancelled():
            # Interrupted right after the reply ended: none of its calls run,
            # and the unflushed last sentence was never said.
            outcomes = await run_tool_calls(calls, turn.execute, stop=lambda: True)
            reply = {**end.assistant_message, "content": "".join(spoken)}
            return _Pass(
                "".join(think), "".join(speech), end, outcomes, reply, cancelled=True,
            )
        # The last (unterminated) sentence goes out before any tool runs.
        if chunker is not None:
            for sentence in chunker.flush():
                if sentence:
                    speak(sentence)

        outcomes: list[ToolOutcome] = []
        if calls:
            if turn.before_tools is not None:
                turn.before_tools()
            outcomes = await run_tool_calls(calls, turn.execute, stop=cancelled)
        return _Pass(
            "".join(think), "".join(speech), end, outcomes, end.assistant_message,
            cancelled=cancelled(),
        )
