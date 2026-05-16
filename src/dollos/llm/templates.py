"""PromptTemplate — model-family-specific prompt rendering."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod

from pydantic import BaseModel


class PromptTemplate(ABC):
    """Render a (system, user, prefill) tuple into the single prompt string
    the model expects.

    For "server-applied" templates (e.g. Anthropic / OpenAI chat completions
    where the API takes messages instead of a raw prompt), a concrete
    PromptTemplate may be a no-op stub and the corresponding Provider would
    talk in messages directly. Plan 3 v1 doesn't ship such a Provider, but
    the interface allows it.
    """

    @abstractmethod
    def render(
        self,
        *,
        system: str,
        user: str,
        prefill: str,
        tools: list[type[BaseModel]] | None = None,
    ) -> str:
        ...


def _format_tools_block(tools: list[type[BaseModel]]) -> str:
    """Render the `# Tools` system-prompt section — Qwen3 native format.

    Strips pydantic JSON Schema boilerplate (`title` fields, duplicate
    `description` at parameters level), uses OpenAI/Hermes function envelope,
    and serializes JSON with no whitespace. Saves ~37% vs raw model_json_schema().
    Uses Qwen3 canonical preamble wording from tokenizer_config.json.
    """

    def _compact_schema(cls: type[BaseModel]) -> dict:
        raw = cls.model_json_schema()
        props_raw = raw.get("properties", {})
        props: dict = {}
        for fname, finfo in props_raw.items():
            entry: dict = {}
            if "type" in finfo:
                entry["type"] = finfo["type"]
            if "description" in finfo:
                entry["description"] = finfo["description"]
            if "default" in finfo:
                entry["default"] = finfo["default"]
            if "minimum" in finfo:
                entry["minimum"] = finfo["minimum"]
            if "maximum" in finfo:
                entry["maximum"] = finfo["maximum"]
            props[fname] = entry
        return {
            "type": "function",
            "function": {
                "name": cls.__name__,
                "description": (cls.__doc__ or "").strip(),
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": raw.get("required", []),
                },
            },
        }

    schemas = [_compact_schema(cls) for cls in tools]
    schemas_json = "\n".join(
        json.dumps(s, ensure_ascii=False, separators=(",", ":")) for s in schemas
    )
    return (
        "\n\n# Tools\n\n"
        "You may call one or more functions to assist with the user query.\n\n"
        "You are provided with function signatures within <tools></tools> XML tags:\n"
        "<tools>\n"
        f"{schemas_json}\n"
        "</tools>\n\n"
        "For each function call, return a json object with function name "
        "and arguments within <tool_call></tool_call> XML tags:\n"
        "<tool_call>\n"
        '{"name": <function-name>, "arguments": <args-json-object>}\n'
        "</tool_call>"
    )


# Pragmatic JSON string content rules (handles \" \\ \n \t \uXXXX).
# Shared tail appended to every B4-typed grammar.
#
# Deny list inside the char class:
#   "  \\                              ASCII quote / backslash (true JSON escape)
#   “ ”                      “ ”  typographic double quotes
#   ‘ ’                      ‘ ’  typographic single quotes
#   「 」 『 』        「 」 『 』  CJK corner brackets
# Without the second group, byte-level grammar lets the model produce a
# `」}}` close (observed 2026-05-08 T8) — well-formed bytes, malformed JSON.
_JSON_STR_RULES = (
    "str ::= \"\\\"\" str-char* \"\\\"\"\n"
    "str-char ::= "
    "[^\"\\\\\\u201C\\u201D\\u2018\\u2019\\u300C\\u300D\\u300E\\u300F] | "
    "\"\\\\\" [\"\\\\/bfnrt] | "
    "\"\\\\u\" hex hex hex hex\n"
    "hex ::= [0-9a-fA-F]\n"
    # Non-negative JSON integer (no leading zeros except for the literal 0).
    # Sufficient for current tools (timeout_s: 1..600 — pydantic still
    # validates bounds after grammar accepts the digits).
    "integer ::= \"0\" | [1-9] [0-9]*\n"
)


def build_qwen3_think_tool_grammar(tools: list[type[BaseModel]]) -> str:
    """Build a B4-typed GBNF grammar for Qwen3 <think>+tool_call output.

    Constrains the model to:
      SEEN: <line>
      INTENT: <line>
      TOOL: <tool-name>
      </think>
      <tool_call>...</tool_call>

    Each tool gets a per-tool call rule whose JSON body lists only the
    tool's *required string* fields. Optional fields are dropped (matches
    proven B4-typed probe — extras add noise).
    Raises NotImplementedError if any required field is non-string, or if
    a tool / field name contains characters needing escaping.
    """
    if not tools:
        raise ValueError("tools must be non-empty for grammar build")

    def _check_ident(s: str, what: str) -> None:
        if "\\" in s or '"' in s:
            raise NotImplementedError(
                f"{what} {s!r} contains backslash/quote; grammar escape unsupported"
            )

    # alias suffix for per-tool call rules: ToolName -> tool-name-call
    def _rule_id(name: str) -> str:
        out: list[str] = []
        for i, ch in enumerate(name):
            if ch.isupper() and i > 0:
                out.append("-")
            out.append(ch.lower())
        return "".join(out) + "-call"

    tool_names: list[str] = []
    call_rule_ids: list[str] = []
    call_rules: list[str] = []
    extra_rules: list[str] = []
    used_aux_rule_ids: set[str] = set()

    def _resolve_ref(schema: dict, ref: str) -> dict:
        # `$ref` is always "#/$defs/<Name>" for pydantic-generated schemas.
        prefix = "#/$defs/"
        if not ref.startswith(prefix):
            raise NotImplementedError(
                f"unsupported $ref {ref!r}; only #/$defs/<Name> supported"
            )
        defname = ref[len(prefix):]
        return schema["$defs"][defname]

    def _aux_rule_id(base: str, suffix: str) -> str:
        # Convert CamelCase base + suffix into a unique kebab-case rule id.
        out: list[str] = []
        for i, ch in enumerate(base):
            if ch.isupper() and i > 0:
                out.append("-")
            out.append(ch.lower())
        rid = "".join(out) + "-" + suffix
        # Avoid duplicate rules across tools that share a $def.
        return rid

    for cls in tools:
        name = cls.__name__
        _check_ident(name, "tool name")
        schema = cls.model_json_schema()
        props = schema.get("properties", {})
        required = schema.get("required", [])
        body_parts: list[str] = []
        for fname in required:
            _check_ident(fname, "field name")
            finfo = props.get(fname, {})
            ftype = finfo.get("type")
            enum_vals = finfo.get("enum")
            if ftype == "string" and enum_vals:
                # Literal[...] / enum string: emit a quoted alternation.
                # Each enum value gets its own JSON-string literal.
                for v in enum_vals:
                    if not isinstance(v, str):
                        raise NotImplementedError(
                            f"tool {name} field {fname!r} enum value {v!r} "
                            f"is not a string; grammar build unsupported"
                        )
                    _check_ident(v, "enum value")
                alt = " | ".join(f'"\\"{v}\\""' for v in enum_vals)
                # body emits: "fname": (<alt>)
                body_parts.append(
                    f'\\"{fname}\\": " ({alt}) "'
                )
            elif ftype == "string":
                # Each required string field: "fname": <str>
                body_parts.append(f'\\"{fname}\\": " str "')
            elif ftype == "integer":
                # Required integer field: "fname": <integer>
                body_parts.append(f'\\"{fname}\\": " integer "')
            elif ftype == "array":
                # Array of $defs-referenced objects with required string /
                # integer fields. Used by WriteSchedule.entries:
                # list[ScheduleEntryArg].
                items = finfo.get("items", {})
                ref = items.get("$ref")
                if not ref:
                    raise NotImplementedError(
                        f"tool {name} required field {fname!r} array items "
                        f"have no $ref; only $ref-typed array items supported"
                    )
                item_schema = _resolve_ref(schema, ref)
                if item_schema.get("type") != "object":
                    raise NotImplementedError(
                        f"tool {name} required field {fname!r} item is not "
                        f"an object; grammar build unsupported"
                    )
                item_props = item_schema.get("properties", {})
                item_required = item_schema.get("required", [])
                inner_parts: list[str] = []
                for ifname in item_required:
                    _check_ident(ifname, "field name")
                    iinfo = item_props.get(ifname, {})
                    ityp = iinfo.get("type")
                    if ityp == "string":
                        inner_parts.append(f'\\"{ifname}\\": " str "')
                    elif ityp == "integer":
                        inner_parts.append(f'\\"{ifname}\\": " integer "')
                    else:
                        raise NotImplementedError(
                            f"tool {name} field {fname!r} item field "
                            f"{ifname!r} has unsupported type {ityp!r}; "
                            f"grammar build unsupported"
                        )
                inner_joined = (
                    ', '.join(inner_parts)
                    if len(inner_parts) > 1
                    else (inner_parts[0] if inner_parts else "")
                )
                # Build per-tool item rule + array rule. Both reference
                # `str` and `integer` from the shared tail.
                item_rule_id = _aux_rule_id(name, f"{fname}-item")
                array_rule_id = _aux_rule_id(name, f"{fname}-array")
                if item_rule_id not in used_aux_rule_ids:
                    extra_rules.append(
                        f'{item_rule_id} ::= "{{{inner_joined}}}"'
                    )
                    used_aux_rule_ids.add(item_rule_id)
                if array_rule_id not in used_aux_rule_ids:
                    # Non-empty array: item ("," item)*
                    extra_rules.append(
                        f'{array_rule_id} ::= "[" {item_rule_id} '
                        f'("," {item_rule_id})* "]"'
                    )
                    used_aux_rule_ids.add(array_rule_id)
                body_parts.append(
                    f'\\"{fname}\\": " {array_rule_id} "'
                )
            else:
                raise NotImplementedError(
                    f"tool {name} required field {fname!r} has unsupported "
                    f"type {ftype!r}; grammar build unsupported"
                )
        joined = ', '.join(body_parts) if len(body_parts) > 1 else (body_parts[0] if body_parts else "")
        # Build literal: <tool_call>\n{"name": "ToolName", "arguments": {<fields>}}\n</tool_call>
        rule_id = _rule_id(name)
        rule = (
            f'{rule_id} ::= "<tool_call>\\n'
            f'{{\\"name\\": \\"{name}\\", \\"arguments\\": {{'
            f'{joined}}}}}\\n</tool_call>"'
        )
        tool_names.append(name)
        call_rule_ids.append(rule_id)
        call_rules.append(rule)

    tool_name_alts = " | ".join(f'"{n}"' for n in tool_names)
    tool_call_alts = " | ".join(call_rule_ids)

    head = (
        "root ::= think tool-call\n"
        'think ::= "SEEN: " line "INTENT: " line "REVIEW: " line "MOOD: " line "TOOL: " tool-name "\\n</think>\\n\\n"\n'
        'line ::= [^\\n]+ "\\n"\n'
        f"tool-name ::= {tool_name_alts}\n"
        f"tool-call ::= {tool_call_alts}\n"
    )
    body = "\n".join(call_rules) + "\n"
    if extra_rules:
        body += "\n".join(extra_rules) + "\n"
    return head + body + _JSON_STR_RULES


def build_mind_actions_grammar(action_names: list[str]) -> str:
    """Build a GBNF grammar for MindLoop output: <think>...</think> + JSON array.

    Constrains the model to:
      <think>
      SEEN: <line>
      INTENT: <line>
      MOOD: <line>
      </think>

      [{"action": "<name>", ...}, ...]

    Each element of the array must have an ``"action"`` key whose value is one
    of the registered action names.  The remaining fields in each object are
    free-form JSON (any key/value pairs), so no pydantic schema enumeration is
    needed — the tolerant parser in ``parse_actions`` handles field extraction.

    Raises ValueError if ``action_names`` is empty or contains names that
    require backslash/quote escaping (unsupported).
    """
    if not action_names:
        raise ValueError("action_names must be non-empty for grammar build")

    for name in action_names:
        if "\\" in name or '"' in name:
            raise ValueError(
                f"action name {name!r} contains backslash/quote; "
                "grammar escape unsupported"
            )

    action_name_alts = " | ".join(f'\\"{name}\\"' for name in action_names)

    head = (
        "root ::= think-block mind-actions\n"
        'think-block ::= "<think>\\n" think-body "\\n</think>\\n\\n"\n'
        'think-body ::= "SEEN: " line "INTENT: " line "MOOD: " line\n'
        'line ::= [^\\n]+ "\\n"\n'
        "ws ::= [ \\t\\n]*\n"
        f"action-name ::= {action_name_alts}\n"
        'mind-actions ::= "[" ws "]" | "[" ws action-call (ws "," ws action-call)* ws "]"\n'
        'action-call ::= "{" ws "\\"action\\"" ws ":" ws "\\"" action-name "\\"" action-extra ws "}"\n'
        'action-extra ::= ("," ws json-pair (ws "," ws json-pair)*)?\n'
        'json-pair ::= str ws ":" ws json-value\n'
    )

    # Recursive JSON value rule — covers all JSON value types.
    # json-object and json-array are mutually recursive via json-value.
    json_rules = (
        "json-value ::= str | json-number | json-object | json-array "
        '| "true" | "false" | "null"\n'
        'json-number ::= "-"? ("0" | [1-9] [0-9]*) ("." [0-9]+)? '
        '(([eE] [+-]? [0-9]+))?\n'
        'json-object ::= "{" ws "}" | "{" ws json-pair (ws "," ws json-pair)* ws "}"\n'
        'json-array ::= "[" ws "]" | "[" ws json-value (ws "," ws json-value)* ws "]"\n'
    )

    return head + json_rules + _JSON_STR_RULES


class Qwen3ThinkingTemplate(PromptTemplate):
    """Qwen3.x thinking-model ChatML.

    Opens the <think> block inside the assistant turn so prefill content
    goes inside the thinking block. Renders an optional `# Tools` section
    in the system prompt for tool calling.
    """

    def render(
        self,
        *,
        system: str,
        user: str,
        prefill: str,
        tools: list[type[BaseModel]] | None = None,
    ) -> str:
        if tools:
            system = system + _format_tools_block(tools)
        rendered = (
            f"<|im_start|>system\n{system}<|im_end|>\n"
            f"<|im_start|>user\n{user}<|im_end|>\n"
            f"<|im_start|>assistant\n<think>\n"
        )
        if prefill:
            rendered += prefill
        return rendered

    def render_messages(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[type[BaseModel]] | None = None,
    ) -> str:
        """Multi-message ChatML render for cascade history within a turn.

        Each entry of `messages` is `{"role": "user"|"assistant", "content": str}`.
        Tool results are passed in by the caller as a user-role message whose
        content is wrapped in `<tool_response>...</tool_response>` (Qwen3 native
        format). Tool calls are passed as assistant-role messages whose content
        is the model's verbatim emit (think + tool_call XML).

        Regardless of the last message's role, this method always opens a fresh
        `<|im_start|>assistant\\n<think>\\n` turn at the end so the model can
        continue. No `prefill` argument — cascade design has no prefill.
        """
        if tools:
            system = system + _format_tools_block(tools)
        rendered = f"<|im_start|>system\n{system}<|im_end|>\n"
        for msg in messages:
            rendered += (
                f"<|im_start|>{msg['role']}\n{msg['content']}<|im_end|>\n"
            )
        rendered += "<|im_start|>assistant\n<think>\n"
        return rendered


class Qwen3PlainTemplate(PromptTemplate):
    """Qwen3.x ChatML with thinking immediately closed.

    Inner Voice's small models. Rejects non-empty tools — small-model
    code paths must not attempt tool calling (raises NotImplementedError
    to surface misuse loudly).
    """

    def render(
        self,
        *,
        system: str,
        user: str,
        prefill: str,
        tools: list[type[BaseModel]] | None = None,
    ) -> str:
        if tools:
            raise NotImplementedError(
                "Qwen3PlainTemplate does not support tool calling; "
                "use Qwen3ThinkingTemplate for tool-calling code paths."
            )
        rendered = (
            f"<|im_start|>system\n{system}<|im_end|>\n"
            f"<|im_start|>user\n{user}<|im_end|>\n"
            f"<|im_start|>assistant\n<think>\n\n</think>\n\n"
        )
        if prefill:
            rendered += prefill
        return rendered
