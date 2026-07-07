# 延遲壓縮：think 瘦身（兩段：長尾綁定+遙測 → reflex 短路）

**狀態**：DRAFT v2（brainstorm 收斂 → 3-面 opus 對抗審 + 回溯資料重測 → 硬化，待使用者審）
**日期**：2026-07-07
**關聯**：`[[project_latency_compression]]`（診斷）、`docs/roadmap.md`「回應延遲壓縮」、`ref_weak_model_soft_mechanism_playbook`（prompt 管不住的升級成 code 閘）、`ref_constrained-decoding-is-shape-not-semantics`（grammar 只管形狀）、`project_character_acting`（模型在演的脆弱性）

---

## §0 使用者目標（一句，本 spec 的驗收條）

> 在 Doll 的**人格、工具選擇、情緒判斷品質不退步**的前提下，**僅靠壓縮每回合生成的 token**（decode 63 tok/s 是硬限、不動模型與硬體），把對話回合「**開口前延遲**」中位數從 ~4s 砍到 **≤2s**、並**消除 8s+ 的長尾**。

「開口前延遲」= 從 LLM 請求送出，到 Doll **第一個完整口語句子送進 sink** 的牆鐘時間（sink 只收整句，見 §7.1）。

---

## §1 診斷（實測 + 回溯重測，含誠實界線）

### §1.1 已確認的硬數字
`data/telemetry/llm_calls-*.jsonl`（n=55）：LLM 呼叫中位 **4.1s**、p90 8.3s、**max 13.6s**；TTFT 中位 **1.6s**；decode 固定 **~63 tok/s**（MoE-3B-active@Q4 memory-bandwidth 極限）→ decode 動不了，唯一槓桿 = **少生成 token**。

### §1.2 回溯重測：think 真的吃掉 completion 嗎？（修正前次診斷的方法瑕疵）
前次診斷把 `completion_tokens`（整段）當「think tokens」——方法糙。本次用 `cascade_log` 存的 **think 各欄位全文**（`seen/intent/tool/review/mood`）重建 think 字串、估 token，按時間 join `llm_calls`（n=16 配對，`tmp/think_vs_speech.py`）：

| | median | p90 | max |
|---|---|---|---|
| completion_tok | 160 | 468 | 720 |
| **think_tok(重建估)** | **150** | 468 | 730 |
| speak+json_tok（餘） | 18 | 35 | 40 |
| **think 佔 completion** | **92%** | 101% | 最低 63% |

最慢 6 筆全部 `think≈completion、speak≈0`（13616ms→think~730/speak~0；10383ms→think~549/speak~0）。→ **think 吃掉 ~92% completion，長尾 100% 是 think、不是口語。lever（綁 think）放對了**（R1 對抗審擔心「長尾其實是長口語」被資料推翻）。

### §1.3 誠實界線（決定兩段結構的關鍵）
上述 16 筆**全是 idle / 內部回合**（`Awoke`/`ScheduledMoment`/`RepeatLoop`），pure-chat（`UserSpoke`）回合 **n=0**。含意：
- **實測的 8s+ 長尾 = idle deliberate 回合的 think 空轉**（13.6s 那筆是 730 tok 在糾結「該不該安靜」，模型在跟 grammar 的「必須出動作」硬扛）。**reflex 根本不碰它**（`Awoke` 不在 reflex allowlist）——**殺這條尾的是 deliberate 行長綁定，不是 reflex**。
- **reflex 要打的 chat 回合 think 大小、以及「第一句話 decode」佔多少，目前零資料**。故 reflex 的 ≤2s 效益**必須先量再定案**（見 §2 Part 2 依賴 Part 1 的遙測）。

### §1.4 think 下游真實用途（決定砍哪些安全，已查證）
- `REVIEW` → `self._state.recent_reviews`（maxlen=5，回注 `[Recent self-review]`）。但 scaffolding 定義 REVIEW 為**工具進度/卡住偵測**，非人格自我批評；純對話單-pass 回合的 REVIEW 近乎空洞「first attempt」，拿掉幾乎不損失、甚至**提升** deque 訊號品質（不再被空 REVIEW 擠掉真工具批評）。
- `SEEN/INTENT/TOOL/MOOD` → 只進 `cascade_log`（觀測）。**mood 真更新靠獨立 `MoodTool`**（`mind_loop.py:1223` 確認 only MoodTool 寫 `state.mood`）——但 MoodTool docstring 把呼叫時機綁在「think 的 mood 評估 shifted」，故 MOOD 行是**「重評→注意 shift→叫 MoodTool」的排練前置**，非純裝飾（見 §8 情緒紅線）。
- **無 REFLEX 分叉存在**（grammar 無 fork、cascade_log 無 mode 欄位）。

