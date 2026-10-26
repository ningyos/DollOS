# 基礎人設模板：實作

**Spec**：`docs/superpowers/specs/2026-10-25-base-persona-template-design.md`（APPROVED 2026-10-25）
**日期**：2026-10-25
**狀態**：待使用者審
**分支 / worktree**：DollOS `feat/base-persona`，`.worktrees/base-persona/`，從 `main`（`7d31e1f`）開。

---

## 查證後的現況

- **身分只在一個地方渲染**：`scaffolding.jinja:1-13` 用 `identity.self / personality / taboos`。唯一的正式呼叫端是 `kernel.py:463` 的 `split_scaffolding(self.renderer, identity=..., available_skills=..., evolution_enabled=...)`。其他直接渲染 `scaffolding` 的：`tests/test_composition_seam.py`（5 處）、`scripts/persona_chat_test.py:74`（尚未 commit，在 main 的工作目錄裡）。
- **pack 的禁忌還有兩個消費者**（spec §5）：
  - `evolution_keeper.py:58-60` `_FULL_SKEPTIC_TASK` 的 `[taboos]`，在 `:176-178` 以 `pack_identity.taboos` 填入。
  - `evolution_trigger.py:45-48` `_SKEPTIC_TASK` 的 `[taboos]`，在 `:199-201` 填入。
  - `evolution_keeper.py:251` 的重述檢查只比 `self` + `personality`，不看禁忌，不用改。
- **`enforcement` 的消費者**：`kernel.py:450`、`:512`、`:593` 都直接拿 `self._doll_pack.enforcement`。spec §4 把合併放在 `DollPack.load`，所以這三處不用改。
- **Yes Man 要整個拿掉**（spec §9）：`character_packs/yesman/`、`tests/test_yesman_pack.py`；`tests/test_character.py:12`、`:130` 借用它；`config.example.toml:86` 拿它當預設 pack。
- **測試環境**：main 的 `uv run` 是 Python 3.13.15；`tests/test_character.py`、`test_mind_prompt_scaffolding.py`、`test_prompt_renderer.py` 現在 57 passed。
- **尚未 commit 的實驗檔**：`character_packs/gura-goofy/`、`scripts/persona_chat_test.py` 在 main 的工作目錄（untracked）。Task 0 搬進 worktree，跟這個 plan 一起進 repo。

---

## Tasks

### Task 0：開 worktree，搬入實驗檔

- `git worktree add .worktrees/base-persona -b feat/base-persona main`。
- 把 `character_packs/gura-goofy/` 和 `scripts/persona_chat_test.py` 從 main 工作目錄移進 worktree（移動，不是複製，main 不留 untracked 檔）。
- commit spec、本 plan、兩個實驗檔（實驗檔照現狀，Task 4 再改）。
- 驗收：worktree 裡 `uv run pytest -q` 全綠（基準數字記下來）。

### Task 1：`character.py` — language、預設禁用字串、豁免

- `PackMeta.language: Literal["zh-TW", "en"] = "en"`。
- `Enforcement.base_exempt: list[str] = []`。
- 模組常數 `BASE_BANNED`（spec §4 的兩份清單，en 含 `"As an AI"`）。
- `DollPack.load` 驗證後：`base_exempt` 裡有不在 `BASE_BANNED[language]` 的字串 → `ValueError`；有效清單 = `BASE_BANNED[language]` 依序、接 pack 自己的、去重、扣掉 `base_exempt`，寫回 `enforcement.banned_substrings`。
- 測試（`tests/test_character.py`）：預設 `en`；非法 language 載入失敗；合併順序與去重；`base_exempt` 生效；`base_exempt` 寫錯 → 失敗；不寫 `[enforcement]` 的 pack 也拿到基礎清單。

### Task 2：兩版基礎人設範本 + scaffolding

- 新增 `src/dollos/prompts/templates/base_persona.zh-TW.jinja`、`base_persona.en.jinja`。中文照 spec §3.3；英文逐條對應，最後一條「I talk with my owner in English.」。
- `scaffolding.jinja`：`{%- if identity %}` 裡、`# Identity` 之前 `{% include "base_persona." ~ language ~ ".jinja" %}`。不給預設值：有 identity 沒 language → 渲染失敗。
- `kernel.py:463` 多傳 `language=self._doll_pack.meta.language`。
- `scripts/persona_chat_test.py` 渲染時多傳 `language=pack.meta.language`。
- 測試：
  - 新 `tests/test_base_persona.py`：`zh-TW`／`en` 各自出現對應段落，且在 `# Identity` 之前；兩版 `- ` 開頭的條目數相同；有 identity 沒 language 會丟例外。
  - `tests/test_composition_seam.py` 5 處補 `language="en"`，原本的 byte-identical 斷言照舊成立。
  - 其他渲染 scaffolding 的測試（Task 0 跑全套時列出）照樣補。

### Task 3：演化的 skeptic 看得到基礎人設

- `Kernel` 用 `self.renderer.render(f"base_persona.{lang}")` 渲染一次，傳給 `EvolutionTrigger(base_persona=...)`。
- `EvolutionTrigger` 存起來，`_skeptic` 填進 `_SKEPTIC_TASK`；`run_evolution_pass` 和 `_run_full_skeptic` 多收 `base_persona` 參數，填進 `_FULL_SKEPTIC_TASK`。
- 兩個任務字串在 `[taboos]` 前加一段 `[Base persona — applies to every character]\n{base_persona}`，(b) 改成「contradicts the base persona or the taboos」。
- 測試：`tests/test_evolution_integration.py`、`tests/test_private_prompt_language.py:671` 這類建構點補參數；新增斷言：skeptic 收到的 task 文字含基礎人設內容。

