"""Tests for PromptRenderer."""

import pytest
from jinja2 import TemplateNotFound

from dollos.prompts import PromptRenderer


def test_render_scaffolding_with_character_includes_text():
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", character="You are Gura.")
    assert "You are Gura." in out


def test_render_scaffolding_no_ctx_returns_base_sections():
    """No ctx vars → conditional blocks (Identity, Rules, Examples) skipped,
    but Behavior / Skills are unconditional. Tool Calling section removed
    (now handled by Qwen3 native preamble in _format_tools_block)."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding")
    assert "# Behavior" in out
    assert "# Tool Calling" not in out
    assert "# Skills" in out
    assert "# Identity" not in out


def test_render_with_ctx_substitutes_variables():
    renderer = PromptRenderer()
    out = renderer.render("_test_fixture", greeting="hi")
    assert out.strip() == "hi"


def test_render_unknown_template_raises():
    renderer = PromptRenderer()
    with pytest.raises(TemplateNotFound):
        renderer.render("does_not_exist")


def test_renderer_does_not_html_escape():
    """Prompts are plain text, not HTML — angle brackets must pass through verbatim."""
    renderer = PromptRenderer()
    out = renderer.render("_test_fixture", greeting="<tag>")
    assert "<tag>" in out


def test_render_blocks_returns_dict_with_each_block():
    """A template with multiple {% block %} sections returns a dict keyed by name."""
    renderer = PromptRenderer()
    blocks = renderer.render_blocks("_test_blocks_fixture", greeting="hi", item="apple")
    assert set(blocks.keys()) == {"system", "user"}
    assert blocks["system"] == "hi from system"
    assert blocks["user"] == "user wants apple"


def test_render_blocks_strips_per_block_whitespace():
    renderer = PromptRenderer()
    blocks = renderer.render_blocks("_test_blocks_fixture", greeting="hi", item="apple")
    for v in blocks.values():
        assert v == v.strip()


def test_render_blocks_substitutes_ctx_into_each_block():
    renderer = PromptRenderer()
    blocks = renderer.render_blocks("_test_blocks_fixture", greeting="yo", item="banana")
    assert "yo" in blocks["system"]
    assert "banana" in blocks["user"]


def test_render_blocks_unknown_template_raises():
    renderer = PromptRenderer()
    with pytest.raises(TemplateNotFound):
        renderer.render_blocks("does_not_exist")


def test_scaffolding_includes_meta_rule_about_multi_try():
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", character="You are Doll.")
    assert "嘗試多次" in out or "tried multiple times" in out.lower()
    assert "換方法" in out or "change approach" in out.lower() or "different" in out.lower()
    assert "停止" in out or "stop" in out.lower()


def test_scaffolding_includes_skill_convention():
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", character="You are Doll.")
    assert "skill" in out.lower()
    assert "InvokeSkill" in out
    assert "skills/" in out
    assert "skill_bodies/" in out


def test_scaffolding_has_partitioned_sections():
    """Partitioned layout uses H1 headers. Tool Calling section removed
    (now handled by Qwen3 native preamble in _format_tools_block)."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", character="You are Doll.")
    assert "# Identity" in out
    assert "# Behavior" in out
    assert "# Tool Calling" not in out
    assert "# Skills" in out


def test_scaffolding_includes_think_bridging_rule():
    """Behavior section explains tool result -> think bridging."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", character="You are Doll.")
    assert "Tool 結果回來" in out or "tool" in out.lower()
    assert "<think>" in out


def test_scaffolding_includes_act_after_thinking_rule():
    """Behavior section explicitly forbids ACTION: / plan plaintext."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", character="You are Doll.")
    assert "思考後動手" in out
    assert "ACTION" in out  # mentioned as anti-pattern


def test_iv_summary_template_includes_language_rule():
    """Inner Voice prompt must enforce 繁體中文 output."""
    renderer = PromptRenderer()
    blocks = renderer.render_blocks(
        "iv_summary", prev_summary="(none)", perception="hello"
    )
    assert "繁體中文" in blocks["system"] or "Traditional Chinese" in blocks["system"]
