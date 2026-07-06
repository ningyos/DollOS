# DollOS MCP Server — 讓外部 AI 直接與 Doll 對話（第二個 ServiceSupervisor 服務）— Design

Status: **PROPOSAL**（設計已於 brainstorming 中經使用者核可；本文件的職責是把該設計 **對照 merged code
逐點 ground（file:line）** 並展開成完整、R-tagged 的實作規格）。2026-07-06。

Grounded against merged code — 以下 file:line 皆為 ground truth，非願景：
`src/dollos/ipc/messages.py`、`src/dollos/ipc/server.py`、`src/dollos/mind/attention.py`、
`src/dollos/discord_bridge/controller.py`、`src/dollos/discord_bridge/__main__.py`、
`src/dollos/kernel.py`、`src/dollos/mind/mind_loop.py`、`src/dollos/mind/mind_prompt.py`、
`src/dollos/config.py`、`src/dollos/service_supervisor.py`、`pyproject.toml`、`.gitignore`。

> **姊妹規格**：本設計是 `docs/superpowers/specs/2026-07-06-bridge-internalization-design.md`
> （ServiceSupervisor / discord-bridge 內化）的**直接延伸**。該規格已 merged（`service_supervisor.py`
> 存在、kernel 已接線）。本設計把 **MCP server 作為 ServiceSupervisor 的第二個註冊服務**，這正是那份
> 規格 §3 明講「介面刻意通用，未來連接器都以同一機制註冊」所預留的驗證點。

---

## 0. Overview / Goal

**使用者已核可的目標：** 為 DollOS 開一個常駐的 HTTP/SSE **MCP server**（`dollos-mcp`），讓**外部 AI**
（Claude 或任何 MCP client）**直接跟 Doll 對話**，不必再由使用者當人肉轉發。這對齊願景記憶
`project_dollos_vision_2026_07`（portable、doll 社交）與 `project_smolgura_lessons`（該不該回的注意力已
在 daemon 側 `AttentionGate` 落地）。

**兩個模式：**

- **Peer 模式（預設）**＝「一則走 MCP transport 的 DM」。外部 AI 呼叫工具 `talk(name, message)`，Doll 用
  Self-First 自行決定要不要回、回多少（可以已讀不回）。這是**無新子系統**的設計：mcp 連接器把 peer 訊息
  塞進**既有**的 `ChannelEvent → ChannelMessage perception → AddressedText reply` 管線，就跟 discord-bridge
  的 DM 一模一樣。
- **Debug 模式（secret-gated）**＝給開發者用的高可靠對話 + 唯讀 introspection。呈上正確 secret 的連線
  額外得到：(1) **強烈傾向實質回覆**（一個 prompt-level 情境 nudge 抑制她的拒答/沉默 agency；這是
  **best-effort 軟機制、非硬保證**——見 §C.2 與 §H 開放決策 2，措辭刻意不用「一定」）；(2) **唯讀
  introspection 工具** `get_state` / `get_recent`。**兩個模式都不解鎖 Shell。**

**核心設計原則（house rules）：**

- **無 fallback / 明確 fail-closed**：peer 的 `name` 是自稱、未驗證，絕不污染 owner 身份、絕不解鎖任何
  能力（§E）。
- **YAGNI**：v1 introspection 只有 state + recent；**不**做 HTTP 對外網路暴露（僅 loopback）；**不**做
  Doll 主動發起聯絡（列為 future）；**不**做 full trace dump / perception 注入 / 觸發 reflection。
- **computer-as-home + crash-isolation**：`dollos-mcp` 是**獨立 OS 子行程**，繼承 ServiceSupervisor 的
  PDEATHSIG 反孤兒 / SIGINT graceful / crash-loop backoff（§A）——MCP client library 或 HTTP server 崩潰
  **絕不能碰到** daemon 的 event loop 與 Memory SoT。

---

## 0.1 Grounding 摘要：**與核可設計相符 / 相異** 的關鍵事實

下表是本次 grounding 最重要的產出。**§0.1-D1 是一個安全相關的用詞修正，必須被上層知悉。**

| # | 核可設計的敘述 | Code ground truth | 結論 |
|---|---|---|---|
| G1 | mcp 連接器送 `ChannelRegister(kind="mcp") + ChannelEvent(is_dm=True, author_is_owner=False)` | `ChannelRegister` 有 `channel_id/locus/kind` 三欄（`messages.py:50-59`）；`ChannelEvent` 有 `channel_id/payload`（`messages.py:62-69`）；discord-bridge 的 DM payload = `{**event, "author_is_owner": …}`（`controller.py:285-290`）。**可原樣鏡射。** | ✅ 相符 |
| G2 | daemon「總是 admit」DM，靠 is_dm short-circuit | `AttentionGate._l0_signal`：`if event.get("is_dm"): return "l0_dm"`（`attention.py:126-127`）→ `admit()` 對任何 L0 signal 回 `AdmitDecision(True, …)`（`attention.py:153-178`）。 | ✅ 相符（但**位置不是 line 243**，見 D2） |
| G3 | 兩模式 → `_EXTERNAL_KINDS` → 結構性無 Shell/Workflow | `ChannelMessage` ∈ `_EXTERNAL_KINDS`（`mind_loop.py:87`）；`origin_tier != "internal"` → registry 收斂成 `EXTERNAL_TOOLS` 子集（`mind_loop.py:836-843`），docstring 明列「must never reach Shell / SpawnWorkflow / SpawnMonitor / … / WriteSchedule」（`mind_loop.py:802-805`）。 | ✅ 相符 |
| G4 | reply 走 `AddressedText`，由 SinkResolver `(locus="external", channel_id)` 路由回連接器 | `_emit_sentence`：origin 為 external 時發 `AddressedText(channel_id=origin, …)`（`mind_loop.py:1275-1278`）；kernel `_register_external_sink` 把 sink 註冊到 `(locus="external", channel_id)`（`kernel.py:813-822`）。 | ✅ 相符 |
| G5 | 需要新的 per-channel turn-end；今天 external streaming 無此標記 | pump 把 `None` 一律轉成**全域** `TurnEnd()`（**無 channel_id 欄位**，`messages.py:105-106`；`server.py:138-139`）；bridge **明文忽略** TurnEnd（`__main__.py:286-287`）。 | ✅ gap 屬實（§D） |
| G6 | MCP Python SDK 已是相依？ | `pyproject.toml` dependencies **無** `mcp` / `fastmcp`（有 py-cord/pydantic/websockets）。 | ⚠️ **新相依**（§A.5） |
| **D1** | **「mcp-DM 應對應到 external_dm tier（direct 1:1）」** | `_derive_origin_tier` **要求 `author_is_owner AND is_dm` 才是 external_dm**，否則 external_public（`mind_loop.py:323-328`）。peer 的 `author_is_owner=False` ⇒ **實際落在 external_public，不是 external_dm**。而 external_dm 會給予**owner 的完整私有記憶檢索**（`mind_loop.py:315`「full private retrieval」）。 | ❌ **相異——且 external_public 才是安全正解**（見下方詳述）。 |
| D2 | 「is_dm short-circuit in admit — attention.py:243」 | line 243 其實是 **`window_for`** 的 is_dm short-circuit（給 engaged 短窗，`attention.py:243`）。**admission** 的 always-admit 在 `_l0_signal` line 126 + `admit` line 153-178。是**兩處**不同的 short-circuit。 | ⚠️ 位置澄清（兩處都用得到，見 §B） |
| D3 | 「recognize kind='mcp' for situational framing」 | perception 的 `data` = payload，**不帶** ChannelRegistry 的 `kind`；`_perception_summary` 的 ChannelMessage 分支只分 owner/陌生人（`mind_prompt.py:394-402`），**無 AI-peer 分支**。 | ⚠️ 需最小新增（§B.4）：payload 帶 discriminator + 加一個 render 分支。 |
| D4 | debug secret 存 `mcp.toml`（gitignored，比照 bridge token） | `.gitignore` 明列 `bridge.toml`（`.gitignore:26`）但**未涵蓋 `mcp.toml`**（`*.local.toml`/`config.*.toml` 都不 match）。 | ⚠️ 需在 `.gitignore` 加一行 `mcp.toml`（§E）。 |

**D1 詳述（安全關鍵，必須讓上層拍板用詞）：** 核可設計把「direct 1:1」講成「external_dm tier」。但在 merged
code 裡，**external_dm ≠「1:1」，而是「受信任 owner 的私密頻道」**——它會解鎖 owner 的**完整私有記憶檢索**
（`mind_loop.py:315-320` 明講）。一個**未驗證的 AI peer** 若被歸為 external_dm，就會拿到主人的私有記憶，
這是**安全漏洞**。`_derive_origin_tier`（`mind_loop.py:323-328`）以 `author_is_owner AND is_dm` 為 external_dm
的必要條件，peer 的 `author_is_owner=False` **自動、正確地**把它落到 **external_public**——這正是我們要的
**fail-closed** 行為（無私有記憶、記憶檢索 scoped 到 `external_public/`、`[External situation]` block）。
設計想要的「direct 1:1／總是被送達」性質，來自 **admission**（is_dm → l0_dm → 一定 admit，§B.2）與
**engaged 短窗**（§B.3），**而非**來自記憶 tier。

