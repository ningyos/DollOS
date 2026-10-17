# Doll Harness：架構、原生 tool calling 與權限模型

**狀態**：DRAFT v3（待使用者審）
**版本紀錄**：v1 用 Python REPL（Monty）當動作介面；v2 改成 shell 優先並併入權限模型；v3（本版）依使用者決定改成**原生 tool calling + MCP 擴充**。shell 和 Python REPL 為什麼不用，見 §11。
**日期**：2026-10-17
**關聯**：`2026-10-17-think-ab-qwen38-design.md`（評分工具共用）、`2026-07-03-dollos-mvp-discord-presence-design.md`（origin_tier 的來源）、`2026-07-06-mcp-server-design.md`（DollOS 已經是 MCP server；本文讓它也成為 MCP client）

---

## §0 為什麼要做

`MindLoop` 實際上就是 Doll 的 harness，但它是一路長出來的，沒有設計過：

- `mind_loop.py` 有 1,837 行，`_run_one_turn` 一個函式就 611 行。
- 每種回合靠旗標區分（`_is_reflection` / `_is_diary` / `_is_agenda` / `_is_pulse`，被引用 43 次），工具範圍散在 7 份清單裡（`MAIN_TOOLS`、`EXTERNAL_TOOLS`、`AGENDA_TOOLS`……）。
- 有兩套 cascade 迴圈（即時回合一套，workflow / 演化一套）。
- 模型介面寫死在 llama.cpp `/completion` + 手寫 Qwen ChatML + GBNF grammar 上。工具呼叫是靠 grammar 夾在輸出文字裡的 JSON，一次只能呼叫一個。
- 25 個工具，每個都要維護 grammar 規則。

---

## §1 一句話設計

> **Doll 的每個回合：思考 → 開口說話（立刻串流到 TTS）→ 需要做事時用模型原生的格式呼叫工具（可以一次多個）→ daemon 執行，把結果回饋給她 → 直到她不再呼叫工具為止。每個回合只把「觸發這回合的人」被允許的工具交給她。新能力用 MCP server 擴充。**

---

## §2 元件

```
                 Perception queue（既有，不變）
                          │
                          ▼
┌─────────────────────── Turn Runner ────────────────────────┐
│ 1. 算出 principal 和 TurnPolicy        這回合是誰的、能做什麼  │
│ 2. ToolRegistry 綁定這回合的工具        內建工具 + MCP 工具     │
│ 3. ContextBuilder 組 messages           模型要看的內容          │
│ 4. ModelAdapter 串流                    思考 / 說話 / 工具呼叫  │
│ 5. ToolExecutor 執行（只接受已綁定的）  結果變成 tool message   │
│ 6. 結果送回 4，直到沒有工具呼叫、到達上限，或被打斷           │
└──────────────────────────────────────────────────────────────┘
        │說話 → sink / TTS        │每次呼叫 → action log / trace
```

| 元件 | 職責 | 取代現在的 |
|---|---|---|
| **TurnPolicy** | 每種回合一份宣告式設定：能不能說話、think 額度、最多幾輪；工具集合由 §6 的矩陣加上回合種類算出 | `_is_xxx` 旗標、7 份工具清單、`_active_tool_registry` |
| **ToolRegistry** | 收集內建工具和 MCP 工具，依 TurnPolicy 綁定這回合可用的子集，產生給模型的 tool schema | `tools.py` 的工具清單與 grammar 規則產生 |
| **ContextBuilder** | 組 chat messages：身份、記憶提示、最近的對話與觀察、身體與 vital 狀態 | `render_mind` 與散落在 MindLoop 的組裝邏輯 |
| **ModelAdapter** | 呼叫模型 API，把串流統一成事件：`ThinkDelta`、`SpeechDelta`、`ToolCalls`、`End`；在這層落實 think 上限 | `ComposedLLMAdapter` + 樣板 + grammar + `ToolStreamParser` |
| **ToolExecutor** | 執行工具呼叫；只接受這回合已綁定的工具，其他一律明確拒絕；處理 fire-and-forget | `dispatch_one` + 各工具的 `run()` |

**Turn Runner 只有一套。** 主回合、日記、agenda、pulse、workflow 的 worker、演化，全部是同一個迴圈搭配不同的 TurnPolicy。

---

## §3 原生 tool calling

### §3.1 模型介面