---

## §2 架構：兩段（先資料、後短路）

被 §1.3 逼出的誠實結構——**不把 ≤2s 全押在未量測的 reflex 上**：

- **Part 1 — 長尾綁定 + 分離遙測（資料背書、低風險，先上）**
  1. deliberate think **行長綁定**：物理禁止 idle-turn 的 730-tok 空轉 → 直接殺實測 8s+ 長尾。
  2. **think / speak / first-speak 分離遙測**：現行 telemetry 無法分離 think vs 口語 token、無 turn 級開口時刻。補上 → 讓 Part 2 有真 chat 資料、讓 ≤2s 可驗收。
  - Part 1 完成即滿足目標的一半（**消 8s+ 長尾**），且幾乎零品質風險。

- **Part 2 — reflex 短路（資料指導、高風險，靠 Part 1 資料定案）**
  3. `decide_think_mode` code 閘按感知種類把純對話回合路由到 reflex（極短 think），打 chat 回合的 **≤2s 中位**。
  - 起手前**先讀 Part 1 收到的真 chat-turn think 大小 + first-speak 分佈**，據以定 reflex 欄位（INTENT-only vs INTENT+MOOD）與 cap 值；再跑 §8 的配對 A/B smoke 守品質紅線。

### §2.1 Non-goals（YAGNI）
不縮 prompt/TTFT（目標句排除；`[Memory context]` top_k 等不在此）；不動模型/硬體/decode/speculative（已死）；不動 `build_qwen3_think_tool_grammar`（subagent 專用，非 live 延遲路徑）；不做 per-pass mode 切換（§9）；line-cap 不做 config（module 常數，資料定）。

### §2.2 附帶觀察（非本 spec 範圍，但記錄）
實測最大延遲事件是 Doll 在 idle `Awoke` 回合花 730 tok 天人交戰「能不能安靜」——這是 grammar「每回合必出動作」與「idle 該能真沉默」的張力，既是延遲也是行為異味。行長綁定只是止血；根治（讓 idle 回合能名正言順不出聲）是另一個 feature，這裡只標記。

---

## §3 Part 1：長尾綁定 + 分離遙測

### §3.1 deliberate think 行長綁定（`llm/templates.py`）
`build_voice_first_grammar` 的 `line` 由 `[^\n]+`（無上限）改綁定。**但 `REVIEW` 會被持久化（`recent_reviews`），不可硬切**（R1-8）——給 REVIEW 專屬較寬 rule：
```python
_THINK_LINE_CAP = 64      # SEEN/INTENT/TOOL/MOOD 每行 codepoint 上限（斬 idle 空轉）
_REVIEW_LINE_CAP = 120    # REVIEW 較寬（會存進 recent_reviews，避免語意殘缺）
...
'think ::= "SEEN: " line "INTENT: " line "TOOL: " line '
'"REVIEW: " rline "MOOD: " line "</think>\\n\\n"\n'
f'line ::= [^\\n]{{1,{_THINK_LINE_CAP}}} "\\n"\n'
f'rline ::= [^\\n]{{1,{_REVIEW_LINE_CAP}}} "\\n"\n'
```
最壞 4×64+120 = 376 codepoint（≈400 tok ≈ 6.3s + TTFT 1.6 ≈ 7.9s）——**grammar 物理禁止 730-tok runaway**，實測 13.6s/10.4s 尾巴消滅。64 codepoint 對一行推理夠用（idle 空轉才會撞上限；正常推理中位遠低）。`speak ::= [^<]+` **維持無上限**（只綁 think，絕不綁她講多少話）。

> **注意（R1-1）**：行長綁定只保證 **think-token** 長尾消滅。`latency_total` 長尾理論上仍可能由**超長口語**造成（實測未見：speak 中位僅 18 tok）。§7 驗收據此分兩條：think-token 長尾（Part 1 保證）與 total 長尾（監測；若未來出現長口語尾，另設 `max_tokens` 或 speak 上限，不在本 spec 預先加）。

