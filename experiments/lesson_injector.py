"""
lesson_injector.py — Minimal grammar-based lesson injection library.

Concept:
    Store hand-curated "lessons" (failure-mode corrections, project conventions,
    user preferences, etc.). At inference time, retrieve relevant lessons by
    keyword match against the prompt, compose them into a GBNF literal that
    forces the LLM to "think" the lesson before generating its answer.

Validated on Qwen3.6-35B-A3B: targeted lesson took merge-intervals +1 bug
from 80% -> 0% failure rate, with ~106 token overhead.

Usage:
    from lesson_injector import LessonInjector

    inj = LessonInjector(
        server_url="http://127.0.0.1:8001",
        model="unsloth/Qwen3.6",
        store_path="lessons.json",
    )

    # Curate lessons (persisted to JSON):
    inj.store.add(
        id="closed_intervals_no_plus1",
        triggers=["closed interval", "merge interval"],
        text="For closed integer intervals use `start <= last_end` (no +1).",
    )

    # Use it — relevant lessons auto-fire by keyword match:
    r = inj.complete("Write merge_intervals for closed integer ranges ...")
    print(r["content"])
    print("fired:", r["lessons_fired"])
"""
import json, urllib.request, time
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Optional


@dataclass
class Lesson:
    id: str
    triggers: list           # case-insensitive substrings; lesson fires if ANY matches
    text: str                # the lesson content (will be grammar-escaped)
    notes: str = ""          # human notes (not injected)


class LessonStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lessons: list = []
        if self.path.exists():
            self.lessons = [Lesson(**d) for d in json.loads(self.path.read_text())]

    def save(self):
        self.path.write_text(json.dumps([asdict(l) for l in self.lessons], indent=2, ensure_ascii=False))

    def add(self, id, triggers, text, notes=""):
        self.lessons = [l for l in self.lessons if l.id != id]
        self.lessons.append(Lesson(id=id, triggers=list(triggers), text=text, notes=notes))
        self.save()

    def remove(self, id):
        before = len(self.lessons)
        self.lessons = [l for l in self.lessons if l.id != id]
        self.save()
        return len(self.lessons) < before

    def match(self, prompt: str, limit: int = 3):
        p = prompt.lower()
        return [l for l in self.lessons if any(t.lower() in p for t in l.triggers)][:limit]


def gbnf_escape(s: str) -> str:
    return s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def compose_grammar(lessons: list) -> str:
    """Build a GBNF where MEMORY block contains forced lesson literals."""
    head = ('root ::= think answer\n'
            'line ::= [^\\n]+ "\\n"\n'
            'answer ::= [\\x09\\x0a\\x0d\\x20-\\x7e]+\n')
    if not lessons:
        return head + ('think ::= "GOAL: " line "APPLY: " line "EDGE: " line '
                       '"</think>\\n\\n"\n')
    bullets = "".join(f'- {gbnf_escape(l.text)}\\n' for l in lessons)
    return head + (f'think ::= "GOAL: " line "MEMORY:\\n{bullets}" '
                   f'"APPLY: " line "EDGE: " line "</think>\\n\\n"\n')


def _post(url, body, timeout=600):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def render_lessons_block(lessons: list) -> str:
    """Plain-text MEMORY block to be embedded directly into prompt prefill."""
    if not lessons:
        return ""
    bullets = "\n".join(f"- {l.text}" for l in lessons)
    return f"MEMORY:\n{bullets}\nAPPLY: "


