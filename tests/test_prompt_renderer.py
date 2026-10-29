"""Tests for PromptRenderer."""

from pathlib import Path

import pytest
from jinja2 import TemplateNotFound

from dollos.character import DollPack, Identity
from dollos.kernel import split_scaffolding
from dollos.prompts import PromptRenderer


def _identity(
    self_: str = "You are Doll.",
    personality: str = "- chill",
    taboos: str = "- no LARP",
) -> Identity:
    return Identity(self=self_, personality=personality, taboos=taboos)


def test_render_scaffolding_with_character_includes_text():
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(self_="You are Gura."), language="en")
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


def test_scaffolding_output_states_speech_precedes_tool_calls():
    """Model API plan Task 6: the protocol puts speech before tool calls in
    one reply (no more 'interleave freely'); after the tool results come back
    she may speak again or stay silent."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding")
    start = out.index("# Output")
    end = out.index("# Interrupts", start)
    output_section = out[start:end]
    assert "interleave" not in out.lower()
    assert "what you say comes before your tool calls" in output_section
    assert "After the tool results come back" in output_section


def test_scaffolding_includes_meta_rule_about_multi_try():
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), language="en")
    assert "嘗試多次" in out or "repeated attempts" in out.lower()
    assert "換方法" in out or "change approach" in out.lower() or "different" in out.lower()
    assert "停止" in out or "stop" in out.lower()


def test_scaffolding_omits_skills_section_when_no_skills_available():
    """available_skills=[] → entire # Skills section absent. Model must
    never see InvokeSkill / skill_bodies references when no skills exist."""
    renderer = PromptRenderer()
    out = renderer.render(
        "scaffolding", identity=_identity(), language="en", available_skills=[]
    )
    assert "# Skills" not in out
    assert "InvokeSkill" not in out
    assert "skill_bodies" not in out


def test_scaffolding_omits_skills_section_when_argument_missing():
    """Same as above but available_skills not passed at all."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), language="en")
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
        language="en",
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
        language="en",
        available_skills=["x"],
    )
    assert "# Identity" in out
    assert "# Behavior" in out
    assert "# Tool Calling" not in out
    assert "# Skills" in out


def test_scaffolding_includes_tool_result_rule():
    """Behavior section explains what to do when a tool result comes back —
    without the old `<think>` markup (model API plan Task 6)."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), language="en")
    assert "When a tool result comes back, re-evaluate" in out
    assert "<think>" not in out


def test_scaffolding_includes_act_after_thinking_rule():
    """Behavior section explicitly forbids ACTION: / plan plaintext."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), language="en")
    assert "Think first, then act" in out
    assert "ACTION" in out  # mentioned as anti-pattern


def test_scaffolding_memory_section_includes_curiosity_fallback():
    """The # Memory section includes the don't-know fallback flow:
    Recall first, then ask user, then NoteMemory after they tell."""
    renderer = PromptRenderer()
    rendered = renderer.render("scaffolding", identity=_identity(self_="Test"), language="en")
    # Anchor on the `# Memory` section.
    assert "# Memory" in rendered
    # The fallback flow mentions both tools by name.
    assert "Recall" in rendered
    assert "NoteMemory" in rendered
    # The flow language anchors the don't-know case.
    assert "unsure" in rendered or "don't fabricate" in rendered


def test_scaffolding_teaches_no_wire_format():
    """Model API plan Task 6: thinking and tool calls are native on the chat
    API and the server's own template teaches the tool-call format, so the
    scaffolding carries no # Format / # Think structure / # Tools available
    section and no `<think>` / JSON `<tool_call>` markup — two formats in one
    prompt would fight. Rendered the way the kernel renders it (real pack)."""
    pack = DollPack.load(Path(__file__).resolve().parent.parent / "character_packs" / "gura")
    prefix, suffix = split_scaffolding(
        PromptRenderer(), identity=pack.identity, language=pack.meta.language, available_skills=["x"],
        evolution_enabled=True)
    out = prefix + suffix
    for marker in ("# Format", "# Think structure", "# Tools available",
                   "<think>", "</think>", "<tool_call>", "</tool_call>",
                   '"arguments"', "SEEN:", "INTENT:", "REVIEW:", "MOOD:"):
        assert marker not in out, f"old format marker still taught: {marker!r}"


def test_scaffolding_behavior_disambiguates_user_first_person():
    """Model previously misread user '我' as referring to Doll (T2 fabrication
    bug). Scaffolding now anchors '我' = speaker = user."""
    renderer = PromptRenderer()
    rendered = renderer.render("scaffolding", identity=_identity(self_="Test"), language="en")
    assert 'When the user says "I" / "me", they always mean themselves, not you.' in rendered