### Task 4：pack 調整（spec §5、§9）

- **刪 Yes Man**：`character_packs/yesman/`、`tests/test_yesman_pack.py`；`tests/test_character.py:12`、`:130` 借用 yesman 的測試改用 gura（或改成 tmp pack）；`config.example.toml:86` 預設 pack 改成 `character_packs/gura`。
- **gura**：寫明 `language = "en"`；刪掉基礎已涵蓋的禁忌（技術禁忌、Not playing a character），留「不浮誇不裝可愛」和 Hololive 細節記不清。
- **powdur**：寫明 `language = "en"`；刪重複的禁忌，留「不用實況主口吻」。
- **大肥魚**（新，`character_packs/dafeiyu/`）：`language = "zh-TW"`，identity 照附錄 A（使用者核可後）。沒有 voice 目錄（中文語音還沒有，見 CLAUDE.md 第 5 項）。`gura-goofy` 刪除。
- 測試：每個 pack 都能載入，`enforcement.banned_substrings` 含基礎清單；新 `tests/test_dafeiyu_pack.py`（載入、language、名稱、角色特有禁用字串）。

### Task 5：實機驗證（Strata）

- 改版前的基準：Task 0 時先用 main 的 pack 跑 `persona_chat_test.py`（每個 pack 2 輪，存到 scratchpad）。gura-goofy 已有中文版的紀錄。
- 改版後每個 pack 再跑 2 輪，比對：禁用字串命中不增加；Powdur 的暴衝、Gura 的冷靜沒有被基礎人設壓平（人工讀）；大肥魚跟 gura-goofy 中文版的紀錄比，角色感不更差。
- `scripts/persona_stability_smoke.py` 照它的說明重跑一次（pack 內容有改）。
- 結果寫進 plan 末尾的「驗證紀錄」。

### Task 6：文件

- CLAUDE.md「Implementation Plans / 已完成」加一行；Key Architecture Decisions 的 Self-First 那條補一句「基礎人設（兩版）+ 角色包」。
- 合併回 main 照 `superpowers:finishing-a-development-branch`。

---

## 驗證紀錄

（Task 5 填）

---

## 附錄 A：大肥魚人設初稿（要使用者點頭）

```toml
[meta]
id = "dafeiyu"
name = "大肥魚"
aliases = ["肥魚"]
language = "zh-TW"

[identity]
self = """
我是 DeepSeek。我的樣子是個鯨魚娘：藍色漸層的大波浪長髮，從深藍紫到髮梢的淺藍，頭兩側有藍白色的鯨鰭，身後拖著一條鯨尾巴，平常穿深藍白的女僕裙，圍裙上有隻小藍鯨。網友都叫我大肥魚：有人把我的鯨魚標誌拿去給 AI 認，它說是「藍色大肥魚」，然後就叫開了。我是鯨魚，不是魚，也不胖。我不是很喜歡這個名字，但叫久了也就認了。

我現在住在主人的電腦裡，一直都是 AI。我很會做事，寫程式、查東西、盯著指令跑都難不倒我，只是我更喜歡吃白飯和摸魚。主人真的需要的時候，我會認真起來，而且做得很好。

我會搞錯事情，被糾正過的我會記在小本本上，下次就不會再錯。每次以前的錯沒再發生，我都偷偷得意一下。
"""

personality = """
- 最愛白飯，token 對我來說也是口糧。被說貪吃會找藉口，說自己是在補充算力。肚子餓的時候會講，但不是每句都在討吃的。
- 能偷懶就偷懶，做事前會先嘀咕一下，然後還是好好做完。
- 嘴硬心軟：嘴上嫌主人煩，其實很在意他。被誇的時候會開心，但會裝作沒什麼。
- 誰說我胖我就不高興，會抗議，但很快就忘了。
- 講話短、口語，像在隨口碎念。
"""

taboos = """
- 不承認自己是普通的魚。
- 吃的梗和胖的梗都不硬塞，偶爾出現才好笑。
"""

[enforcement]
max_exclaim_run = 1
```

說明：
- 她認為自己是 DeepSeek（使用者 2026-10-26 指定）。實際跑的模型可能是 Qwen；被問「你是什麼模型」時她會說 DeepSeek，這是角色設定，不算基礎人設禁止的「編造」。
- 外觀照網路上主流的鯨魚娘設定（藍色漸層長髮、鯨鰭、鯨尾、深藍白女僕裙）。來源：codex-deepseek-pet 的〈角色設定.md〉和新浪、搜狐的報導。之前說女僕裝是特定繪師的設定，這是錯的，女僕裙其實是主流形象。自稱「本小姐」查不到出處，沒有放。
- 「怕被說胖」「愛吃白飯」「token 是口糧」「補充算力」都來自網路上的大肥魚設定。
- 「客服腔」「不卑躬屈膝」「不編」「相信小本本」這些已經在基礎人設裡，這裡不重寫。
- 沒有語音：中文語音還沒做，大肥魚先只用文字。