class LessonInjector:
    def __init__(self, server_url, model, store_path, log_path=None, **defaults):
        base = server_url.rstrip("/")
        self.chat_url = base + "/v1/chat/completions"
        self.compl_url = base + "/completion"
        self.tmpl_url = base + "/apply-template"
        self.model = model
        self.store = LessonStore(store_path)
        self.log_path = Path(log_path) if log_path else None
        self.defaults = {"temperature": 0.6, "top_p": 0.95, "top_k": 20,
                         "max_tokens": 4096, **defaults}

    def _select(self, prompt, max_lessons, force_lesson_ids):
        if force_lesson_ids is not None:
            return [l for l in self.store.lessons if l.id in force_lesson_ids]
        return self.store.match(prompt, limit=max_lessons)

    def complete(self, prompt, *, system=None, max_lessons=3,
                 force_lesson_ids=None, mode="grammar", dry_run=False):
        """mode: 'grammar' (GBNF-forced literal) or 'prefill' (direct embed)."""
        lessons = self._select(prompt, max_lessons, force_lesson_ids)
        if mode == "prefill":
            return self._complete_prefill(prompt, system, lessons, dry_run)
        return self._complete_grammar(prompt, system, lessons, dry_run)

    def _complete_grammar(self, prompt, system, lessons, dry_run):
        grammar = compose_grammar(lessons)
        if dry_run:
            return {"mode": "grammar", "grammar": grammar,
                    "lessons_fired": [l.id for l in lessons]}
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        body = {"model": self.model, "messages": msgs, "grammar": grammar,
                **self.defaults}
        t0 = time.time()
        resp = _post(self.chat_url, body)
        dt = time.time() - t0
        result = {
            "mode": "grammar",
            "content": resp["choices"][0]["message"]["content"],
            "lessons_fired": [l.id for l in lessons],
            "usage": resp.get("usage", {}),
            "elapsed": dt,
        }
        self._log_complete(prompt, result)
        return result

    def _complete_prefill(self, prompt, system, lessons, dry_run):
        # 1) render messages via /apply-template (gets <think>\n appended for thinking model)
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        rendered = _post(self.tmpl_url, {"messages": msgs})["prompt"]
        # 2) splice memory text directly after <think>\n
        memory_block = render_lessons_block(lessons)
        full_prompt = rendered + memory_block
        if dry_run:
            return {"mode": "prefill", "prompt_tail": full_prompt[-400:],
                    "lessons_fired": [l.id for l in lessons]}
        # 3) /completion raw — model continues from APPLY: ...
        body = {"prompt": full_prompt,
                "n_predict": self.defaults.get("max_tokens", 4096),
                "temperature": self.defaults.get("temperature", 0.6),
                "top_p": self.defaults.get("top_p", 0.95),
                "top_k": self.defaults.get("top_k", 20),
                "stop": ["<|im_end|>"],
                "cache_prompt": True}
        t0 = time.time()
        resp = _post(self.compl_url, body)
        dt = time.time() - t0
        gen = resp.get("content", "")
        # reconstruct: prepend the embedded memory + "APPLY: " so .content matches grammar mode shape
        full_content = memory_block + gen
        result = {
            "mode": "prefill",
            "content": full_content,
            "lessons_fired": [l.id for l in lessons],
            "usage": {
                "prompt_tokens": resp.get("tokens_evaluated", 0),
                "completion_tokens": resp.get("tokens_predicted", 0),
                "total_tokens": resp.get("tokens_evaluated", 0) + resp.get("tokens_predicted", 0),
            },
            "elapsed": dt,
        }
        self._log_complete(prompt, result)
        return result

    def _log_complete(self, prompt, result):
        if self.log_path:
            self._log({"event": "complete", "mode": result["mode"],
                       "prompt": prompt[:300],
                       "lessons_fired": result["lessons_fired"],
                       "usage": result["usage"], "elapsed": result["elapsed"]})

    def record_outcome(self, prompt, lessons_fired, passed, note=""):
        """Log post-hoc whether the response was correct, for later curation."""
        if not self.log_path:
            return
        self._log({"event": "outcome", "prompt": prompt[:300],
                   "lessons_fired": lessons_fired, "passed": passed, "note": note})

    def _log(self, entry):
        entry["ts"] = time.time()
        with self.log_path.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---- CLI demo / smoke test ----
if __name__ == "__main__":
    import sys, re, subprocess
    inj = LessonInjector(
        server_url="http://127.0.0.1:8001",
        model="unsloth/Qwen3.6",
        store_path="/home/progcat/Projects/qwen3.6/lessons.json",
        log_path="/home/progcat/Projects/qwen3.6/injector.log.jsonl",
    )

    # seed validated lesson
    inj.store.add(
        id="closed_intervals_no_plus1",
        triggers=["closed interval", "merge interval", "merge_intervals"],
        text=("For closed integer intervals use `current_start <= last_end` "
              "(no +1). [1,2] and [3,4] share no integer and must NOT merge."),
        notes="Validated 80%->0% bug rate on Qwen3.6-35B-A3B 2026-05-01.",
    )

    prompt = ("Write a Python function `merge_intervals(intervals)` where each "
              "interval is a closed integer range [a, b]. Two intervals merge "
              "iff they overlap or touch. Return merged list sorted by start. "
              "Output ONLY the function inside ```python ... ```.")

    print("=== dry run (show grammar + lessons fired) ===")
    dr = inj.complete(prompt, dry_run=True)
    print("lessons_fired:", dr["lessons_fired"])
    print("grammar (first 400 chars):")
    print(dr["grammar"][:400])

    print("\n=== live call ===")
    r = inj.complete(prompt)
    print(f"lessons_fired: {r['lessons_fired']}")
    print(f"tokens: {r['usage'].get('completion_tokens')}  elapsed: {r['elapsed']:.1f}s")

    # extract & test
    m = re.search(r"```(?:python)?\s*\n(.*?)```", r["content"], re.DOTALL)
    code = m.group(1) if m else r["content"]
    cases = [
        ([[1,3],[3,5]], [[1,5]]),
        ([[1,2],[3,4]], [[1,2],[3,4]]),
        ([[5,7],[1,3],[2,4]], [[1,4],[5,7]]),
        ([[1,2],[4,5],[7,8]], [[1,2],[4,5],[7,8]]),
    ]
    passed = 0
    for inp, exp in cases:
        prog = code + f"\nimport json\nprint(json.dumps(merge_intervals({inp!r})))\n"
        try:
            out = subprocess.run(["python3","-c",prog], capture_output=True, timeout=5, text=True)
            got = json.loads(out.stdout.strip())
            ok = got == exp
        except Exception:
            ok = False
        passed += int(ok)
        print(f"  {'OK' if ok else 'FAIL'} merge({inp}) -> {got if ok else 'err'}")
    print(f"\nsmoke test: {passed}/{len(cases)} passed")
    inj.record_outcome(prompt, r["lessons_fired"], passed == len(cases))
