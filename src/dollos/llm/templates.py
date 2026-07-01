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
            # Unwrap Optional[X] — Pydantic emits anyOf: [<T>, {"type":"null"}].
            # Without this, Optional fields lose their type entirely.
            finfo_effective = finfo
            if "type" not in finfo and "anyOf" in finfo:
                non_null = [s for s in finfo["anyOf"] if s.get("type") != "null"]
                if len(non_null) == 1:
                    finfo_effective = non_null[0]
            if "type" in finfo_effective:
                entry["type"] = finfo_effective["type"]
            # Preserve enum for Literal fields (e.g. Literal["ok", "fail"]).
            if "enum" in finfo_effective:
                entry["enum"] = finfo_effective["enum"]
            if "description" in finfo:
                entry["description"] = finfo["description"]
            if "default" in finfo:
                entry["default"] = finfo["default"]
            if "minimum" in finfo_effective:
                entry["minimum"] = finfo_effective["minimum"]
            if "maximum" in finfo_effective:
                entry["maximum"] = finfo_effective["maximum"]
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
    # JSON integer with optional leading minus (for ReadToolOutput.offset's
    # negative counts from end). No leading zeros except for the literal 0.
    # Pydantic still validates bounds (ge/le) after grammar accepts the digits.
    "integer ::= \"-\"? ( \"0\" | [1-9] [0-9]* )\n"
)


def _field_value_token(
    name: str,
    fname: str,
    finfo: dict,
    schema: dict,
    used_aux_rule_ids: set[str],
    extra_rules: list[str],
    check_ident,
    resolve_ref,
    aux_rule_id,
) -> str:
    """GBNF value token for one field: 'str' / 'integer' / '(<enum-alts>)' /
    '<array-rule-id>'. Appends any array aux rules to extra_rules.

    Handles `anyOf: [<T>, {"type":"null"}]` (optional X | None) by extracting
    the single non-null branch. Raises NotImplementedError on unsupported types.
    """
    ftype = finfo.get("type")
    enum_vals = finfo.get("enum")
    if ftype is None and "anyOf" in finfo:
        non_null = [s for s in finfo["anyOf"] if s.get("type") != "null"]
        if len(non_null) != 1:
            raise NotImplementedError(
                f"tool {name} field {fname!r} anyOf {finfo['anyOf']!r} is not a "
                f"simple `X | None`; grammar build unsupported"
            )
        finfo = non_null[0]
        ftype = finfo.get("type")
        enum_vals = finfo.get("enum")
    if ftype == "string" and enum_vals:
        for v in enum_vals:
            if not isinstance(v, str):
                raise NotImplementedError(
                    f"tool {name} field {fname!r} enum value {v!r} is not a string"
                )
            check_ident(v, "enum value")
        alt = " | ".join(f'"\\"{v}\\""' for v in enum_vals)
        return f"({alt})"
    if ftype == "string":
        return "str"
    if ftype == "integer":
        return "integer"
    if ftype == "array":
        items = finfo.get("items", {})
        ref = items.get("$ref")
        if not ref:
            raise NotImplementedError(
                f"tool {name} field {fname!r} array items have no $ref; "
                f"only $ref-typed array items supported"
            )
        item_schema = resolve_ref(schema, ref)
        if item_schema.get("type") != "object":
            raise NotImplementedError(
                f"tool {name} field {fname!r} item is not an object; "
                f"grammar build unsupported"
            )
        item_props = item_schema.get("properties", {})
        item_required = item_schema.get("required", [])
        inner_parts: list[str] = []
        for ifname in item_required:
            check_ident(ifname, "field name")
            iinfo = item_props.get(ifname, {})
            ityp = iinfo.get("type")
            if ityp == "string":
                inner_parts.append(f'\\"{ifname}\\": " str "')
            elif ityp == "integer":
                inner_parts.append(f'\\"{ifname}\\": " integer "')
            else:
                raise NotImplementedError(
                    f"tool {name} field {fname!r} item field {ifname!r} has "
                    f"unsupported type {ityp!r}; grammar build unsupported"
                )
        inner_joined = (
            ', '.join(inner_parts) if len(inner_parts) > 1
            else (inner_parts[0] if inner_parts else "")
        )
        item_rule_id = aux_rule_id(name, f"{fname}-item")
        array_rule_id = aux_rule_id(name, f"{fname}-array")
        if item_rule_id not in used_aux_rule_ids:
            extra_rules.append(f'{item_rule_id} ::= "{{{inner_joined}}}"')
            used_aux_rule_ids.add(item_rule_id)
        if array_rule_id not in used_aux_rule_ids:
            extra_rules.append(
                f'{array_rule_id} ::= "[" {item_rule_id} ("," {item_rule_id})* "]"'
            )
            used_aux_rule_ids.add(array_rule_id)
        return array_rule_id
    raise NotImplementedError(
        f"tool {name} field {fname!r} has unsupported type {ftype!r}; "
        f"grammar build unsupported"
    )


