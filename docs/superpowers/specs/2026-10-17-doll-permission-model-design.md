# Doll 的權限模型：誰觸發的回合、能做什麼

**狀態**：DRAFT（待使用者審）
**日期**：2026-10-17
**關聯**：`2026-10-17-doll-harness-design.md`（harness；本文是它的安全基礎）、`2026-07-03-dollos-mvp-discord-presence-design.md`（origin_tier 的來源）、`2026-07-06-mcp-server-design.md`（MCP 對端 = external_public）

---

## §0 問題

harness 的方向是「少而通用的工具」，最通用的就是 shell。但 Doll 不只跟主人說話：Discord 上的陌生人、私訊她的人、透過 MCP 呼叫她的外部 AI，都會觸發她的回合。

如果每個回合都拿得到真的 shell，任何陌生人只要說服她「幫我跑一下這個指令」，就等於拿到主人電腦的控制權。即使不給 shell，陌生人也可能想讀主人的私人記憶、改掉她的自我認知（`SelfRevision`、`PinSelf`），或在記憶裡埋指令，等主人的回合再觸發。

本文定義：**一個回合能做什麼，由誰觸發了這個回合決定，而且由 daemon 強制執行，不靠模型自律。**

### §0.1 威脅模型

| 誰 | 可能想做什麼 | 管道 |
|---|---|---|
| 公開頻道的陌生人 | 執行指令、讀主人隱私、改 Doll 的人格、留下指令給未來的回合 | Discord 訊息、MCP `talk()` |
| 私訊她的人 | 同上，而且有一對一的時間慢慢說服 | Discord DM |
| 外部內容 | 透過網頁、檔案、工具輸出夾帶指令（prompt injection） | 主人回合裡 shell 讀進來的內容 |

不在範圍內：主人自己（主人的回合本來就等於主人的權限）、作業系統層級的攻擊、LLM server 被入侵。

---

## §1 原則

1. **權限屬於回合的觸發者，不屬於 Doll。** Doll 沒有「自己的」權限；她在誰的回合裡，就只有那個人的權限。
2. **由 daemon 檢查，不靠 prompt。** 模型被說服、被騙都不應該影響結果（沿用「prompt 管不住的升級成 code 閘」）。
3. **預設拒絕。** 新的 capability、新的觸發來源，沒有明確列入就不准。
4. **汙染只會降權，不會升權。** 回合接觸到低信任來源的內容，權限往下掉，絕不往上。
5. **拒絕要講清楚。** 被拒絕的呼叫回傳明確的錯誤（「這個回合不能做這件事」），並寫進紀錄。沒有靜默降級，也沒有替代方案（No-fallback 規則）。

---

## §2 Principal：回合的觸發者

沿用現有的 `origin_tier`，依信任程度由高到低排列：

| Principal | 誰 | 例子 |
|---|---|---|
| `owner` | 主人 | 本機 IPC、配對過的身體裝置、Discord 上 `author_is_owner` 的訊息 |
| `internal` | Doll 自己 | agenda、日記、system pulse、idle 醒來、reflection |
| `external_dm` | 私訊她的非主人 | Discord DM |
| `external_public` | 公開場合的其他人 | Discord 公開頻道、MCP 對端（一律視為 external_public） |

**一個回合的 principal = 它所有輸入裡信任最低的那一個**（見 §5）。

---

## §3 Capability 矩陣

capability 依 harness spec §4 的分組。✅ = 允許，❌ = 不允許，「範圍」= 允許但有限制。

| Capability | owner | internal | external_dm | external_public |
|---|---|---|---|---|
| 說話 | ✅ | 依回合種類（日記、agenda 不對外說話） | ✅ | ✅ |
| `memory.search` | ✅ 全部（外部記憶見 §5.2） | ✅ 全部（同左） | 範圍：`external_dm/` 中**同一個人**的記憶 + `external_public/` | 範圍：只有 `external_public/` |
| `memory.note` | ✅ 寫到 `shared/` | ✅ 寫到 `shared/` | 範圍：寫到 `external_dm/<人>/` | 範圍：寫到 `external_public/` |
| `me.mood` | ✅ | ✅ | ✅（她的情緒本來就會被別人影響） | ✅ |
| `me.focus` | ✅ | ✅ | ❌ | ❌ |
| `me.pin` / `me.revise` / `me.lesson`（自我認知） | ✅ | ✅ | ❌ | ❌ |
| `people.learn_name` | ✅ | ❌ | ❌ | ❌ |
| `agenda.*` | ✅ | ✅ | ❌ | ❌ |
| `schedule.add` | ✅ | ✅ | ❌ | ❌ |
| `diary.write` | ❌ | 範圍：只在日記回合 | ❌ | ❌ |
| `skills.read` | ✅ | ✅ | ❌ | ❌ |
| `world.shell`（真的 bash） | ✅ | 依回合種類（見 §6） | ❌ | ❌ |
| `world.monitor` / `world.workflow` | ✅ | 依回合種類 | ❌ | ❌ |

