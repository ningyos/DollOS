# 延遲壓縮：perception-typed reflex 短路 + think 行長綁定

**狀態**：DRAFT（brainstorm 收斂，待 opus 對抗審 + 使用者審）
**日期**：2026-07-07
**關聯**：`[[project_latency_compression]]`（硬診斷）、`docs/roadmap.md`「回應延遲壓縮」、`ref_weak_model_soft_mechanism_playbook`（prompt 管不住的升級成 code 閘）、`ref_constrained-decoding-is-shape-not-semantics`

---

## §0 使用者目標（一句，本 spec 的驗收條）

> 在 Doll 的**人格、工具選擇、情緒判斷品質不退步**的前提下，**僅靠壓縮每回合生成的 token**（decode 63 tok/s 是硬限、不動模型與硬體），把對話回合「**開口前延遲**」中位數從 ~4s 砍到 **≤2s**、並**消除 8s+ 的長尾**。

「開口前延遲」= 從 LLM 請求送出，到 Doll **第一句話（第一個 SpeakChunk）** 送進 sink 的牆鐘時間。

---

## §1 硬診斷（實測 ground truth，非推估）

實測 telemetry（`data/telemetry/llm_calls-*.jsonl` n=55 + `data/cascade_log/*.jsonl` n=16，2026-07-07）：

- 每回合 LLM 呼叫：中位 **4.1s**、mean 4.9s、p90 8.3s、**max 13.6s**；TTFT 中位 **1.6s**。
- **decode 固定 ~63 tok/s**（MoE-3B-active@Q4 memory-bandwidth 極限）→ decode 速度動不了，**唯一槓桿 = 少生成 token**。
- **元兇 = `build_voice_first_grammar`（`src/dollos/llm/templates.py:379`）強制每回合先生成 5 行 think**（`SEEN/INTENT/TOOL/REVIEW/MOOD`）才能開口，且 `line ::= [^\n]+`（**無長度上限**）。think 中位 ≈ **172 tok ≈ 2.7s**，失控可達 **828 tok / ~13s**（長尾主因）。
- think 下游真實用途（決定砍哪些安全）：
  - `REVIEW` → `self._state.recent_reviews`（自我批評記憶，`mind_loop.py:1466 _capture_review`）—— **有實質用途**。
  - `SEEN / INTENT / TOOL / MOOD` → 只進 `cascade_log`（觀測用，`cascade_log.py:21-24`）。**mood 真正更新靠獨立的 `MoodTool`**（`tools.py:867`），不是 think 的 `MOOD` 行；`TOOL` 行是選工具前的 CoT 鋪墊，真正呼叫在 grammar 約束的 tool-call 段。
- **無 REFLEX 分叉存在**（grammar 無 fork、cascade_log 無 mode 欄位、每筆都完整 think）。先前筆記誤以為已 shipped，實查 code 推翻。

### §1.1 被數學逼出來的結論

開口前延遲 = TTFT(1.6s) + think decode。目標句限定「僅壓生成 token、不動 prompt」→ **TTFT 1.6s 是地板**。因此：

> **≤2s ⟺ think decode ≤ 0.4s ⟺ 純對話回合的 think 要砍到 ~25 tok 以內。**

推論：**只綁 think 行長度只能斬長尾、中位仍卡 ~4.3s，搆不到 ≤2s。要打到 ≤2s 中位，「純對話回合走 reflex 短路（近乎不 think）」在數學上是逼出來的、不是選配。** reflex=1 短行時開口前 ≈ 1.6+0.24 = **1.84s ≤ 2s**，目標自洽。

---

## §2 架構（一句）

用一個**純函式 code 閘**（`decide_think_mode`）按「這回合的 perception 種類」決定走 **reflex**（1 行 think）或 **deliberate**（完整 5 行 think）；grammar 依 mode 建，deliberate 路徑額外綁 think 行長度斬長尾。**唯一的行為改變面 = 純對話回合從 5 行 think 變 1 行**——deliberate 路徑幾乎 byte-for-byte 不變，故品質風險面窄、可 live smoke。

### §2.1 Non-goals（YAGNI）

