"""PromptTemplate — model-family-specific prompt rendering."""

from abc import ABC, abstractmethod


class PromptTemplate(ABC):
    """Render a (system, user, prefill) tuple into the single prompt string
    the model expects.

    For "server-applied" templates (e.g. Anthropic / OpenAI chat completions
    where the API takes messages instead of a raw prompt), a concrete
    PromptTemplate may be a no-op stub and the corresponding Provider would
    talk in messages directly. Plan 3 v1 doesn't ship such a Provider, but
    the interface allows it.
    """

    @abstractmethod
    def render(
        self,
        *,
        system: str,
        user: str,
        prefill: str,
    ) -> str:
        ...


class Qwen3ThinkingTemplate(PromptTemplate):
    """Qwen3.x thinking-model ChatML.

    Opens the <think> block inside the assistant turn so prefill content
    goes inside the thinking block. This matches the Plan 1 review decision
    to optimize for Qwen3.6-thinking models (see grammar_injection_techreport
    §2.3 for the prefill technique).
    """

    def render(self, *, system: str, user: str, prefill: str) -> str:
        parts = [
            "<|im_start|>system",
            system,
            "<|im_end|>",
            "<|im_start|>user",
            user,
            "<|im_end|>",
            "<|im_start|>assistant",
            "<think>",
            "",
        ]
        rendered = "\n".join(parts)
        if prefill:
            rendered += prefill
        return rendered


class Qwen3PlainTemplate(PromptTemplate):
    """Qwen3.x ChatML with thinking immediately closed.

    Inner Voice's small models may be either non-thinking Instruct OR
    thinking-trained variants. We emit an empty closed <think></think>
    block before the prefill so the model skips thinking and goes
    straight to producing the answer.

    Works on both: non-thinking Instruct models treat <think> as a
    known no-op token; thinking-trained models see closed empty block
    and skip the thinking phase. This mirrors llama-server's
    `--chat-template-kwargs '{"enable_thinking": false}'` but works
    through the raw /completion prompt path.
    """

    def render(self, *, system: str, user: str, prefill: str) -> str:
        parts = [
            "<|im_start|>system",
            system,
            "<|im_end|>",
            "<|im_start|>user",
            user,
            "<|im_end|>",
            "<|im_start|>assistant",
            "<think>",
            "",
            "</think>",
            "",
            "",
        ]
        rendered = "\n".join(parts)
        if prefill:
            rendered += prefill
        return rendered
