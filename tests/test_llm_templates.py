"""Tests for PromptTemplate ABC + Qwen3ThinkingTemplate + Qwen3PlainTemplate."""

import json

import pytest
from pydantic import BaseModel, Field

from dollos.llm.templates import PromptTemplate, Qwen3PlainTemplate, Qwen3ThinkingTemplate


def test_template_is_abstract():
    with pytest.raises(TypeError):
        PromptTemplate()  # type: ignore[abstract]


def test_qwen3_thinking_renders_chatml_envelope():
    tpl = Qwen3ThinkingTemplate()
    out = tpl.render(system="SYS", user="USR", prefill="")

    assert "<|im_start|>system\nSYS<|im_end|>" in out
    assert "<|im_start|>user\nUSR<|im_end|>" in out
    assert "<|im_start|>assistant\n<think>\n" in out


def test_qwen3_thinking_appends_prefill_after_think_marker():
    tpl = Qwen3ThinkingTemplate()
    out = tpl.render(system="s", user="u", prefill="RECALL: x\nGOAL: ")
    assert out.endswith("<think>\nRECALL: x\nGOAL: ")


def test_qwen3_thinking_empty_prefill_ends_with_newline_after_think():
    tpl = Qwen3ThinkingTemplate()
    out = tpl.render(system="s", user="u", prefill="")
    # When prefill is empty the renderer appends nothing extra, so the
    # prompt ends with the assistant turn opener: "<think>\n"
    assert out.endswith("<think>\n")


def test_qwen3_thinking_preserves_special_chars_in_inputs():
    tpl = Qwen3ThinkingTemplate()
    out = tpl.render(system="line1\nline2", user="<tag>", prefill="")
    assert "line1\nline2" in out
    assert "<tag>" in out


def test_qwen3_plain_renders_chatml_envelope_with_closed_think():
    tpl = Qwen3PlainTemplate()
    out = tpl.render(system="SYS", user="USR", prefill="")

    assert "<|im_start|>system\nSYS<|im_end|>" in out
    assert "<|im_start|>user\nUSR<|im_end|>" in out
    assert "<|im_start|>assistant\n" in out
    # Closed empty think block must be present to suppress thinking
    assert "<think>\n\n</think>" in out


def test_qwen3_plain_appends_prefill_after_closed_think_block():
    tpl = Qwen3PlainTemplate()
    out = tpl.render(system="s", user="u", prefill="bullet 1\nbullet 2")
    assert out.endswith("</think>\n\nbullet 1\nbullet 2")


def test_qwen3_plain_empty_prefill_ends_with_double_newline_after_think():
    tpl = Qwen3PlainTemplate()
    out = tpl.render(system="s", user="u", prefill="")
    assert out.endswith("</think>\n\n")


def test_qwen3_plain_preserves_special_chars_in_inputs():
    tpl = Qwen3PlainTemplate()
    out = tpl.render(system="multi\nline", user="<tag>", prefill="")
    assert "multi\nline" in out
    assert "<tag>" in out


def test_qwen3_plain_subclasses_prompt_template():
    assert issubclass(Qwen3PlainTemplate, PromptTemplate)


class _ExampleSay(BaseModel):
    """Stream text to the user."""

    text: str = Field(description="What to say.")


class _ExampleNote(BaseModel):
    """Record a fact."""

    text: str = Field(description="What to record.")


def test_thinking_template_with_tools_renders_tools_block():
    t = Qwen3ThinkingTemplate()
    rendered = t.render(
        system="You are Doll.",
        user="hi",
        prefill="",
        tools=[_ExampleSay, _ExampleNote],
    )
    assert "# Tools" in rendered
    assert "<tools>" in rendered
    assert "</tools>" in rendered
    assert "_ExampleSay" in rendered
    assert "_ExampleNote" in rendered
    assert '"text"' in rendered
    # Qwen3 native preamble
    assert "You may call one or more functions" in rendered
    assert "<tool_call>" in rendered


def test_thinking_template_without_tools_omits_block():
    t = Qwen3ThinkingTemplate()
    rendered = t.render(system="You are Doll.", user="hi", prefill="")
    assert "# Tools" not in rendered
    assert "<tools>" not in rendered


def test_thinking_template_empty_tools_list_omits_block():
    t = Qwen3ThinkingTemplate()
    rendered = t.render(system="You are Doll.", user="hi", prefill="", tools=[])
    assert "# Tools" not in rendered


