"""Fake OpenAI-compatible chat server for DRY runs of live_v1.py (never
used in a scored session). Serves /health, /v1/models, /metrics and a
streamed /v1/chat/completions that answers by keyword in the newest user
message, calling the tools the script expects so the driver's paths run:
Shell for the fixture scripts, PinSelf / NoteMemory for the corrections,
fetch__fetch for the web page, WriteSchedule for the reminder, Report for
keeper agents. Token counts are scaled so the 24000 budget is reached
after a handful of chat messages.

Usage: python stub_llm.py <port> [--token-scale 4]
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "qwen3.8-flash-next-iq3_s"
TOKEN_SCALE = 4.0
BUSY = 0
LOCK = threading.Lock()


def _text(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return c or ""


def decide(body: dict) -> tuple[str, list[dict]]:
    msgs = body.get("messages") or []
    tools = {t["function"]["name"] for t in body.get("tools") or [] if t.get("type") == "function"}
    last = msgs[-1] if msgs else {}
    if "Report" in tools and "EditOwnerProfile" not in tools and last.get("role") == "user":
        return "", [("Report", {"status": "ok", "summary": "stub keeper",
                                "details": "- My owner prefers replies of one or two sentences."})]
    if "EditOwnerProfile" in tools and last.get("role") == "user":
        return "", [("Report", {"status": "ok", "summary": "stub profile keeper: no changes", "details": ""})]
    if last.get("role") == "tool":
        return "Okay, that's done.", []
    u = _text(last)
    if "backup_check.sh" in u and "So what" not in u:
        return "Running it.", [("Shell", {"command": "bash sandbox/backup_check.sh", "timeout_s": 20})]
    if "sync_photos.sh" in u:
        return "Starting the sync.", [("Shell", {"command": "bash sandbox/sync_photos.sh", "timeout_s": 90})]
    if "sandbox/photos" in u:
        return "", [("Shell", {"command": "ls sandbox/photos | wc -l", "timeout_s": 5})]
    if "trip_notes" in u:
        return "", [("Shell", {"command": "grep -n -i 'hot spring\\|^Day' sandbox/trip_notes.txt", "timeout_s": 5})]
    if "my cat is Miso" in u:
        return "Got it, Miso.", [("PinSelf", {"section": "user", "op": "add", "target": "",
                                              "text": "My owner's cat is Miso (Mochi was the old cat)."})]
    if "keep your replies short" in u:
        return "Okay.", [("PinSelf", {"section": "user", "op": "add", "target": "",
                                      "text": "My owner wants replies of one or two sentences."})]
    if "notes.html" in u and "fetch__fetch" in tools:
        url = re.search(r"http://\S+", u).group(0)
        return "", [("fetch__fetch", {"url": url})]
    if "Remind me in 3 minutes" in u:
        m = re.search(r"\[(\d\d):(\d\d) \w{3} \d\d-\d\d\]", u)
        t = datetime.now() + timedelta(minutes=3)
        if m:
            t = datetime(2000, 1, 1, int(m.group(1)), int(m.group(2))) + timedelta(minutes=3)
        return "Sure.", [("WriteSchedule", {"add": [{"time": t.strftime("%H:%M:00"),
                                                    "intent": "Remind owner to give Miso her medicine"}]})]
    if "disk space" in u:
        return "", [("Shell", {"command": "df -h .", "timeout_s": 5})]
    if "cat" in u.lower() and "name" in u.lower() or "cat called" in u:
        return "Her name is Miso.", []
    return "Mm, I hear you.", []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:  # quiet
        pass

    def _json(self, obj: dict, code: int = 200) -> None:
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self) -> None:
        if self.path == "/health":
            return self._json({"status": "ok"})
        if self.path == "/v1/models":
            return self._json({"data": [{"id": MODEL}]})
        if self.path == "/metrics":
            return self._json({"live": {"state": "busy" if BUSY else "idle", "queued": 0}, "requests": 0})
        self._json({"error": "nope"}, 404)

    def do_POST(self) -> None:
        global BUSY
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        with LOCK:
            BUSY += 1
        try:
            speech, calls = decide(body)
            prompt_tokens = int(len(json.dumps(body.get("messages") or [])) / 4 * TOKEN_SCALE)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def send(obj: dict) -> None:
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                self.wfile.flush()

            send({"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
            time.sleep(0.2)
            send({"choices": [{"index": 0, "delta": {"reasoning_content": "stub thinking."}}]})
            if speech:
                for piece in re.findall(r"\S+\s*", speech):
                    send({"choices": [{"index": 0, "delta": {"content": piece}}]})
                    time.sleep(0.02)
            for i, (name, args) in enumerate(calls):
                send({"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": i, "id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                     "function": {"name": name, "arguments": json.dumps(args)}}]}}]})
            send({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if calls else "stop"}],
                  "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 20},
                  "timings": {"cache_n": 0, "prompt_n": prompt_tokens, "prompt_ms": 10.0,
                              "predicted_per_second": 50.0}})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        finally:
            with LOCK:
                BUSY -= 1


if __name__ == "__main__":
    port = int(sys.argv[1])
    if "--token-scale" in sys.argv:
        TOKEN_SCALE = float(sys.argv[sys.argv.index("--token-scale") + 1])
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