def _build_tool_call_rule(
    tool: type[BaseModel],
    used_aux_rule_ids: set[str] | None = None,
    *,
    include_optional: bool = False,
) -> tuple[str, str]:
    """Build the GBNF rule for a single <tool_call>...</tool_call>.

    Returns ``(rule_id, rule_text)``. With ``include_optional=True`` AND ≥1
    required field, optional (default-valued) fields are appended as
    fixed-order ``( ", \\"<name>\\": " <type> )?`` suffixes so they can be
    emitted-or-omitted; each carries its own leading comma so the JSON stays
    valid. Zero-required tools keep an empty ``{}`` body regardless (spec §3.1).

    Required-only output (include_optional=False) is byte-compatible with the
    pre-refactor builder.

    Raises NotImplementedError if any field has an unsupported type, or if any
    name contains characters needing escaping.
    """
    if used_aux_rule_ids is None:
        used_aux_rule_ids = set()

    def _check_ident(s: str, what: str) -> None:
        if "\\" in s or '"' in s:
            raise NotImplementedError(
                f"{what} {s!r} contains backslash/quote; grammar escape unsupported"
            )

    def _rule_id(name: str) -> str:
        out: list[str] = []
        for i, ch in enumerate(name):
            if ch.isupper() and i > 0:
                out.append("-")
            out.append(ch.lower())
        return "".join(out) + "-call"

    def _resolve_ref(schema: dict, ref: str) -> dict:
        prefix = "#/$defs/"
        if not ref.startswith(prefix):
            raise NotImplementedError(
                f"unsupported $ref {ref!r}; only #/$defs/<Name> supported"
            )
        defs = schema.get("$defs")
        if defs is None:
            raise NotImplementedError(
                f"$ref {ref!r} found but schema has no $defs section"
            )
        key = ref[len(prefix):]
        if key not in defs:
            raise NotImplementedError(
                f"$ref {ref!r} key {key!r} not found in $defs"
            )
        return defs[key]

    def _aux_rule_id(base: str, suffix: str) -> str:
        out: list[str] = []
        for i, ch in enumerate(base):
            if ch.isupper() and i > 0:
                out.append("-")
            out.append(ch.lower())
        return "".join(out) + "-" + suffix

    name = tool.__name__
    _check_ident(name, "tool name")
    schema = tool.model_json_schema()
    props = schema.get("properties", {})
    required = schema.get("required", [])
    rule_id = _rule_id(name)
    extra_rules: list[str] = []

    if not required:
        # Zero required fields → empty arguments body. Optional fields (if any)
        # are unreachable; see spec §3.1. Preserves prior behavior + tests.
        call_rule = (
            f'{rule_id} ::= "<tool_call>\\n'
            f'{{\\"name\\": \\"{name}\\", \\"arguments\\": {{}}}}\\n</tool_call>"'
        )
        return rule_id, call_rule

    for fname in required:
        _check_ident(fname, "field name")

    def _val(fname: str) -> str:
        return _field_value_token(
            name, fname, props.get(fname, {}), schema, used_aux_rule_ids,
            extra_rules, _check_ident, _resolve_ref, _aux_rule_id,
        )

    first = required[0]
    terms: list[str] = [
        f'"<tool_call>\\n{{\\"name\\": \\"{name}\\", \\"arguments\\": '
        f'{{\\"{first}\\": "',
        _val(first),
    ]
    for fname in required[1:]:
        terms.append(f'", \\"{fname}\\": "')
        terms.append(_val(fname))

    if include_optional:
        for fname in [f for f in props if f not in required]:
            _check_ident(fname, "field name")
            terms.append(f'( ", \\"{fname}\\": " {_val(fname)} )?')

    terms.append('"}}\\n</tool_call>"')
    call_rule = f"{rule_id} ::= " + " ".join(terms)
    rule_text = call_rule + ("\n" + "\n".join(extra_rules) if extra_rules else "")
    return rule_id, rule_text