def test_scaffolding_renders_identity_sections():
    """Scaffolding renders all three Identity fields under labeled sections."""
    renderer = PromptRenderer()
    identity = Identity(
        self="我是測試 Doll。",
        personality="- 簡短\n- 好奇",
        taboos="- 不 LARP",
    )
    out = renderer.render("scaffolding", identity=identity, language="en")
    assert "# Identity" in out
    assert "我是測試 Doll。" in out
    assert "## Personality" in out
    assert "- 簡短" in out
    assert "- 好奇" in out
    assert "## Taboos" in out
    assert "- 不 LARP" in out


def test_scaffolding_explains_the_append_only_conversation():
    """Plan 2026-10-18 Task 9: scaffolding teaches the new format — one new
    message per turn starting with the time, a page snapshot, hits given once
    per page — and no longer explains blocks that never render."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), language="en")
    assert "[New conversation page" in out
    assert "each turn adds one new message" in out
    assert "once per page" in out
    assert "[Pending events]" not in out
    assert "[Active monitors]" not in out


def test_scaffolding_documents_tool_output_paging():
    """Scaffolding teaches Doll how to use ReadToolOutput / GrepToolOutput
    for paginated reads of Shell and Subagent results."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), language="en")
    assert "ReadToolOutput" in out
    assert "GrepToolOutput" in out
    assert "output_id" in out


def test_scaffolding_documents_scratchpad():
    """Scaffolding teaches Doll about the Scratchpad working memory."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), language="en", available_skills=[])
    assert 'Scratchpad(op="set"' in out
    assert 'Scratchpad(op="append"' in out
    assert 'Scratchpad(op="clear"' in out
    assert "working memory" in out.lower()


def test_scaffolding_voice_framing_present():
    """Voice-first output framing: the reply text is spoken aloud via TTS,
    thinking and tool calls are not heard, and silent turns are explained."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), language="en")
    start = out.index("# Output")
    end = out.index("# Interrupts", start)
    output_section = out[start:end]
    assert "spoken aloud" in output_section and "the user hears" in output_section
    assert "Your thinking is not heard." in output_section
    assert "Tool calls are not heard." in output_section
    assert "Nothing to say?" in output_section


def test_subagent_scaffolding_documents_scratchpad():
    """Subagent scaffolding documents Scratchpad as independent memory."""
    renderer = PromptRenderer()
    out = renderer.render("subagent_scaffolding")
    assert "Scratchpad" in out
    assert "your own private" in out.lower()


def test_subagent_scaffolding_states_the_per_agent_cap():
    """A workflow worker learns its run's wall-clock cap before a Shell is
    refused for it (plan 2026-10-28 decision 2: a worker's Shell waits inside
    the cap). Keepers render it without one: no Shell, their own caps."""
    out = PromptRenderer().render("subagent_scaffolding", agent_timeout_s=300)
    assert "cut off after 300 s" in out
    assert "`timeout_s` of 300 or more is refused" in out
    assert "cut off after" not in PromptRenderer().render("subagent_scaffolding")


def test_scaffolding_has_interrupts_section():
    renderer = PromptRenderer()
    out = renderer.render("scaffolding")
    assert "Interrupt" in out
    assert "cut short" in out.lower()


def test_scaffolding_has_recovery_section():
    """Scaffolding documents how Doll should react to Awoke(recovered)."""
    from dollos.prompts.renderer import PromptRenderer
    out = PromptRenderer().render("scaffolding")
    # Either explicit "Recovery" header or "recovered" body text — flexible
    assert "recover" in out.lower()


def test_scaffolding_reflection_section_mentions_pinself():
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), language="en")
    assert "PinSelf" in out


def test_scaffolding_reflection_section_splits_by_subject():
    """The always-in-context Reflection description must describe BOTH
    channels (PinSelf for about-you, NoteMemory for about-the-world),
    not just NoteMemory — otherwise it silently contradicts the
    ReflectionMoment nudge every turn (R1 finding, spec §3.5)."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), language="en")
    start = out.index("# Reflection")
    end = out.index("# Output", start)
    reflection_section = out[start:end]
    assert "PinSelf" in reflection_section
    assert "NoteMemory" in reflection_section
    assert "NoteToolLesson" in reflection_section


def test_scaffolding_reflection_section_allows_not_yet_durable():
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), language="en")
    start = out.index("# Reflection")
    end = out.index("# Output", start)
    reflection_section = out[start:end]
    assert "pruning" in reflection_section.lower() or "淘汰" in reflection_section


def test_scaffolding_reflection_section_keeps_operational_details():
    """Cadence/housekeeping details from the old section must survive the
    rewrite — this is a reframe, not a rewrite of the mechanics."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), language="en")
    start = out.index("# Reflection")
    end = out.index("# Output", start)
    reflection_section = out[start:end]
    assert "~30 iterations" in reflection_section
    assert "Do not speak to the user" in reflection_section


