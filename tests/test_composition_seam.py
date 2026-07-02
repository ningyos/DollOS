"""Three-piece system-prompt composition seam (spec §3.1)."""
from dollos.character import Identity
from dollos.kernel import _CURRENT_SELF_SEAM, split_scaffolding
from dollos.prompts import PromptRenderer


def _identity():
    return Identity(self="You are Gura.", personality="- chill", taboos="- no LARP")


def test_split_reconstructs_today_when_no_section():
    r = PromptRenderer()
    prefix, suffix = split_scaffolding(
        r, identity=_identity(), available_skills=[], tool_registry={})
    baseline = r.render("scaffolding", identity=_identity(), available_skills=[], tool_registry={})
    # Empty section ⇒ prefix + suffix is byte-identical to today's render.
    assert prefix + suffix == baseline
    assert _CURRENT_SELF_SEAM not in (prefix + suffix)


def test_split_omits_selfrevision_hint_when_evolution_flag_absent():
    """Byte-neutrality holds only when evolution_enabled is absent/False —
    old (pre-evolution) callers of split_scaffolding must see no SelfRevision
    text leak into the suffix."""
    r = PromptRenderer()
    prefix, suffix = split_scaffolding(
        r, identity=_identity(), available_skills=[], tool_registry={})
    assert "SelfRevision" not in (prefix + suffix)


def test_split_includes_selfrevision_hint_when_evolution_enabled():
    r = PromptRenderer()
    prefix, suffix = split_scaffolding(
        r, identity=_identity(), available_skills=[], tool_registry={},
        evolution_enabled=True)
    assert "SelfRevision" in suffix
    assert "PinSelf" in suffix


def test_split_seam_between_taboos_and_behavior():
    r = PromptRenderer()
    prefix, suffix = split_scaffolding(
        r, identity=_identity(), available_skills=[], tool_registry={})
    assert "no LARP" in prefix
    assert "# Behavior" in suffix


def test_mindloop_compose_renders_section_when_sanctioned(tmp_path):
    from dollos.mind import self_history
    self_history.log_event(tmp_path / "self_history.jsonl", kind="evo_adopt",
                           text="我現在監控數字時會主動來勁。", old_text=None, drift_score=None)
    from tests._mindloop_factory import make_mindloop  # see note below
    ml = make_mindloop(memory_root=tmp_path, system_prompt="PFX\n",
                       system_prompt_suffix="\n# Behavior\n")
    out = ml._system_prompt_for_turn()
    assert "## 現在的我" in out and "監控數字" in out


def test_mindloop_compose_omits_section_when_no_sanctioned(tmp_path):
    from tests._mindloop_factory import make_mindloop
    ml = make_mindloop(memory_root=tmp_path, system_prompt="PFX\n",
                       system_prompt_suffix="\n# Behavior\n")
    assert ml._system_prompt_for_turn() == "PFX\n\n# Behavior\n"


def test_mindloop_compose_cache_keyed_on_sanctioned(tmp_path):
    from dollos.mind import self_history
    from tests._mindloop_factory import make_mindloop
    ml = make_mindloop(memory_root=tmp_path, system_prompt="PFX\n",
                       system_prompt_suffix="\n# Behavior\n")
    first = ml._system_prompt_for_turn()
    second = ml._system_prompt_for_turn()
    assert first is second  # cached object identity (no recompose)
    self_history.log_event(tmp_path / "self_history.jsonl", kind="evo_adopt",
                           text="換了一版的我。", old_text=None, drift_score=None)
    third = ml._system_prompt_for_turn()
    assert third is not first and "換了一版" in third