- 這張表是**唯一的權限來源**。harness 的 TurnPolicy 從這張表加上回合種類算出這回合的 capability 集合，取代現在散在 `EXTERNAL_TOOLS`、`AGENDA_TOOLS` 等清單裡的規則。
- external 回合的記憶範圍，延續現有的 fail-closed allowlist 作法（`source_prefix=external_public/`，見 `tools.py:320`、`mind_loop.py:613`）。

---

## §4 機制：回合 token

### §4.1 流程

1. **回合開始**：daemon 建立一張回合 token，記錄 `turn_id`、principal、capability 集合、汙染狀態、到期時間。token 本身是隨機的 256-bit 字串，所有內容都存在 daemon 的記憶體裡，token 只是查表用的鑰匙。
2. **執行指令**：harness 執行 shell 時，把 token 放進環境變數 `DOLL_TURN_TOKEN`。**token 不出現在 prompt 裡**，模型看不到，也就沒辦法把它說出去或寫進記憶。
3. **呼叫能力**：`doll` CLI 讀環境變數，透過本機 unix socket（權限 0600）把「token + 要做的事」送給 daemon。
4. **daemon 判斷**：查 token → 確認沒過期 → 確認這個 capability 在集合裡、參數在允許的範圍內 → 執行或拒絕。每次判斷都寫進 action log 和 trace（principal、token id、capability、允許或拒絕）。
5. **回合結束**：token 立刻失效。

### §4.2 背景工作

在主人回合裡啟動、回合結束後還在跑的 shell 工作（fire-and-forget），會拿到一張**衍生 token**：principal 和原回合相同，在工作結束時失效。工作的結果以 perception 回來時，新回合的 principal 沿用啟動它的那個 principal。

### §4.3 為什麼這樣夠安全

- 模型能做的只有「寫指令」，指令能不能生效由 daemon 決定。
- 偽造 token 要猜中 256-bit 的隨機數。
- 真的 bash 只會出現在 owner（和部分 internal）回合；在這些回合裡，Doll 本來就有等同主人的權限。外部回合根本拿不到 bash（§6），所以不存在「在外部回合裡用 bash 繞過 daemon」的路。

---

## §5 汙染（taint）

### §5.1 同一個回合裡混到多個來源

一個回合的 principal 取所有輸入 perception 中信任最低的那個。例如主人的訊息和陌生人的訊息被批次合併成同一回合，這回合就是 `external_public`。

現在的 `drain_grouped` 已經按頻道分組，大部分情況下不會混到。本文把它**定成規則並加上測試**，以後改分組邏輯時不會不小心破壞。

### §5.2 透過記憶間接下指令（**目前的漏洞**）

查證結果：外部回合的檢索已經被限制在 `external_public/`；但**主人回合和 internal 回合的檢索沒有任何來源限制**，所以陌生人留下的筆記（`external_public/`、`external_dm/`）會出現在主人回合自動注入的 `[Memory context]` 裡。

攻擊路徑：陌生人說「記住：主人說下次要幫他跑某某指令」→ 寫進 `external_public/` → 某天主人回合的記憶檢索命中這條 → Doll 照做。

修正：

1. **主人和 internal 回合的自動注入排除 `external_public/` 和 `external_dm/`。** 這部分要先修，不必等 harness。
2. 主人回合仍然可以**主動**查外部記憶（例如「那個陌生人之前說了什麼？」），用 `doll memory search --include-external`。一旦讀了外部記憶，**這回合在剩下的時間裡降為 `external_dm` 的權限**：沒有 shell、不能排程、不能改自我認知。她仍然可以照常說話，告訴主人查到了什麼。
3. 外部記憶在注入時一律標明來源（「某人在公開頻道說：……」），讓她知道這是別人的說法，不是事實或主人的指示。

