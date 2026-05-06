# Skills System — Design

**日期：** 2026-05-06
**狀態：** 草案（待使用者最終審閱）
**範圍：** Roadmap step 10——把 Anthropic-skill 風格的 markdown 檔整合進 DollOS memory 體系。Skill 拆兩檔：**entry**（短 description + frontmatter `name`）由 memsearch 索引、進 RECALL；**body**（完整 instructions）不索引、由新 `InvokeSkill(name)` returning tool 主動載入（吃 step 9 的 success-cascade）。Doll 用 Shell tool 寫新 skill。
**對齊：**
- `2026-05-06-success-cascade-shell-design.md`（步 9 success-cascade + Shell——本 step 直接用上）
- `2026-05-05-tool-calling-design.md`（步 6 pydantic Tool）
- `2026-05-06-memory-autowrite-diary-design.md`（步 8 transcripts/diary——同套 memsearch path 擴展模式）

---

## §1 設計原則

1. **Skill 是 procedural memory in disguise**——形式上是 markdown 檔，行為上跟 NoteMemory bullet / Diary 段是同層級的記憶條目。差別：skill 兩檔分離（entry 索引、body 不索引），讓 RECALL 看到「我有這個 skill」但只在 Doll 主動 invoke 時才把完整步驟塞進 context。
2. **No new infrastructure beyond step 9**——InvokeSkill 是普通 returning pydantic tool，跟 Shell 同層級。No new ABC、no skill registry class、no hot-reload。memsearch 既有 multi-path index 機制覆蓋 skills/。
3. **Doll 自寫**——透過 Shell tool `cat > ... << EOF` 寫兩檔（entry + body）。step 9 Shell trust-only 已完備。**沒有 CreateSkill 專屬 tool**——Shell 涵蓋。
4. **No seed skills**——start fresh。Doll 想要自己寫。User 想 seed 自己丟 md 進去（記得對應 skill_bodies/ 也要寫）。
5. **Anthropic-skill style frontmatter**——但簡化：只 `name`，description 寫正文（YAML 對 retrieval 不友善；自然語言更貼合 memsearch 的 dense + BM25 hybrid）。
6. **No graceful-fail design**——InvokeSkill 找不到檔直接 raise，step 9 cascade fail 路徑接住，Doll 看到錯誤自決下一步。

---

## §2 檔案結構

`data/memory/` 在 step 8 已有：
- `shared/` (LT memory：NoteMemory bullets + Diary entries) — memsearch 索引
- `transcripts/` (原話 turn-by-turn) — memsearch 索引

step 10 新增：
- `skills/` (entry 檔，短 description + frontmatter) — **memsearch 索引**
- `skill_bodies/` (body 檔，完整 instructions) — **不索引**

```
data/memory/
├── shared/
│   └── 2026-05-06.md
├── transcripts/
│   └── 2026-05-06.md
├── skills/                       (NEW, indexed)
│   ├── morning_routine.md
│   └── coffee_check.md
└── skill_bodies/                 (NEW, NOT indexed)
    ├── morning_routine.md
    └── coffee_check.md
```

---

## §3 Skill 檔格式

### Entry: `data/memory/skills/{name}.md`

```markdown
---
name: morning_routine
---
早上問候、給天氣狀況、推今天行程。在主人剛起床或新一天的第一個對話時 invoke。
```

- YAML frontmatter 只一個 `name` field（必須跟 filename basename 一致——caller 用 filename 找 body）
- 正文是自然語言 description（1-3 句）：講 skill 做什麼 + 何時 invoke
- 整個 entry 檔通常 < 10 行，被 memsearch 整個 chunk 進索引——RECALL hit 時 Doll 看完整 entry

### Body: `data/memory/skill_bodies/{name}.md`

```markdown
# Steps

1. 用 Say tool 對主人說「早安～」
2. 用 Shell tool 跑 `curl wttr.in?format=3` 取得天氣
3. 把結果整合，用 Say tool 告訴主人天氣 + 早安
```

- 自由形式 markdown（步驟列、注意事項、範例 tool call 等）
- Filename basename 必須跟 entry 一致
- 任意長度——但記得 cascade 把 body 整個丟回 perception，太長會吃 context

---

## §4 `InvokeSkill` tool

```python
# src/dollos/tools.py

class InvokeSkill(BaseModel):
    """Load a skill's full instructions into context.

    Use this when you've seen a skill entry in RECALL and decide to follow
    its procedure. The skill body will be returned as the next perception,
    after which you should follow its instructions step by step.
    """

    name: str = Field(
        description="Skill name (matches the entry's frontmatter `name` field and filename basename)."
    )

    async def run(self, ctx: ToolCtx) -> str:
        path = ctx.memory_root / "skill_bodies" / f"{self.name}.md"
        return path.read_text()
```