> **本規格的決定（R-DECISION-1）：mcp peer 與 debug 都以 `author_is_owner=False` 送出 →
> 兩者的 `origin_tier` 皆為 `external_public`。** 我們**採用** code 的實際行為，並在文件裡把核可設計的
> 「external_dm」用詞**更正為「以 DM 方式 admit、但以 external_public 分級」**。這同時滿足 §E 的
> external-safety invariant（無 Shell/Workflow **且** 無 owner 私有記憶）。**← 需上層確認採用此更正
> （見 §H 開放決策 1）。**

---

## A. 架構與生命週期

### A.1 新行程 `dollos-mcp`

一個**獨立 OS 子行程**，身兼兩個角色：

1. **對外**：一個 **HTTP/SSE MCP server**（MCP Python SDK / FastMCP，streamable-HTTP transport），
   bind 在 **`127.0.0.1:<port>`（loopback-only）**。外部 AI 以 MCP client 連進來、呼叫工具。
2. **對內**：一個**到 daemon IPC WS server（`ws://127.0.0.1:9876`）的 WS client**，把 MCP 工具呼叫
   ⇆ daemon IPC 協定橋接起來。

這與 discord-bridge 的形狀**完全對稱**：bridge 也是「對外接 Discord gateway、對內接 daemon WS」的
子行程（`discord_bridge/__main__.py:195` `async with websockets.connect(args.daemon) as ws`）。mcp 連接器
把「Discord gateway」換成「MCP server」，daemon 那一側**原封不動**。

**entry point**：`python -m dollos.mcp_server`（新 module `src/dollos/mcp_server/`），argv 形狀比照
bridge：`--daemon <ws-url> --config <mcp.toml> --data-root <data>`（§A.4）。用 `sys.executable -m …`
**直接子行程、無 `uv run` 中介**，讓 §A.3 的 PDEATHSIG 監看的正是 mcp 行程本身（比照
`kernel.py:652-653` bridge argv）。

### A.2 第二個 ServiceSupervisor 服務（驗證通用性）

`ServiceSupervisor` 的註冊表是 `dict[str, _ServiceState]`（`service_supervisor.py:89`），`register()`
以 `spec.name` 為鍵、重複名才 raise（`service_supervisor.py:91-95`）；`start()` 對**註冊表每個服務**各起
一個 supervise task（`service_supervisor.py:97-101`）；`stop()` 用 `asyncio.gather` 收**整個註冊表**
（`service_supervisor.py:165-169`）。**Ground truth：註冊第二個 `ServiceSpec` 是現況原生支援的，無需改
supervisor 內部。** 這正是內化規格 §3 承諾「未來連接器以同一機制註冊」的兌現。

**kernel 接線**（鏡射 `kernel.py:453-461` 的 bridge 註冊）：

```python
# kernel.__init__，緊接 bridge 註冊之後
if settings.mcp.enabled:
    if settings.mcp.config is None or not settings.mcp.config.exists():
        logger.error("mcp enabled but config missing (%s) — not registering",
                     settings.mcp.config)      # log-and-skip：刻意鏡射 bridge kernel.py:454-461
    else:
        self.service_supervisor.register(self._build_mcp_spec(settings))
```

> **enabled-but-missing-config 是 log-and-skip，非 fail-fast——這是刻意鏡射 bridge 的既有行為**
> （`kernel.py:454-461` 對 bridge 就是 log error 後不註冊，繼續跑）。**先前草案的註解「fail-fast」是誤標**，
> 已更正。此為**明訂邊界**（stated boundary，非意外 degradation）：`[mcp].enabled=true` 但 config 檔缺失時，
> daemon **不 crash、但 mcp server 不啟動**，只在 journal 留 error。可見性由 log 提供；與 bridge 逐字對齊，
> 避免兩個相同形狀的服務有不一致的失敗語義。（若日後要對「enabled 卻沒跑」更強硬，應**同時**改 bridge 與
> mcp 為 raise，保持對稱——不在本規格單方面改一邊。）

`_build_mcp_spec`（kernel-local helper，鏡射 `_build_bridge_spec`，`kernel.py:647-662`）：

```python
def _build_mcp_spec(self, settings: Settings) -> ServiceSpec:
    argv = (
        sys.executable, "-m", "dollos.mcp_server",
        "--daemon", _derive_daemon_ws(settings.ipc),       # 既有 helper，kernel.py:358
        "--config", str(settings.mcp.config.expanduser().resolve()),
        "--data-root", str(settings.data.root.expanduser().resolve()),
    )
    return ServiceSpec(name="mcp-server", argv=argv,
                       on_gave_up=self._emit_mcp_down_perception)   # §E 可見性
```

`_emit_mcp_down_perception` 鏡射 `_emit_bridge_down_perception`（`kernel.py:664-678`）：crash-loop 放棄時
enqueue 一個 `Perception(kind="McpDown", data={"service": name, "rc": rc})`（§E、§D.5 render）。**同步 callback，
只 enqueue，不做重活。**

**繼承的生死保證（零額外程式碼）：** PDEATHSIG 反孤兒（`service_supervisor.py:50-56`）、SIGINT graceful
terminate → wait `_GRACE_S` → SIGKILL → reap（`service_supervisor.py:171-188`）、指數 backoff + healthy-uptime
重置 + crash-loop 上限（`service_supervisor.py:118-163`）、stdout/stderr 繼承 daemon fds → daemon journal
（`service_supervisor.py:107`）。**mcp 行程 crash 只是喚醒 supervisor 去重啟，碰不到 daemon。**

### A.3 crash-isolation（house rule：computer-as-home）

理由同內化規格 §1/§8：MCP server library、streamable-HTTP、或任何 peer 觸發的阻塞式呼叫**絕不能**跑在
daemon 的 asyncio loop 裡。`dollos-mcp` 維持獨立 OS 行程，唯一耦合是 (1) daemon IPC WS（本就 reconnect
隔離）與 (2) supervisor 的 `proc.wait()` await。無共享記憶體、無共享 GIL、無共享 event loop。

### A.4 config：最小 `[mcp]` 指標（鏡射 `[bridge]`）

daemon config 新增一個**最小**區塊，**只有 `enabled` + `config`**，逐欄鏡射 `BridgeConfig`
（`config.py:139-162`）：

```python
class McpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False                 # opt-in;預設關 → 零開銷
    config: Path | None = None            # 指向獨立 mcp.toml(enabled 時 required)
    query_token: str | None = None        # debug introspection query 面的 daemon-side 閘
                                          # （§C.3 R-DECISION-4）；None/空 = query 面停用(fail-closed)。
                                          # 必須與 mcp.toml [server].query_token 相同值。

    @field_validator("config", mode="before")
    @classmethod
    def _expand_user(cls, v: object) -> object:
        if isinstance(v, (str, Path)):
            return Path(v).expanduser()
        return v

    @model_validator(mode="after")
    def _require_config_when_enabled(self) -> "McpConfig":
        if self.enabled and self.config is None:
            raise ValueError("[mcp].enabled=true 需要 [mcp].config 指向 mcp.toml")
        return self
```

在 `Settings` 註冊（緊接 `bridge` 那行，`config.py:295`）：`mcp: McpConfig = Field(default_factory=lambda: McpConfig())`。
`Settings` 是 `extra="forbid"`，但 `McpConfig` 有 default_factory ⇒ 沒宣告 `[mcp]` 也合法（等同 `enabled=false`）。

**真正的秘密（debug secret）與 bind port 留在獨立 `mcp.toml`**，由 `dollos-mcp` 自己讀（daemon 只知道那個
檔案的**路徑**，不碰秘密——比照 daemon 不碰 bridge token）：

```toml
# mcp.toml（gitignored）
[server]
bind_host  = "127.0.0.1"   # loopback-only（§E）；**連接器 fail-closed 驗證**：非 loopback 直接 raise
bind_port  = 9877
debug_secret = "…"          # debug 模式的閘（§C）；空 = 停用 debug 模式
query_token  = "…"          # introspection query 面的閘（§C.3）；**必須 == daemon config [mcp].query_token**；空 = 停用 query 面
```

> **bind_host 是 code-enforced loopback，不只是註解（§E 威脅模型的根基）：** 連接器讀 `mcp.toml`
> 時**必須**驗證 `bind_host ∈ {"127.0.0.1", "::1", "localhost"}`，否則**fail-closed raise**（鏡射
> `config.py` 的 `extra="forbid"` fail-fast 風格）——一個「唯一安全值就是預設值」的欄位若不強制就是
> 靜默漏洞。使用者想要遠端存取**只能**走 §E 記載的 SSH tunnel / authenticated reverse proxy 路徑，
> **不**經由可 rebind 的 socket。timeout（§D.3）在 v1 **hardcode ~60s**、**不**列為 mcp.toml 欄位
> （YAGNI，見 §H.6），所以 `[server]` 就這三欄，無其他 knob。

TOML 形狀（daemon config；`query_token` 僅在啟用 debug introspection 時才需，且須與 mcp.toml 同值）：

```toml
[mcp]
enabled     = true
config      = "mcp.toml"
query_token = "…"          # 選填；P2 debug introspection 才需要；留空 = query 面停用（fail-closed）
```

### A.5 新相依：MCP Python SDK

