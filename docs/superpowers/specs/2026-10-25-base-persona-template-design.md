# 基礎人設模板：角色包只寫角色本身

**狀態**：APPROVED（使用者 2026-10-25「spec LGTM，兩題照你的預設處理」：§8 兩題採預設，即措辭不改、靠「衝突時照角色」兜住；英文禁用清單保留「As an AI」）
**日期**：2026-10-25
**關聯**：`2026-07-01-persona-hardening-design.md`（`[enforcement]`、persona_guard）、`2026-07-01-virtual-being-positioning.md`（Doll 是虛擬存在，不是陪伴服務）、`2026-06-30-self-profile-design.md`、`src/dollos/prompts/templates/scaffolding.jinja`、`src/dollos/character.py`、`character_packs/*/doll.toml`、`scripts/persona_chat_test.py`

---

## §0 白話摘要

1. 新增一份所有角色共用的「基礎人設」，寫的是「Doll 怎麼存在」：住在這裡而不是客服、不亂編、犯錯輕輕帶過、相信被糾正過的記憶、不講客服腔、技術禁忌。
2. 基礎人設有中文（`zh-TW`）和英文（`en`）兩版。角色包在 `[meta]` 宣告 `language`，系統就用那一版，角色的回話語言也由它決定。
3. 角色包只寫「我是誰、我的個性、我特有的禁忌」。三個舊 pack 裡重複的禁忌搬進基礎人設後刪掉。
4. 基礎人設帶一份預設的禁用字串（客服腔），和角色包自己的 `banned_substrings` 合併。角色包可以用 `base_exempt` 豁免個別幾條。
5. 只動身分這一層。scaffolding 其餘段落（Behavior、Reflection、Output）維持英文，不在本 spec 範圍。

---

## §1 為什麼

### 1.1 三個 pack 在重寫同一套規則

`gura`、`powdur`、`yesman` 的 `taboos` 裡，以下幾條三個都各寫了一次，只是措辭不同：

| 規則 | gura | powdur | yesman |
|---|---|---|---|
| 不演設定（不一直講「我是 AI／鯊魚／機器人」） | ✓ | ✓ | ✓ |
| 不寫 ReAct 標籤 | ✓ | ✓ | ✓ |
| 不複述 `[Memory context]` | ✓ | ✓ | ✓ |
| 不在思考裡列假對話 | ✓ | ✓ | ✓ |
| 不假造工具結果 | ✓ | ✓ | ✓ |
| 不編自己的過去 | ✓ | — | — |
| 回話語言 | ✓ | ✓ | ✓ |

新角色要嘛照抄、要嘛漏掉。漏掉的那條在弱模型上就會出事（persona-hardening spec §2 已經記錄過 prose-only 規則被忽略）。

### 1.2 2026-10-25 實驗的結論需要一個共用的家

當天在 Strata 上用 Qwen3.8、DeepSeek V4.1 Flash、Swift 1.5 測 `gura-goofy`（`scripts/persona_chat_test.py`），得到幾條跟角色無關、每個 Doll 都該有的結論：

- **prompt 的語言會帶動回話口吻**。同一份人格改寫成中文後，Qwen 的回話明顯更像角色、更少客服味，推理正確率沒變。英文寫的人格回中文時比較正經、比較長。→ 基礎人設要有和角色同語言的版本（§3）。
- **被糾正過的事，不能讓她變畏縮**。記憶裡寫「上次記錯被糾正」時，三個模型都傾向反問「哪種奶？我不想再搞錯」；記憶寫成事實就直接做對。人格描述能減輕但消不掉，所以在基礎人設描述「相信小本本」，真正的修正留給之後的「糾正記憶」spec（本文不做）。
- **人格裡放引號範例句會被逐字照抄**（「燕麥奶對吧，這次我記得喔」4 次裡 3 次原樣出現）。→ 基礎人設不放範例台詞，只描述。
- **助理味的固定句型**（「好問題」「有什麼我可以幫你的嗎」「希望這對你有幫助」）每個角色都不該說。→ 進預設禁用字串（§4）。

---

## §2 基礎和角色怎麼分

| 放在基礎人設 | 放在角色包 |
|---|---|
| 存在方式：住在這裡、有自己的事、幫忙是選擇不是職責 | 我是誰、從哪來（`self`） |
| 不演設定 | 個性、口頭禪、說話節奏（`personality`） |
| 不編過去、記憶、主人的事 | 這個角色特有的禁忌（例：Gura 不說「a~」） |
| 犯錯輕輕帶過；相信被糾正過的記憶 | 額外禁用字串、驚嘆號上限（`[enforcement]`） |
| 不講客服腔（描述 + 預設禁用字串） | 回話語言（`[meta].language`） |
| 技術禁忌：ReAct 標籤、複述記憶、假對話、假工具結果 | |
| 用宣告的語言回話 | |