加進 `TOOLS = [Say, NoteMemory, WriteDiary, Shell, InvokeSkill]`。

**錯誤處理**：`read_text()` 對不存在檔自然 raise `FileNotFoundError`。step 9 dispatcher 的 `_dispatch_tool_call` 已經 catch all exceptions → `ToolResult(success=False, detail="runtime error: ...")` → cascade → Doll 看到「你 call 了 InvokeSkill tool 失敗：runtime error: [Errno 2] ...」自決下一步。

---

## §5 memsearch path 擴展

`build_memsearch` (kernel.py) 加 `skills/`：

```python
def build_memsearch(settings: Settings) -> MemSearch:
    shared_path = settings.data.root / "memory" / "shared"
    transcripts_path = settings.data.root / "memory" / "transcripts"
    skills_path = settings.data.root / "memory" / "skills"           # NEW
    shared_path.mkdir(parents=True, exist_ok=True)
    transcripts_path.mkdir(parents=True, exist_ok=True)
    skills_path.mkdir(parents=True, exist_ok=True)                   # NEW
    return MemSearch(
        paths=[str(shared_path), str(transcripts_path), str(skills_path)],
        embedding_provider="onnx",
    )
```

**`skill_bodies/` 不加進 `paths`**——這是 invariant，body 永不索引。Daemon 啟動時不 mkdir `skill_bodies/`（讓 Doll 自己寫時 mkdir）。

---

## §6 `scaffolding.jinja` 加一段

`src/dollos/prompts/templates/scaffolding.jinja` 末尾（meta-rule 之後）加：

```jinja
你有「skill」可以累積經驗：

- Skill = 一個 procedural memory，分兩檔：
  - `data/memory/skills/<name>.md` — entry，YAML frontmatter `name: <name>` + 一段自然語言 description（1-3 句講做什麼、何時用）
  - `data/memory/skill_bodies/<name>.md` — body，完整步驟 / 細節（任意 markdown）
- RECALL 會自動讓你看到 entry，本身就是「我有這個 skill」的訊號
- 想用某個 skill → call `InvokeSkill(name=...)`，body 會透過 cascade 進你的下個 perception
- 想寫新 skill → 用 Shell tool 同時寫 entry + body 兩個檔
```

讓 Doll 從 system prompt 學到 skill 格式跟用法，不依賴 in-context example。

---

## §7 Failure modes

| 情境 | 處理 |
|---|---|
| `InvokeSkill(name="nonexistent")` | `read_text()` raise FileNotFoundError → step 9 cascade fail 路徑 → Doll 看到失敗訊息 |
| Entry 存在但 body 不存在（半損） | 同上 |
| Body markdown 內容奇怪 / Doll 看不懂 | cascade 成功（body 內容給 Doll），Doll 自決怎麼跑（可能 fall through 到 Say 解釋 / 重寫 skill）|
| Skill name 含 `/` 或 `..`（path traversal）| pydantic 不擋；自然走 read_text，可能讀到任意檔（**trust-only**——user 接受）。Follow-up 加 validator 限制 |
| memsearch 還沒 reindex 新寫的 skill | RECALL 暫時看不到；下次 daemon restart / 下次 NoteMemory triggered index_file 後可見。Follow-up 改 Doll 寫 skill 後立刻 trigger reindex |

---

## §8 Tests

### `tests/test_tools.py`（擴）

1. `InvokeSkill` 在 `TOOLS` list
2. `InvokeSkill` schema 含 `name: str` field
3. `InvokeSkill.run()` 對存在的 body 檔回傳全文
4. `InvokeSkill.run()` 對不存在 raise `FileNotFoundError`（step 9 cascade 會接，這 test 直接 assert 例外）
5. `InvokeSkill.run()` 不讀 entry 檔（path 走 skill_bodies/）

### `tests/test_kernel_factories.py` 或 `test_kernel.py`（擴）

1. `build_memsearch` 包含 `skills/` 路徑、不包含 `skill_bodies/` 路徑
2. `build_memsearch` 啟動時 mkdir `skills/`、不 mkdir `skill_bodies/`

### `tests/test_prompt_renderer.py`（擴）

1. `scaffolding.jinja` 渲染後含 skill convention 字眼（`InvokeSkill`、`skills/`、`skill_bodies/`）

### `tests/test_e2e.py`（擴）

完整 trace：seed 一個 skill entry+body → user message 觸發 RECALL 撈到 entry → Doll emit InvokeSkill → cascade body → Doll 照 body 跑（emit Shell + Say）

---

## §9 不做的（明確 out-of-scope）

