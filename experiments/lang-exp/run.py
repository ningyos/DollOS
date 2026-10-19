"""Send variant requests to Strata, one at a time, politely.
usage: run.py VARIANT SCENARIO N [user_text]"""
import json, re, sys, time
from pathlib import Path
import httpx
from variants import build

BASE = "http://100.110.158.36:8080"
MODEL = "qwen3.8-flash-next-iq3_s"
HERE = Path(__file__).resolve().parent
HAN = re.compile(r"[㐀-鿿]")
LAT = re.compile(r"[A-Za-z]")


def classify(text: str) -> str:
    t = re.sub(r"\bGura\b", "", text)
    h, l = len(HAN.findall(t)), len(LAT.findall(t))
    if h == 0 and l == 0:
        return "silent"
    score = h / (h + l / 4)  # ~4 latin letters per word ~ 1 han
    return "zh" if score > 0.8 else "en" if score < 0.2 else "mixed"


def wait_idle():
    for _ in range(120):
        try:
            h = httpx.get(f"{BASE}/health", timeout=5).json()
            m = httpx.get(f"{BASE}/metrics", timeout=5).json()
            live, cc = m["live"], m["conversation_cache"]
            if h.get("status") == "ok" and live["state"] == "idle" and not live["queued"]:
                return cc
            print(f"  server busy ({live['state']}, queued {live['queued']}); waiting", flush=True)
        except Exception as e:
            print("  health err", e, flush=True)
        time.sleep(5)
    raise SystemExit("server never idle; stopping")


def one(variant, scenario, user_text=None):
    req = build(variant, scenario, user_text)
    body = {"model": MODEL, "messages": req["messages"], "tools": req["tools"], "stream": True,
            "max_tokens": 400, "reasoning_budget_tokens": 256, "presence_penalty": 1.3}
    cc = wait_idle()
    t0 = time.time()
    think, speech, tools, final = [], [], {}, {}
    with httpx.stream("POST", f"{BASE}/v1/chat/completions", json=body,
                      timeout=httpx.Timeout(300, connect=5)) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line or line.startswith(":"):
                continue
            p = line.removeprefix("data:").strip()
            if p == "[DONE]":
                break
            d = json.loads(p)
            if d.get("usage"): final["usage"] = d["usage"]
            if d.get("timings"): final["timings"] = d["timings"]
            for ch in d.get("choices") or []:
                if ch.get("finish_reason"): final["finish"] = ch["finish_reason"]
                dl = ch.get("delta") or {}
                if dl.get("reasoning_content"): think.append(dl["reasoning_content"])
                if dl.get("content"): speech.append(dl["content"])
                for f in dl.get("tool_calls") or []:
                    c = tools.setdefault(f["index"], {"name": "", "args": ""})
                    fn = f.get("function") or {}
                    c["name"] = c["name"] or fn.get("name") or ""
                    c["args"] += fn.get("arguments") or ""
    s, th = "".join(speech).strip(), "".join(think)
    rec = {"variant": variant, "scenario": scenario, "user_text": user_text,
           "lang": classify(s), "think_lang": classify(th), "speech": s,
           "think": th, "tools": list(tools.values()), "finish": final.get("finish"),
           "cache_n": final.get("timings", {}).get("cache_n"),
           "prompt_n": final.get("timings", {}).get("prompt_n"),
           "prompt_tokens": final.get("usage", {}).get("prompt_tokens"),
           "secs": round(time.time() - t0, 1), "evictions_before": cc["evictions"]}
    with open(HERE / "results.jsonl", "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"{variant} {scenario} {user_text or ''} -> {rec['lang']:6} (think {rec['think_lang']}) "
          f"{rec['secs']}s cache_n={rec['cache_n']} prompt_n={rec['prompt_n']} "
          f"tools={[t['name'] for t in rec['tools']]} | {s[:160]!r}", flush=True)
    return rec


if __name__ == "__main__":
    v, sc, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
    ut = sys.argv[4] if len(sys.argv) > 4 else None
    for _ in range(n):
        one(v, sc, ut)