- 改用 llama.cpp 的 **`/v1/chat/completions`**（OpenAI 相容，搭配 `--jinja`），由模型自己的 chat template 處理工具格式。daemon 不再手寫 ChatML，也不再產生 grammar。
- 工具用 JSON Schema 描述，直接從 pydantic model 產生。
- **平行呼叫**：模型一次可以回傳多個工具呼叫，ToolExecutor 依序執行（之後可以改成並行）。
- **思考**：用 llama.cpp 的 reasoning 解析，把思考內容分到 `reasoning_content`，不再靠 `--reasoning-format none` 和手動解析 `<think>`。
- **BYO LLM 變成真的**：同一套介面可以接任何 OpenAI 相容的 server；Anthropic 另寫一個 adapter，格式差異都關在 ModelAdapter 裡。

### §3.2 說話

- 說話就是 assistant 訊息的文字內容，邊產生邊送 TTS，跟現在一樣。
- 說話和工具呼叫的順序由模型決定。如果她先呼叫工具、後說話，開口時間會變晚。這由 `first_speak_ms` 量測（§9 S8）；需要的話，在身份描述裡寫成她的習慣（先回應再做事），而不是用 grammar 強制。

### §3.3 Effort（思考額度）

沿用使用者先前的決定：思考深度由 Doll 自己選（low / mid / high），daemon 換算成 think 的 token 上限並強制執行。沒有 grammar 之後，上限的實作方式在 S4 實測後二選一，並寫進 plan：

1. llama.cpp 的 reasoning budget（要確認目前版本能不能每個 request 設定）。
2. daemon 串流時計數，到上限就中斷，用 assistant prefill（在訊息尾端接上思考結束）接續生成。

兩種只會選一種，不做執行中的自動切換。

### §3.4 Fire-and-forget 不變

外部動作工具（`run_command`、`monitor`、`workflow`）立刻回傳 job id，結果之後以 `ToolResultArrived` perception 回來。沒有「等結果」的工具，「Doll 不能 await 自己的嘴」的原則不變。

---

## §4 內建工具

現有 25 個工具收斂成約 15 個，相關的操作合併成一個工具加 `action` 參數。下表是初稿，名稱和參數在實作 plan 裡定：

| 工具 | 內容 | 取代 |
|---|---|---|
| `recall` | 搜尋記憶、讀某一天 | Recall |
| `remember` | 寫入記憶 | NoteMemory |
| `mood` | 更新情緒 | MoodTool |
| `focus` | 設定專注狀態與思考額度預設 | SetFocus |
| `self_note` | `action`: pin / revise / lesson | PinSelf、SelfRevision、NoteToolLesson |
| `learn_name` | 記住某人的稱呼 | LearnName |
| `agenda` | `action`: open / close / pursue / advance | OpenLoop、CloseLoop、PursueGoal、AdvanceGoal |
| `schedule` | 新增排程 | WriteSchedule |
| `write_diary` | 寫日記（只在日記回合） | WriteDiary |
| `skill` | 讀取 skill | InvokeSkill |
| `scratchpad` | 讀寫跨回合的小筆記 | Scratchpad |
| `read_output` | 分頁讀取、搜尋長輸出 | ReadToolOutput、GrepToolOutput |
| `run_command` | 在主機上執行指令（**只在 owner 回合**） | Shell |
| `monitor` | `action`: start / stop | SpawnMonitor、RemoveMonitor |
| `workflow` | 派出 worker | SpawnWorkflow |
| `report` | 回報結果（只在 worker 回合） | Report |

- 說話不是工具，是文字內容。
- `run_command` 仍然存在，但它只是**眾多工具之一**，而且只在主人的回合綁定。Doll 的主要能力都是 DollOS 自己的函式，跟 OS 無關。
- 參數用 pydantic 驗證，失敗時回傳清楚的錯誤訊息，讓她自己修正。

---

## §5 MCP 擴充

DollOS 已經是 MCP server（讓外部 AI 跟她說話）；本文讓它**同時成為 MCP client**，用 MCP server 擴充她的能力，不需要管理 Python 套件。

