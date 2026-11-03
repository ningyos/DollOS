"""Tool schemas. Which side a tool is on is fixed here, never decided at run time.

Internal: reads/writes only DollOS's own data; runs inside the turn.
External: touches anything outside DollOS; always becomes a job, and the turn ends.
"""

from __future__ import annotations


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


_S = {"type": "string"}

INTERNAL = [
    _fn("Recall", "Search your memory notes. Use keywords of 3+ characters. Returns up to 5 notes.",
        {"query": _S}, ["query"]),
    _fn("NoteMemory", "Save one durable fact worth remembering long-term. Not for small talk.",
        {"text": _S}, ["text"]),
    _fn("PinSelf", "Record a correction or standing request from your owner (how to address them, "
        "what they like, how you should behave). Pinned items are always in front of you.",
        {"text": _S}, ["text"]),
    _fn("OpenLine", "Open a work line: something you will keep pushing over time (a project, a paper, "
        "a search). Fails when too many lines are active.",
        {"title": _S, "next_step": _S}, ["title", "next_step"]),
    _fn("UpdateLine", "Update a work line after you moved it forward. status: active, paused or done.",
        {"line_id": _S, "status": {"type": "string", "enum": ["active", "paused", "done"]},
         "next_step": _S, "summary": _S}, ["line_id", "status", "next_step", "summary"]),
    _fn("CancelJob", "Cancel one of your running jobs.", {"job_id": _S}, ["job_id"]),
]

EXTERNAL = [
    _fn("Shell", "Run a shell command on the owner's Mac (cwd: your workspace). It runs as a "
        "background job: this turn ends right after you call it, and the output arrives later as a "
        "new event. Put line_id when the command belongs to a work line.",
        {"command": _S, "line_id": _S}, ["command"]),
]

INTERNAL_NAMES = {t["function"]["name"] for t in INTERNAL}
EXTERNAL_NAMES = {t["function"]["name"] for t in EXTERNAL}
OWNER_TOOLS = sorted(INTERNAL + EXTERNAL, key=lambda t: t["function"]["name"])
