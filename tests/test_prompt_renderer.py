"""Tests for PromptRenderer."""

import pytest
from jinja2 import TemplateNotFound

from dollos.character import Identity
from dollos.prompts import PromptRenderer


def _identity(
    self_: str = "You are Doll.",
    personality: str = "- chill",
    taboos: str = "- no LARP",
) -> Identity:
    return Identity(self=self_, personality=personality, taboos=taboos)


def test_render_scaffolding_with_character_includes_text():
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(self_="You are Gura."))
    assert "You are Gura." in out


def test_render_scaffolding_no_ctx_returns_base_sections():
    """No ctx vars → conditional blocks (Identity, Skills, Rules, Examples)
    skipped; only Behavior / Memory remain. Tool Calling section removed
    (now handled by Qwen3 native preamble in _format_tools_block)."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding")
    assert "# Behavior" in out
    assert "# Tool Calling" not in out
    assert "# Skills" not in out
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
    out = renderer.render("scaffolding", identity=_identity())
    assert "嘗試多次" in out or "tried multiple times" in out.lower()
    assert "換方法" in out or "change approach" in out.lower() or "different" in out.lower()
    assert "停止" in out or "stop" in out.lower()


def test_scaffolding_omits_skills_section_when_no_skills_available():
    """available_skills=[] → entire # Skills section absent. Model must
    never see InvokeSkill / skill_bodies references when no skills exist."""
    renderer = PromptRenderer()
    out = renderer.render(
        "scaffolding", identity=_identity(), available_skills=[]
    )
    assert "# Skills" not in out
    assert "InvokeSkill" not in out
    assert "skill_bodies" not in out


def test_scaffolding_omits_skills_section_when_argument_missing():
    """Same as above but available_skills not passed at all."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity())
    assert "# Skills" not in out
    assert "InvokeSkill" not in out
    assert "skill_bodies" not in out


def test_scaffolding_renders_skill_list_when_available():
    """available_skills non-empty → # Skills section appears with literal
    skill names listed and InvokeSkill mentioned in usage bullet."""
    renderer = PromptRenderer()
    out = renderer.render(
        "scaffolding",
        identity=_identity(),
        available_skills=["morning", "bedtime"],
    )
    assert "# Skills" in out
    assert "- morning" in out
    assert "- bedtime" in out
    assert "InvokeSkill" in out


def test_scaffolding_has_partitioned_sections():
    """Partitioned layout uses H1 headers. Tool Calling section removed
    (now handled by Qwen3 native preamble in _format_tools_block).
    # Skills now conditional on available_skills."""
    renderer = PromptRenderer()
    out = renderer.render(
        "scaffolding",
        identity=_identity(),
        available_skills=["x"],
    )
    assert "# Identity" in out
    assert "# Behavior" in out
    assert "# Tool Calling" not in out
    assert "# Skills" in out


def test_scaffolding_includes_think_bridging_rule():
    """Behavior section explains tool result -> think bridging."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity())
    assert "Tool 結果回來" in out or "tool" in out.lower()
    assert "<think>" in out


def test_scaffolding_includes_act_after_thinking_rule():
    """Behavior section explicitly forbids ACTION: / plan plaintext."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity())
    assert "思考後動手" in out
    assert "ACTION" in out  # mentioned as anti-pattern


def test_scaffolding_memory_section_includes_curiosity_fallback():
    """The # Memory section includes the don't-know fallback flow:
    Recall first, then ask user, then NoteMemory after they tell."""
    renderer = PromptRenderer()
    rendered = renderer.render("scaffolding", identity=_identity(self_="Test"))
    # Anchor on the `# Memory` section.
    assert "# Memory" in rendered
    # The fallback flow mentions both tools by name.
    assert "Recall" in rendered
    assert "NoteMemory" in rendered
    # The flow language anchors the don't-know case.
    assert "不確定" in rendered or "不瞎掰" in rendered


def test_scaffolding_has_think_structure_section():
    """# Think structure section documents the 5 think-block fields
    (SEEN / INTENT / REVIEW / MOOD / TOOL) so the model knows what each
    slot is for. REVIEW is self-reflection; MOOD is the emotional snapshot
    the dispatcher parses to update Doll's current mood."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity())
    assert "# Think structure" in out
    for label in ("SEEN", "INTENT", "REVIEW", "MOOD", "TOOL"):
        assert f"**{label}**:" in out, f"missing labeled bullet for {label}"


def test_scaffolding_behavior_disambiguates_user_first_person():
    """Model previously misread user '我' as referring to Doll (T2 fabrication
    bug). Scaffolding now anchors '我' = speaker = user."""
    renderer = PromptRenderer()
    rendered = renderer.render("scaffolding", identity=_identity(self_="Test"))
    assert "主人說的「我」永遠是主人" in rendered


def test_scaffolding_renders_identity_sections():
    """Scaffolding renders all three Identity fields under labeled sections."""
    renderer = PromptRenderer()
    identity = Identity(
        self="我是測試 Doll。",
        personality="- 簡短\n- 好奇",
        taboos="- 不 LARP",
    )
    out = renderer.render("scaffolding", identity=identity)
    assert "# Identity" in out
    assert "我是測試 Doll。" in out
    assert "## 個性" in out
    assert "- 簡短" in out
    assert "- 好奇" in out
    assert "## 禁忌" in out
    assert "- 不 LARP" in out


def test_iv_compact_template_renders_with_perception_and_messages():
    """iv_compact template renders system + user blocks, threading
    perception + cascade_messages content into the user block."""
    renderer = PromptRenderer()
    msgs = [
        {"role": "assistant", "content": "想了一下要不要 Recall"},
        {"role": "user", "content": "<tool_response>\n找到了\n</tool_response>"},
    ]
    blocks = renderer.render_blocks(
        "iv_compact",
        perception="我剛才說了什麼",
        cascade_messages=msgs,
    )
    assert set(blocks.keys()) >= {"system", "user"}
    # System: voice / language rules.
    assert "第一人稱" in blocks["system"] or "first-person" in blocks["system"].lower()
    assert "1-2" in blocks["system"] or "一" in blocks["system"]
    # User: perception + cascade content threaded.
    assert "我剛才說了什麼" in blocks["user"]
    assert "想了一下要不要 Recall" in blocks["user"]
    assert "找到了" in blocks["user"]
    # User block markers per plan §1.
    assert "[我]" in blocks["user"]
    assert "[Result]" in blocks["user"]


def test_iv_compact_template_no_longer_references_mood():
    """Mood is now produced by the big model in <think>, not the small
    model post-hoc. iv_compact must not mention prior_mood / MOOD output."""
    renderer = PromptRenderer()
    blocks = renderer.render_blocks(
        "iv_compact",
        perception="hi",
        cascade_messages=[],
    )
    assert "我之前的心情" not in blocks["user"]
    assert "MOOD" not in blocks["system"]
    assert "SUMMARY:" not in blocks["system"]


def test_iv_summary_template_includes_language_rule():
    """Inner Voice prompt must enforce 繁體中文 output."""
    renderer = PromptRenderer()
    blocks = renderer.render_blocks(
        "iv_summary", prev_summary="(none)", perception="hello"
    )
    assert "繁體中文" in blocks["system"] or "Traditional Chinese" in blocks["system"]


def test_scaffolding_explains_pending_events_block():
    """gap #4: scaffolding teaches Doll what `[Pending events]` means."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity())
    assert "[Pending events]" in out
