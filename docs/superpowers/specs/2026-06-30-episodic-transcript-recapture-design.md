# Spec — B1: Episodic 逐字稿重接（Episodic Transcript Recapture）

- **Date**: 2026-06-30
- **Status**: Design（待 writing-plans）
- **Scope**: 本 spec 只做 B1。A1 self-profile、B2 consolidation、B3 energy 各自後續獨立 spec。

## 1. 背景與問題

DollOS 的對話逐字內容**從不進入可搜尋記憶**。Doll 唯一的長期記憶來自她主動呼叫 `NoteMemory` 記下的零星事實；實際對話（使用者說了什麼、Doll 回了什麼）一旦滑出 context 視窗就永久消失。

程式裡本來就有對應功能 `append_transcript`（`src/dollos/memory_writer.py:22`），作用是把每輪對話寫進當天逐字稿檔並重新索引。但它的呼叫點在兩次重構中被刪除（`4813954` 砍 Say、`06dcfc7` MindLoop 取代 EventDispatcher），現在 **src 內零 caller**（codegraph trail 確認：只有 5 個測試呼叫）。配套基礎設施全部還在：

- helper 在（簽名/格式已定）
- `transcripts/` 目錄開機建立並納入 `FtsMemory` 索引 paths
- `transcripts_root` 已一路 plumb 進 `MindCtx`（`src/dollos/mind/mind_ctx.py:43`）

亦即「電燈裝好了，開關的線被剪斷」。B1 = 把兩條線接回去。

### 為何優先（keystone）

B2 consolidation 需要對話素材才能整理；B3 energy 需要活躍度訊號。兩者都踩在「對話有被存下來」之上。對話現在沒存 → 後續皆空中樓閣。B1 又是最便宜的一塊（重接，非從零造）。

## 2. 目標 / 非目標

**目標**：使用者每則發言、Doll 每輪說出的完整內容，自動寫進當日逐字稿檔（`transcripts_root/YYYY-MM-DD.md`）並進入 `FtsMemory` 索引，立即可被 `[Memory context]` / `Recall` 召回，並成為 B2 的素材。

**非目標**：
- 不抓系統 perception（schedule / monitor / subagent result / reflection）——它們有自己的紀錄，混入只會稀釋對話語料。
- 不做 consolidation / 摘要 / 淘汰（B2）。
- 不改 `append_transcript` 的格式或行為（既有 5 測試保留）。

## 3. 現成元件（不改）

`append_transcript(*, transcripts_root: Path, memsearch: FtsMemory, role: str, text: str) -> None`
（`src/dollos/memory_writer.py:22`）：寫一行 `- HH:MM:SS {主人|我}說：{text}\n` 到 `transcripts_root/{今日}.md`，然後 `await memsearch.index_file(path)`。`role` 為 `"user"`→「主人」、`"doll"`→「我」。

`MindCtx`（`src/dollos/mind/mind_ctx.py:41-43`）已具備 `memsearch` / `memory_root` / `transcripts_root`，`MindLoop` 透過 `self._ctx` 取用。

## 4. 設計：兩個重接點

兩點都在 `MindLoop.iterate()`（`src/dollos/mind/mind_loop.py:139`），不碰 cascade 內部邏輯（除 doll 端的 buffer append，見下）。

### 4.1 User 端（單純）

`iterate()` 開頭已有 perception 迴圈（`mind_loop.py:145-159`）逐一處理 perceptions。在其中，對每個 `p.kind == "UserSpoke"` 的 perception：

```
await append_transcript(
    transcripts_root=self._ctx.transcripts_root,
    memsearch=self._ctx.memsearch,
    role="user",
    text=p.data.get("text", ""),
)
```

文字完整（`p.data["text"]`，與 `_derive_memory_hits` 取法一致，`mind_loop.py:267`）。空字串跳過。一個 turn 可能含多個 `UserSpoke` → 各寫一條。

### 4.2 Doll 端（需 turn-local speech buffer）

**關鍵限制**：`OutputRecord` 只有 `t / kind / summary` 三欄（`mind_state.py:85`），Doll 說的話在 `recent_outputs` 只存 `summary="spoke: {sentence[:60]}"`（**截斷摘要**，`mind_loop.py:598,620`）。完整逐字內容只存在於流向 sink 的 `TextChunk`、用完即逝。因此**無法**用「turn 邊界 diff `recent_outputs`」取得完整內容。

解法 — 加一個 turn-local 完整句緩衝：