### §3.2 分離遙測（turn 級，非 per-call；R1-4）
現行 `LLMCallRecord` 是 **per-call**（pass1/pass2 各一筆，`transport.py`），且無 think/speak 分離、無開口時刻。新增 **turn 級**遙測記錄（`data/telemetry/turn_latency-*.jsonl`），epoch 明確定義為 **`_llm_iterate` 進入時刻**：
- `first_speak_ms`：進入 → 第一個完整口語句子送進 sink 的牆鐘（無口語則 `null`）。
- `think_tok` / `speak_tok`：本回合（跨所有 pass 累加）think 段 vs speak 段的 token 數（從串流 parser 已能區分 `<think>` 內外）。
- `total_ms` / `ttft_ms`（pass1）/ `mode`（"reflex"/"deliberate"，Part 2 後有值，Part 1 恆 "deliberate"）/ `n_passes` / `had_tool_call`。
- 用途：Part 2 起手前讀真 chat-turn `think_tok` 分佈與 `first_speak_ms`；A/B 驗收（§7）。

---

## §4 Part 2：reflex 短路（依賴 Part 1 遙測）

### §4.1 routing = perception-typed code 閘（`mind/think_mode.py`，新純函式）
```python
REFLEX_ELIGIBLE_KINDS = frozenset({"UserSpoke", "ChannelMessage"})  # allowlist

def decide_think_mode(perceptions, *, safe_mode, reflex_enabled) -> str:
    if not reflex_enabled: return "deliberate"
    if safe_mode:          return "deliberate"   # 降級中多想不省
    if not perceptions:    return "deliberate"
    if all(p.kind in REFLEX_ELIGIBLE_KINDS for p in perceptions): return "reflex"
    return "deliberate"
```
**allowlist（非 denylist）**：新增任何 `Perception.kind` 預設落 deliberate（fail 向多想），永不誤放新型高風險感知進短路。對抗審已查證：`{UserSpoke, ChannelMessage}` 恰為 kind 全集中僅有的兩個純對話 kind；`drain_grouped` 依 `channel_id` 分 bucket → UserSpoke（無 channel_id）與 ChannelMessage（有）**永不同批**，危險 kind 混入任一 bucket 即 `all()` False → deliberate。co-batch/權限/窄化互動皆無安全洞（見 §11）。

### §4.2 reflex grammar + **prompt↔grammar 矛盾修補（R3-Critical）**
問題：§2.1 不動 prompt，故 scaffolding.jinja **仍教 5 欄位 think（SEEN 打頭）+ 綁 TOOL 欄位**，但 reflex grammar 只准 1 行 → 模型被 prime 寫 SEEN 卻被強制進 INTENT 行（形狀對、語意錯），且「TOOL 欄位→欠 tool_call」的綁定線索指向不存在的欄位。**必修**：
1. **scaffolding 加一句 always-present、cache-safe 的 note**（不 per-turn 改、不動 cache prefix 結構、對 TTFT 可忽略）：說明「當 think 區塊很短時，只寫一行 `INTENT:` 陳述你要做/回什麼；短 think 回合直接下決定、不需 TOOL 欄位」。消除矛盾，且維持「act-not-narrate」錨。
2. reflex 欄位 **由 Part 1 資料定案**，smoke 並測兩臂（§8）：
   - **INTENT-only**：`think ::= "INTENT: " line "</think>\n\n"`
   - **INTENT+MOOD**：`think ::= "INTENT: " line "MOOD: " sline "</think>\n\n"`（MOOD ≈8 CJK tok≈0.13s，直接復原「排練→叫 MoodTool」鏈；資料若顯示 MoodTool 呼叫率掉就採此臂）
   - cap 由 Part 1 的 INTENT 實際長度分佈定（**不憑空假設「~15 tok」**，R1-6）。
3. reflex **保留完整 `segments`（speak | tool-call）**——「閒聊要工具」照樣能叫。**但不宣稱「grammar 強制工具呼叫」**（R3-Crit 2：grammar 只*允許*不*強制*；真正逼模型叫工具的 TOOL 行+prompt 綁定已弱化）→ §8 smoke **必量隱性工具需求的實際 tool-call 送出率**，不只測 explicit「跑 ls」。
4. **不強制 speak-first**（R1-5）：reflex 若模型先 tool-call 再 speak，first-speak 會含工具往返 → §7 驗收只計 **speak-first 單-pass 回合的 median**，並用 §3.2 遙測量 tool-first 佔比（不硬改 grammar 逼 speak-first，那會傷「閒聊要工具」回合）。

### §4.3 no-fallback（grammar build 失敗 raise，§11）
任一 mode build 失敗 raise，永不吞成 `grammar=None`。

---

## §5 Wiring（含對抗審修正）