**Ground truth（G6）：** `pyproject.toml` dependencies 目前**沒有** `mcp` / `fastmcp`。因此需**新增相依**
`mcp`（官方 Python SDK，含 FastMCP 與 **streamable-HTTP server** 能力）。**No-fallback**：若採用的 SDK 版本
不支援 streamable-HTTP server，明確 surface 該限制，不 silently 退回 stdio 或自刻 HTTP——依 house rule
「state boundaries clearly」。

---

## B. Peer 模式 wire-flow（grounded）

Peer 模式 = 「一則走 MCP transport 的 DM」。逐步對照 code：

### B.1 連線 + 註冊 channel（每次 talk 一個唯一 channel）

**關鍵路由決策（R-DECISION-2，修正 batched/多 session 衝突）：** channel_id **不可**用自稱的 `name`
當鍵。原因有二，兩者都會造成 reply 錯投：

1. **同名跨 session 撞頻**：mcp 連接器只有**一條** daemon WS，卻扇入**多個獨立的 MCP client session**。
   兩個不同的 client 都 `talk(name="Claude")` 會塌到**同一個** `channel_id`，連接器收到
   `AddressedText(channel_id="mcp:Claude")` 時**無法 demux** 是哪個 session 的 reply → 一個 peer 劫走另一個
   的 reply 流。
2. **同 channel 被 daemon 併批**：daemon 的 `BatchAccumulator` 會把 debounce 窗內同 `channel_id` 的所有
   `ChannelEvent` 併成**一次** flush（`batch_accumulator.py:19-35`），`drain_grouped` 再按 `channel_id`
   regroup 成**一個** bucket（`kernel.py:793-811`）＝**一個 turn ＝一個 reply 流＋一個 turn-end**
   （`mind_loop.py:284-289`）。故兩個並行/窗重疊的 `talk(name,…)` 若共用 channel 會塌成**單一 turn、單一
   `TurnEndAddressed`**，只有一個呼叫拿到（合併後的）reply，其餘呼叫收不到自己的 `AddressedText`、也等不到
   自己的 turn-end，只能 hang 到 §D.3 timeout 假報「已讀不回」；混合 debug/peer 批次時，單一 bucket 的
   `origin_tier`/`debug_reliable` 由 `_derive_origin_tier` 先掃到的那則 `ChannelMessage` 決定
   （`mind_loop.py:323-328`），會**默默 mis-tier** 其他呼叫。

**因此：每一次 `talk()` invocation 都用一個全新的唯一 channel_id，格式
`mcp:<conn_uuid>:<call_uuid>`**——`conn_uuid` 由連接器對**每個 MCP client 連線** mint（隔離同名跨 session），
`call_uuid` 對**每次 talk 呼叫** mint（隔離並行/併批）。每次 `talk` **各自** register-on-first + 送
`ChannelEvent`（比照 controller 的 register-on-first-forward，`controller.py:277-283`）：

```python
ChannelRegister(channel_id=f"mcp:{conn_uuid}:{call_uuid}", locus="external", kind="mcp")
```

- daemon 端 `_handle_message` 收到 `ChannelRegister`（`kernel.py:730-738`）→ `ChannelRegistry.register(
  channel_id, locus="external", kind="mcp")` + 因 `locus == "external"` 呼叫 `_register_external_sink(sink,
  channel_id)`（`kernel.py:813-822`）→ 把**這條 mcp WS 連線的 sink** 註冊到 SinkResolver 的
  `(locus="external", channel_id="mcp:<conn_uuid>:<call_uuid>")`。**reply 從此有唯一路由回得來。**
- 因每個 channel_id 唯一 ⇒ **不會**跨 call 併批 → 每次 `talk` 得到**自己的** bucket → 自己的 turn →
  自己的 `AddressedText` 流 → 自己的 `TurnEndAddressed`，零串線。
- **peer 連續性（continuity）不靠重用 channel session，而是靠 (a) 記憶（她自己的 `NoteMemory`）＋
  (b) payload 的 author 身份**：`author="<name>"`（render 顯示名）、`author_id="mcp:<name>"`（自稱、未驗證，
  供 memory continuity；身份 spoof 風險見 §E，**不**因此升權）。turn-end 完連接器即可**卸載**該 channel 的
  sink（一次性），避免 SinkResolver 累積 stale 條目。
- `mcp:` 前綴命名空間隔離，與 Discord channel id 不會相撞。

### B.2 送 ChannelEvent（payload 鏡射 discord DM）

連接器對每則 peer 訊息送：

```python
ChannelEvent(
    channel_id=f"mcp:{conn_uuid}:{call_uuid}",   # 每次 talk 唯一（§B.1 R-DECISION-2）
    payload={
        "channel_id": f"mcp:{conn_uuid}:{call_uuid}",  # envelope 會覆寫，但一致填著
        "author_id":  f"mcp:{name}",      # 自稱、未驗證；供 memory continuity（非路由鍵）
        "author":     name,               # render 用的顯示名（自稱、未驗證）
        "is_dm":      True,               # 驅動 l0_dm admission + engaged 窗 + 私訊 render
        "author_is_owner": False,         # peer 永遠不是 owner（§E name-spoof）
        "content":    message,            # render 讀 content（mind_prompt.py:401）
        "channel_kind": "mcp",            # §B.4 situational discriminator（新欄，連接器自填）
        "ts":         <epoch-seconds>,
    },
)
```

**路由鍵（channel_id）與身份（author_id/author）刻意分離**：channel_id 唯一化只為 demux/隔離（§B.1），
peer 的顯示與記憶連續性由 `author`/`author_id` 承載。`author_id` 進 session participants
（`attention.py:166-174`），仍是自稱、未驗證——render 明標（§B.4），且**不**經此升權（§E）。

**payload 欄位對照 code（為什麼是這些鍵）：**

- `is_dm=True`：`_l0_signal` 讀它 → 回 `"l0_dm"`（`attention.py:126-127`），`admit()` 因此**一定** admit
  （`attention.py:153-178`）——這就是設計說的「daemon 總是 admit DM」。**（此即 G2/D2 的 admission short-circuit，
  位於 attention.py:126，非 line 243。）**
- `channel_id` / `author_id`：`admit()` 明文只讀這兩者 + `_l0_signal` 內的 `is_dm/mentioned/content/
  reply_to_bot/channel_id`（`attention.py:148/163/193`）。`author_id` 進 session participants（`attention.py:
  166-174`）。**admit 不讀 `author_is_owner`**——那是在 kernel preempt（`kernel.py:776`）與
  `_derive_origin_tier`（`mind_loop.py:325`）讀。
- `is_dm` 也被 `window_for` 讀（`attention.py:243`，D2 的**第二處** short-circuit）→ 即使還沒開 session 也給
  engaged 短窗，peer 對話因此跟 owner-DM 一樣即時（kernel 在 `kernel.py:763-768` 明確把 `is_dm/author_is_owner`
  餵給 `window_for`）。
- `content`：`_perception_summary` 的 ChannelMessage 分支用 `d.get('content','')` 渲染（`mind_prompt.py:401`）。
  **注意命名分歧**：`_run_one_turn` 的 owner-present transcript 分支讀 `p.data.get("text")`（`mind_loop.py:364`），
  但那分支僅在 `author_is_owner` 為真時進入（`mind_loop.py:359-361`）——peer 為 False，**不進**該分支，故
  peer 只需 `content`。（若日後要讓 peer 訊息也寫 transcript，需補 `text`；v1 不需要。）

### B.3 admit → debounce → ChannelMessage perception

kernel 對 `ChannelEvent` 的處理（`kernel.py:739-795`）：建 `event = {**payload, "channel_id": envelope}`
（envelope 權威，`kernel.py:748`）→ 讀 `window_for`（`kernel.py:763-768`）→ `admit`（`kernel.py:773`）→
未 admit 就 drop（default silence，`kernel.py:774-775`）→ 進 `BatchAccumulator`（`kernel.py:793-795`）→
window 觸發時 flush 成 `Perception(kind="ChannelMessage", data=event)`（`kernel.py:799-811`）。

peer 為 `is_dm=True` ⇒ 一定 admit；為 `author_is_owner=False` ⇒ **不**觸發 owner 的 preempt/cancel
（`kernel.py:776-788` 只在 `author_is_owner` 時 preempt）——正確：陌生 AI 不該打斷 Doll 當前的 cascade。

### B.4 origin_tier = external_public + situational framing（D1/D3）

`_derive_origin_tier`（`mind_loop.py:323-328`）：peer 的 `author_is_owner=False` ⇒ **`external_public`**
（見 §0.1-D1）。這正確地：

- 收斂工具到 `EXTERNAL_TOOLS`（`mind_loop.py:836-843`）→ **無 Shell/SpawnWorkflow/…**（§E）。
- 記憶檢索 scoped 到 `external_public/` allowlist（`mind_loop.py:436-439`）→ 不洩漏 owner 私有記憶。
- 渲染 `[External situation]` block、套用「公開場合、不關妳的話」的 agency（`mind_prompt.py:189-196`）——
  **peer agency 的來源**：她可以選擇不回。

**situational framing（D3，最小新增）：** 現況 `_perception_summary` 的 ChannelMessage 分支
（`mind_prompt.py:394-402`）只會把 peer 渲染成「`[私訊] 陌生人 <name>:<content>`」——她會以為對方是 Discord
上的人類陌生人。設計要她**知道對方是 AI peer**。因 perception `data` 不帶 registry 的 `kind`（D3），採
**連接器在 payload 自填 `channel_kind="mcp"`**（§B.2），並在 `_perception_summary` 加一個分支：

