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
