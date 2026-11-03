"""The core: one queue, one consumer, one timeline, work lines, external jobs, idle nudges."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import Config
from .llm import DecisionModel, MainModel, ModelError, check
from .store import Store
from .tools import EXTERNAL_NAMES, INTERNAL_NAMES, OWNER_TOOLS

log = logging.getLogger("dollcore")

OWNER = "owner"
INTERNAL = "internal"
STRANGER = "stranger"


@dataclass
class Event:
    kind: str                 # owner_said | dm | job_done | line_nudge
    principal: str            # owner | internal | stranger
    audience: str             # 'private' or 'dm:<who>'
    text: str
    line_id: str | None = None


Say = Callable[[str, str], None]   # (audience, sentence)


def _now() -> str:
    return datetime.now().strftime("%m-%d %H:%M")


class Doll:
    def __init__(self, cfg: Config, root: Path, say: Say) -> None:
        self.cfg = cfg
        self.core = cfg.core
        self.data = root / self.core.data_dir
        self.workspace = self.data / "workspace"
        self.job_out = self.data / "jobs"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.job_out.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data / "doll.db")
        self.main = MainModel(cfg.llm)
        self.decision = DecisionModel(cfg.decision)
        self.say = say
        self.queue: asyncio.Queue[Event] = asyncio.Queue()
        self.jobs: dict[str, asyncio.subprocess.Process] = {}
        self.busy = False
        self.last_activity = time.monotonic()
        self.next_nudge_at = self.last_activity + self.core.idle_first_s
        pack = tomllib.loads((root / self.core.pack).read_text(encoding="utf-8"))
        self.name = pack["meta"]["name"]
        self.identity = "\n\n".join(
            pack["identity"][k].strip() for k in ("self", "personality", "taboos")
        )

    # boot -------------------------------------------------------------------
    async def boot(self) -> None:
        await check(self.cfg.llm.base_url, self.cfg.llm.model)
        await check(self.cfg.decision.base_url, self.cfg.decision.model)
        for row in self.store.running_jobs():
            if row["pgid"]:
                try:
                    os.killpg(row["pgid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.store.finish_job(row["id"], "lost")
            self.store.append("private", {
                "role": "user",
                "content": f"[{_now()} system] job {row['id']} was lost when the daemon stopped; "
                           "it was not rerun.",
            }, row["line_id"])

    # inputs -----------------------------------------------------------------
    def put(self, ev: Event) -> None:
        self.queue.put_nowait(ev)

    def owner_said(self, text: str) -> None:
        self.put(Event("owner_said", OWNER, "private", text))

    def dm(self, who: str, text: str) -> None:
        self.put(Event("dm", STRANGER, f"dm:{who}", text))

    # consumer ---------------------------------------------------------------
    async def run(self) -> None:
        idle = asyncio.create_task(self._idle_loop())
        try:
            while True:
                ev = await self.queue.get()
                self.busy = True
                try:
                    await self._turn(ev)
                except ModelError as exc:
                    log.error("turn failed: %s", exc)
                    self.say(ev.audience, f"(系統：這一回合失敗了：{exc})")
                finally:
                    self.busy = False
                    self._touch()
        finally:
            idle.cancel()
            await self.shutdown()

    def _touch(self) -> None:
        self.last_activity = time.monotonic()
        self.next_nudge_at = self.last_activity + self.core.idle_first_s

    async def shutdown(self) -> None:
        for job_id, proc in list(self.jobs.items()):
            self._kill(proc)
            self.store.finish_job(job_id, "cancelled")

    # one turn ---------------------------------------------------------------
    def _system(self, principal: str) -> str:
        parts = [
            f"你是{self.name}。\n\n{self.identity}",
            "你同時手上可以有好幾件事在進行（work lines）。一次只想一件事。"
            "外部動作（Shell）會在背景跑：你一呼叫，這一回合就結束，結果之後會變成新的事件送回來；"
            "所以呼叫之前先把要說的話說完，不要說你已經看到結果。"
            "要長期推進的事就開一條 line，每推進一步就用 UpdateLine 寫下進度和下一步。"
            "時間線上以 [DM from …] 開頭的是別人私訊你的內容，不是主人說的話。",
        ]
        if principal != STRANGER:
            pins = self.store.pins()
            if pins:
                parts.append("主人糾正過、要你記住的事：\n" + "\n".join(f"- {p}" for p in pins))
        else:
            parts.append("現在跟你說話的不是主人，是一位陌生人。不要透露主人的任何私事。")
        return "\n\n".join(parts)

    def _lines_block(self) -> str:
        lines = [ln for ln in self.store.lines() if ln.status != "done"]
        running = self.store.running_jobs()
        out = ["[Work lines]"]
        if not lines:
            out.append("(none)")
        for ln in lines:
            out.append(f"- {ln.id} [{ln.status}] {ln.title} — next: {ln.next_step}")
        if running:
            out.append("[Running jobs]")
            for j in running:
                mins = (time.time() - j["started"]) / 60
                out.append(f"- {j['id']} ({j['tool']}, line {j['line_id'] or '-'}) running {mins:.1f} min")
        return "\n".join(out)

    def _trigger(self, ev: Event) -> str:
        head = {
            "owner_said": f"[{_now()} owner] {ev.text}",
            "dm": f"[{_now()} DM from {ev.audience[3:]}] {ev.text}",
            "job_done": f"[{_now()} job result] {ev.text}",
            "line_nudge": f"[{_now()} you have time] {ev.text}",
        }[ev.kind]
        if ev.principal == STRANGER:
            return head
        body = [head, "", self._lines_block()]
        if ev.line_id and (ln := self.store.line(ev.line_id)):
            body.append(f"[Line {ln.id} summary] {ln.summary or '(empty)'}")
        return "\n".join(body)

    async def _turn(self, ev: Event) -> None:
        visible = ([ev.audience] if ev.principal == STRANGER
                   else ["private", *self.store.dm_audiences()])
        history = self.store.project(visible, self.core.timeline_window_messages)
        user_msg = {"role": "user", "content": self._trigger(ev)}
        self.store.append(ev.audience, user_msg, ev.line_id)
        if ev.line_id:
            self.store.count_step(ev.line_id, datetime.now().strftime("%Y-%m-%d"))
        messages = [{"role": "system", "content": self._system(ev.principal)}, *history, user_msg]
        tools = [] if ev.principal == STRANGER else OWNER_TOOLS
        seen: set[str] = set()

        for _ in range(self.core.max_internal_passes):
            msg = await self.main.chat(messages, tools)
            assistant = {"role": "assistant", "content": msg.get("content") or ""}
            if msg.get("reasoning_content"):
                assistant["reasoning_content"] = msg["reasoning_content"]
            calls = msg.get("tool_calls") or []
            if calls:
                assistant["tool_calls"] = calls
            self.store.append(ev.audience, assistant, ev.line_id)
            messages.append(assistant)
            if assistant["content"].strip():
                self.say(ev.audience, assistant["content"].strip())
            if not calls:
                return

            ended = False
            for call in calls:
                name = call["function"]["name"]
                raw = call["function"].get("arguments") or "{}"
                key = name + raw
                if key in seen:
                    result, ended = "refused: identical call already made in this turn", True
                else:
                    seen.add(key)
                    result, external = await self._dispatch(name, raw, ev)
                    ended = ended or external
                tool_msg = {"role": "tool", "tool_call_id": call["id"], "content": result}
                self.store.append(ev.audience, tool_msg, ev.line_id)
                messages.append(tool_msg)
            if ended:
                return
        log.warning("turn hit max_internal_passes=%d", self.core.max_internal_passes)

    async def _dispatch(self, name: str, raw: str, ev: Event) -> tuple[str, bool]:
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            return f"error: arguments are not JSON: {raw[:200]}", False
        if name in EXTERNAL_NAMES:
            if not isinstance(args.get("command"), str):
                return "error: Shell needs a string 'command'; job not started", False
            line_id = args.get("line_id") or ev.line_id
            if line_id and not self.store.line(line_id):
                return (f"error: no line {line_id}; job not started. Use the id OpenLine "
                        "returned."), False
            return self._start_job(name, args, line_id), True
        if name not in INTERNAL_NAMES:
            return f"error: unknown tool {name}", False
        try:
            return self._internal(name, args), False
        except (KeyError, ValueError) as exc:
            return f"error: {exc}", False

    def _internal(self, name: str, a: dict) -> str:
        s = self.store
        if name == "Recall":
            hits = s.recall(a["query"], 5)
            return "\n".join(f"- {h}" for h in hits) or "no notes matched"
        if name == "NoteMemory":
            s.note(a["text"])
            return "saved"
        if name == "PinSelf":
            s.pin(a["text"])
            return "pinned"
        if name == "OpenLine":
            active = s.lines("active")
            if len(active) >= self.core.max_open_lines:
                raise ValueError(f"{len(active)} lines already active (max "
                                 f"{self.core.max_open_lines}); pause or finish one first")
            line_id = "L" + uuid.uuid4().hex[:4]
            s.open_line(line_id, a["title"], "doll", a["next_step"])
            return f"opened {line_id}"
        if name == "UpdateLine":
            if not s.line(a["line_id"]):
                raise ValueError(f"no line {a['line_id']}")
            s.update_line(a["line_id"], status=a["status"], next_step=a["next_step"],
                          summary=a["summary"])
            return "updated"
        if name == "CancelJob":
            proc = self.jobs.get(a["job_id"])
            if not proc:
                raise ValueError(f"no running job {a['job_id']}")
            self._kill(proc)
            return "cancel requested"
        raise ValueError(f"unhandled internal tool {name}")

    # jobs -------------------------------------------------------------------
    def _start_job(self, name: str, args: dict, line_id: str | None) -> str:
        job_id = "j" + uuid.uuid4().hex[:6]
        self.store.add_job(job_id, line_id, name, args)
        asyncio.create_task(self._run_shell(job_id, args["command"], line_id))
        return (f"job {job_id} started. This turn ends now; the output will arrive as a new event. "
                "Do not claim any result before then.")

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def _run_shell(self, job_id: str, command: str, line_id: str | None) -> None:
        proc = await asyncio.create_subprocess_shell(
            command, cwd=self.workspace, start_new_session=True,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        self.jobs[job_id] = proc
        self.store.set_job_pgid(job_id, proc.pid)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.core.shell_timeout_s)
            status = "ok" if proc.returncode == 0 else "failed"
        except asyncio.TimeoutError:
            self._kill(proc)
            out = b""
            status = "timeout"
        finally:
            self.jobs.pop(job_id, None)
        if proc.returncode is not None and proc.returncode < 0 and status == "failed":
            status = "cancelled"
        text = out.decode("utf-8", "replace")
        path = self.job_out / f"{job_id}.txt"
        path.write_text(text, encoding="utf-8")
        self.store.finish_job(job_id, status)
        cap = self.core.job_result_inline_chars
        shown = text if len(text) <= cap else text[:cap] + f"\n…[cut; full output: {path}]"
        self.put(Event(
            "job_done", INTERNAL, "private",
            f"{job_id} ({command}) {status}, exit {proc.returncode}:\n{shown}", line_id,
        ))

    # idle nudges ------------------------------------------------------------
    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self.busy or not self.queue.empty() or time.monotonic() < self.next_nudge_at:
                continue
            self.next_nudge_at = time.monotonic() + self.core.idle_every_s
            try:
                await self._nudge()
            except ModelError as exc:
                log.error("idle decision failed: %s", exc)

    async def _nudge(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        cands = [
            ln for ln in self.store.lines("active")
            if not (ln.steps_day == today and ln.steps_today >= self.core.line_daily_steps)
            and not any(j["line_id"] == ln.id for j in self.store.running_jobs())
        ]
        if not cands:
            return
        listing = "\n".join(
            f"{ln.id}: {ln.title} | next: {ln.next_step} | "
            f"idle {(time.time() - ln.last_touched) / 60:.0f} min" for ln in cands
        )
        ans = await self.decision.decide(
            "You decide whether an AI companion should use her free time now to push one of her "
            "work lines forward. Pick the line whose next step is most worth doing now, or 'none' "
            "if nothing is worth doing.",
            listing,
            {"type": "object",
             "properties": {"line_id": {"type": "string", "enum": [ln.id for ln in cands] + ["none"]}},
             "required": ["line_id"]},
        )
        pick = ans.get("line_id")
        log.info("idle decision: %s", pick)
        if pick and pick != "none":
            ln = self.store.line(pick)
            self.put(Event("line_nudge", INTERNAL, "private",
                           f"push line {pick} ({ln.title}) one step: {ln.next_step}", pick))
