# OOBE Character Generation — Design Spec

## Overview

Replace the current OOBE Personality + Voice pages with a single AI-powered character generation page. The LLM generates a unique AI companion, and the user can regenerate, edit, or import a .doll character pack.

## Prerequisites

- DollOSAIService must be bound
- API Key is configured in the previous step (but may be skipped)

## OOBE Flow Change

### Before
```
Welcome → Theme → Wi-Fi → GMS → Model Download → API Key → Personality → Voice → Complete
```

### After
```
Welcome → Theme → Wi-Fi → GMS → Model Download → API Key → Character Generation → Complete
```

- Remove Personality page and Voice placeholder page
- Update `skipTargets`: `"api_key" to "character_gen"`

## Character Generation Page

### Layout

```
┌──────────────────────────────┐
│    Meet Your AI Companion    │  ← title
│                              │
│  ┌────────────────────────┐  │
│  │  「璃月」               │  │  ← generated name
│  │                        │  │
│  │  性格：溫柔但偶爾毒舌   │  │  ← backstory (truncated)
│  │  稱呼：主人             │  │  ← address
│  │  語言：繁體中文          │  │  ← language
│  │                        │  │
│  │  預覽對話：              │  │  ← only for LLM-generated
│  │  「早安～今天想做什麼？」 │  │  ← sample dialogue 1
│  │  「...才沒有擔心你。」   │  │  ← sample dialogue 2
│  │  「嗯，交給我吧。」     │  │  ← sample dialogue 3
│  └────────────────────────┘  │
│                              │
│  [重新生成]  [修改]          │  ← action buttons
│                              │
│  ─── 或者 ───               │  ← divider
│                              │
│  [Import .doll]              │  ← import button
│                              │
│      [ Continue → ]          │  ← bottom nav
└──────────────────────────────┘
```

### States

1. **Loading** — "Generating your AI companion..." with progress indicator
2. **Generated** — Shows character card with Regenerate / Edit actions
3. **Editing** — Inline edit fields replace card content (see Edit Mode)
4. **No API Key** — Skip API Key 時，直接進入 Edit 模式讓使用者手動填寫，隱藏「重新生成」按鈕，保留 Import .doll
5. **Imported** — 顯示已匯入角色的卡片（無 sample dialogue），隱藏「重新生成」和「修改」
6. **Error** — "Generation failed" with Retry + Edit buttons（Edit 讓使用者手動填寫）

### Generation (Async)

Fragment 在背景線程呼叫 `IDollOSAIService.generateCharacter(locale)`。生成期間顯示 Loading 狀態，完成後切回 main thread 更新 UI。

**AIDL method:**
```aidl
String generateCharacter(String locale);
```

呼叫端：
```kotlin
Thread {
    val json = aiService?.generateCharacter(deviceLocale)
    runOnUiThread { handleResult(json) }
}.start()
```

**LLM Prompt（service 端組裝，locale 動態帶入）:**
```
Generate a unique AI companion character for a personal AI assistant.
The user's language is: {locale_display_name}

Return ONLY valid JSON, no markdown fences:
{
  "name": "a short, memorable name",
  "backstory": "2-3 sentences describing personality, quirks, and how they interact with their owner. Be creative and specific — give them a distinct personality with interesting contradictions.",
  "responseDirective": "1 sentence describing their speaking style",
  "dynamism": <float between 0.3 and 0.9>,
  "address": "how they address their owner",
  "languagePreference": "{locale_language_name}",
  "sampleDialogue": [
    "morning greeting",
    "showing concern",
    "being helpful"
  ]
}
```

**Service 端 JSON 解析：** 先 strip markdown code fences（` ```json ... ``` ` → 取中間內容），再 `JSONObject()` 解析。

### Edit Mode

使用者按「修改」或沒有 API Key 時進入：

- **Name** → EditText（單行）
- **Backstory** → Multi-line EditText
- **Response Directive** → EditText（單行）
- **Dynamism** → Slider + 標籤「冷靜 ← → 活潑」（0.0 ~ 1.0）
- **Address** → EditText（單行）
- **Language** → EditText（單行，預填裝置 locale）
- **[儲存]** 按鈕 → 回到 Generated 狀態，更新卡片預覽

Edit 模式時隱藏 sample dialogue（手動模式不需要預覽對話）。

### Import .doll

1. 開啟系統 file picker（`ACTION_OPEN_DOCUMENT`）
2. `IDollOSAIService.importCharacter(fd)` → 返回 character ID
3. 成功 → `IDollOSAIService.setActiveCharacter(characterId)`
4. 呼叫 `IDollOSAIService.getCharacterInfo(characterId)` 取得角色資訊
5. 顯示角色卡片（name, backstory, address, language — 無 sample dialogue）
6. 切換到 Imported 狀態
7. 使用者按 Continue 才跳下一頁
8. 失敗 → toast 顯示錯誤

### On Continue

**驗證：** name 不得為空。空白時 toast 提示「Please name your AI companion」，不跳頁。

**Generated/Edited character:**
- 呼叫 `IDollOSAIService.createCharacterFromOobe(json)` 傳入 JSON 字串
- Service 端：建立 character directory，寫入 manifest.json / personality.json / voice.json / scene.json，呼叫 `setActiveCharacter()`
- 返回 character ID

**Imported .doll:**
- 已在 import 階段處理完畢（importCharacter + setActiveCharacter）
- 直接跳下一頁

## New AIDL Methods

```aidl
// Generate a character using the configured LLM (synchronous, call from background thread)
// Returns JSON string with character data, or null on failure
// locale: device locale string (e.g. "zh-TW", "en-US", "ja-JP")
String generateCharacter(String locale);

// Create a character from OOBE generation/editing
// json: JSON string with fields: name, backstory, responseDirective, dynamism, address, languagePreference
// Writes manifest.json, personality.json, voice.json (defaults), scene.json (defaults)
// Calls setActiveCharacter() internally
// Returns character ID
String createCharacterFromOobe(String json);
```

## SetupWizardActivity Changes

```kotlin
// Before
private val pageKeys = listOf(
    "welcome", "theme", "wifi", "gms", "model_download",
    "api_key", "personality", "voice", "complete"
)
private val skipTargets = mapOf("api_key" to "voice")

// After
private val pageKeys = listOf(
    "welcome", "theme", "wifi", "gms", "model_download",
    "api_key", "character_gen", "complete"
)
private val skipTargets = mapOf("api_key" to "character_gen")
```

Adapter:
```kotlin
"character_gen" -> CharacterGenPage()
```

Remove: `PersonalityPage.kt`, `VoicePage.kt`, `page_personality.xml`

## Error Handling

| 情況 | 處理 |
|------|------|
| 沒有 API Key（skip） | 直接進 Edit 模式，手動填寫，可 import |
| LLM 回傳非法 JSON | 重試一次，仍失敗 → Error 狀態，可 Retry 或 Edit |
| 網路錯誤 | Error 狀態，可 Retry 或 Edit |
| Import 失敗 | Toast 錯誤訊息，留在當前頁 |
| Service 未綁定 | 顯示提示，只能 Edit 或 Import |
| Continue 時 name 為空 | Toast 提示，不跳頁 |

## UI Style

遵循現有 OOBE theme：
- Card: `@color/card_bg`，16dp 圓角
- Text: `SetupTitle` / `SetupSubtitle` styles
- Buttons: Material outlined（重新生成、修改）+ Material text（Import）
- Slider: Material Slider，accent color
- 與 ThemePage、ApiKeyPage 風格一致