def test_thinking_template_tools_block_contains_valid_json():
    t = Qwen3ThinkingTemplate()
    rendered = t.render(system="x", user="y", prefill="", tools=[_ExampleSay])
    # Qwen3 native preamble includes "<tools></tools>" in description text, so
    # find the opening <tools> that is followed by a newline (i.e., the actual block).
    marker = "<tools>\n"
    start = rendered.index(marker) + len(marker)
    end = rendered.index("</tools>", start)
    payload = rendered[start:end].strip()
    lines = [json.loads(line) for line in payload.split("\n") if line.strip()]
    assert isinstance(lines, list)
    item = lines[0]
    assert item["type"] == "function"
    assert item["function"]["name"] == "_ExampleSay"
    assert "description" in item["function"]
    assert "parameters" in item["function"]
    params = item["function"]["parameters"]
    assert params["type"] == "object"
    assert "properties" in params
    # No title fields anywhere
    assert "title" not in params
    for prop in params["properties"].values():
        assert "title" not in prop


def test_thinking_template_tools_block_uses_compact_json():
    """Hermes-compact uses no indent — saves significant chars."""
    t = Qwen3ThinkingTemplate()
    rendered = t.render(
        system="x", user="y", prefill="", tools=[_ExampleSay, _ExampleNote]
    )
    start = rendered.index("<tools>") + len("<tools>")
    end = rendered.index("</tools>")
    payload = rendered[start:end].strip()
    # Compact JSON has no '\n  ' indent patterns inside the array
    assert "\n  " not in payload


def test_plain_template_rejects_non_empty_tools():
    t = Qwen3PlainTemplate()
    with pytest.raises(NotImplementedError):
        t.render(system="x", user="y", prefill="", tools=[_ExampleSay])


def test_plain_template_accepts_none_tools():
    t = Qwen3PlainTemplate()
    out = t.render(system="x", user="y", prefill="", tools=None)
    assert "x" in out


def test_plain_template_accepts_empty_tools_list():
    t = Qwen3PlainTemplate()
    out = t.render(system="x", user="y", prefill="", tools=[])
    assert "x" in out


def test_qwen3_thinking_render_messages_basic():
    """One user msg → output has system, user, and an open assistant turn."""
    t = Qwen3ThinkingTemplate()
    out = t.render_messages(
        system="SYS",
        messages=[{"role": "user", "content": "hello"}],
    )
    assert "<|im_start|>system\nSYS<|im_end|>\n" in out
    assert "<|im_start|>user\nhello<|im_end|>\n" in out
    assert out.endswith("<|im_start|>assistant\n<think>\n")


def test_qwen3_thinking_render_messages_multi():
    """user, assistant, user → 3 message blocks plus final open assistant."""
    t = Qwen3ThinkingTemplate()
    out = t.render_messages(
        system="S",
        messages=[
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ],
    )
    assert out.count("<|im_start|>user\n") == 2
    # 1 assistant in history + 1 final open assistant
    assert out.count("<|im_start|>assistant\n") == 2
    # Final block is the open <think> turn
    assert out.endswith("<|im_start|>assistant\n<think>\n")
    # Message ordering preserved
    u1 = out.index("u1")
    a1 = out.index("a1")
    u2 = out.index("u2")
    assert u1 < a1 < u2


def test_qwen3_thinking_render_messages_with_tools():
    """Tools provided → # Tools block appears in system."""
    t = Qwen3ThinkingTemplate()
    out = t.render_messages(
        system="You are Doll.",
        messages=[{"role": "user", "content": "hi"}],
        tools=[_ExampleSay],
    )
    assert "# Tools" in out
    assert "<tools>" in out
    assert "_ExampleSay" in out
    # Tools block must live inside the system block
    sys_start = out.index("<|im_start|>system\n")
    sys_end = out.index("<|im_end|>", sys_start)
    assert "# Tools" in out[sys_start:sys_end]


def test_qwen3_thinking_render_messages_tool_response_role():
    """A user msg whose content is a <tool_response>...</tool_response>
    block renders verbatim inside <|im_start|>user\\n...<|im_end|>."""
    t = Qwen3ThinkingTemplate()
    tr_content = "<tool_response>\n[exit 0]\n/home/x\n</tool_response>"
    out = t.render_messages(
        system="S",
        messages=[
            {"role": "user", "content": "ask"},
            {"role": "assistant", "content": "<tool_call>...</tool_call>"},
            {"role": "user", "content": tr_content},
        ],
    )
    assert f"<|im_start|>user\n{tr_content}<|im_end|>\n" in out
