"""Test MindCtx dataclass structure."""


def test_mind_ctx_has_self_profile_max_chars():
    """MindCtx should have self_profile_max_chars field."""
    from dollos.mind.mind_ctx import MindCtx

    fields = MindCtx.__dataclass_fields__
    assert "self_profile_max_chars" in fields
