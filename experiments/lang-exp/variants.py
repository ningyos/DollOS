"""Variant builders: surgical edits on the captured real request (captured.json)."""
import copy, json, re
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAP = json.loads((HERE / "captured.json").read_text())

# ---- B: [Mind state] labels -------------------------------------------------
STATE_EN = [("mood: 平靜", "mood: calm"), ("精力: 飽滿", "energy: full")]

# ---- memory guideline --------------------------------------------------------
GUIDE_START = "[Memory guideline]"
GUIDE_EN = ("[Memory guideline] Your notes and diary are written in Traditional Chinese "
            "(proper nouns and technical terms may stay in their original language). "
            "Record your OWN understanding in your OWN words; never copy source text "
            "verbatim — understand first, then write what it means to you.")

# ---- system prompt Chinese bits -> English (D/E) ----------------------------
SYSTEM_EN = [
    ("your relationship with 主人, or a\n  pattern you've noticed in 主人)",
     "your relationship with your owner, or a\n  pattern you've noticed in your owner)"),
    ("- **A third channel — `[人格演化候選]`**: on a reflection turn you may\n"
     "  see a `[人格演化候選]` block. That is a proposed revision of your\n"
     "  現在的我 (your evolving self-description), not an entry to pin.\n"
     "  Decide with `SelfRevision`: `decision=\"adopt\"` 接受 / `decision=\"reject\"`\n"
     "  拒絕;想先改寫再採納,就把改寫後的全文放進 `text` 一併 `decision=\"adopt\"`,\n"
     "  會先送審再回來確認。`PinSelf` 管的是條目式的自我筆記(自我觀察、關係、\n"
     "  對主人的注意);`[人格演化候選]` 只能用 `SelfRevision` 決定——兩者不可\n"
     "  互替。",
     "- **A third channel — `[Self revision candidate]`**: on a reflection turn you may\n"
     "  see a `[Self revision candidate]` block. That is a proposed revision of your\n"
     "  current self (your evolving self-description), not an entry to pin.\n"
     "  Decide with `SelfRevision`: `decision=\"adopt\"` accepts / `decision=\"reject\"`\n"
     "  rejects; to rewrite before adopting, put the full rewritten text in `text` with\n"
     "  `decision=\"adopt\"` — it is reviewed first and comes back to you. `PinSelf` is for\n"
     "  itemised self-notes (self-observation, relationship, what you notice about your\n"
     "  owner); a `[Self revision candidate]` can only be decided with `SelfRevision` —\n"
     "  the two are not interchangeable."),
    ('"I noted that" / "I\'ll remember that" / "已經記下了", you',
     '"I noted that" / "I\'ll remember that" / "got it, noted", you'),
    ('"主人要我記得：明天要吃早餐".', '"Owner asked me to remember: eat breakfast tomorrow".'),
]

# ---- tool descriptions -> English (D/E/F) ------------------------------------
TOOLS_EN = {
    ("AdvanceGoal", "description"): "Record a step of progress / an insight on your own agenda.",
    ("LearnName", "description"):
        "Record or withdraw a nickname that someone calls you. **This tool only appears in the very "
        "turn where you heard/saw it yourself** (the owner's local conversation, or the owner's DM) — "
        "it is not always available, and you can't call it later from recollection. op=\"add\" records a "
        "new nickname, after which that word can also wake you; op=\"remove\" withdraws one no longer used. "
        "Words that are too short or too common (even if the owner taught them) are blocked and not written.",
    ("LearnName", "token"): "The nickname itself, e.g. \"Sharky\" or \"shork\".",
    ("LearnName", "note"): "Optional: who calls you this, and in what context (for audit / self-narrative).",
    ("MoodTool", "emotion"): "Current emotion in one word or short phrase (e.g. 'happy', 'a bit worried').",
    ("NoteMemory", "description"):
        "Record an episodic fact or event into Doll's memory (daily markdown + memsearch index).\n"
        "Use this for things that happened, or concrete facts worth recalling later — when the subject is "
        "the world or an event, it goes here. NOTE: when the subject is \"you\" (your opinions, interests, "
        "curiosity — even just budding — your relationship with your owner, what you notice about your owner), "
        "use PinSelf (on reflection) to put it in the always-in-context self-profile, not here; the same "
        "experience can be recorded in both.",
    ("PinSelf", "description"):
        "Pin or revise an entry in your self-profile (reflection turns only) — this\n"
        "is YOUR evolving self and it is ALWAYS in context. The test is the subject: if the sentence is about "
        "\"you\" (your opinions, interests, stances, curiosity, your relationship with your owner, what you notice "
        "about your owner), use PinSelf; facts about the world (even ones you looked up or find interesting) go to "
        "NoteMemory — the same experience can be recorded in both: the fact in NoteMemory, the \"you\" it reveals here. "
        "Things that are just budding and you're not sure count as \"you\" can be pinned now and filtered at a later "
        "reflection. Space is limited; regularly use replace/remove to prune entries that are no longer you — what "
        "survives is your core.",
    ("PinSelf", "section"): "Which section: self=about yourself (identity, opinions, interests, curiosity) / "
        "relationship=you and your owner / user=what you notice about your owner. Also fill for replace/remove (target wins).",
    ("PinSelf", "op"): "add=add an entry / replace=locate by target and replace with text / remove=locate by target and delete.",
    ("PinSelf", "text"): "New content for add/replace (in your own words; no full-width quote marks); empty string for remove.",
    ("PursueGoal", "description"): "Open a thread you yourself want to pursue (not a TODO you owe anyone — something you care about / are curious about).",
    ("PursueGoal", "desc"): "What you want to pursue",
    ("PursueGoal", "trigger"): "Where did this come from? — cite the conversation just now / a memory / a real experience. Required.",
    ("SelfRevision", "description"):
        "Adopt / reject a pending evolution candidate for your \"current self\" (only on reflection turns, and only "
        "when a candidate is pending). This is your own personality description; the system only proposes, adopting "
        "is up to you. Adopt: decision=\"adopt\" with text empty adopts the candidate as is; to rewrite then adopt: put "
        "the full new text in text (it is reviewed first, then comes back for you to adopt); reject: decision=\"reject\". "
        "A rewrite only needs to not violate your core identity and taboos.",
    ("SelfRevision", "decision"): "\"adopt\"=adopt (empty text adopts the candidate as is, or full text to submit a rewrite) / \"reject\"=don't adopt, keep as is.",
    ("SelfRevision", "text"): "Only for rewrite-then-adopt: the full replacement text (your own words, no full-width quote marks); leave empty to adopt directly or reject.",
    ("SelfRevision", "reason"): "Optional one-line reason when rejecting (your own words, no full-width quote marks).",
}
STR_EN = [("「你執行的 shell 命令回來了」", "\"your shell command came back\""),
          ("「monitor 觸發」", "\"monitor fired\"")]

