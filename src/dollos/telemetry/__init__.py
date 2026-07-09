"""Telemetry — persistent records of Doll's LLM consumption.

Used by ``perception.cognition`` to surface mind-state vitals.
"""

from dollos.telemetry.llm_calls import LLMCallRecord, TelemetryRecorder
from dollos.telemetry.turn_latency import TurnLatencyRecord, TurnLatencyRecorder

__all__ = [
    "LLMCallRecord",
    "TelemetryRecorder",
    "TurnLatencyRecord",
    "TurnLatencyRecorder",
]