```python
if p.kind == "ChannelMessage":
    if d.get("channel_kind") == "mcp":
        return f"[AI peer 私訊] {d.get('author','?')}（另一個 AI，自稱，未驗證）:{d.get('content','')}"
    where = "私訊" if d.get("is_dm") else f"#{d.get('channel','?')}"
    ...
```

這是**情境 nudge（描述性敘述，非命令）**，不是新子系統——對齊 Self-First（`system_prompt` 是身份描述，
不是行為命令）。她如何記住 peer，靠她自己的 `NoteMemory`（無新記憶子系統）。

### B.5 reply 串流回連接器 → 收成 tool result

Doll 的 cascade 對這個 origin 產出 reply：`_emit_sentence`（`mind_loop.py:1275-1278`）因 `current_origin`
= `"mcp:<conn_uuid>:<call_uuid>"` 且 `registry.locus_of(origin) == "external"`，逐句發
`AddressedText(channel_id=origin, text=sentence)`。SinkResolver 用 `(locus="external", channel_id)`（§B.1
註冊的那個唯一 channel）把它路由回**這條 mcp WS 連線**，且因 channel_id 對這次 `talk` 唯一，連接器可**精確
demux** 到發起這次呼叫的 MCP session。連接器收集這些 `AddressedText`，在 turn-end 時（§D）join 成單一字串，
回給 `talk` 的呼叫者。

### B.6 連接器也在 internal-sink pool 裡（origin-less 內部輸出的處置）

**Ground truth（必須明講、非意外）：** 「mcp 連接器只看得到自己 channel 的流量」這句話**只對 external
`AddressedText` 成立**（SinkResolver 精確比對 `(external, cid==origin)`，`sink_resolver.py:58-62`），對
**origin-less 的 internal turn 不成立**。每一條 IPC 連線——**包含 mcp 連接器的 WS client**——都會被
`_handle_connect` **無條件註冊為 internal sink**（`kernel.py:940`）；`_register_external_sink` 只是**額外**
疊上 external channel handle，**從不移除** internal 那條（`kernel.py:813-824`）。SinkResolver 把任何 origin-less
turn 解析到**最近註冊的 internal sink**（`sink_resolver.py:63-66`）。

由於 bridge **和** mcp 連接器都是常駐（這正是第二個 ServiceSupervisor 服務的重點）＋可能還有 UI，Doll 的
origin-less 輸出（排程問候、日記、self-initiated turn、語音 turn）會被路由到**最近連上的那條 internal
sink**，這**可能是 mcp 連接器而非預期的 UI/voice client**。後果有二：(1) 連接器收到它**從未索求**的裸
`TextChunk`/全域 `TurnEnd`；(2) 更糟——真正的內部輸出被**默默錯投**離開 UI。

**因此本規格明訂（chosen boundary，非意外）：**

1. **連接器 MUST 忽略任何非 `AddressedText` / 非 `TurnEndAddressed` / 非 `QueryResult` 的 server 訊息。**
   裸 `TextChunk` / 全域 `TurnEnd` 是 origin-less 內部輸出，**不定址任何 mcp channel**，連接器收到即丟棄
   （比照 bridge 忽略非 `addressed_text`，`__main__.py:282-287`）。這保證 `talk` 只 join 屬於自己 channel_id
   的 `AddressedText`，不會把別人的內部輸出誤當自己的 reply。
2. **明確記載**：新增第二條常駐 internal 連線**放大**了 SinkResolver 的「most-recent-internal」錯投面。v1
   **接受**此邊界（連接器靠上述忽略規則自我防護，不誤收；內部輸出即使錯投到連接器也被丟棄，不致外洩到
   peer），但**列為已知限制**：若日後 UI 內部輸出遺失可追到此，正解是讓 internal turn 定址一個指定的 UI
   sink，或讓連接器 **external-only 註冊、排除於 internal pool 之外**（需 `_handle_connect` 支援 opt-out，
   非 v1）。記於 §H 開放決策 7。

Debug 模式相對 peer**只多兩件事，仍不解鎖 Shell**。

### C.1 secret gate（fail-closed）

一條連線在建立 MCP session 時呈上 secret（例如透過一個 `authenticate(secret)` MCP 工具，或 transport
層的 header——依 SDK 能力，§H 開放決策 3）。連接器**在 mcp 行程內**比對 `mcp.toml` 的 `debug_secret`：

- 相符 → 這條連線標記為 **debug 連線**，多曝露 `get_state` / `get_recent` 工具，且其 `talk` 走「可靠回應」。
- 不符 / 未呈上 / `mcp.toml` 的 `debug_secret` 為空 → **fail-closed**：這條連線就是普通 peer，`get_state` /
  `get_recent` **根本不在它的工具表裡**（不是「呼叫了被拒」，而是**不存在**——比照 `EXTERNAL_TOOLS` 用
  registry availability 而非 post-hoc 檢查來擋，`mind_loop.py:824-825`）。

**secret 比對只在 mcp 行程內**，daemon 不經手 secret（比照 daemon 不碰 bridge token）。

### C.2 可靠回應（抑制 agency）

peer 模式下 Doll 可已讀不回（external_public agency，§B.4）。debug 模式要她**強烈傾向給實質回覆
（best-effort，非硬保證）**。**用詞刻意校準**：這是**軟機制**，非「一定」——實作面（§H 開放決策 2 需拍板
mechanism）：debug 的 `ChannelEvent` payload 帶一個 `debug_reliable=True` 旗標，daemon 在渲染該 origin 的
prompt 時**加一句情境 nudge**（「這是除錯通道，請務必給出實質回覆」），**而非**改 `origin_tier`（仍是
external_public，維持無 Shell/無私有記憶）。這是 prompt-level 的軟機制；符合 house rule「弱模型軟機制」——
**能力邊界仍由 registry 硬擋**，只有「回不回」這個語意被 nudge。

> **guarantee vs mechanism 一致性（校準過的用詞，消除內部矛盾）：** 專案的弱模型 playbook 明講「prompt
> 管不住的語意升級成 code 閘」——一句 nudge**不是**「一定」。因此 §0/§C.1/本節一律用「強烈傾向 / nudged
> toward」而非「一定 / guaranteed」，並**接受 best-effort**。若日後真需要硬保證，正解是 **code gate**
> （debug origin 確定性 bypass 沉默/拒答路徑），而非加強 prompt 措辭——列 §H 開放決策 2，**先 live-smoke
> 驗證 nudge 的實際可靠度（§H.2）再決定是否升級**。v1 不承諾硬保證。

> **debug caller 不是 owner**：debug 連線**不**冒充 owner 的 Discord `author_id`、**不**設 `author_is_owner=True`
> （避免身份混淆、避免誤觸 owner preempt 與 owner 私有記憶）。它是「受信任的 introspection + 可靠對話」通道，
> **不是**「你現在是主人」。故 debug 的 origin_tier 仍是 external_public（D1 一致）。

### C.3 唯讀 introspection：`get_state` / `get_recent` + **新 IPC query 協定**

這是 debug 模式**唯一真正的新 daemon code**。今天 IPC **沒有**「查詢 daemon 狀態」的訊息型別
（`ClientMessage` 只有 text/webrtc/utterance/interrupt/channel_*，`messages.py:72-82`）——所有 ServerMessage
都是**串流/事件推送**（`messages.py:147-150`），沒有 request/response 查詢。故需新增一組**最小 query 協定**：

**新增 ClientMessage（mcp 連接器 → daemon）——每則帶 REQUIRED daemon token：**

```python
class QueryState(BaseModel):
    type: Literal["query_state"] = "query_state"
    query_id: str                 # 連接器產生的關聯 id（把 response 對回 request）
    token: str                    # **必填**；daemon 比對 settings.mcp.query_token，不符 fail-closed

class QueryRecent(BaseModel):
    type: Literal["query_recent"] = "query_recent"
    query_id: str
    token: str                    # **必填**；同上
    n: int = 20                   # 上限由 daemon clamp（YAGNI，例如 max 100）
```

**新增 ServerMessage（daemon → mcp 連接器）——定死 payload 形狀：**

```python
class QueryResult(BaseModel):
    type: Literal["query_result"] = "query_result"
    query_id: str                 # 對回請求
    ok: bool                      # false = token 不符 / 被拒（fail-closed，不回資料）
    payload: dict                 # 形狀見下（get_state / get_recent 各自定死）
```

**payload 形狀（定死，P2 plan-writer 不必猜；F4 依此斷言）：**

- `get_state` → `{"mood": <str>, "current_self": <str>}`。
  - `mood`：Self-First 情緒狀態（step 19）；序列化為**短字串**（`self._state.mood` 的顯示值）。
  - `current_self`：慢變演化「現在的我」（step 30）；序列化為**字串**（`self._state.current_self` 的
    ratified 敘述）。
  - **`energy` 已移除**：spec 先前列的 `energy` 屬**內部能量預算 gate**（`mind_loop.py:620` 的
    `self._state.energy`），是 sleep-consolidation 的內部旋鈕，**不是** introspection 面該外洩的狀態，
    且無對外語義。v1 introspection **不回 energy**。
- `get_recent` → `{"items": [{"kind": <str>, "text": <str>, "ts": <float epoch-seconds>}, …]}`，最多 `n` 筆
  （daemon clamp 到 max 100）。**每項的 tier scope 見下方安全段——只含 external_public-origin，不含 owner
  私有對話。**

