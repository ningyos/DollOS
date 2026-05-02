"""Tests for PromptTemplate ABC + Qwen3ThinkingTemplate."""

import pytest

from dollos.llm.templates import PromptTemplate, Qwen3ThinkingTemplate


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