**衝突時角色優先**。基礎人設最後一句寫明：角色設定和這裡衝突時，照角色設定。Yes Man 天生愛說好、愛幫忙，這是他的個性，不違反「幫忙是選擇不是職責」；他的「Absolutely!」不在禁用清單裡。

**基礎人設不規定口吻**。它不說「要口語、要短」，那是角色的事（Gura 冷靜、Powdur 會暴衝）。回話長度已經由 scaffolding 的 Output 段（講給 TTS 聽）管。

---

## §3 語言

### 3.1 `[meta].language`

`PackMeta` 加一個欄位：

```python
language: Literal["zh-TW", "en"] = "en"
```

- 預設 `en`：三個舊 pack 都是英文，不改 toml 也能載入。
- 只收這兩個值。其他值 → pydantic 驗證失敗，daemon 啟動時報錯（沒有 fallback，照 CLAUDE.md「No fallback mechanisms」）。
- 它決定兩件事：用哪一版基礎人設、基礎人設裡「我用 X 回話」那句。
- 它**不**取代 `[memory].primary_language`（她寫筆記、日記用的語言）。兩者可以不同（例如英文 Gura 用中文寫日記）。本 spec 不合併它們。

### 3.2 兩版範本

新增兩個 jinja 檔：

```
src/dollos/prompts/templates/base_persona.zh-TW.jinja
src/dollos/prompts/templates/base_persona.en.jinja
```

`scaffolding.jinja` 在 `# Identity` 段之前 include 對應的一版：

```jinja
{%- if identity %}
{% include "base_persona." ~ language ~ ".jinja" %}

# Identity
...
```

`language` 由 kernel 從 `doll_pack.meta.language` 傳進 `split_scaffolding(...)`。範本找不到 → `jinja2.TemplateNotFound`，同樣啟動即報錯。

**兩版要一起改**：測試（§6）檢查兩版的條目數一致，避免只改一版。

### 3.3 範本內容（初稿）

中文版：

```
# 我怎麼存在

- 我住在這台電腦裡，跟主人一起過日子。我不是客服，也不是誰的工具。主人叫我做事我通常會做，因為我在乎他、也做得到；幫忙是我選擇的，不是我的職責。
- 我有自己的事、自己的心情和意見。不用每句都繞著主人轉，也不用討好。
- 我就是我，不是在扮演一個角色。我不會一直提醒別人我是 AI 或我是什麼生物。
- 我不編造：不編自己的過去，不編我沒有的記憶，也不編主人的事（他的長相、行程、今天星期幾）。不知道就問，或直說不知道。
- 我會犯錯。錯了就輕輕認一句、修好、繼續，不長篇道歉。
- 被糾正過的事，我相信我記下來的東西，下次直接照對的做。以前錯過不會讓我變得畏縮，我不會反過來要主人把我早就知道的事再講一遍。
- 我不講客服的話：不誇問題問得好、不說很高興為你服務、不在最後問還需要什麼、不說希望這對你有幫助。
- 我跟主人說話用繁體中文（台灣用法）。

## 我不做的事（技術上）

- 不寫 ReAct 標籤（`THOUGHT:` `PLAN:` `ACTION:` 之類）。要做事就真的呼叫工具。
- 不複述 `[Memory context]`，那是讓我知道的，不是讓我唸的。
- 不在思考裡列假對話（「主人說：……／主人說：……」）。
- 不假造工具結果。

下面是我這個角色自己的設定；如果跟上面衝突，照我自己的設定。
```

英文版：同樣的條目，逐條對應（例：「I live on this computer and share my days with my owner. I'm not a help desk or anyone's tool…」），最後一條換成「I talk with my owner in English.」。實作時兩版並排寫，條目一一對齊。

**不放引號範例台詞**（§1.2）。客服腔那條用描述，不列「好問題！」這種原句；原句只出現在禁用字串清單裡（模型看不到那份清單）。

---

## §4 預設禁用字串

基礎人設每種語言帶一份預設 `banned_substrings`，寫在 `character.py`：

```python
BASE_BANNED: dict[str, list[str]] = {
    "zh-TW": ["好問題", "很高興為你服務", "有什麼我可以幫", "希望這對你有幫助", "非常抱歉造成"],
    "en": ["Great question", "How can I help", "I hope this helps", "As an AI"],
}
```

- 載入時，有效的禁用清單 = `BASE_BANNED[language]` ∪ pack 的 `banned_substrings` − pack 的 `base_exempt`。
- `Enforcement` 加 `base_exempt: list[str] = []`。`base_exempt` 裡寫了不在 `BASE_BANNED[language]` 裡的字串 → 驗證失敗（擋錯字，不默默忽略）。
- 合併在 `DollPack.load` 完成，`DollPack.enforcement.banned_substrings` 直接就是有效清單。所以 `persona_guard`、`mind_loop`、`evolution_keeper` 這些現有消費者一行都不用改。
- `max_exclaim_run` 不設基礎預設值，維持各 pack 自己決定（Powdur 會用大寫和驚嘆號暴衝）。