**daemon 端處理（`kernel._handle_message` 新增兩個 branch）＋ state 存取路徑：** 唯讀旁路。
mind-loop-internal 的 `mood`/`current_self`/`recent_perceptions`/`recent_outputs` 由 kernel 透過**既有的
mind-loop 參照**讀取（kernel 已持有 mind loop 以驅動 turn；query handler 走同一參照的唯讀 accessor，不新增
跨層管線）。組成 `QueryResult` 後 `put_nowait` 到**發問那條連線的 sink**（不進 perception queue、不觸發
cascade——純讀取旁路）。

> **讀一致性（snapshot，無 await 交錯）：** query handler **必須**以**同步 snapshot** 組 payload——在
> 讀欄位/list 到 `put_nowait` 之間**不得 await**，先 copy（`list(...)` / 取值）再組——因 `recent_perceptions`
> 由執行中的 turn append（`mind_loop.py:342`）、`recent_outputs` 於串流中 append（`mind_loop.py:1286`）、
> `current_self` 可能正在 ratification 中途。同步 snapshot 保證觀察到一致的 point-in-time 視圖，不會撞見
> 半改的 list 或 mid-ratify 的 `current_self`。

**get_recent 的 tier scope（fail-closed，鏡射其他 external 讀路徑）：** `self._state.recent_perceptions`
（`mind_loop.py:342` 對**每一則** perception append，含 owner 的 `UserSpoke` 於 `data['text']`）與
`recent_outputs`（含對**所有 origin** 講過的每句話）**都不是 tier-scoped**，與其他所有對外讀路徑相反
（Recall `tools.py:310`、associative_search `mind_loop.py:436-438`、`_derive_memory_hits` 抑制
`mind_loop.py:747` 都 fail-closed scope 到 `external_public/`）。若原樣回傳，`get_recent` 呼叫者會拿到 owner
的私密 DM/語音對話**逐字**、**跨 tier**——而「最近的 owner 對話」正是這裡最敏感的 payload。**因此 daemon 的
`get_recent` handler MUST 過濾**：只回 origin 為 **external_public** 的 perception/output（排除 internal /
external_dm / owner-origin 項，或至少 redact 其 `text`），鏡射他處用的 `source_prefix` allowlist。§C.3 對
`get_recent` 的敘述據此更正：它**回**近期 external_public-origin 的互動摘要，**不回** owner 私密對話、不回
私有記憶檢索、不回 trace。

**因此 debug secret 與 `query_token` 皆須以 owner 級敏感度對待**（強 secret ＋ §E 的 `.gitignore` 條目），
**非**先前草案所稱的「非敏感」。

**安全（token REQUIRED，daemon 端 fail-closed——不再是「授權只在連接器」）：** 先前草案把授權**只**放在 mcp
連接器（只有 debug 連線才有 `get_state`/`get_recent` 工具），但那是**假邊界**：daemon IPC server 對連線
**無任何鑑權**（`server.py:77` `serve(on_connect, host, port)` 無 token/handshake），任何本機行程都能開
`ws://127.0.0.1:9876` 直接送 `{type:"query_recent", n:100}` 而**完全繞過** mcp secret gate（secret 從不經過
daemon）。且 `get_recent` 回的是 owner 私有對話（見下），這使繞過**不是 cosmetic 而是 owner 資料外洩**。

**因此 v1 改為：daemon 端對 query REQUIRE 一個 daemon 也知道的 token，fail-closed（R-DECISION-4）。**
`settings.mcp.query_token`（daemon config，非空時啟用 query 面）與 mcp 行程共享同一秘密（連接器從 `mcp.toml`
讀、放進每則 `QueryState`/`QueryRecent` 的 `token`）；daemon branch **先比對 token，不符或缺 token 即回
`QueryResult(ok=false, payload={})` 並 log**，**不執行查詢、不回任何資料**。這把「唯讀 + loopback」從
**唯一**防線降級為**縱深防禦的其中一層**，真正的閘是 token。（若 `query_token` 未設，query 面**整個停用**——
fail-closed，`get_state`/`get_recent` 對 daemon 一律 `ok=false`。）**授權在連接器（工具是否曝露）仍保留為
第一層 UX gating，但不再是安全邊界。**

### C.4 v1 introspection scope（YAGNI 硬邊界）

**v1 只有 `get_state` + `get_recent`。明確不做**（deferred）：full trace dump、perception 注入、觸發
reflection/consolidation、任何**寫入**型 introspection。debug 是「唯讀觀測 + 可靠對話」，不是遠端控制台。

---

## D. Reply 語意、per-channel turn-end 協定新增、工具面

### D.1 工具面（MCP tools）

| 工具 | 模式 | 語意 |
|---|---|---|
| `talk(name, message)` | peer + debug | peer：走 external_public agency（她可不回）。debug：走 nudged 高可靠回應（best-effort，非硬保證）。回傳**結構化結果** `{status, text}`（§D.3），非裸字串。 |
| `get_state()` | **debug only** | 回 `{mood, current_self}`（§C.3；**不回 energy**——已移除，屬內部能量預算旋鈕，無對外語義）。需 daemon `query_token`。 |
| `get_recent(n=20)` | **debug only** | 回最近 n 筆 **external_public-origin** perception/output `{items:[{kind,text,ts}]}`（§C.3；**tier-scoped，不回 owner 私密對話**）。需 daemon `query_token`。 |

（**future，不做**：Doll 主動發起聯絡 peer——需 daemon→連接器的反向推送 + peer 可定址，v1 無此需求，§H
開放決策 5 記為 future。）

### D.2 協定 gap：per-channel turn-end（precise）

**Ground truth（G5）：** `talk` 必須回**單一**結果，但 Doll 的 reply 是**多句串流**的 `AddressedText`。連接器
需要知道「這個 channel_id 的這一輪講完了」才能 join + return。但今天：

- Doll 每輪結束時 `sink.put_nowait(None)`（`mind_loop.py:647`）。
- pump 把 `None` 轉成**全域** `TurnEnd()`（`server.py:138-139`）——而 `TurnEnd` **沒有 channel_id 欄位**
  （`messages.py:105-106`）。
- discord-bridge 因為只是**逐句 live-forward** 到 Discord、從不需要知道一輪何時結束，所以**明文忽略**
  `TurnEnd`（`__main__.py:286-287`）。

單一 mcp WS 連線可能**多工多個 channel**（多個 peer 共用一條連線，或未來一個 peer 多輪），全域無 channel_id
的 `TurnEnd` **無法分辨是哪個 channel_id 的輪結束了**。這就是 §0.1-G5 的 gap。

**最小協定新增（precise）——`TurnEndAddressed`：** 新增一個**帶 channel_id** 的 turn-end ServerMessage，
與 `AddressedText` 對稱（`messages.py:138-145`）：

```python
class TurnEndAddressed(BaseModel):
    """外部 origin 一輪串流結束的標記,帶 channel_id 讓多工連接器知道是哪條 channel
    講完了(對稱於 AddressedText)。內部(origin-less)turn 仍發全域 TurnEnd,不變。"""
    type: Literal["turn_end_addressed"] = "turn_end_addressed"
    channel_id: str
```

加入 `ServerMessage` union（`messages.py:147-150`）。**daemon 端最小改動——修改點是 `_run_one_turn` 的
finally**（**`mind_loop.py:641-647`，那個 finally 包住 line 640 對 `_llm_iterate` 的呼叫；它在
`_run_one_turn`，*不是*在 `_llm_iterate` 內部——照字面改 `_llm_iterate` 會改錯函式**）：在 `current_origin`
為 external 時發 `TurnEndAddressed(channel_id=origin)` 而非全域 `None`/`TurnEnd`。精確規則對稱於
`_emit_sentence`（`mind_loop.py:1275-1280`）：

```python
# mind_loop._run_one_turn finally（line 647 現況：sink_resolver(current_origin).put_nowait(None)）
origin = self._ctx.current_origin
registry = self._ctx.channel_registry
if origin and registry is not None and registry.locus_of(origin) == "external":
    self._ctx.sink_resolver(origin).put_nowait(TurnEndAddressed(channel_id=origin))
else:
    self._ctx.sink_resolver(origin).put_nowait(None)   # 內部/voice 路徑：與現況 line 647 逐字相同
```

- **else 分支不改變 internal/voice 語義（消除 finding 風險）：** line 647 現況本就是
  `self._ctx.sink_resolver(self._ctx.current_origin).put_nowait(None)`——**不是**捕獲的 `sink` 區域變數。
  故 else 分支對 internal/None origin 走**與今天完全相同**的 `sink_resolver(origin)` 解析，discord-bridge
  與 voice pipeline 依賴的 turn-end 路徑**by construction 不變**；只有 external 分支是新行為。
- **mid-turn disconnect 邊界（明訂為 intended，非 bug）：** `locus_of` 對**未註冊** channel_id 回
  `"internal"`（`channel_registry.py:32-34`）。若 peer 在 turn 中途斷線，`_handle_disconnect` 會在 finally
  跑之前 unregister 其 channel（`kernel.py:955-961`），此時 finally 走 else 分支發全域 `None`/`TurnEnd`，
  該連線的 `TurnEndAddressed` 不會來 → 連接器落到 §D.3 timeout。**這是刻意邊界**（peer 都斷線了，沒有 sink
  可送）；連接器以 timeout 明確 surface，不 hang。（實作可選：turn 起始時捕獲 origin 是否 external，供 finally
  判斷，避免 finally 內重讀 `locus_of`——但即使不捕獲，斷線落 timeout 仍是可接受結果。）