def test_scaffolding_reflection_section_mentions_selfrevision_when_evolution_enabled():
    """Live-smoke root cause (p2-live-smoke-report.md): SelfRevision was
    invisible in the model-facing prompt (PinSelf 22x vs SelfRevision 1x),
    so the weak model routed [Self revision candidate] decisions to PinSelf. The
    always-in-context Reflection section must name SelfRevision explicitly
    when evolution is enabled, and distinguish it from PinSelf."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), language="en", evolution_enabled=True)
    start = out.index("# Reflection")
    end = out.index("# Output", start)
    reflection_section = out[start:end]
    assert "SelfRevision" in reflection_section
    assert "[Self revision candidate]" in reflection_section
    assert "PinSelf" in reflection_section


def test_scaffolding_reflection_section_omits_selfrevision_when_evolution_disabled():
    """Byte-neutrality: with the flag absent/False, the render must be
    identical to today's (no SelfRevision leakage into the always-in-context
    prompt for runs that don't have evolution enabled)."""
    renderer = PromptRenderer()
    out_absent = renderer.render("scaffolding", identity=_identity(), language="en")
    out_false = renderer.render("scaffolding", identity=_identity(), language="en", evolution_enabled=False)
    assert out_absent == out_false
    assert "SelfRevision" not in out_absent
    assert "[Self revision candidate]" not in out_absent


def test_scaffolding_example_speaks_and_notes_in_english():
    """The only dialogue example: the owner asks in English and Doll answers
    in plain English (no particle, no "~"); the NoteMemory text is English
    too, matching `[memory] primary_language` (all-English plan Task 2, Q7
    reversed). Model API plan Task 6: described in prose, no format — the
    server's template teaches how a tool call is written."""
    import re
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), language="en")
    start = out.index("Concrete example")
    end = out.index("The `NoteMemory` call is what makes the note real.", start)
    example = out[start:end]
    assert '"Can you remember that I need to eat breakfast tomorrow?"' in example
    assert 'you say "Okay, noted. Breakfast tomorrow."' in example
    assert "call `NoteMemory`" in example
    flat = " ".join(example.split())
    assert '"My owner wants me to remember: breakfast tomorrow"' in flat
    assert not re.search(r"[\u3000-\u303f\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]", example)
    assert "{" not in example and "<" not in example
    assert "~" not in example


def test_scaffolding_keeps_saying_is_not_doing():
    """'Saying you did something is NOT doing it' survives the rewrite, with
    NoteMemory as the example (model API plan Task 6)."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity(), language="en")
    assert "saying you did something is NOT doing it" in out
    assert "**MUST also call `NoteMemory`**" in out
    assert '"I noted that" / "I\'ll remember that" / "got it, noted", you' in out


def test_scaffolding_speaks_of_your_owner_in_english():
    """English-speech plan Task 3: the scaffolding's own words are English
    ("主人" → "your owner")."""
    out = PromptRenderer().render("scaffolding", identity=_identity(), language="en",
                                  evolution_enabled=True)
    start = out.index("# Reflection")
    reflection = out[start:out.index("# Output", start)]
    flat = " ".join(reflection.split())
    assert "your relationship with your owner" in flat
    assert "a pattern you've noticed in your owner" in flat


def test_system_prompt_has_no_cjk_at_all():
    """All-English plan Task 2: the system prompt the kernel builds from the
    real gura pack carries no CJK at all — the example note is English now,
    so nothing in it shows her writing Chinese."""
    import re
    pack = DollPack.load(Path(__file__).resolve().parent.parent / "character_packs" / "gura")
    for evolution_enabled in (False, True):
        prefix, suffix = split_scaffolding(
            PromptRenderer(), identity=pack.identity, language=pack.meta.language, available_skills=["x"],
            evolution_enabled=evolution_enabled)
        found = re.findall(r"[\u3000-\u303f\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]+",
                           prefix + suffix)
        assert not found, found


def test_subagent_scaffolding_teaches_no_wire_format():
    """Model API plan Task 6: the worker prompt drops # Think structure and
    the `<think>` / `<tool_call>` lines, keeps the Report rule, and says her
    text reaches no one — acting means calling tools, ending means Report."""
    out = PromptRenderer().render("subagent_scaffolding")
    for marker in ("# Think structure", "<think>", "</think>", "<tool_call>",
                   "SEEN", "INTENT:", "REVIEW", "naked-text"):
        assert marker not in out, f"old format marker still taught: {marker!r}"
    assert "you **must** call the `Report` tool to end" in out
    assert 'Report(status="incomplete"' in out
    assert "The text of your replies is not shown to anyone" in out
    assert "To do anything, call a tool" in out