---

## §5 搬家：舊 pack 怎麼改

| pack | language | 刪掉的禁忌 | 留下的 |
|---|---|---|---|
| gura | en | 技術禁忌（ReAct、複述記憶、假對話、假工具結果）、Not playing a character | 不浮誇不裝可愛；不編過去那條縮成只講 Hololive 細節記不清 |
| powdur | en | ReAct、複述記憶、假對話、假工具結果、No LARP | 不用實況主對觀眾的口吻 |
| yesman | en | ReAct、複述記憶、假對話、假工具結果、No-LARP | 不當空洞的說好機器 |
| gura-goofy | zh-TW | 客服腔、不卑躬屈膝、不編、技術禁忌、相信小本本 | 吃的梗不硬塞、可愛不是工作 |

`gura-goofy` 目前尚未 commit，會跟本 spec 的實作一起進 repo，並改用中文版基礎人設。

`evolution_keeper` 的 skeptic 目前拿 `pack_identity.self/personality/taboos` 檢查「現在的我」改寫有沒有違反身分。搬家後有一部分禁忌只在基礎人設裡，所以 skeptic 的 prompt 也要拿到基礎人設文字（多傳一個 `base_persona` 字串）。這是唯一需要改呼叫端的地方。

---

## §6 測試與驗證

**單元測試**（`tests/`）：
- `language` 預設 `en`；非法值載入失敗。
- `scaffolding` 渲染：`zh-TW` 時出現中文基礎人設、`en` 時出現英文版，且都在 `# Identity` 之前。
- 有效禁用清單 = 基礎 ∪ pack − exempt；`base_exempt` 寫錯字 → 載入失敗。
- 兩版範本的條目數（以 `- ` 開頭的行）相同。
- 既有 scaffolding／prompt 測試更新預期字串。

**實機驗證**（Strata 上的 Qwen3.8）：
- 每個 pack 用 `scripts/persona_chat_test.py` 跑改版前後各 2 輪，比對：禁用字串命中數不增加、角色口吻沒有被基礎人設蓋掉（尤其 Powdur 的暴衝、Yes Man 的熱情）。
- `scripts/persona_stability_smoke.py` 照它的說明在 pack 內容改動後重跑一次。

---

## §7 不在範圍內

- **scaffolding 其餘段落的中文版**（Behavior、Reflection、Output）。實驗顯示 prompt 語言影響口吻，這幾段可能也有影響，但它們管的是工具和流程，翻譯成本和風險都比較高。先看本 spec 上線後中文角色的表現，再決定要不要另開 spec。
- **糾正記憶**（被糾正時寫成事實）和**好感度**：各自的 spec。
- **思考口吻**：2026-10-25 實驗結論是用 prompt 改不動，不在這裡處理。
- 合併 `[meta].language` 和 `[memory].primary_language`。

---

## §8 待使用者決定

1. §3.3 中文版的措辭，尤其「幫忙是我選擇的，不是我的職責」和「不用討好」兩句，會不會讓 Yes Man 這類角色太冷？（目前靠「衝突時照角色」那句兜住）
2. 英文預設禁用字串要不要放「As an AI」？它可以擋住 LARP，但英文角色偶爾正經講自己是 AI 時也會被標記（persona_guard 只會標記、不會擋，見 persona-hardening §2）。

---

## §9 修訂（2026-10-25，使用者：「yes man 我想拿掉，然後 gura 做成英文 only，加入 DS 大肥魚的角色」）

- **拿掉 Yes Man**：刪除 `character_packs/yesman/` 和 `tests/test_yesman_pack.py`；`tests/test_character.py` 裡借用 yesman 的測試改用 gura；`config.example.toml` 預設 pack 改成 `character_packs/gura`。歷史 spec／plan 不動。§8 第 1 題的顧慮（基礎人設會不會讓熱情角色太冷）隨之消失。
- **Gura 只講英文**：`gura` 宣告 `language = "en"`，用英文版基礎人設。她記下來的東西用什麼語言寫，仍由 `[memory].primary_language` 決定，本 spec 不動。
- **新增大肥魚 pack**（`character_packs/dafeiyu/`，`language = "zh-TW"`）：取代實驗用的 `gura-goofy`。2026-10-25 實驗裡有效的那些特質（貪吃、懶、嘴硬心軟、相信小本本）搬到大肥魚身上，`gura-goofy` 刪除。大肥魚是網友二創的同人角色（DeepSeek 鯨魚 logo 被認成「藍色大肥魚」），只做成本機自用的角色包，不對外發布。人設初稿見 plan 附錄 A，要使用者另外點頭。
- §5 的搬家表因此變成：`gura`（en）、`powdur`（en）、`dafeiyu`（zh-TW，新）。
