"""Telemetry — persistent records of Doll's LLM consumption.

Used by ``perception.cognition`` to surface mind-state vitals.
"""

from dollos.telemetry.llm_calls import LLMCallRecord, TelemetryRecorder

__all__ = ["LLMCallRecord", "TelemetryRecorder"]