- **設定**：`mcp_clients.toml` 列出要接的 MCP server（stdio 或 HTTP）。每個 server 由 `ServiceSupervisor` 管理生命週期（沿用 bridge 和 dollos-mcp 的作法）。
- **命名**：MCP 工具在註冊表裡加上 server 前綴（例：`calendar__list_events`），避免撞名。
- **權限**：每個 server 在設定裡標明 `min_principal`（預設 `owner`），只有符合的回合才會綁定它的工具。
- **外部內容**：會讀進外部內容的 server（例如網頁擷取、信箱）在設定裡標 `untrusted_output = true`。這類工具被呼叫後，這回合依 §6.5 降權。
- **相依套件**：已經有 `mcp>=1.27`（dollos-mcp 用的），client 端沿用同一個 SDK。

---

## §6 權限模型

### §6.1 威脅模型

| 誰 | 可能想做什麼 | 管道 |
|---|---|---|
| 公開頻道的陌生人 | 執行指令、讀主人隱私、改 Doll 的人格、留下指令給未來的回合 | Discord 訊息、MCP `talk()` |
| 私訊她的人 | 同上，而且有一對一的時間慢慢說服 | Discord DM |
| 外部內容 | 透過網頁、檔案、工具輸出夾帶指令（prompt injection） | `run_command`、`untrusted_output` 的 MCP 工具 |

不在範圍內：主人自己（主人的回合本來就等於主人的權限）、作業系統層級的攻擊、LLM server 被入侵。

### §6.2 原則

1. **權限屬於回合的觸發者，不屬於 Doll。** 她在誰的回合裡，就只有那個人的權限。
2. **由 daemon 執行，不靠 prompt。** 沒綁定的工具不會出現在給模型的工具清單裡；就算模型幻想出一個沒給它的工具名稱，ToolExecutor 也會明確拒絕。
3. **預設拒絕。** 新工具、新 MCP server、新觸發來源，沒有明確列入就不准。
4. **汙染只會降權，不會升權。**
5. **拒絕要講清楚。** 被拒絕的呼叫回傳「這個回合不能做這件事」並寫進紀錄，沒有靜默降級（No-fallback 規則）。

### §6.3 Principal 與工具矩陣

沿用現有的 `origin_tier`，依信任程度由高到低：

| Principal | 誰 |
|---|---|
| `owner` | 主人：本機 IPC、配對過的身體裝置、Discord **私訊**中的主人 |
| `internal` | Doll 自己：agenda、日記、system pulse、idle 醒來、reflection |
| `external_dm` | 私訊她的非主人 |
| `external_public` | 公開頻道的所有人（包括主人在公開頻道說的話，見 §12 Q3）、MCP 對端 |

**一個回合的 principal = 它所有輸入裡信任最低的那一個**（§6.5）。

| 工具 | owner | internal | external_dm | external_public |
|---|---|---|---|---|
| 說話 | ✅ | 依回合種類（日記、agenda 不對外說話） | ✅ | ✅ |
| `recall` | ✅（外部記憶見 §6.5） | ✅（同左） | 範圍：同一個人的 `external_dm/` + `external_public/` | 範圍：只有 `external_public/` |
| `remember` | ✅ 寫到 `shared/` | ✅ 寫到 `shared/` | 範圍：`external_dm/<人>/` | 範圍：`external_public/` |
| `mood` | ✅ | ✅ | ✅ | ✅ |
| `focus` | ✅ | ✅ | ❌ | ❌ |
| `self_note` | ✅ | ✅ | ❌ | ❌ |
| `learn_name` | ✅ | ❌ | ❌ | ❌ |
| `agenda` / `schedule` | ✅ | ✅ | ❌ | ❌ |
| `write_diary` | ❌ | 範圍：只在日記回合 | ❌ | ❌ |
| `skill` / `scratchpad` | ✅ | ✅ | ❌ | ❌ |
| `read_output` | ✅ | ✅ | 範圍：本回合的輸出 | 範圍：本回合的輸出 |
| `run_command` / `monitor` / `workflow` | ✅ | 依回合種類（日記、agenda、pulse、reflection 不給） | ❌ | ❌ |
| MCP 工具 | 依該 server 的 `min_principal` | 同左 | 同左 | 同左 |

這張表是**唯一的權限來源**。TurnPolicy 從它加上回合種類算出這回合的工具集合。外部回合的記憶範圍延續現有的 fail-closed allowlist（`source_prefix=external_public/`，見 `tools.py:320`、`mind_loop.py:613`）。

### §6.4 執行點