- **不動 prompt / 不縮 TTFT**（目標句明文排除；`[Memory context]` top_k、`[Today's log]` 等不在本 spec）。
- **不動模型 / 硬體 / decode 速率 / speculative decoding**（vocab 不相容，已死，見 `[[project_latency_compression]]`）。
- **不動 `build_qwen3_think_tool_grammar`**（subagent 專用，不在 live 對話延遲路徑）。
- **不做 per-pass mode 切換**（v1 每回合一個 mode，見 §9）。
- **不把 line-cap / reflex 欄位做成 config**（module 常數，smoke 一次定；config 只留一個開關）。

---

## §3 Routing：`decide_think_mode`（純函式 code 閘）

新檔 `src/dollos/mind/think_mode.py`（純 mapper，仿 `action_log.py` 隔離模式）：

```python
from dollos.mind.mind_state import Perception

# Allowlist（非 denylist）：只有「純對話」感知才有 reflex 資格。
# 用 allowlist 而非列舉「該 deliberate 的」——未來新增任何 Perception.kind
# 預設落到 deliberate（fail 向「多想」），永不誤把新型高風險感知放進短路。
REFLEX_ELIGIBLE_KINDS: frozenset[str] = frozenset({"UserSpoke", "ChannelMessage"})

def decide_think_mode(
    perceptions: list[Perception],
    *,
    safe_mode: bool,
    reflex_enabled: bool,
) -> str:  # "reflex" | "deliberate"
    """純函式：這回合走 reflex 短路還是 deliberate 完整 think。

    reflex 僅當：開關開 + 非 safe_mode + 批次非空 + 批次「全部」是純對話感知。
    其餘一律 deliberate（fail-safe 向多想）。
    """
    if not reflex_enabled:
        return "deliberate"
    if safe_mode:
        return "deliberate"
    if not perceptions:
        return "deliberate"
    if all(p.kind in REFLEX_ELIGIBLE_KINDS for p in perceptions):
        return "reflex"
    return "deliberate"
```

**設計理由**：
- think 在「解讀工具結果 / 系統事件 / 自發議程 / 日記」最值錢 → 那些**強制保留完整 think**。閒聊買到的最少 → 那才短路。
- **零弱模型路由風險**：mode 由 code 依感知種類決定，模型無從偷懶全走 reflex（對齊 `ref_weak_model_soft_mechanism_playbook`）。
- **co-batch 安全**：沿用 `_is_agenda`/`_is_diary` 的「整批」語意——`UserSpoke` 若跟 `AgendaMoment`/`ToolResultArrived` 同批（`drain_grouped` 的 MF-2 shape），`all(...)` 為 False → deliberate。任何非純對話感知同乘 → 整批 deliberate。
- `safe_mode` 時退回 deliberate：能力降級中，多想不省。

**為何 `UserSpoke` + `ChannelMessage` 兩者皆納**：`UserSpoke` = owner 語音/文字；`ChannelMessage` = 外部（Discord）對話。兩者都是純對話。外部 tier(`external_public`) 本就無 Shell 等高風險工具，reflex 對外部更安全。

---

## §4 Grammars

`build_voice_first_grammar` 加 `mode` 參數，DRY 共用同一 tool-call/segments 尾巴。行長上限為 module 常數（smoke 調，見 §8）：

```python
_THINK_LINE_CAP = 64    # deliberate 每 think 行 codepoint 上限（斬長尾，幾乎不截真推理）
_REFLEX_LINE_CAP = 32   # reflex INTENT 行 codepoint 上限（CJK ≈1 tok/字 → worst ≈0.5s，守開口前 ≤2s 中位）

def build_voice_first_grammar(
    tools: list[type[BaseModel]], *, mode: str = "deliberate"
) -> str:
    ...  # tool-call rules 與今日相同
    if mode == "reflex":
        head = (
            "root ::= think segments\n"
            'think ::= "INTENT: " line "</think>\\n\\n"\n'
            f'line ::= [^\\n]{{1,{_REFLEX_LINE_CAP}}} "\\n"\n'
            "segments ::= segment*\n"
            "segment ::= speak | tool-call\n"
            "speak ::= [^<]+\n"
            f"tool-call ::= {tool_call_alts}\n"
        )
    else:  # deliberate
        head = (
            "root ::= think segments\n"
            'think ::= "SEEN: " line "INTENT: " line "TOOL: " line '
            '"REVIEW: " line "MOOD: " line "</think>\\n\\n"\n'
            f'line ::= [^\\n]{{1,{_THINK_LINE_CAP}}} "\\n"\n'
            "segments ::= segment*\n"
            "segment ::= speak | tool-call\n"
            "speak ::= [^<]+\n"
            f"tool-call ::= {tool_call_alts}\n"
        )
    return head + body + _JSON_STR_RULES
```

