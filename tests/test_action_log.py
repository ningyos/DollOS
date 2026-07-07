from dollos.mind.action_log import (
    action_phrase_for_tool, action_phrase_for_perception, redact_secrets,
)


def test_redact_secrets_strips_common_secret_values():
    assert redact_secrets("export TOKEN=abc123") == "export TOKEN=***"
    assert "xyz" not in redact_secrets('curl -H "Authorization: Bearer xyz"')
    assert redact_secrets("ls -la") == "ls -la"


def test_tool_phrase_whitelist_and_summaries():
    assert action_phrase_for_tool("Shell", {"command": "ls -la"}, "") == "我跑了指令 ls -la"
    assert action_phrase_for_tool(
        "Shell", {"command": "export TOKEN=secret && run"}, ""
    ) == "我跑了指令 export TOKEN=*** && run"
    assert action_phrase_for_tool(
        "PursueGoal", {"id": "x", "desc": "學攝影", "trigger": "t"}, ""
    ) == "我起了新目標:「學攝影」"
    assert action_phrase_for_tool(
        "AdvanceGoal", {"id": "photo", "progress": "看了構圖教學"}, ""
    ) == "我推進了目標「photo」:看了構圖教學"
    assert action_phrase_for_tool(
        "NoteMemory", {"text": "主人喜歡冰美式" * 5}, ""
    ).startswith("我記下了:主人喜歡冰美式")
    assert len(action_phrase_for_tool("NoteMemory", {"text": "x" * 200}, "")) < 60
    assert action_phrase_for_tool("LearnName", {"op": "add", "token": "小鯊"}, "") == "有人開始叫我「小鯊」"
    assert action_phrase_for_tool("LearnName", {"op": "remove", "token": "小鯊"}, "") is None


def test_tool_phrase_mood_only_on_change():
    # emotion changed → logged
    assert action_phrase_for_tool("MoodTool", {"emotion": "開心", "reason": "聊得好"}, "平靜") == "我心情變成「開心」:聊得好"
    # emotion unchanged → not logged
    assert action_phrase_for_tool("MoodTool", {"emotion": "平靜"}, "平靜") is None


def test_tool_phrase_skips_non_whitelisted():
    assert action_phrase_for_tool("Recall", {"query": "x"}, "") is None
    assert action_phrase_for_tool("WriteDiary", {"content": "..."}, "") is None
    assert action_phrase_for_tool("Report", {}, "") is None


def test_perception_phrase_world_events():
    assert action_phrase_for_perception(
        "ToolResultArrived", {"tool": "Shell", "task_id": "sh-1", "status": "ok", "summary": "done"}
    ) == "Shell「sh-1」跑完了[ok]:done"
    assert action_phrase_for_perception(
        "MonitorFired", {"monitor_id": "mon-2", "line": "OOM killed"}
    ) == "Monitor mon-2 觸發:OOM killed"
    assert action_phrase_for_perception(
        "MonitorEnded", {"monitor_id": "mon-2", "exit_status": 0}
    ) == "Monitor mon-2 結束(exit 0)"
    assert action_phrase_for_perception(
        "BridgeDown", {"service": "discord-bridge", "rc": 1}
    ) == "discord-bridge 掛了(rc=1)"
    assert action_phrase_for_perception(
        "McpDown", {"service": "mcp-server", "rc": 2}
    ) == "mcp-server 掛了(rc=2)"
    assert action_phrase_for_perception("UserSpoke", {"text": "hi"}) is None
    assert action_phrase_for_perception("AgendaMoment", {}) is None
