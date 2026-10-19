"""Chat-completions is the only model path: the old ``/completion`` +
prompt template + GBNF grammar path is deleted, not kept as a fallback."""

import importlib
import pkgutil

import pytest

import dollos.kernel
import dollos.llm


def test_old_completion_path_is_gone():
    # The transport / template / grammar / composed modules are deleted;
    # the llm package holds only the chat-completions path.
    assert {m.name for m in pkgutil.iter_modules(dollos.llm.__path__)} == {
        "chat_completions",
        "model",
        "tool_schema",
    }
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("dollos.tool_parser")
    for attr in ("build_adapter", "_MindLLMAdapter"):
        assert not hasattr(dollos.kernel, attr)


def test_llm_package_exports_only_the_model_interface():
    assert set(dollos.llm.__all__) == {
        "ChatCompletionsAdapter",
        "End",
        "ModelAdapter",
        "ModelError",
        "ModelEvent",
        "SpeechDelta",
        "ThinkDelta",
        "ToolCall",
        "ToolCalls",
        "Usage",
        "tool_message",
        "tool_schemas",
    }
    for name in dollos.llm.__all__:
        assert getattr(dollos.llm, name) is not None
