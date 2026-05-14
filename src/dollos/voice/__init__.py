"""Voice subsystem — ASR + TTS engine plugins, character pack voice config.

Importing this package triggers the concrete engine modules so their
@register_asr / @register_tts decorators populate the registries before
anyone tries to look up engines by name.
"""

from dollos.voice import asr_sherpa  # noqa: F401 — register_asr side effect
from dollos.voice import tts_luxtts  # noqa: F401 — register_tts side effect
from dollos.voice import tts_fish  # noqa: F401 — register_tts side effect
from dollos.voice import tts_piper  # noqa: F401 — register_tts side effect
from dollos.voice import tts_openai  # noqa: F401 — register_tts side effect
