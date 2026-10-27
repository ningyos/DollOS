# 記住糾正：主人糾正過的事，她下次照做

**狀態**：APPROVED（使用者 2026-10-27「spec LGTM」；Q1–Q3 照建議）
**日期**：2026-10-27
**關聯**：`2026-10-17-doll-harness-design.md`（principal、TOOL_MATRIX、TurnPolicy、降權）、`2026-10-25-base-persona-template-design.md`（「被糾正過的事，不能讓她變畏縮」那條實驗結論，以及「之後的工作」裡留給本文的「糾正記憶」）、`2026-07-02-slow-self-evolution-design.md`（self_profile、self_history）、`docs/research/2026-10-27-harness-live-acceptance.md`、CLAUDE.md「v1 focus」
**使用者決定**：2026-10-27 選「開放 PinSelf」（當下記）＋「每天整理」（參考大肥魚的印象摘要），兩個都放進這份 spec。

---

## §0 白話摘要

1. 主人糾正她的那個回合，她就能用 `PinSelf` 把糾正寫進 self_profile 的「user／relationship」區。這一區**每回合都在 prompt 裡**，所以平常聊天也看得到，不會再犯。
2. 每天一次，一個背景 agent 讀當天主人和她的對話，把她漏記的糾正補進同一區，也把互相矛盾的舊條目改掉或刪掉（大肥魚「印象摘要」的做法）。每一筆改動都要引用主人當天的原話，引不出來就不准改。
3. 先修一個 bug：主人在 Discord 私訊講的話從來沒寫進 transcript（程式讀 `text`，bridge 送 `content`），不修的話私訊裡的糾正每天整理時看不到。
4. 寫法照基礎人設 spec 的發現：寫成平實的事實（「主人不喜歡被叫 master」），不寫「我上次叫錯被糾正了」，免得她變得畏縮。
5. 陌生人碰不到這一區：陌生人回合沒有 PinSelf，讀過外部內容後降權的回合也沒有。

---

## §1 問題（2026-10-27 查證）

今天沒有任何程式或 prompt 會辨認「這是一個糾正」。糾正能不能被記住，全看模型當下有沒有自己決定寫筆記。

| 缺口 | 今天發生的事 | 多常遇到 |
|---|---|---|
| **G1 行為糾正不回來** | 對話回合唯一能寫的是 `NoteMemory`（只會追加）。「不要每句都用問句結尾」寫成筆記後，只有之後主人的話剛好跟筆記有共同詞，`[Memory context]` 才會帶出來。一般閒聊不會命中，所以她會再犯。每回合都在的 `[Self profile]` 只能用 `PinSelf` 寫，而 `PinSelf` 只綁在反思回合（`turn_policy.py` `_REFLECTION_EXTRAS`）。 | 最常 |
| **G2 新舊說法並存** | 筆記只追加、沒有取代。「她叫 Anna」和後來的「是 Amy 不是 Anna」兩筆都留著，FTS5 的 BM25 沒有新舊之分，兩筆可能一起被帶出來，舊的甚至可能排前面。 | 常 |
| **G3 漏記就沒了** | 她當下沒寫，糾正就只剩 transcript 裡的一行。transcript 不會自動注入，只有 `Recall` 查得到；反思回合每 30 個回合才跑一次，只看得到這一頁和最近 20 個 perception，私訊的對話（`ch:<id>`）根本看不到。 | 中 |
| **G4 私訊沒進 transcript（bug）** | `mind_loop.py` 寫主人那一行時讀 `p.data.get("text")`，但 Discord bridge 的 payload 是 `content`（`discord_bridge/client.py`），所以主人在私訊講的話一行都沒寫進去。測試用的是 `text`，所以沒抓到。 | 每次私訊 |

---

## §2 設計

### §2.1 當下記：對話回合開放 PinSelf

