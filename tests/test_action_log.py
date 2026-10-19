from dollos.mind.action_log import (
    action_phrase_for_tool, action_phrase_for_perception, redact_secrets,
)
from tests._cjk import cjk_runs


def test_redact_secrets_strips_common_secret_values():
    assert redact_secrets("export TOKEN=abc123") == "export TOKEN=***"
    assert "xyz" not in redact_secrets('curl -H "Authorization: Bearer xyz"')
    assert redact_secrets("ls -la") == "ls -la"


def test_tool_phrase_whitelist_and_summaries():
    assert action_phrase_for_tool("Shell", {"command": "ls -la"}, "") == "ran a command: ls -la"
    assert action_phrase_for_tool(
        "Shell", {"command": "export TOKEN=secret && run"}, ""
    ) == "ran a command: export TOKEN=*** && run"
    assert action_phrase_for_tool(
        "PursueGoal", {"id": "x", "desc": "學攝影", "trigger": "t"}, ""
    ) == 'took on a new goal: "學攝影"'
    assert action_phrase_for_tool(
        "AdvanceGoal", {"id": "photo", "progress": "看了構圖教學"}, ""
    ) == 'advanced goal "photo": 看了構圖教學'
    assert action_phrase_for_tool(
        "CloseLoop", {"id": "photo", "outcome": "學會了"}, ""
    ) == 'closed "photo": 學會了'
    assert action_phrase_for_tool(
        "NoteMemory", {"text": "主人喜歡冰美式" * 5}, ""
    ).startswith("noted: 主人喜歡冰美式")
    assert len(action_phrase_for_tool("NoteMemory", {"text": "x" * 200}, "")) < 60
    assert action_phrase_for_tool(
        "LearnName", {"op": "add", "token": "Sharky"}, ""
    ) == 'someone started calling me "Sharky"'
    assert action_phrase_for_tool("LearnName", {"op": "remove", "token": "Sharky"}, "") is None
    assert action_phrase_for_tool(
        "WriteSchedule", {"entries": [{}, {}, {}, {}]}, ""
    ) == "scheduled 4 things"
    assert action_phrase_for_tool(
        "WriteSchedule", {"entries": [{}]}, ""
    ) == "scheduled 1 thing"
    assert action_phrase_for_tool(
        "SpawnWorkflow", {"tasks": [{}, {}]}, ""
    ) == "dispatched a workflow (2 tasks)"
    assert action_phrase_for_tool(
        "SpawnWorkflow", {"tasks": [{}]}, ""
    ) == "dispatched a workflow (1 task)"
    assert action_phrase_for_tool(
        "SpawnMonitor", {"command": "tail -F log"}, ""
    ) == "set up a monitor: tail -F log"
    assert action_phrase_for_tool(
        "RemoveMonitor", {"monitor_id": "mon-1"}, ""
    ) == "removed monitor mon-1"
    assert action_phrase_for_tool(
        "SelfRevision", {"decision": "adopt"}, ""
    ) == "adopted a revision of who I am now"
    assert action_phrase_for_tool("SelfRevision", {"decision": "reject"}, "") is None
    assert action_phrase_for_tool(
        "PinSelf", {"op": "add", "section": "self"}, ""
    ) == "updated my self-profile (add self)"


def test_tool_phrase_mood_only_on_change():
    # emotion changed → logged
    assert action_phrase_for_tool(
        "MoodTool", {"emotion": "happy", "reason": "聊得好"}, "calm"
    ) == 'mood changed to "happy": 聊得好'
    assert action_phrase_for_tool(
        "MoodTool", {"emotion": "happy"}, "calm"
    ) == 'mood changed to "happy"'
    # emotion unchanged → not logged
    assert action_phrase_for_tool("MoodTool", {"emotion": "calm"}, "calm") is None


def test_tool_phrase_skips_non_whitelisted():
    assert action_phrase_for_tool("Recall", {"query": "x"}, "") is None
    assert action_phrase_for_tool("WriteDiary", {"content": "..."}, "") is None
    assert action_phrase_for_tool("Report", {}, "") is None


def test_perception_phrase_world_events():
    assert action_phrase_for_perception(
        "ToolResultArrived", {"tool": "Shell", "task_id": "sh-1", "status": "ok", "summary": "done"}
    ) == 'Shell "sh-1" finished [ok]: done'
    assert action_phrase_for_perception(
        "MonitorFired", {"monitor_id": "mon-2", "line": "OOM killed"}
    ) == "Monitor mon-2 fired: OOM killed"
    assert action_phrase_for_perception(
        "MonitorEnded", {"monitor_id": "mon-2", "exit_status": 0}
    ) == "Monitor mon-2 ended (exit 0)"
    assert action_phrase_for_perception(
        "BridgeDown", {"service": "discord-bridge", "rc": 1}
    ) == "discord-bridge went down (rc=1)"
    assert action_phrase_for_perception(
        "McpDown", {"service": "mcp-server", "rc": 2}
    ) == "mcp-server went down (rc=2)"
    assert action_phrase_for_perception("UserSpoke", {"text": "hi"}) is None
    assert action_phrase_for_perception("AgendaMoment", {}) is None


def test_pulse_moment_action_phrase():
    phrase = action_phrase_for_perception(
        "PulseMoment",
        {"concern": "battery_critical", "detail": "battery down to 12% and discharging", "severity": "critical"},
    )
    assert phrase == "body signal: battery down to 12% and discharging"


def test_non_pulse_kind_still_none():
    # a kind action_phrase_for_perception doesn't handle → still None (unchanged)
    assert action_phrase_for_perception("ReflectionMoment", {}) is None


def test_phrases_carry_no_daemon_chinese():
    """The daemon's own words in every phrase are English (plan 2026-10-19
    Task 8); only her content (here ASCII) passes through."""
    tools = [
        ("Shell", {"command": "ls"}), ("SpawnWorkflow", {"tasks": [{}]}),
        ("SpawnMonitor", {"command": "x"}), ("RemoveMonitor", {"monitor_id": "m"}),
        ("PursueGoal", {"desc": "d"}), ("AdvanceGoal", {"id": "g", "progress": "p"}),
        ("CloseLoop", {"id": "g", "outcome": "o"}), ("WriteSchedule", {"entries": []}),
        ("SelfRevision", {"decision": "adopt"}), ("PinSelf", {"op": "add", "section": "self"}),
        ("LearnName", {"op": "add", "token": "t"}), ("NoteMemory", {"text": "n"}),
        ("MoodTool", {"emotion": "happy", "reason": "r"}),
    ]
    events = [
        ("ToolResultArrived", {"tool": "Shell", "task_id": "s", "status": "ok", "summary": "x"}),
        ("MonitorFired", {"monitor_id": "m", "line": "x"}),
        ("MonitorEnded", {"monitor_id": "m", "exit_status": 0}),
        ("BridgeDown", {"service": "b", "rc": 1}), ("McpDown", {"service": "m", "rc": 1}),
        ("PulseMoment", {"detail": "x"}),
    ]
    phrases = [action_phrase_for_tool(n, a, "calm") for n, a in tools]
    phrases += [action_phrase_for_perception(k, d) for k, d in events]
    assert all(phrases)
    assert [p for p in phrases if cjk_runs(p)] == []