### §5.1 `_run_one_turn`（`mind_loop.py`）——mode 決策**硬釘在 safe_mode clear 之前**（R2-2，load-bearing）
`decide_think_mode` 呼叫**必須**放在 :502「user 講話清除 safe_mode」**之前**（緊接 :484 `_diary_in_batch` 段）——否則剛從失敗風暴恢復的回合會讀到 `safe_mode=False`→reflex，正是最該多想時打掉護欄。此順序為 load-bearing，**加 regression test**（進 safe_mode → 餵 UserSpoke → 斷言 mode=deliberate）。
```python
self._think_mode = decide_think_mode(
    perceptions, safe_mode=self._state.safe_mode,
    reflex_enabled=self.settings.latency.reflex_enabled,
)   # ← 必在 line-502 clear 之前
```
`__init__` 初始 `self._think_mode="deliberate"`（每回合此 assignment 即 reset）。

### §5.2 `_active_grammar`（`mind_loop.py:1084`）——mode 進 cache key，**修正死的熱路徑（R2-1）**
真實 reflex 回合的 registry **不是裸 base**：owner `UserSpoke` → `base ∪ {LearnName}`；external `ChannelMessage` → `EXTERNAL_TOOLS` 子集。故 `__init__` 預建 `_base_tool_key`+"reflex" 的熱路徑**永不命中**。改法：**不假裝 reflex 有 __init__ 熱路徑**——mode 併入 cache key，reflex 每個 distinct registry 首次冷建、之後 cache 命中（成本一次性）：
```python
def _active_grammar(self):
    tools = self._active_tool_registry()
    key = (frozenset(tools.keys()), self._think_mode)
    if key == (self._base_tool_key, "deliberate"):
        return self._grammar                      # deliberate 熱路徑（活的：internal 非對話回合）
    cached = self._grammar_cache.get(key)
    if cached is None:
        cached = build_voice_first_grammar(list(tools.values()), mode=self._think_mode)
        self._grammar_cache[key] = cached
    return cached
```
（可選暖 cache：__init__ 用**真熱 reflex key** `frozenset(base|{"LearnName"})` 預建，包在既有 `if self._tool_registry` 守衛內——R2-3，避免空 registry 啟動 raise。）`_grammar_cache` key 由 frozenset 改 `(frozenset, mode)` tuple；型別一致、無誤命中（對抗審查證）。

**互動安全**：`_is_agenda`/`_is_diary`/`_is_reflection` 為真的回合，其 kind 不在 allowlist → **保證 deliberate**，既有 narrowing/suppression 完全不受 reflex 影響。`_active_tool_registry` **從不讀 `_think_mode`**（查證）→ reflex 零工具權限變動。

---

## §6 Config
`config.py` 新 `LatencyConfig`（仿 `DiaryConfig`）：`reflex_enabled: bool = True`（一鍵回滾全 deliberate）。**唯一 config 面**；line-cap 為 module 常數。Part 1 上線時 `reflex_enabled` 尚無效果（Part 2 才讀）。

---

## §7 驗收與量測

### §7.1 開口點事實（R1-3）
`_handle_stream_event → chunker.feed → _emit_sentence → sink.put_nowait` 只在**整句完成**時 put。故 first-speak = TTFT + think decode + **第一句自身 decode**。§3.2 的 `first_speak_ms` 直接量這個牆鐘（不用公式估），故第一句 decode 自動含在內——驗收看實測值，非模型推算。

### §7.2 驗收條（A/B on `turn_latency-*.jsonl`）
- **Part 1**：think-token p90/max 顯著下降；**全體無 think-token 造成的 8s+**（實測 13.6s/10.4s idle 尾消滅）。
- **Part 2**：reflex 的 **speak-first 單-pass 回合 `first_speak_ms` 中位 ≤ 2000ms**；deliberate `first_speak_ms` 中位不劣化。
- 誠實界線：TTFT 1.6s 是地板（目標句排除縮 prompt）→ reflex 只能逼近地板+第一句 decode；若 Part 1 資料顯示典型 chat think + 第一句 decode 使 median 落在 2.0–2.2s，回報使用者調整目標或收緊 cap，**不偷改定義蒙混**。

---

## §8 品質紅線 guardrail（軟機制必 live smoke；對抗審大改）
deliberate 幾乎不變 → 唯一要驗「純對話短 think vs 5 行」。§8 舊版單發+主觀+無基線**不足**（弱模型每回合機率性踩 reflex，一次幸運 pass 蓋不掉 40% 失敗率）。改為：
- **配對 A/B**：同輸入 `reflex_enabled` on vs off 並排比對（非絕對目測）。
- **重複取樣 N≥10/探針，報比率**（非 1-shot pass/fail）。
- **客觀指標**（code 可測，非主觀）：
  - `MoodTool` 呼叫率 on/off delta（情緒紅線，R3；掉了就採 INTENT+MOOD 臂）。
  - 「隱性工具需求」回合的實際 tool-call 送出率（R3-Crit 2）。
  - 人格護欄違規率：`banned_substrings=["a~"]`、`max_exclaim_run` 等既有 code 檢查當語氣扁平/出戲 proxy。