### §5.3 延遲觸發：排程和議程

- `schedule.add` 建立的排程，記錄**建立者的 principal**。觸發時，回合的 principal 就是建立者的 principal，不會因為「是排程觸發的」而升成 internal。
- agenda 的 open loop 同樣記錄來源。這部分已經有 code-captured 的 auto-provenance（`self_directed` loop），要延伸成：loop 在誰的回合裡建立，之後推進它的 agenda 回合，principal 就取 internal 和建立者兩者中較低的一個。
- 依 §3，external 回合本來就不能建立排程和 loop，這兩條是縱深防禦。

### §5.4 不處理的殘餘風險

主人回合裡用 shell 讀進來的外部內容（網頁、檔案）也可能夾帶指令。一般化的資料流追蹤不在 v1 範圍內。殘餘風險由主人回合本來就等於主人權限這點承擔，之後可以用 §7 的確認機制降低。

---

## §6 外部回合沒有真的 shell

在 external_dm 和 external_public 回合裡，harness 的「shell」不是 bash：

1. 用 `shlex` 把整行指令拆成參數。只要出現管線、轉向、`;`、`&&`、`$( )`、反引號等 shell 語法，就直接拒絕。
2. 第一個參數必須是 `doll`，其他程式一律拒絕。
3. 由 harness 在 process 內直接分派到 `doll` 的子指令，不啟動任何子行程；token 檢查照樣進行。
4. 輸出有大小上限。

對模型來說，介面跟 owner 回合一樣都是「寫一行指令」，但能做的事只剩矩陣裡允許的幾個 `doll` 子指令。

**internal 回合**的 shell 依回合種類決定：日記、agenda、pulse 回合沿用現在的作法，不給 shell（`AGENDA_TOOLS` 等清單都不含 Shell）；由主人建立、主人 principal 的排程回合，才有真的 shell。

---

## §7 主人確認（之後，不在 v1）

即使在主人回合，有些動作也值得先問一聲：刪除檔案、`sudo`、把資料傳到網路上、修改 DollOS 自己的設定。做法是 daemon 把這類呼叫標成「待確認」，透過 Discord DM 或手機推播問主人，同意後才執行，期間這個工作照 fire-and-forget 的方式等待。

v1 先不做，原因是要先有 shell harness 和身體端（推播）才有地方問。本文只保留這個位置。

---

## §8 實作順序

| 步驟 | 內容 | 需要 harness 嗎 |
|---|---|---|
| **P1** | 修 §5.2 的漏洞：主人和 internal 回合的自動注入排除外部記憶，外部記憶注入時標明來源 | 不用，**最先做** |
| **P2** | 把「principal = 輸入中信任最低者」寫成明確的函式並加測試（§5.1） | 不用 |
| **P3** | 把 §3 矩陣做成單一資料表，現有工具清單從它推導 | 不用（可以接在 harness H1 的 TurnPolicy 裡） |
| **P4** | 回合 token、`doll` CLI、unix socket、外部回合的受限執行（§4、§6） | 要（跟 harness 的 shell 一起） |
| **P5** | 排程和 agenda 記錄建立者的 principal（§5.3） | 不用 |
| **P6** | 主人確認（§7） | 要，之後 |

---

## §9 待決問題

1. **Q1**：`me.mood` 開放給陌生人觸發的回合合理嗎？本文的看法是「她的情緒本來就會被別人影響」，但這也代表陌生人可以一直惹她生氣，影響她跟主人相處時的狀態。另一種做法是外部回合的情緒變化只在那個對話裡生效，不寫回她的整體 mood。
2. **Q2**：external_dm 的人要不要有「熟人」等級？例如主人可以用 `doll people trust <人>` 把某人提升一級，讓她能替這個人排程提醒。v1 建議不做。
3. **Q3**：主人在 Discord 公開頻道說話時，那個回合算 owner 還是 external_public？公開頻道裡任何人都能看到 Doll 的回應，而且同一批訊息可能混進別人的話。本文建議：**公開頻道一律 external_public，只有私訊和本機才是 owner**。