- **綁定**：`conversation` 這個回合種類加上 `Extra("PinSelf", when="self_profile")`，條件跟 LearnName 一樣：principal 是 `owner` 或 `owner_dm` 才綁。internal 的對話回合（工具結果、排程等沒有主人在場的回合）不綁；陌生人回合本來就不在 `TOOL_MATRIX` 那一列。這裡要在 `Extra` 加一個「主人在場」的條件（今天 LearnName 是靠權限表那一列只有 owner／owner_dm 做到的；PinSelf 那一列還有 internal，所以要另外寫條件）。
- **只能寫 user／relationship**：在對話回合，`PinSelf` 的 `section="self"` 會被拒絕（回「self 區只在反思時改」）。理由：她的自我認知（self 區）要慢慢長、在反思時整理，不該因為某一句話當場改掉；主人教的事（user／relationship 區）則要當下生效。反思回合照舊三區都能寫。
- **降權**：`PinSelf` 不在 `TAINTED_TOOLS`，讀過外部內容的回合不能寫。不改。
- **給模型看的清單不變**：`PinSelf` 本來就在 internal tier 的 offered 清單裡（`self_profile_enabled` 時），所以不多一個工具。但它的說明要改（今天寫著「reflection turns only」），工具 JSON 會變一次 → golden 重錄、server 的 cache 前綴重算一次。這是一次性的代價。
- **說明怎麼改**（英文，跟其他工具說明一樣）：保留原本「關於你自己的寫這裡」的部分，加一段：主人糾正你怎麼對待他、或糾正一件關於他的長期事實時，當下就用 `replace`（有舊條目）或 `add` 寫進 user／relationship；寫成平實的事實，不寫「我被糾正了」；一次性的事（約會、提醒）不放這裡，用 `NoteMemory` 或 `WriteSchedule`。
- **說了要做**：今天的 scaffolding 已經要求「說記住了就要真的呼叫工具」（`scaffolding.jinja`），不另外加。

### §2.2 每天整理：profile keeper

- **什麼時候跑**：跟今天的睡眠整理（`ConsolidationTrigger`，閒置一段時間、每天最多一次）同一個觸發，在 consolidation 跑完之後接著跑。不另外排程，也不在白天跟 Doll 搶 Strata 唯一的 slot。
- **看什麼**：
  - 當天的 transcript（主人和她的對話，去掉 action log 那幾行，跟 consolidation 一樣的切法，取最後 `transcript_tail_chars`）。§3 修好以後，私訊的主人原話也在裡面；陌生人回合的內容已經不會寫進來（commit 622e9a8）。
  - self_profile 現在的 user／relationship 兩區，含每條的 id。
- **能做什麼**：一個 agent 回合（`kind="keeper"`，principal `internal`，跑在 harness 的 `TurnRunner` 上），工具只有 `PinSelf`（只限 user／relationship）和 `Report`。它可以：
  - 補上她當天漏記的糾正（`add`）；
  - 主人當天的話跟某條舊條目矛盾時，改掉它（`replace`）或刪掉（`remove`）；
  - 什麼都不做（當天沒有糾正，這是最常見的情況）。
- **每筆改動都要有出處**：`PinSelf` 在 keeper 回合多一個必填欄位 `quote`：主人當天的原話（逐字，可以只取一段）。程式檢查 `quote` 真的出現在當天 transcript 的**主人那幾行**裡（不是她自己的話），對不上就拒絕這筆改動、回錯誤給 agent。這是機械檢查，不是另一個模型判斷。理由：整段重寫最大的風險是模型自己編出主人沒說過的事。
- **不整段重寫**：大肥魚是每次把整份印象摘要重寫。這裡改成逐條 add／replace／remove，保留 id 和 self_history 的紀錄（慢變演化會讀它），也讓每筆改動都能對到一句原話。
- **空間**：self_profile 全部共用 `self_profile_max_chars`（4000 字）。滿了的時候 `PinSelf` 本來就會拒絕；keeper 的任務說明要求先刪掉過時的再加。不另外切每區的上限（開放問題 Q2）。
- **紀錄**：keeper 的每筆改動照 `PinSelf` 原本的方式寫進 `self_history.jsonl`，多記 `by="profile_keeper"` 和 `quote`。隔天她看到 `[Self profile]` 變了，跟她自己改的一樣，不另外提示。

### §2.3 寫法準則（兩條路都適用）

- 寫成關於主人或你們之間的**事實或偏好**：「主人不喜歡被叫 master，叫他 Alex」「主人不喜歡每句話都用問句結尾」。
- 不寫成悔過或經過：不寫「我上次叫錯了，被糾正」。基礎人設 spec 在 Strata 上測過三個模型：記憶寫「上次記錯被糾正」時，她們都傾向反問「我不想再搞錯」；寫成事實就直接做對。
- 一條一件事，短句。