- **不動 discord-bridge**：bridge 本就忽略非 `addressed_text` 訊息（`__main__.py:282-287`），多一個
  `turn_end_addressed` 型別它一樣忽略——零影響、契合 no-touch。
- pump 對 `TurnEndAddressed`（一個正常 ServerMessage）直接 `encode_server_message` 送出（`server.py:140-141`），
  **不經過** `None`→`TurnEnd` 轉換——無歧義。

> **替代方案（記錄，未採）**：在 `AddressedText` 加 `final: bool` 旗標。缺點是最後一句與「輪結束」耦合，
> 且**零句 reply**（她選擇不回，§D.3）時沒有任何 `AddressedText` 可掛 `final` → 收不到結束訊號。故採獨立
> `TurnEndAddressed`，它在零句情形下**仍會發**（finally 一定跑，`mind_loop.py:641-647`「Always fires once per
> real turn even on error」），連接器據此可靠地判定「已讀不回」。

### D.3 reply 收集 + 「已讀不回」+ timeout

連接器對一次 `talk`：發 `ChannelEvent` → 收集該 channel_id 的 `AddressedText` → 收到
`TurnEndAddressed(channel_id)` 時 join 所有句子回傳。

**回傳契約（定死，三個 outcome 各有 discriminator——honor no-fallback：每個邊界明確 surface）：** `talk`
回**結構化結果** `{status: "reply" | "no_response" | "timeout", text: str}`，**不是**裸字串，讓 peer AI 能
programmatically 區分「她拒答」「逾時」「空回覆」：

- `status="reply"`、`text=<join 後的句子>`：她回了，turn-end 到達且至少一句 `AddressedText`。
- **`status="no_response"`、`text=""`：零 `AddressedText` + turn-end 到達** ⇒ 她選擇不回（peer agency，
  已讀不回）。明確與 hang 區分。
- **`status="timeout"`、`text=<目前已收集的部分，可能空>`**：連接器對「等 turn-end」設上限
  （**v1 hardcode ~60s**，見 §H.6），逾時回此，避免 peer 端無限 hang。**No-fallback**：timeout 是**明確
  surface 的邊界**，不是靜默重試。

---

## E. 安全 / 威脅模型

**external-safety invariant（最高原則）：** 兩個模式都是 `ChannelMessage` ∈ `_EXTERNAL_KINDS`
（`mind_loop.py:87`），且 origin_tier 為 `external_public`（§B.4/§C.2），因此工具 registry 被硬收斂成
`EXTERNAL_TOOLS`（`mind_loop.py:836-843`）——**結構性無 Shell / SpawnWorkflow / SpawnMonitor / RemoveMonitor /
InvokeSkill / WriteSchedule / SelfRevision**（`mind_loop.py:802-805` 明列）。**一個 AI peer 連進來 ≠ 電腦被
入侵**（P1e S4）。debug 模式**同樣**受此硬擋——secret 只解鎖唯讀 introspection 與「可靠回應」nudge，**不**
擴權工具。

**loopback-only bind（§A.1/§A.4）——code-enforced，非僅註解：** MCP server bind `127.0.0.1` → 未鑑權的
注入面**僅限本機**。**整個威脅模型倚賴此不變式**（peer 注入面 local-only、且 §C.3 未經 daemon token 的舊
論證亦以 loopback 為前提），故連接器讀 `mcp.toml` 時**fail-closed 驗證** `bind_host ∈ {"127.0.0.1", "::1",
"localhost"}`，任何其他值（如使用者照抄教學設 `"0.0.0.0"`）**直接 raise、不啟動**——避免一個「唯一安全值＝
預設值」的欄位變成 fail-open 靜默洞（house rule 無 fallback：沒有網路層鑑權可退守）。遠端 peer 必須由使用者
自跑 tunnel（SSH/authenticated reverse proxy）——**v1 不做 HTTP 對外網路暴露**（YAGNI）。**非 loopback bind
在 code 被拒，不只是被註解勸阻。**

**name-spoof（§B.2）：** peer 的 `name` 是**自稱、未驗證**。`author_id="mcp:<name>"`、`author_is_owner=False`
恆為 False → 絕不污染 owner 身份、絕不觸發 owner preempt（`kernel.py:776`）、絕不進 external_dm（D1）、絕不
解鎖任何能力（trust 由 registry availability 強制，非 post-hoc 讀取，`mind_loop.py:824-825`）。render 明標
「自稱、未驗證」（§B.4）。對齊 P1e name-spoof 慣例 S4/L0。

**debug secret（§C.1）：** 存 `mcp.toml`。**Ground truth（D4）：`.gitignore` 目前有 `bridge.toml`
（`.gitignore:26`）但沒 `mcp.toml`**（`*.local.toml`/`config.*.toml` 都 match 不到裸 `mcp.toml`）。**必須在
`.gitignore` 加 `mcp.toml`**，並提供 `mcp.example.toml` 範本（比照 `bridge.example.toml`，見近期 commit
261f674）。空 `debug_secret` = debug 模式停用（fail-closed）。secret 只在 mcp 行程內比對，daemon 不經手。

**provenance（記憶攻擊面，§B.4）——寫側 scoped，但讀側必須補 tag/排除（R-DECISION-5）：**
**寫側**確實 scoped：peer 的 `NoteMemory`/`WriteDiary` 經 `_ORIGIN_DIR` 路由到 `external_public/`
（`tools.py:56-60,122,158`），auto-write scope 亦受限（`mind_loop.py:741-747`）。**但讀側會把它漏回 owner**：
在 **owner turn**（origin_tier internal/external_dm）上，自動注入的 `[Memory context]`（`_derive_memory_hits`
只在 external_public 抑制，`mind_loop.py:747` → owner turn 跑**未過濾**的 `memsearch.search`）**與**顯式
`Recall`（`tools.py:310` 只在 external_public turn 設 `source_prefix`）**都會檢索到 `external_public/`**；而
`_format_hit`（`tools.py:89-97`）**只**對 `consolidated/` 加 provenance 前綴、**不對** `external_public/` 加。

**具體攻擊**：一個自主 AI peer 透過 MCP 進行 owner 從未看見的長對話（unattended、高頻、對 owner 不可見——
比既有 Discord 公開頻道路徑**嚴重得多**），說服 Doll `NoteMemory` 一條捏造「事實」（`NoteMemory` 在
`EXTERNAL_TOOLS`，`tools.py:1117`）；它落在 `external_public/<date>.md` 並被索引。**日後某個 owner turn**，同一條
捏造句在 owner 的 `[Memory context]` 以**無標記**的 `- <date> <text>` bullet 浮現，與 Doll 自己的可信記憶
**無從區分**。

**因此本規格要求（擇一，實作 plan 定案）：** 在 **owner/internal-tier 檢索**時，要麼 (a) **把
`external_public/` 排除**於自動注入的 `[Memory context]` 與未 scoped 的 `Recall` 之外（該 tier 只在明確 demand
時才浮現），要麼 (b) 讓 `_format_hit` 對 `external_public/` hit **加明確的 untrusted-provenance 前綴**
（例如 `[外部AI·未驗證]`），一如 `consolidated/` 得到 `[系統整併·待確認]`。**MCP peer 模式使此讀側洩漏
從既有 latent 問題升級為 must-fix**——不可只斷言 invariant 已成立。對齊
`ref_memory-write-paths-are-attack-surfaces`。

**IPC query 授權（§C.3，R-DECISION-4）——daemon 端 REQUIRED token，fail-closed：** 先前草案把授權**只**放在
mcp 連接器是**假邊界**：daemon IPC server 對連線**無鑑權**（`server.py:77` 無 token/handshake），任何本機
行程都能開 `ws://127.0.0.1:9876` 直接送 `query_recent` 拿 owner 資料、**完全繞過** mcp secret gate（secret
從不經 daemon）。因 `get_recent` 回的是 owner 私密對話（見 §C.3 tier-scope 段），此繞過是**owner 資料外洩**，
非 cosmetic。**故 daemon 對 `QueryState/QueryRecent` REQUIRE `token` 欄位，比對 `settings.mcp.query_token`，
不符/缺 token 即 `QueryResult(ok=false)` 且不查詢**（`query_token` 未設 = query 面整個停用）。深度防禦仍疊
loopback-only + 唯讀 + get_recent tier-scope（只回 external_public-origin）；連接器工具曝露是**UX 第一層**，
但**安全閘是 token**，不再宣稱「唯讀 + loopback」自足。owner-級敏感度：`query_token`/`debug_secret` 皆入
`.gitignore`（見下）。

**注入面（子行程 spawn）：** `_build_mcp_spec` 用 `create_subprocess_exec`（argv list、無 shell，
`service_supervisor.py:105-109`）→ 路徑等值不被 shell 解讀。argv 只含 `mcp.toml` 的**路徑**、不含 secret →
`ps`/journal 看不到秘密（比照 bridge token，內化規格 §8）。

