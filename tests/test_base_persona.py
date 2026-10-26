"""Base persona template (spec 2026-10-25-base-persona-template-design §3)."""
from importlib.resources import files

import jinja2
import pytest

from dollos.character import Identity
from dollos.prompts import PromptRenderer

_TEMPLATES = files("dollos.prompts") / "templates"


def _identity():
    return Identity(self="You are Gura.", personality="- chill", taboos="- no LARP")


@pytest.mark.parametrize(
    ("language", "heading", "talk_line"),
    [
        ("zh-TW", "# 我怎麼存在", "我跟主人說話用繁體中文（台灣用法）。"),
        ("en", "# How I exist", "I talk with my owner in English."),
    ],
)
def test_base_persona_precedes_identity(language, heading, talk_line):
    out = PromptRenderer().render("scaffolding", identity=_identity(), language=language)
    assert talk_line in out
    assert out.index(heading) < out.index(talk_line) < out.index("# Identity")


def test_both_templates_have_the_same_items():
    def items(lang):
        text = (_TEMPLATES / f"base_persona.{lang}.jinja").read_text(encoding="utf-8")
        return sum(line.startswith("- ") for line in text.splitlines())

    assert items("zh-TW") == items("en") > 0


def test_identity_without_language_fails():
    with pytest.raises(jinja2.TemplateNotFound):
        PromptRenderer().render("scaffolding", identity=_identity())
