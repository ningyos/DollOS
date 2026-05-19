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


def test_scaffolding_no_longer_restricts_single_speak_block():
    """Plan 2 lifted the Plan 1 'single trailing speak' restriction."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding")
    assert "single block at the end of your turn" not in out
    assert "Plan 2 fixes it" not in out
    # The "interleave freely" line stays
    assert "interleave" in out.lower()


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
    assert "嘗試多次" in out or "repeated attempts" in out.lower()
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
    assert "Think first, then act" in out
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
    assert "unsure" in rendered or "don't fabricate" in rendered


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
    assert 'When the user says "I" / "me", they always mean themselves, not you.' in rendered


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
    assert "## Personality" in out
    assert "- 簡短" in out
    assert "- 好奇" in out
    assert "## Taboos" in out
    assert "- 不 LARP" in out



def test_scaffolding_explains_pending_events_block():
    """gap #4: scaffolding teaches Doll what `[Pending events]` means."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity())
    assert "[Pending events]" in out


def test_scaffolding_documents_tool_output_paging():
    """Scaffolding teaches Doll how to use ReadToolOutput / GrepToolOutput
    for paginated reads of Shell and Subagent results."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity())
    assert "ReadToolOutput" in out
    assert "GrepToolOutput" in out
    assert "output_id" in out


def test_scaffolding_documents_scratchpad():
    """Scaffolding teaches Doll about the Scratchpad working memory."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), available_skills=[])
    assert "WriteScratchpad" in out
    assert "AppendScratchpad" in out
    assert "EditScratchpad" in out
    assert "ClearScratchpad" in out
    assert "working memory" in out.lower()


def test_scaffolding_voice_framing_present():
    """Voice-first output framing: scaffolding tells Doll that text outside
    `<think>`/`<tool_call>` is spoken aloud via TTS, and explains silent turns."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), tool_registry={})
    # Voice framing — text outside <think>/<tool_call> is spoken
    assert "spoken aloud" in out or "the user hears" in out or "TTS" in out
    # Tool call markup present
    assert "<tool_call>" in out
    # Silent turn convention — empty/nothing
    assert "nothing" in out.lower() or "silent" in out.lower()


def test_scaffolding_drops_legacy_json_action_array():
    """Old `{"action": "..."}` JSON array convention must be gone."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), tool_registry={})
    assert '"action":' not in out
    assert "JSON array" not in out


def test_scaffolding_renders_tools_doc():
    """tool_registry → tools_doc rendered as `# Tools available` section."""
    from dollos.tools import NoteMemory

    renderer = PromptRenderer()
    out = renderer.render(
        "scaffolding",
        identity=_identity(),
        tool_registry={"NoteMemory": NoteMemory},
    )
    assert "NoteMemory" in out


def test_scaffolding_no_recent_thoughts_block_doc():
    """[Recent thoughts] block will be deleted in Task 8 — scaffolding no
    longer references it (avoid having to revisit later)."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), tool_registry={})
    assert "[Recent thoughts]" not in out


def test_subagent_scaffolding_documents_scratchpad():
    """Subagent scaffolding documents Scratchpad as independent memory."""
    renderer = PromptRenderer()
    out = renderer.render("subagent_scaffolding")
    assert "WriteScratchpad" in out
    assert "your own private" in out.lower()


def test_scaffolding_has_interrupts_section():
    renderer = PromptRenderer()
    out = renderer.render("scaffolding")
    assert "Interrupt" in out
    assert "cut short" in out.lower()