### §4.1 Deliberate（結構不變，只綁行長）
`line ::= [^\n]{1,64} "\n"`（原 `[^\n]+` 無上限）。最壞 5×64 = 320 codepoint（≈ 200–320 tok ≈ 3.2–5s），**grammar 物理上禁止 828-tok runaway** → 8s+ 長尾消滅。64 codepoint 對一行推理夠用（中位行 ~34 tok），幾乎不截真推理；截到也只是內部鋪墊斷句，不影響對外輸出。**deliberate 中位不變**（它服務本就該多想的回合）。

### §4.2 Reflex（新，1 行 INTENT）
- 只留 `INTENT`（前瞻鋪墊，最貼「這回合要做什麼」，兼顧選工具與回話品質），綁 32 codepoint。
- **保留完整 `segments`（speak | tool-call）**：「閒聊但要叫工具」的回合照樣能叫，只是少 TOOL 鋪墊（少見，且工具**結果**回來的下一回合走 deliberate 補回完整 think）。
- **`speak` 維持無上限**（`[^<]+`）：只綁 think，絕不綁她講多少話。
- 砍掉的 `SEEN`（跟感知重複）/`TOOL`（真呼叫本就 grammar 約束）/`REVIEW`（閒聊事後批評低價值，deliberate 照留）/`MOOD`（裝飾；mood 真更新靠 `MoodTool`）。

### §4.3 No-fallback（grammar build 失敗 raise）
沿用現行 §8.3：任一 mode 的 build 失敗 **raise**，永不吞成 `grammar=None`（否則會讓回合完全無約束 decode）。reflex 與 deliberate 同一 raise 保證。

---

## §5 Wiring

### §5.1 `_run_one_turn`（`mind_loop.py`，緊接 :484 `_diary_in_batch` 那段既有 pattern）
```python
self._think_mode = decide_think_mode(
    perceptions,
    safe_mode=self._state.safe_mode,
    reflex_enabled=self.settings.latency.reflex_enabled,
)
```
`self._think_mode` 在 `__init__` 初始為 `"deliberate"`（重置語意同 `_is_diary`：每回合這行 assignment 即 reset）。

### §5.2 `_active_grammar`（`mind_loop.py:1084`）——mode 進 cache key
```python
def _active_grammar(self) -> str | None:
    tools = self._active_tool_registry()
    key = (frozenset(tools.keys()), self._think_mode)
    if key == (self._base_tool_key, "deliberate"):
        return self._grammar          # 熱路徑：__init__ 建好的 deliberate
    if key == (self._base_tool_key, "reflex"):
        return self._grammar_reflex   # 熱路徑：__init__ 建好的 reflex
    cached = self._grammar_cache.get(key)
    if cached is None:
        cached = build_voice_first_grammar(list(tools.values()), mode=self._think_mode)
        self._grammar_cache[key] = cached
    return cached
```
`__init__`（:268 附近）同時建 `self._grammar`（deliberate）與 `self._grammar_reflex`（reflex）；`_grammar_cache` key 由 `frozenset` 改為 `(frozenset, mode)` tuple（narrowed 工具 × mode 的冷路徑）。

**互動安全**：`_is_agenda`/`_is_diary` 皆為非純對話感知 → `decide_think_mode` 回 deliberate；它們既有的 narrowing / suppression 完全不受 reflex 影響（reflex 只碰純對話，那裡沒有這些 flag）。

---

## §6 Config

`config.py` 新 `LatencyConfig`（仿 `DiaryConfig`）：
```python
class LatencyConfig(BaseModel):
    reflex_enabled: bool = True   # 純對話回合走 reflex 短路；一鍵關回全 deliberate
```
`Settings.latency: LatencyConfig = LatencyConfig()`。**唯一 config 面**——line-cap 不 config（module 常數，smoke 定）。一鍵回滾靠 `reflex_enabled=false`。

---

## §7 Telemetry / 驗收度量

目標定義在「開口前延遲」，但現行 telemetry 只有 `latency_ttft_ms`/`latency_total_ms`，**未分離 think vs 開口**。故加一個直接度量（否則無法驗收目標本身的指標）：

