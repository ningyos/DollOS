"""Tests for PromptTemplate ABC + Qwen3ThinkingTemplate + Qwen3PlainTemplate."""

import pytest

from dollos.llm.templates import PromptTemplate, Qwen3ThinkingTemplate, Qwen3PlainTemplate


def test_template_is_abstract():
    with pytest.raises(TypeError):
        PromptTemplate()  # type: ignore[abstract]


def test_qwen3_thinking_renders_chatml_envelope():
    tpl = Qwen3ThinkingTemplate()
    out = tpl.render(system="SYS", user="USR", prefill="")

    assert "<|im_start|>system\nSYS\n<|im_end|>" in out
    assert "<|im_start|>user\nUSR\n<|im_end|>" in out
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

    assert "<|im_start|>system\nSYS\n<|im_end|>" in out
    assert "<|im_start|>user\nUSR\n<|im_end|>" in out
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
