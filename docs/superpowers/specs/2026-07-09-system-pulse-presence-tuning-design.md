# System Pulse — 臨場感調校(severity + [Body signal] 框架 + diary 整合)

**Date**: 2026-07-09
**Status**: Design proposed, pending user approval → writing-plans
**Builds on**: `2026-07-09-system-pulse-proactive-trigger-design.md`(已 merge fb1f961)。

---

## 0. 一句話

主動觸發已上線,但 live smoke 顯示她對 battery-critical 喚醒 **3 次只開口 1 次**(另兩次轉內在 mood 沉默)。根因:pulse 喚醒是個**通用內部回合**,沒有「你為何醒來、該不該說」的框架,三條規則也**沒嚴重度區分**。本 pass 用 **Self-First 相容的軟槓桿** 提高她在 critical 時的出聲機率,同時保留她沉默的自由。

## 1. 根因(現況)

- pulse 喚醒回合 = 普通內部回合。`PulseMoment` 只渲染在 `[Recent perceptions]`(其 `detail`),再到 `[Decision time]`「0..N actions」。**無任何發話/沉默框架** —— 唯一會 nudge 的 `[External situation]` block 只在 `external_public` 出現。
- `Alert` 只有 `slug`+`text`,**無嚴重度**:battery-critical 與 window-stuck 對她一樣重。
- 保守人設 + 通用框架 → 預設安靜。
- `PulseMoment` 未進 `action_phrase_for_perception` → 喚醒事件**進不了 diary 的 `[Today's log]`**(她的發話仍進 transcript,但「為何醒來」這個世界事件缺席)。

## 2. 目標 / 非目標

**目標**:critical 喚醒時她**更可靠地**出聲(仍是她的選擇,不是強制);advisory 維持安靜傾向;喚醒事件能被 diary 反思。

**非目標(YAGNI)**:
- 不做程式強制發話(user 拍板 A:軟槓桿、非命令,保 Self-First)。
- 不加新規則(RAM/網路)—— 等實測要了再說。
- 不動觸發政策 / throttle / observer 生命週期(上一 spec 已定)。

## 3. 設計(三塊)

### 3.1 嚴重度(`Alert.severity`)

`Alert` 加 `severity: str`(`"critical"` | `"advisory"`),`evaluate_alerts` 產生時填:

| rule | severity |
|---|---|
| `battery_critical` | critical(可行動、主人多半想知道) |
| `gpu_hot` | critical(硬體風險) |
| `window_stuck` | advisory(輕提醒,安靜也 OK) |

`severity` 一路帶進 `PulseMoment.data["severity"]`(與既有 `concern`/`detail` 並列)。純資料欄位,無行為 —— 行為在 3.2 的框架。

### 3.2 `[Body signal]` 框架 block(核心槓桿)

只在**純 `_is_pulse` 回合**渲染(仿 `[External situation]` 的 caller-gated 前例),放在 `[Decision time]` 之前(決定前最後讀到,最salient)。**描述性、非命令**(Self-First):描述她為何醒來 + 依嚴重度給不同傾向,把選擇留給她。

措辭(初稿,實作時定):
- **critical**(批次含任一 critical):
  > `[Body signal]` 你因為身體狀況醒過來:{detail}。這種事主人多半會想知道 —— 想說就跟他講一聲,不想說就自己記著。你決定。
- **advisory**(批次全 advisory):
  > `[Body signal]` 你注意到:{detail}。不緊急 —— 想順口提一句或默默記著都行。

多條 alert 同批(罕見,throttle 下通常一條)→ 逐條列 `detail`,severity 取最重(任一 critical → critical 框架)。

**為什麼這是對的槓桿**:它給模型兩個現在缺的信號 ——(a)「這個回合是因為身體狀況醒的」;(b)嚴重度。critical 的「主人多半會想知道」把她推向出聲**而不命令**(她仍可沉默,只是先驗機率提高)。這正是 user 選的 A。

### 3.3 Diary 整合(`action_phrase_for_perception`)

加 `PulseMoment` case,回一句世界事件描述(例:`f"身體狀況:{d.get('detail','')}"`)。**無 origin-gating 疑慮**:action-log 的世界事件結構上 owner/internal-only(`mind_loop.py:445-451` 註解),陌生人產不出 `PulseMoment`(daemon-internal/origin-less)—— 與既有 ToolResult/Monitor/BridgeDown 同類。這讓她的「為何醒來」進 `[Today's log]`,日記能反思。

## 4. 資料流 / 接線

```
evaluate_alerts → Alert(slug, text, severity)
   → PulseObserver put Perception("PulseMoment", data={concern, detail, severity})
       → mind_loop 純 _is_pulse 回合:從批次 PulseMoment 抽 (severity, detail)
           → render_mind(body_signal_block=render_body_signal(wakes))  # 只純 pulse 回合傳
               → mind_prompt 插 [Body signal] 於 [Decision time] 前
       → mind_loop world-event loop:action_phrase_for_perception("PulseMoment", data)
           → append_action_log → [Today's log]（diary 反思）
```

- `render_body_signal(wakes: list[(severity, detail)]) -> str` 放 `mind_prompt.py`(措辭/severity→wording 是 presentation)。mind_loop 在純 pulse 回合抽 wakes 並呼叫,把結果當 `body_signal_block` 傳(仿既有 `pulse_block`/`cognition_block` 傳 pre-rendered string 的慣例)。非純 pulse 回合傳 `None` → 不渲染。

## 5. 測試

- `evaluate_alerts`:三規則各帶正確 severity(battery/gpu=critical、window=advisory)。
- `render_body_signal`:critical 用「主人多半會想知道」措辭、advisory 用「不緊急」措辭;多條取最重;空 → `None`。
- `mind_loop`:純 `PulseMoment` 回合 → `[Body signal]` 在 prompt(critical 措辭);co-batch UserSpoke → **無** `[Body signal]`(非純 pulse);severity 正確流到框架。
- `action_phrase_for_perception("PulseMoment", ...)` → 非 None 的世界事件句。
- **Live smoke(人工,承上 spec §9)**:重跑 battery-critical 喚醒 ≥3 次,對照調校前(1/3 出聲)看 critical 出聲率是否提高;advisory(window_stuck)確認仍傾向安靜;確認 diary log 有喚醒事件。軟機制必 live-smoke(`ref_weak_model_soft_mechanism_playbook`)。

## 6. 開放決策(審查請拍板)

- **D1 措辭**:3.2 的 critical/advisory 初稿 OK 嗎?(這是 Self-First 軟槓桿的實際文字,直接決定成效。)
- **D2 severity 分派**:gpu_hot 算 critical 還是 advisory?(GPU 燙是硬體風險但未必要主人立刻處理 —— 我放 critical,可改。)
- **D3 [Body signal] 位置**:放 `[Decision time]` 前(最salient)還是 `[Self pulse]` 旁(跟身體感放一起)?我傾向前者。