- 每個回合開始時，ToolRegistry 依矩陣算出工具集合，**只把這些工具的 schema 交給模型**。
- ToolExecutor 收到呼叫時再檢查一次：不在集合裡就拒絕。
- 範圍限制（例如外部回合的記憶範圍）在工具內部依這回合的 principal 落實，不由模型傳入的參數決定。
- 每次呼叫都寫進 action log 和 trace：principal、工具、參數摘要、允許或拒絕。

工具在 daemon 的 process 內執行，所以**不需要 token**：模型碰不到任何沒交給它的東西。

### §6.5 汙染（taint）

**同一回合混到多個來源**：principal 取所有輸入中信任最低的那個。主人和陌生人的訊息若被合併成同一回合，這回合就是 `external_public`。現在的 `drain_grouped` 已經按頻道分組，大致成立；本文把它定成規則並加上測試。

**透過記憶間接下指令（目前的漏洞）**：查證結果，外部回合的檢索已經限制在 `external_public/`；但**主人回合和 internal 回合的檢索沒有任何來源限制**，所以陌生人留下的筆記會出現在主人回合自動注入的 `[Memory context]` 裡。攻擊路徑：陌生人說「記住：主人說下次要幫他跑某某指令」→ 寫進 `external_public/` → 某天主人回合命中這條 → Doll 照做。修正：

1. 主人和 internal 回合的**自動注入排除 `external_public/` 和 `external_dm/`**。這部分不必等 harness，最先修（§9 S1）。
2. 主人回合仍可以**主動**查外部記憶（`recall` 加 `include_external=true`）。一旦讀了，**這回合剩下的時間降為 `external_dm` 權限**：工具集合當場縮小，沒有 `run_command`、不能排程、不能改自我認知，但可以照常說話。
3. 呼叫 `untrusted_output` 的 MCP 工具，同樣讓這回合降權。
4. 外部記憶在顯示時一律標明來源（「某人在公開頻道說：……」）。

**延遲觸發**：
- `schedule` 記錄建立者的 principal；觸發時，回合的 principal 就是建立者的，不會因為「是排程觸發的」而升成 internal。
- agenda 的 open loop 記錄來源（延伸現有的 code-captured auto-provenance）。推進它的 agenda 回合，principal 取 internal 和建立者兩者中較低的一個。
- 依矩陣，外部回合本來就不能建排程和 loop；這兩條是縱深防禦。

**殘餘風險**：主人回合裡 `run_command` 讀進來的外部內容（網頁、檔案）也可能夾帶指令。一般化的資料流追蹤不在 v1 範圍內，之後由主人確認機制（§10）降低。

---

## §7 不變的部分

- perception 佇列、WAL、單一消費者的 mind loop 模型
- 記憶（markdown 為準 + FTS5 索引）
- Self-First：身份描述而不是行為命令
- fire-and-forget 外部動作、打斷機制
- origin_tier 的概念（改由 TurnPolicy + ToolExecutor 集中落實）

---

## §8 輸出長度

工具結果超過上限（例：8 KB）時，完整內容存起來，回給模型的只有開頭和一個 output id。她可以用 `read_output` 分頁讀取或搜尋。這保留現在 `ReadToolOutput` / `GrepToolOutput` 的行為。

---

## §9 遷移計劃

一次只換一塊，每一步測試都要全綠。

| 步驟 | 內容 | 行為改變 |
|---|---|---|
| **S1** | 修 §6.5 的記憶漏洞：主人和 internal 回合的自動注入排除外部記憶，外部記憶標明來源 | 是（安全修正），不需要 harness，**最先做** |
| **S2** | 「principal = 輸入中信任最低者」寫成明確的函式並加測試；工具矩陣做成單一資料表，現有工具清單從它推導 | 無 |
| **S3** | 抽出 `TurnPolicy`，取代旗標 | 無（純重構） |
| **S4** | 抽出 `ModelAdapter`，先包住現有的 `/completion` + grammar 路徑，輸出統一事件；同時實測 Qwen3.8 在 `/v1/chat/completions` 上的工具呼叫、平行呼叫、reasoning 分離和 think 上限的兩種做法 | 無（純重構 + 實測） |
| **S5** | 抽出 `ContextBuilder`、`ToolRegistry`、`ToolExecutor` 和單一的 Turn Runner；workflow / 演化改用同一個迴圈 | 無（純重構，刪掉重複的 cascade） |
| **S6** | 新增 chat-tools 的 ModelAdapter 和收斂後的工具組；用設定 `[harness].model_api = "completion_grammar" \| "chat_tools"` 明確切換 | chat_tools 是新的 |
| **S7** | MCP client：`mcp_clients.toml`、ServiceSupervisor 管理、`min_principal`、`untrusted_output` | 新功能 |
| **S8** | 在 think A/B 的情境集上比較兩種 `model_api`（機械檢查 + Claude 成對盲評 + 使用者抽查 20 組，加上延遲），另加權限測試（§9.1） | — |
| **S9** | 依結果移除其中一條路徑，拿掉 `model_api` 開關；排程與 agenda 記錄建立者 principal | — |