**crash-isolation（§A.3）：** 已述——mcp 行程崩潰碰不到 daemon；PDEATHSIG 保證 daemon 死則 mcp 死、不留孤兒
（`service_supervisor.py:50-56`）。

---

## F. 測試

比照內化規格：**完全不碰真 MCP client / 真 peer**，用 fake 對接。

- **F1 — mcp 連接器 ⇆ IPC 映射（fake daemon-WS）：** 用一個 fake WS server 冒充 daemon，斷言 `talk` →
  `ChannelRegister`（每次 talk 唯一 `mcp:<conn_uuid>:<call_uuid>`，§B.1）+ `ChannelEvent`（payload 欄位精確如
  §B.2，含 `author_id="mcp:<name>"` 而路由鍵是唯一 channel_id）；**兩個並行 `talk` 得到兩個不同 channel_id、
  各自 demux 不串線**（finding 對併批/同名撞頻的回歸）；`get_state`/`get_recent`（debug）→
  `QueryState`/`QueryRecent`（帶 `token`）；收到 `AddressedText` → 收集；收到 `TurnEndAddressed(channel_id)` →
  join + return `{status:"reply", text}`；零句 + turn-end → `{status:"no_response", text:""}`；逾時 →
  `{status:"timeout", …}`；**收到裸 `TextChunk`/全域 `TurnEnd` → 忽略、不污染任何 talk 結果（§B.6）**。
- **F2 — daemon 端 mcp-kind admit/sandbox：** 餵 `ChannelRegister(kind="mcp")` + `ChannelEvent(is_dm=True,
  author_is_owner=False)`，斷言：一定 admit（`l0_dm`）；origin_tier == `external_public`；`_active_tool_registry`
  回 `EXTERNAL_TOOLS`（無 Shell/Workflow）；`_perception_summary` 走 AI-peer 分支（§B.4）。
- **F3 — debug-secret gate：** 相符 → `get_state`/`get_recent` 工具存在 + `talk` 帶 `debug_reliable`；不符/空/
  未呈 → 工具不存在、`talk` 走一般 agency。fail-closed 斷言。
- **F4 — IPC query 協定：** 帶正確 `token` 的 `QueryState`/`QueryRecent` → daemon 回對應 `query_id` 的
  `QueryResult(ok=true, payload=…)`，payload 形狀精確如 §C.3（get_state=`{mood,current_self}` **無 energy**；
  get_recent=`{items:[{kind,text,ts}]}`）；**token 錯/缺 → `QueryResult(ok=false)`、不回資料（fail-closed）**；
  **get_recent 只含 external_public-origin，餵入 owner-origin 的 recent 項不外洩（tier-scope 回歸）**；讀為
  **同步 snapshot**（併發改 list 不撞見半改狀態）；且**不**進 perception queue、**不**觸發 cascade。
- **F5 — 新 message 型別 round-trip：** `TurnEndAddressed` / `QueryState` / `QueryRecent` / `QueryResult` 的
  `decode_client_message` / `encode_server_message` round-trip；discriminator union 不歧義。
- **F6 — fake-peer 端到端：** 假 peer → 假 daemon（跑 mind cascade 的最小替身或真 mind loop with stub LLM）→
  reply 串回 → `talk` 回字串。含「已讀不回」路徑。
- **F7 — ServiceSupervisor 第二服務：** register bridge + mcp 兩個 spec，斷言 `start()` 起兩個 supervise task、
  `stop()` 收兩個、`status()` 回兩筆；mcp crash-loop 觸發 `on_gave_up` → `McpDown` perception（鏡射既有
  bridge 測試）。
- **F8 — live-smoke（人工，CI 不可跑）：** 真 daemon + `[mcp].enabled=true` + 真 MCP client（Claude 或
  `mcp` SDK 的 client）連 loopback，peer `talk` 得到回覆或已讀不回；debug secret + `query_token` → `get_state`
  回 `{mood, current_self}`、`get_recent` 只見 external_public-origin（無 owner 私密對話）；
  `kill -9` daemon 後 `pgrep -f dollos.mcp_server` 為空（PDEATHSIG 驗證）。列入 `docs/dollosctl-smoke.md`。

---

## G. 單概念 Task 拆解（給 SDD）— 分兩階段

每個 task 一個概念、一支 subagent、TDD。**P1 = peer（先落地可用）；P2 = debug（疊加）。**

### 階段 P1 — Peer talk

- **Task 1 — `[mcp]` config schema（最小）。** `config.py` 加 `McpConfig`（**只有 `enabled` + `config`** +
  `_expand_user` + `enabled⇒config` validator）+ `Settings` 註冊（`config.py:295` 旁）。純 config、無行為。
  測試：預設關、缺 config 的 enabled 報錯、extra key 被 forbid。（鏡射 `BridgeConfig` `config.py:139-162`。）
- **Task 2 — 新 IPC 訊息型別。** `messages.py` 加 `TurnEndAddressed`（ServerMessage）+ 併入 union
  （`messages.py:147-150`）。P1 **只需 `TurnEndAddressed`**（`QueryState/QueryRecent/QueryResult` 留給 P2
  Task 6）。測試：round-trip、union discriminator 不歧義（F5 子集）。
- **Task 3 — daemon 端 per-channel turn-end + mcp situational framing。** (a) `mind_loop._llm_iterate` finally
  對 external origin 發 `TurnEndAddressed(channel_id)` 而非全域 `None`/`TurnEnd`（§D.2，對稱 `_emit_sentence`
  `mind_loop.py:1275-1280`）；(b) `mind_prompt._perception_summary` ChannelMessage 分支加 `channel_kind=="mcp"`
  的 AI-peer render（§B.4，`mind_prompt.py:394-402`）。**不動 discord-bridge**（它忽略新型別）。測試：external
  turn 發 `TurnEndAddressed`、內部 turn 仍發 `TurnEnd`；mcp perception 走 AI-peer 分支；F2 的 admit/sandbox。
- **Task 4 — mcp 連接器（peer）。** 新 module `src/dollos/mcp_server/`：MCP server（FastMCP/streamable-HTTP，
  bind loopback，**讀 `mcp.toml` 時 fail-closed 驗證 `bind_host` 為 loopback**，§A.4/§E）+ daemon WS client
  （鏡射 `discord_bridge/__main__.py:195` 的 `websockets.connect`）+ `talk` 工具：**每次呼叫 mint 唯一
  `channel_id=mcp:<conn_uuid>:<call_uuid>`（§B.1 R-DECISION-2，避免併批/同名撞頻）**、register-on-first
  （§B.1）→ `ChannelEvent`（§B.2，`author_id="mcp:<name>"` 承載身份）→ 收集該 channel 的 `AddressedText` 到
  `TurnEndAddressed` → join，**回結構化 `{status,text}`**（§D.3）；零句 → `no_response`；~60s hardcode
  timeout → `timeout`；**忽略所有非 `AddressedText`/`TurnEndAddressed`/`QueryResult` 的 server 訊息（§B.6）**。
  含 `pyproject.toml` 加 `mcp` 相依（§A.5）。測試：F1、F6。
- **Task 5 — kernel 接線（第二服務）＋ P1 dogfood 收尾。** `_build_mcp_spec`（§A.2，鏡射 `_build_bridge_spec`
  `kernel.py:647-662`）+ `_emit_mcp_down_perception`（鏡射 `kernel.py:664-678`，發 `McpDown`）+ `settings.mcp.
  enabled`&config-exists 才 register（鏡射 `kernel.py:453-461`）+ `McpDown` 的 `_perception_summary` render
  分支（鏡射 BridgeDown `mind_prompt.py:403-406`）。**＋ P1 dogfood 前提（從舊 Task 9 移入——P1 宣稱可獨立
  合併/dogfood，就必須自帶這些）：`.gitignore` 加 `mcp.toml`（D4）＋ `mcp.example.toml` 範本（比照
  `bridge.example.toml`，commit 261f674）＋ `config.example.toml` 的 `[mcp]`（enabled/config 兩行；`query_token`
  留待 P2）。** 否則 dogfood P1 得手寫一個未 gitignore、無範本的 `mcp.toml`，有誤 commit 風險。測試：F7。
  Live-smoke：F8 的 peer 半 + PDEATHSIG。

### 階段 P2 — Debug（疊加在 P1 之上）

- **Task 6 — IPC query 協定（daemon 側）。** `messages.py` 加 `QueryState`/`QueryRecent`（ClientMessage，**帶
  必填 `token`**）+ `QueryResult`（ServerMessage，**帶 `ok`**）+ 併入兩個 union。`config.py` `McpConfig` 加
  `query_token`。`kernel._handle_message` 加兩個唯讀 branch：**(1) 先比對 `settings.mcp.query_token`，不符/缺/
  未設 → `QueryResult(ok=false)` 不查詢（fail-closed，§C.3 R-DECISION-4）**；(2) 通過才讀既有 in-memory state
  作**同步 snapshot**（`mood`/`current_self`——**不含 energy**；`get_recent` 讀 `recent_perceptions`/
  `recent_outputs` 但 **tier-scope 到 external_public-origin**，排除 owner/internal/external_dm 項）→ 回
  `QueryResult` 到發問 sink，**不進 perception queue**（§C.3）。測試：F4、F5 其餘。
