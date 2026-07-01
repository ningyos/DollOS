from pathlib import Path
from dollos.mind import self_profile as sp


def _p(tmp_path) -> Path:
    return tmp_path / "self_profile.md"


def test_add_scaffolds_file_and_assigns_id(tmp_path):
    p = _p(tmp_path)
    msg = sp.apply(p, section="self", op="add", target="",
                   text="我比表面更在意休息", max_chars=1200, today="2026-06-30")
    body = p.read_text()
    assert "## 我學到的自己" in body
    assert "## 我和主人" in body
    assert "## 我注意到的主人" in body
    assert "- [s1·2026-06-30] 我比表面更在意休息" in body
    assert "s1" in msg


def test_add_increments_id_per_section(tmp_path):
    p = _p(tmp_path)
    sp.apply(p, section="self", op="add", target="", text="a", max_chars=1200, today="2026-06-30")
    sp.apply(p, section="self", op="add", target="", text="b", max_chars=1200, today="2026-06-30")
    sp.apply(p, section="user", op="add", target="", text="c", max_chars=1200, today="2026-06-30")
    body = p.read_text()
    assert "- [s1·2026-06-30] a" in body
    assert "- [s2·2026-06-30] b" in body
    assert "- [u1·2026-06-30] c" in body


def test_replace_keeps_id_updates_text_and_date(tmp_path):
    p = _p(tmp_path)
    sp.apply(p, section="self", op="add", target="", text="舊", max_chars=1200, today="2026-06-01")
    sp.apply(p, section="self", op="replace", target="s1", text="新", max_chars=1200, today="2026-06-30")
    body = p.read_text()
    assert "- [s1·2026-06-30] 新" in body
    assert "舊" not in body


def test_remove_drops_bullet(tmp_path):
    p = _p(tmp_path)
    sp.apply(p, section="self", op="add", target="", text="x", max_chars=1200, today="2026-06-30")
    sp.apply(p, section="self", op="remove", target="s1", text="", max_chars=1200, today="2026-06-30")
    assert "s1" not in p.read_text()


def test_id_reused_after_remove(tmp_path):
    p = _p(tmp_path)
    sp.apply(p, section="self", op="add", target="", text="a", max_chars=1200, today="2026-06-30")
    sp.apply(p, section="self", op="remove", target="s1", text="", max_chars=1200, today="2026-06-30")
    sp.apply(p, section="self", op="add", target="", text="b", max_chars=1200, today="2026-06-30")
    body = p.read_text()
    assert "- [s1·2026-06-30] b" in body
    assert "s2" not in body


def test_id_reused_when_top_id_freed(tmp_path):
    p = _p(tmp_path)
    sp.apply(p, section="self", op="add", target="", text="a", max_chars=1200, today="2026-06-30")
    sp.apply(p, section="self", op="add", target="", text="b", max_chars=1200, today="2026-06-30")
    sp.apply(p, section="self", op="remove", target="s2", text="", max_chars=1200, today="2026-06-30")
    sp.apply(p, section="self", op="add", target="", text="c", max_chars=1200, today="2026-06-30")
    body = p.read_text()
    assert "- [s1·2026-06-30] a" in body
    assert "- [s2·2026-06-30] c" in body


def test_locate_miss_returns_friendly_error_no_write(tmp_path):
    p = _p(tmp_path)
    sp.apply(p, section="self", op="add", target="", text="a", max_chars=1200, today="2026-06-30")
    before = p.read_text()
    msg = sp.apply(p, section="self", op="remove", target="s9", text="", max_chars=1200, today="2026-06-30")
    assert "s9" in msg and ("找不到" in msg or "沒有" in msg)
    assert p.read_text() == before  # 未寫入


def test_cap_rejects_add_over_limit_no_write(tmp_path):
    p = _p(tmp_path)
    long = "字" * 50
    # 先塞到接近上限(3 條共 236 字 <= 250)
    for _ in range(3):
        sp.apply(p, section="self", op="add", target="", text=long, max_chars=250, today="2026-06-30")
    before = p.read_text()
    msg = sp.apply(p, section="self", op="add", target="", text=long, max_chars=250, today="2026-06-30")
    assert "上限" in msg
    assert p.read_text() == before  # 被拒、未寫入


def test_cap_also_guards_replace(tmp_path):
    p = _p(tmp_path)
    sp.apply(p, section="self", op="add", target="", text="短", max_chars=120, today="2026-06-30")
    before = p.read_text()
    msg = sp.apply(p, section="self", op="replace", target="s1", text="長" * 200,
                   max_chars=120, today="2026-06-30")
    assert "上限" in msg
    assert p.read_text() == before


def test_render_block_none_when_no_bullets(tmp_path):
    p = _p(tmp_path)
    assert sp.render_block(p) is None            # 檔不存在
    sp.apply(p, section="self", op="add", target="", text="a", max_chars=1200, today="2026-06-30")
    sp.apply(p, section="self", op="remove", target="s1", text="", max_chars=1200, today="2026-06-30")
    assert sp.render_block(p) is None            # 只剩空標題


def test_render_block_skips_empty_sections(tmp_path):
    p = _p(tmp_path)
    sp.apply(p, section="user", op="add", target="", text="主人常忘記吃午餐", max_chars=1200, today="2026-06-30")
    block = sp.render_block(p)
    assert block is not None
    assert "## 我注意到的主人" in block
    assert "- [u1·2026-06-30] 主人常忘記吃午餐" in block
    assert "## 我學到的自己" not in block  # 空段不渲染


def test_unknown_section_add_friendly_error(tmp_path):
    p = _p(tmp_path)
    msg = sp.apply(p, section="bogus", op="add", target="", text="x",
                   max_chars=1200, today="2026-06-30")
    assert "bogus" in msg
    assert not p.exists()  # 未寫入


def test_render_block_no_bookkeeping_artifacts(tmp_path):
    p = _p(tmp_path)
    sp.apply(p, section="self", op="add", target="", text="a", max_chars=1200, today="2026-06-30")
    sp.apply(p, section="self", op="add", target="", text="b", max_chars=1200, today="2026-06-30")
    block = sp.render_block(p)
    assert block is not None
    assert "- [s1·2026-06-30] a" in block
    assert "- [s2·2026-06-30] b" in block
    assert "<!--" not in block
    assert "counters" not in block