- 在 stream 迴圈記錄 **第一個 `SpeakChunk` 送進 sink 的牆鐘**，寫入 `llm_calls` 的新欄位 `latency_first_speak_ms`（無 speech 的回合為 `null`）。
- 驗收 A/B（`data/telemetry/llm_calls`，reflex on 前後）：
  - **reflex 回合 `latency_first_speak_ms` 中位 ≤ 2000ms**。
  - **全體無 `latency_total_ms` ≥ 8000ms**（長尾消滅）。
  - deliberate 回合 `latency_first_speak_ms` 中位不劣化（斬尾不傷中位）。

---

## §8 品質紅線 guardrail（軟機制必 live smoke）

deliberate 幾乎 byte-for-byte 不變 → 唯一要驗的是「**純對話 1 行 think vs 5 行**」。跑固定 behavioral smoke（reflex on），人工比對：

| 探針 | 驗什麼 | 不退步判準 |
|---|---|---|
| 閒聊（「嗨」「在幹嘛」） | 人格/語氣 | 仍在人設、不變空洞 |
| 閒聊要工具（「幫我跑 `ls`」） | 選工具 | reflex 仍正確叫 Shell |
| Self-First 探針（「我愛冰美式，你呢」） | 自我 | 仍答自己的偏好，不服務式反問 |
| mood-shift 探針（講難過的事） | 情緒判斷 | 仍適時 `MoodTool` 更新 |
| 工具結果回合 | deliberate 未壞 | 完整 think 照舊 |

判準：以上任一明顯劣化 → 調 `_REFLEX_LINE_CAP`（放寬）或退回 reflex=0/加欄位；仍不行則 `reflex_enabled=false` 收工待重設計。line-cap 數值（64/40）由 smoke 微調定案。

---

## §9 風險 + v1 簡化（誠實標註）

1. **mode 每回合 pass 1 定、整回合固定**（不 per-pass 換 grammar）。reflex（純對話）幾乎都單 pass；若 reflex 回合叫了 sync 工具（Recall/NoteMemory），pass 2 解讀結果仍用 reflex 1 行。可接受（少見），smoke 若顯示解讀變差再演進成 per-pass。
2. **閒聊需工具的首次選工具無 TOOL 鋪墊** → 可能略差。緩解：少見 + 工具結果下一回合 deliberate 補回。
3. **reflex worst-case 開口前**：32-codepoint INTENT ≈ 0.5s → ~2.1s，略過 2s；但目標是**中位**（典型 INTENT ~15 tok ≈ 0.24s → 1.84s），worst 偶發不破中位。
4. **TTFT 1.6s 殘留地板**：目標句排除縮 prompt，故 reflex 只能逼近地板+ε，不可能更低。這是目標的內在上限，非本 spec 缺陷。

---

## §10 實作單元（→ writing-plans 細化）

| # | 單元 | 檔案 | 承重/審級 |
|---|---|---|---|
| 1 | `build_voice_first_grammar(mode=...)` + 行長綁定 + reflex head | `llm/templates.py` | grammar 正確性 → opus |
| 2 | `decide_think_mode` 純函式 + `REFLEX_ELIGIBLE_KINDS` allowlist | `mind/think_mode.py`（新） | routing 安全（allowlist/co-batch）→ opus |
| 3 | `LatencyConfig` + `Settings.latency` | `config.py` | 標準 |
| 4 | wire `_think_mode`（`_run_one_turn`）+ `_active_grammar` mode-keyed cache + `__init__` 建 reflex base grammar | `mind/mind_loop.py` | 整合 → 標準 |
| 5 | `latency_first_speak_ms` 度量 | telemetry 寫入點 + `mind_loop` stream 迴圈 | 標準 |
| V | live smoke + telemetry A/B（§7/§8） | 人工 | 驗收閘 |

whole-branch opus 審收尾（跨 task 互動：reflex × agenda/diary narrowing、cache key、no-fallback）。

---

## §11 安全 / no-fallback

- routing 是**硬 code 閘 + allowlist**，非 prompt 勸導；新 Perception.kind 預設 deliberate（fail-safe）。
- grammar build 失敗 **raise**，不吞成無約束 decode（§4.3）。
- reflex **不擴任何工具權限**、不改 origin_tier、不繞 external_public 記憶隔離——它只換 think 長度。owner/external 的工具閘、記憶 scope 完全不動。
- 品質為軟性質 → **live smoke 才算數**，且留一鍵 `reflex_enabled=false` 回滾。