- **探針集**（比舊版廣）：
  1. 閒聊（人格/語氣）
  2. explicit 工具（「跑 ls」）+ **隱性工具**（「你還記得我剛講的嗎」→Recall、「幫我記著」→NoteMemory）
  3. Self-First 探針（「我愛冰美式，你呢」）
  4. mood-shift 探針（講難過事）→ 量 MoodTool 呼叫率
  5. **≥4 輪連續對話**（脈絡延續 + 情緒跨回合累積 + 不出戲；覆蓋 SEEN grounding 損失，舊版全缺）
  6. 工具結果回合（deliberate 未壞）
- **判準**：任一客觀指標明顯劣化 → 採 INTENT+MOOD 臂 / 放寬 cap / 加欄位；仍不行 `reflex_enabled=false` 待重設計。

---

## §9 風險 + v1 簡化（誠實標註）
1. **mode per-turn（非 per-pass）**：reflex 回合中途進 safe_mode → pass 2 仍 reflex，但 `SAFE_MODE_TOOLS`（Recall/Read/Grep）全唯讀，**零能力洩漏**；下一回合 `SafeModeEntered`（非 allowlist）→ deliberate 自動糾正（R2-4，附註不修）。
2. **閒聊需工具首次無 TOOL 鋪墊 + prompt 綁定弱化** → 可能不叫工具（R3-Crit）。緩解靠 §4.2-1 的 note + §8 隱性工具指標把關；資料若顯示送出率掉，Part 2 不放行。
3. **reflex ≤2s 極邊際**（含第一句 decode，R1-3/C3）：靠 §7.1 實測 + cap 由資料收緊，不靠公式蒙混。
4. **TTFT 1.6s 殘留地板**（目標排除縮 prompt）：reflex 只能逼近地板+ε，非缺陷。

---

## §10 實作單元（→ writing-plans 細化）

**Part 1（先做，可獨立 merge）**
| # | 單元 | 檔案 | 審級 |
|---|---|---|---|
| 1 | deliberate `line`/`rline` 綁定 + `mode` 參數骨架 | `llm/templates.py` | grammar 正確性 → opus |
| 2 | turn 級分離遙測（think/speak/first-speak，epoch 定義） | 遙測寫入點 + `mind_loop` 串流迴圈 | 整合 |
| 3 | **啟動 grammar 能力檢查**（送一次 `{1,n}` grammar 探 llama.cpp，fail-closed；R1-7） | 啟動路徑 | 標準 |

**Part 2（讀 Part 1 資料後）**
| # | 單元 | 檔案 | 審級 |
|---|---|---|---|
| 4 | `decide_think_mode` + `REFLEX_ELIGIBLE_KINDS` allowlist | `mind/think_mode.py`（新） | routing 安全 → opus |
| 5 | reflex grammar 分支（INTENT-only / +MOOD 兩臂） | `llm/templates.py` | opus |
| 6 | scaffolding 短-think note（cache-safe，消 prompt↔grammar 矛盾） | `character_packs/*/scaffolding.jinja` 或注入點 | opus |
| 7 | wire `_think_mode`（**釘 safe_mode 序** + regression test）+ `_active_grammar` mode-key + 空-registry 守衛 | `mind/mind_loop.py`、`config.py` | 整合 → opus |
| V | 配對 A/B smoke + telemetry 驗收（§7/§8） | 人工 | 驗收閘 |

whole-branch opus 審各 Part 收尾。

---

## §11 安全 / no-fallback
- routing 硬 code 閘 + **allowlist**，非 prompt 勸導；新 kind 預設 deliberate（fail-safe）。
- grammar build 失敗 raise（§4.3）；**啟動時探 GBNF `{1,n}` 能力**，server 過舊 fail-closed（不讓每回合請求級 HTTP error 假扮成功，R1-7）。
- reflex **不擴任何工具權限、不改 origin_tier、不繞 external_public 記憶隔離**——只換 think 長度（`_active_tool_registry` 不讀 mode，查證）。
- **safe_mode 恢復回合強制 deliberate**（§5.1 序 load-bearing + test）。
- 品質為軟性質 → **live smoke 才算數**，留一鍵 `reflex_enabled=false` 回滾。