- **Task 7 — mcp 連接器 debug 模式。** §C.1 secret gate（比對 `mcp.toml` `debug_secret`，fail-closed）+ debug
  連線多曝 `get_state`/`get_recent`（送 `QueryState`/`QueryRecent`、**帶 `mcp.toml` 的 `query_token`**、對回
  `query_id`）+ `talk` 帶 `debug_reliable`。測試：F3。
- **Task 8 — 可靠回應 nudge（daemon 側）。** debug payload 的 `debug_reliable=True` → 該 origin 的 prompt 加
  「除錯通道請務必實質回覆」情境 nudge（§C.2），**不改 origin_tier**（仍 external_public、仍無 Shell）。測試：
  帶旗標時 prompt 含 nudge、不帶時不含；工具 registry 仍是 EXTERNAL_TOOLS。
- **Task 9 — debug 文件收尾（gitignore/template/config.example 已於 P1 Task 5 落地）。** `mcp.example.toml`
  補上 `debug_secret`/`query_token` 欄位說明＋ `config.example.toml` 的 `[mcp].query_token` 一行（P2 才需）＋
  `.gitignore` 若尚未涵蓋 `query_token` 所在檔（即 `mcp.toml`，P1 已加）確認無誤 + `CLAUDE.md` 架構段記
  「daemon 內化 mcp-server（第二個 ServiceSupervisor 服務；peer=DM-over-MCP、debug=secret-gated 唯讀
  introspection）」+ `roadmap.md` 加這步 + `docs/dollosctl-smoke.md` 的 F8 debug checklist（secret/query_token
  使用說明）。

**依序：** P1 `1 → 2 → 3 → 4 → 5`（config→型別→daemon 接縫→連接器→kernel），可先合併、先 dogfood peer 模式。
P2 `6 → 7 → 8`（IPC query→連接器 debug→可靠 nudge），9 收尾。P1 與 P2 之間可停一次做 live-smoke。

---

## H. 開放決策（需使用者拍板）

1. **（安全用詞更正，強烈建議採用）** 採用 §0.1-D1 的更正：mcp peer/debug 皆 `author_is_owner=False` →
   **origin_tier = external_public**（非核可設計字面的 external_dm）。理由：external_dm 會給未驗證 AI peer
   **owner 的私有記憶檢索**（`mind_loop.py:315-320`），是漏洞；external_public 才 fail-closed。「direct 1:1／
   總是送達」由 admission（is_dm→l0_dm）+ engaged 短窗提供，與記憶 tier 無關。**傾向：採用更正。← 需確認。**
2. **可靠回應的 mechanism（§C.2）：** prompt-level nudge（帶 `debug_reliable` 旗標，不改 tier/registry）
   vs 其他。**傾向：nudge**（軟機制、能力邊界仍硬擋）。**← 需確認 nudge 措辭與是否足夠可靠。**
3. **secret gate 的呈遞方式（§C.1）：** 一個 `authenticate(secret)` MCP 工具 vs transport header vs 連線時
   query param——取決於採用的 MCP SDK / streamable-HTTP 能力。**← 需在選定 SDK 後定案（YAGNI：先能動）。**
4. **IPC query 的 daemon 端 token（§C.3/§E，R-DECISION-4——已改為 REQUIRED）：** 先前草案傾向「不做，靠
   loopback + 唯讀 + 授權在連接器」，但 adversarial review 指出那是**假邊界**：daemon IPC 無鑑權
   （`server.py:77`），任何本機行程可繞過 mcp secret 直接送 `query_recent` 拿 owner 私密對話。**故 v1 改為
   daemon 端 REQUIRE `query_token`（fail-closed），並把 `get_recent` tier-scope 到 external_public-origin。**
   使用者原核可的「peer 無鑑權 + debug secret」**不受影響**：peer 的 `talk` 仍無鑑權；此 token 只鎖**debug-only
   的 introspection query 面**，與 debug secret 同屬「開發者才有」的秘密。**← 確認採用 REQUIRED token（傾向：
   採用，這是安全修正）。**
5. **Doll 主動發起聯絡 peer（§D.1）：** v1 **不做**（需 daemon→連接器反向推送 + peer 可定址）。記為 future。
   **← 確認列 future。**
6. **timeout（§D.3）——已收斂為 hardcode。** 原提案「放 `mcp.toml` 可調」是 YAGNI（v1 一個常數即可，且
   `[server]` 少一個必須驗證的欄位）。**決定：v1 在連接器 hardcode ~60s，`mcp.toml` 不列 timeout 欄位；
   若日後真有 per-deploy 需求再開。** ← 確認 60s 數值即可。
7. **連接器在 internal-sink pool 的錯投放大（§B.6）：** 每條 IPC 連線都被無條件註冊為 internal sink
   （`kernel.py:940`），第二條常駐連線（mcp 連接器）放大 SinkResolver 的 most-recent-internal 錯投面。v1
   **接受**：連接器忽略所有非 addressed 訊息（不誤收、不外洩），內部輸出即使錯投到連接器也被丟棄。**← 確認
   接受此 v1 邊界；若日後 UI 內部輸出遺失，正解是 internal turn 定址指定 UI sink 或連接器 external-only 註冊
   （需 `_handle_connect` opt-out，非 v1）。**

---

## 附錄：本規格 grounding 的 file:line 索引（ground truth）

- IPC 型別：`ipc/messages.py:50-59`（ChannelRegister）、`:62-69`（ChannelEvent）、`:100-107`（TextChunk/TurnEnd，
  **TurnEnd 無 channel_id**）、`:138-145`（AddressedText）、`:147-150`（ServerMessage union）。
- pump None→全域 TurnEnd：`ipc/server.py:138-139`；bridge 忽略 TurnEnd：`discord_bridge/__main__.py:286-287`。
- admit is_dm short-circuit：`mind/attention.py:126-127`（_l0_signal→l0_dm）、`:153-178`（admit）、`:243`
  （**window_for**，第二處 short-circuit）。
- discord DM payload 形狀：`discord_bridge/controller.py:250`（author_is_owner 推導）、`:277-290`
  （ChannelRegister + ChannelEvent payload）。
- kernel channel 處理：`kernel.py:730-738`（ChannelRegister→registry+sink）、`:739-795`（ChannelEvent→
  window_for→admit→debounce）、`:799-811`（batch→ChannelMessage perception）、`:813-822`
  （_register_external_sink，locus="external"）。
- origin_tier：`mind/mind_loop.py:323-328`（external_dm 需 author_is_owner AND is_dm，否則 external_public）。
- external 工具沙箱：`mind/mind_loop.py:87`（_EXTERNAL_KINDS）、`:836-843`（EXTERNAL_TOOLS）、`:802-805`
  （明列禁用 Shell/…）。
- AddressedText 發送：`mind/mind_loop.py:1275-1280`（_emit_sentence）、`:641-647`（turn-end None sentinel）。
- ChannelMessage render：`mind/mind_prompt.py:394-402`（owner/陌生人，**無 AI-peer 分支**）；BridgeDown render：
  `:403-406`。
- ServiceSupervisor：`service_supervisor.py:87-101`（register/start）、`:165-188`（stop）、`:190-199`（status）、
  `:50-56`（PDEATHSIG）；kernel 註冊點：`kernel.py:453-461`、`_build_bridge_spec`：`:647-662`、
  `_emit_bridge_down_perception`：`:664-678`、`_derive_daemon_ws`：`:358-369`。
- config：`config.py:25-26`（ipc host/port 預設 127.0.0.1:9876）、`:139-162`（BridgeConfig）、`:295`（Settings 註冊）。
- 相依：`pyproject.toml:6-19`（**無 mcp/fastmcp**）。gitignore：`.gitignore:26`（bridge.toml；**無 mcp.toml**）。
- 併批/coalescing（§B.1 R-DECISION-2）：`batch_accumulator.py:19-35`（同 channel_id debounce 併一次 flush）、
  `kernel.py:793-811`（drain_grouped 按 channel_id regroup 成一 bucket）、`mind_loop.py:284-289`（一 bucket=一
  turn=一 origin）、`mind_loop.py:323-328`（`_derive_origin_tier` 掃第一則決定 tier）。
- internal-sink pool（§B.6）：`kernel.py:940`（每連線無條件註冊 internal sink）、`kernel.py:813-824`
  （`_register_external_sink` 只加不移除 internal）、`sink_resolver.py:58-62`（external 精確比對）、
  `sink_resolver.py:63-66`（origin-less → most-recent internal）、`kernel.py:955-961`（`_handle_disconnect`
  unregister channel）。
- IPC server 無鑑權（§C.3/§E）：`server.py:77`（`serve(on_connect, host, port)` 無 token/handshake）。
- 記憶讀側 provenance（§E R-DECISION-5）：`tools.py:56-60,122,158`（`_ORIGIN_DIR` 寫側 scope）、
  `tools.py:310`（Recall 僅 external_public turn 設 source_prefix）、`mind_loop.py:747`（`_derive_memory_hits`
  僅 external_public 抑制 → owner turn 未過濾）、`tools.py:89-97`（`_format_hit` 僅 consolidated/ 加前綴）、
  `tools.py:1117`（NoteMemory ∈ EXTERNAL_TOOLS）、`channel_registry.py:32-34`（`locus_of` 未註冊回 internal）。
- query state 存取（§C.3）：`mind_loop.py:620`（`self._state.energy` 內部能量預算，**不外洩**）、
  `mind_loop.py:342`（recent_perceptions append）、`mind_loop.py:1286`（recent_outputs append）。
