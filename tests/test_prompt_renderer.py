"""Tests for PromptRenderer."""

import pytest
from jinja2 import TemplateNotFound

from dollos.prompts import PromptRenderer


def test_render_scaffolding_with_character_includes_text():
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", character="You are Gura.")
    assert "You are Gura." in out


def test_render_scaffolding_no_ctx_returns_empty():
    """No ctx vars → all conditional blocks skipped → output is empty / whitespace only."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding")
    assert out.strip() == ""


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