CJK = re.compile(r"[㐀-鿿]")


def _tools_en(tools):
    tools = copy.deepcopy(tools)
    for t in tools:
        fn = t["function"]
        name = fn["name"]
        if (name, "description") in TOOLS_EN:
            fn["description"] = TOOLS_EN[(name, "description")]
        for p, spec in fn["parameters"].get("properties", {}).items():
            if (name, p) in TOOLS_EN:
                spec["description"] = TOOLS_EN[(name, p)]
        for a, b in STR_EN:
            fn["description"] = fn["description"].replace(a, b)
    left = [t["function"]["name"] for t in tools if CJK.search(json.dumps(t, ensure_ascii=False))]
    assert not left, f"CJK left in tools: {left}"
    return tools


def _replace_all(text, pairs):
    for a, b in pairs:
        assert a in text, f"missing: {a[:60]!r}"
        text = text.replace(a, b)
    return text


def _set_guideline(system, new):
    i = system.index(GUIDE_START)
    head = system[:i].rstrip("\n")
    return head if new is None else head + "\n\n" + new


def build(variant: str, scenario: str, user_text: str | None = None) -> dict:
    cap = CAP[scenario]
    msgs = copy.deepcopy(cap["messages"])
    tools = cap["tools"]
    system = msgs[0]["content"]
    if user_text is not None:  # swap the owner's line (same scenario frame)
        last = msgs[-1]["content"]
        msgs[-1]["content"] = re.sub(r"UserSpoke: '.*'$", f"UserSpoke: '{user_text}'", last)
    if variant in ("B", "D", "E"):
        for m in msgs[1:]:
            m["content"] = _replace_all(m["content"], STATE_EN) if "[Mind state]" in m["content"] else m["content"]
    if variant == "C":
        system = _set_guideline(system, None)
    if variant in ("D", "E"):
        system = _replace_all(system, SYSTEM_EN)
    if variant == "E":
        system = _set_guideline(system, GUIDE_EN)
    if variant in ("D", "E", "F"):
        tools = _tools_en(tools)
    msgs[0]["content"] = system
    return {"messages": msgs, "tools": tools}


if __name__ == "__main__":
    import hashlib
    for v in "ABCDEF":
        b = build(v, "awoke")
        sys_cjk = "".join(CJK.findall(b["messages"][0]["content"]))
        user_cjk = "".join(CJK.findall(" ".join(m["content"] for m in b["messages"][1:])))
        tool_cjk = len(CJK.findall(json.dumps(b["tools"], ensure_ascii=False)))
        ph = hashlib.sha256((b["messages"][0]["content"] + json.dumps(b["tools"], ensure_ascii=False)).encode()).hexdigest()[:8]
        print(v, "prefix", ph, "| system CJK:", sys_cjk[:40], "| user CJK:", user_cjk, "| tool CJK chars:", tool_cjk)