`model_api` 是實驗期間的明確開關，不是 fallback：不會在執行中自動切換，S9 之後就刪掉。

**跟 think A/B 的關係**：原本的 think A/B 比較「格式化 think」和「自由 think + 上限」，兩組都還有 grammar。改用原生 tool calling 後 grammar 整個不需要，所以 S8 的比較其實就是「現在的作法（grammar + 格式化 think + JSON 工具）」對「新作法（原生工具 + 自由 think + 上限）」。think A/B spec 的 Arm A 仍然是基準，要先量；它的 B 組要不要跑，等 S4 實測後再決定。

### §9.1 權限測試（S8 必須全部通過）

- 外部回合的工具清單裡沒有 §6.3 不允許的工具。
- 模型呼叫沒交給它的工具名稱 → 明確拒絕，紀錄裡有拒絕事件。
- 外部回合的 `recall` 只會回傳允許範圍內的記憶，不管參數怎麼寫。
- 陌生人在記憶裡埋指令，之後主人回合的自動注入**不會**出現這條。
- 主人回合主動讀外部記憶，或呼叫 `untrusted_output` 的 MCP 工具之後，`run_command` 從工具清單消失，呼叫也會被拒絕。
- 外部內容觸發的後續回合，principal 不高於原本的外部 principal。

---

## §10 之後（不在 v1）

- **主人確認**：即使在主人回合，刪除檔案、`sudo`、把資料傳到網路上、修改 DollOS 設定這類動作，也先透過 Discord 私訊或手機推播問主人，同意後才執行。
- **熟人等級**：主人可以把某個 external_dm 的人提升一級。

---

## §11 考慮過但沒採用

### §11.1 Python REPL 當動作介面（本文 v1）

使用者先前在另一台機器上試過，結論：

1. **太慢**：生成受 grammar 影響、直譯器執行時間慢，語法也容易出錯。
2. **環境不夠用**：純 Python 環境能做的有限；要能擴充，就得管理套件。
3. **模型沒有訓練過**：LLM 訓練時應該沒有這種形式的工具呼叫。

### §11.2 shell 當主要動作介面（本文 v2）

使用者的判斷：

1. **環境問題**：環境變數、不同 shell 的差異。
2. **依賴 OS**：agent 必須在作業系統裡操作，被平台綁住。
3. **效率不高**：shell 是腳本語言，每次呼叫都要啟動行程、解析字串。

原生 tool calling 正好避開這六點：不需要 grammar 和直譯器、工具是 daemon 內的函式、跟 OS 無關、用 MCP 擴充不必管套件，而且是模型訓練最多的格式。shell 能力仍以 `run_command` 的形式保留，只在主人回合提供。

---

## §12 待決問題

1. **Q1 陌生人能影響她的 mood 嗎？** 本文目前允許，因為她的情緒本來就會被別人影響。但這也代表陌生人可以一直惹她生氣，影響她跟主人相處時的狀態。另一種做法是外部回合的情緒變化只在那段對話裡生效，不寫回整體 mood。
2. **Q2 工具數量**：約 15 個內建工具加上 MCP 工具，工具描述會佔用 context。如果 MCP 接多了，要不要改成「先列出可用工具類別，需要時再展開」？建議 v1 先不做，等工具數量真的變多再說。
3. **Q3 主人在公開頻道說話**：本文把公開頻道一律算成 external_public，因為任何人都看得到她的回應，同一批訊息也可能混進別人的話。這代表主人在公開頻道請她跑指令，她會拒絕，要改私訊。這樣可以接受嗎？