1. `__init__` 初始化 `self._turn_speech: list[str] = []`（與 `self._cascade_ctx` 並列，`mind_loop.py:114` 附近）；在 `iterate()` 呼叫 `_llm_iterate(prompt)`（`mind_loop.py:229`）**之前** `self._turn_speech.clear()`。turn 邊界與 buffer 生命週期都集中在 `iterate()`，便於閱讀。單執行緒 asyncio 下單一 instance 緩衝安全（與既有 `_cascade_ctx` 同模式）。
2. 在兩個 emit 完整句子的點各加一行累積：
   - `_flush_chunker`（`mind_loop.py:594-602`）
   - `_handle_stream_event` 的 `SpeakChunk` 分支（`mind_loop.py:615-625`）
   
   兩處現都已 `sink.put_nowait(TextChunk(text=sentence))` + append 截斷摘要到 `recent_outputs`；額外加 `self._turn_speech.append(sentence)`（完整句）。
3. cascade 結束後（`iterate()` 內 `finally` 區塊之後、`self._state.iter_count += 1`（`mind_loop.py:239`）之前），若 `self._turn_speech` 非空：

```
text = "".join(self._turn_speech).strip()
if text:
    await append_transcript(
        transcripts_root=..., memsearch=..., role="doll", text=text,
    )
```

句子由 `SentenceChunker` 切分、自帶標點，`""` 連接即還原完整段落。寫入前 `strip()`，並在 helper 寫入時確保不引入裸換行破壞「一行一 bullet」格式（內容內換行替換為空白）。

**turn 層級寫一次**（非逐句）→ 避免逐句碎片化與同一日檔多次 reindex。

### 4.3 範圍判斷

user 條與 doll 條**各自獨立判斷**：純系統觸發但 Doll 主動說話的 turn → 只有 doll 條；使用者說話但 Doll 沒回（罕見）→ 只有 user 條。一般對話 turn → user + doll 成對。

## 5. 錯誤處理（不違反 no-fallback）

兩處 `append_transcript` 呼叫各自包 `try / except Exception` + `logger.exception(...)`，失敗**不打斷對話、不 crash loop**。這是附屬寫入的容錯，**不是功能降級**——失敗會明確 log（不 silent），與既有 side-channel（`associative_search` / `tool_habits_search`，`mind_loop.py:169-185`）同一模式。

## 6. 資料流

```
UserSpoke perception ─┐
                      ├─► append_transcript ─► transcripts_root/今日.md ─► FtsMemory.index_file
本 turn Doll Speech ──┘                                                      │
                                                                            ▼
                                              [Memory context] / Recall 召回（立即）
                                              B2 consolidation 素材（日後）
```

## 7. 測試

新增（live loop / iterate 層級）：
- `UserSpoke` perception → 寫出 `role="user"` 行，內容完整。
- 本 turn Doll 說 N 句 → 寫出**單一** `role="doll"` 行，內容為 N 句完整串接（驗證非截斷、非逐句多行）。
- 純系統 turn（無 UserSpoke、Doll 未說話）→ 不寫任何行。
- `append_transcript` 拋例外 → `iterate()` 不 crash，續跑下一輪（驗證容錯 + log）。
- user / doll 成對 ordering（user 行在先）。

保留：`tests/test_memory_writer.py` 既有 5 個 helper 測試不動。

## 8. 風險 / 已知限制

- **reindex 成本**：一個對話 turn 至多 2 次 `index_file`（user + doll）對同一日檔。`FtsMemory.index_file` 為單檔增量，成本低；若日後測得偏高，再批次成 turn 結束一次 reindex（YAGNI，本 spec 不做）。
- **`recent_outputs` maxlen 不影響**：doll 完整內容走獨立 `_turn_speech` buffer，不受 `recent_outputs` deque(maxlen=15) 截斷影響。
- **長 turn**：單 turn 超長 Doll 發言會寫成一條長 bullet；可接受（逐字稿本就忠實），consolidation（B2）負責後續壓縮。
- **at-least-once（WAL barrier 之外）**：user 寫入發生在 `save_state` / `truncate_through`（`mind_loop.py:238-260`）之前。若 turn 在 user 寫入後、save 前 crash，重啟時 WAL replay 會讓該 `UserSpoke` 重跑 → user 行重複 append（doll 內容重新生成）。transcript 為寬容 append-only 日誌，at-least-once 可接受，重複由 B2 consolidation 吸收；**不做冪等工程（YAGNI）**。此為刻意取捨，非 bug。