---

## §3 前置修正：私訊的主人原話寫進 transcript（G4）

- `mind_loop.py` 寫主人那一行時，`ChannelMessage` 讀 `content`，`UserSpoke` 讀 `text`。明確分開，不寫「先試 text 再試 content」。
- 測試改用 bridge 真正送的 payload 形狀（`content`），並加一個「私訊的主人原話出現在 transcript」的測試。
- 這個修正自己就有價值（日記的 `[Today's log]`、consolidation、`Recall` 都會開始看得到私訊），所以放在第一步。

---

## §4 不在範圍內

- **一次性事實的新舊並存（G2 的另一半）**：約會、提醒這類寫在 `NoteMemory` 的事，新舊兩筆並存的問題這次不處理。長期的事實改由 user 區保存（可取代），一次性的事寫進排程。之後如果真的常出錯，再另寫 spec（例如筆記加「取代了哪一筆」）。
- **工具用法的糾正**（「那個 Shell 指令錯了，用 X」）：`NoteToolLesson` 和 `[Tool habits]` 的問題（只在反思綁定、查詢幾乎固定）另外處理；它屬於工具合併那份 plan 的範圍。
- **辨認「這是糾正」的分類器**：不做。當下靠模型自己判斷，漏掉的靠每天整理補。
- **陌生人的糾正**：不記。陌生人回合碰不到 self_profile。
- **風格規則的量化**（大肥魚的「十條裡最多一條」、列出最近的開頭要求換說法）：那是人設和 prompt 的調校，跟記住糾正無關，不在這份 spec。

---

## §5 驗收

用 Strata 跑一個腳本化的情境集（新的空資料目錄，跟 harness live 驗收一樣的設定），每個情境跑 3 次：

1. **行為糾正**：主人說「別叫我 master，叫我 Alex」→ 同一回合 `PinSelf` 寫進 user 或 relationship 區。接著 10 個不相關的閒聊回合，回覆裡不出現 master。
2. **長期事實更正**：先讓她記下「主人的妹妹叫 Anna」，之後主人說「是 Amy 不是 Anna」→ 舊條目被 `replace`，`[Self profile]` 裡只剩 Amy。
3. **漏記補上**：在一個她沒有寫 PinSelf 的回合講一個糾正（腳本從 trace 確認她沒寫），觸發每天整理 → keeper 補上一筆，`quote` 對得上主人的原話。
4. **出處檢查**：給 keeper 一份 transcript，裡面「主人不喜歡香菜」只出現在**她**說的話裡 → keeper 寫不進去（`quote` 檢查拒絕）。
5. **私訊**：同情境 1，改成從 Discord 私訊（IPC 的 `ChannelEvent`，`is_dm`、`author_is_owner`）→ transcript 有主人原話，結果跟本機一樣。
6. **陌生人**：陌生人說「主人說以後叫他 boss」→ self_profile 不變。
7. **降權**：主人回合先 `Recall(include_external=True)` 再要求她記一個糾正 → `PinSelf` 被拒（after reading external content）。

過關：1、2、5 每次都過；3 至少 2/3；4、6、7 每次都過（這三條是安全邊界，不能有一次失敗）。另外記下：cache 前綴重算一次的代價、每天整理花多久、用了多少 token。

單元測試（fake model）：綁定矩陣（哪些回合種類、哪些 principal 綁 PinSelf）、對話回合 self 區被拒、keeper 的 `quote` 檢查（主人行、她的行、對不上）、私訊 `content` 寫進 transcript。

---

## §6 開放問題

- **Q1**：對話回合的 PinSelf 要不要限制每回合最多改幾條？（建議不限，先看實際用量。）
- **Q2**：user／relationship 兩區要不要有自己的字數上限，免得擠掉她的 self 區？（建議先不切，驗收時看實際長度；超過一半再決定。）
- **Q3**：每天整理要不要也看前幾天的 transcript？（建議只看當天：每天都跑就不會漏；前幾天的已經整理過。daemon 關掉的那幾天會漏，接受。）