- ❌ Hot-reload 監測 skills/ 變動（依靠 memsearch 既有 reindex 機制）
- ❌ Doll 自動知道有哪些 skill（只透過 RECALL；不在 system prompt 列表）
- ❌ Skill 版本控制 / 歷史
- ❌ Skill execution sandbox（trust-only，body 是給 Doll 讀的指引，不是執行檔）
- ❌ CreateSkill 專屬 tool（Shell 涵蓋）
- ❌ Seed skills
- ❌ Skill name validator（`/` `..` 等 path traversal trust-only）
- ❌ Skill 互相 invoke（body 可教 Doll call 另一個 skill，但沒特別設計）

---

## §10 已知限制 / Follow-ups

1. **Skill 寫完到可見有延遲**——memsearch 不 watch；只在 daemon restart / `index_file` 觸發時 reindex 整個 paths 設定 OR 該檔。Doll 寫完 skill 想立刻 invoke 自己 → OK（自己知道名字）；想透過 RECALL 確認 entry 已被別個 turn / 自己 → 可能要等。Follow-up：Doll 寫 skill 後 trigger memsearch.index_file。
2. **Skill name 衝突 / 覆寫**——同 name 的 skill 寫第二次會覆寫前一個。User / Doll 自己負責 namespace。Follow-up：寫前先檢查存在 + 警告。
3. **Body 可能很長吃 context**——一個 1000-char body cascade 進 perception → 大模型 prefill 變長 → 多 turn 累積會撐爆。Follow-up：cascade 內容截斷或分段 invoke。
4. **memsearch 索引顆粒**——entry 短，整個被 chunk 進去 OK。但 markdown frontmatter（`---\nname: x\n---`）也被當文字索引——可能干擾 retrieval（query 包含 `name` 字眼會 hit）。實測後若有問題 follow-up。
5. **Skill body 沒結構 schema**——任意 markdown。Doll 寫法可能參差不齊。長期 follow-up：定義「optimal skill body」格式（步驟列 + tool call 範例 + 注意事項）寫進 scaffolding。
6. **No invocation tracking**——沒記錄哪個 skill 被 invoke 過幾次、什麼情境。Follow-up：每次 InvokeSkill 自動寫 transcript line / log。
7. **Path traversal 風險**——`InvokeSkill(name="../../../etc/passwd")` 會讀到 `/etc/passwd`。trust-only 接受；follow-up 加 validator 限制 `name` 為 `[a-zA-Z0-9_]+`。

---

## §11 Demo 驗證

### 階段 1: Doll 寫 skill

User: 「以後我問你『今天怎樣』的時候，你都先看一下 transcript 知道我們今天聊過什麼，再回答」

Doll cascade 流程（簡化）：

1. Inner Voice perceive → STATE 含「主人想我建立一個 routine」
2. Doll round 1：emit `<tool_call>Shell command="mkdir -p data/memory/skill_bodies && cat > data/memory/skills/check_today.md << 'EOF'\n---\nname: check_today\n---\n當主人問今天怎樣，先看 transcript 再回答。\nEOF"</tool_call>`
3. cascade with Shell exit 0
4. Doll round 2：emit `<tool_call>Shell command="cat > data/memory/skill_bodies/check_today.md << 'EOF'\n# Steps\n1. Shell command='cat data/memory/transcripts/$(date +%Y-%m-%d).md'\n2. 讀完 Say 一段話 summarize 今天\nEOF"</tool_call>`
5. cascade with Shell exit 0
6. Doll round 3：emit `<tool_call>Say text="好的，我寫了 check_today skill"</tool_call>`
7. Turn 結束

### 階段 2: 隔一輪 user 觸發

下個 turn，user：「今天怎樣？」

1. Inner Voice perceive → memsearch RECALL hit `skills/check_today.md`（其中含「當主人問今天怎樣...」）
2. Doll prefill 看到 RECALL 含 entry → 知道「我有這 skill」
3. Doll round 1：emit `<tool_call>InvokeSkill name="check_today"</tool_call>`
4. cascade body 進 perception：「# Steps\n1. Shell command='cat data/memory/transcripts/...'\n2. ...」
5. Doll round 2：emit `<tool_call>Shell command="cat data/memory/transcripts/2026-05-06.md"</tool_call>`
6. cascade transcript 內容
7. Doll round 3：emit `<tool_call>Say text="今天我們聊了..."</tool_call>`
8. Turn 結束

**驗證點**：
- 兩個檔都建好（`skills/check_today.md` + `skill_bodies/check_today.md`）
- entry 被 memsearch 索引（restart daemon 看 indexed chunks 數）
- RECALL 能撈到 entry（query: 「今天怎樣」 → top hit 含 `check_today`）
- InvokeSkill 成功、body cascade 給大模型
- Skill body 引導 Doll 跑後續步驟（Shell → Say）