def build_qwen3_think_tool_grammar(tools: list[type[BaseModel]]) -> str:
    """Build a B4-typed GBNF grammar for Qwen3 <think>+tool_call output.

    Constrains the model to:
      SEEN: <line>
      INTENT: <line>
      TOOL: <tool-name>
      REVIEW: <line>
      MOOD: <line>
      </think>
      <tool_call>...</tool_call>

    TOOL is emitted right after INTENT (before REVIEW/MOOD) so the tool
    decision is committed to the token stream before the self-critique is
    written, keeping REVIEW a genuine post-hoc reflection instead of a
    pre-hoc justification for whatever tool comes next (mirrors the
    already-fixed field order in ``build_voice_first_grammar``).

    Each tool gets a per-tool call rule whose JSON body lists only the
    tool's *required string* fields. Optional fields are dropped (matches
    proven B4-typed probe — extras add noise).
    Raises NotImplementedError if any required field is non-string, or if
    a tool / field name contains characters needing escaping.
    """
    if not tools:
        raise ValueError("tools must be non-empty for grammar build")

    tool_names: list[str] = [cls.__name__ for cls in tools]
    used_aux_rule_ids: set[str] = set()
    call_rule_ids: list[str] = []
    rule_texts: list[str] = []
    for cls in tools:
        rid, rtext = _build_tool_call_rule(cls, used_aux_rule_ids)
        call_rule_ids.append(rid)
        rule_texts.append(rtext)

    tool_name_alts = " | ".join(f'"{n}"' for n in tool_names)
    tool_call_alts = " | ".join(call_rule_ids)

    head = (
        "root ::= think tool-call\n"
        'think ::= "SEEN: " line "INTENT: " line "TOOL: " tool-name "\\n" "REVIEW: " line "MOOD: " line "</think>\\n\\n"\n'
        'line ::= [^\\n]+ "\\n"\n'
        f"tool-name ::= {tool_name_alts}\n"
        f"tool-call ::= {tool_call_alts}\n"
    )
    body = "\n".join(rule_texts) + "\n"
    return head + body + _JSON_STR_RULES


def build_voice_first_grammar(tools: list[type[BaseModel]]) -> str:
    """Build a GBNF grammar for voice-first output.

    Constrains the model to:
      <think>SEEN/INTENT/REVIEW/MOOD/TOOL lines</think>\\n\\n
      (speak | tool-call)*

    speak content cannot contain literal `<` (trade-off — natural
    Chinese/English doesn't need `<`). Zero segments after </think>
    is permitted (silent finish).
    """
    if not tools:
        raise ValueError("tools must be non-empty for voice_first grammar build")

    for tool in tools:
        if "\\" in tool.__name__ or '"' in tool.__name__:
            raise ValueError(
                f"tool name {tool.__name__!r} contains backslash/quote; unsupported"
            )

    used_aux_rule_ids: set[str] = set()
    rule_ids: list[str] = []
    rules: list[str] = []
    for tool in tools:
        rid, rtext = _build_tool_call_rule(
            tool, used_aux_rule_ids, include_optional=True
        )
        rule_ids.append(rid)
        rules.append(rtext)

    tool_call_alts = " | ".join(rule_ids)
    head = (
        "root ::= think segments\n"
        'think ::= "SEEN: " line "INTENT: " line "TOOL: " line '
        '"REVIEW: " line "MOOD: " line "</think>\\n\\n"\n'
        'line ::= [^\\n]+ "\\n"\n'
        "segments ::= segment*\n"
        "segment ::= speak | tool-call\n"
        "speak ::= [^<]+\n"
        f"tool-call ::= {tool_call_alts}\n"
    )
    body = "\n".join(rules) + "\n"
    return head + body + _JSON_STR_RULES


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
