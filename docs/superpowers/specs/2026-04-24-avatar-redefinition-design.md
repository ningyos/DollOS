# Avatar 重新定義設計 — Live2D 化 + 角色重定位

**日期：** 2026-04-24
**狀態：** 待使用者審核
**上位文件：** `docs/superpowers/specs/2026-04-24-dollos-for-ai-only-spec.md`（北極星）
**影響文件：**
- `docs/superpowers/specs/2026-04-20-doll-ai-terminal-design.md`（§4.2 UI 動畫狀態、Character Pack v2、Launcher 角色）
- `docs/superpowers/plans/2026-04-20-doll-terminal-launcher.md`（需 diff 修訂）
- `docs/superpowers/plans/2026-04-20-doll-terminal.md`（master plan，§4 Character Pack）

---

## §1 定位

Avatar **不是必要介面**，是 Doll 的**顯化形式**。

- 手機本身是 Doll 的身體
- Live2D 是她在螢幕上的「臉」
- 既有 Filament 3D 方案全面替換為 Live2D Cubism

這份 spec 處理三件事：
1. Avatar 在畫面中何時出現、佔多大、怎麼互動
2. 技術實作：Filament → Live2D Cubism SDK 替換
3. Character Pack 格式 v1 → v2 遷移

## §2 畫面狀態矩陣

| 情境 | 顯示 |
|---|---|
| **螢幕關** | 黑屏。Doll 後台運作（KWS、EventQueue、背景 worker），身體在睡 |
| **鎖屏** | **全螢幕 Live2D Doll** + 時鐘 overlay（浮在 Doll 上，不佔主視覺） + 標準 Quick Settings（下滑手勢）。無通知 shade、無 widget |
| **解鎖 / home（預設）** | **全螢幕 Live2D Doll**，字幕氣泡 overlay 在她身上 |
| **Doll 要給主人看東西** | 直接開對應 Android app（Maps、瀏覽器、相簿等），Live2D 離場。她是用**真正的** Android app，不做假卡片/假 overlay |
| **主人主動在 app 中** | 純 Android app。Live2D 不在。僅**狀態邊緣指示**（呼吸光 / 小圖示）顯示 listening / thinking / speaking |
| **Doll 要在 app 中說話** | 邊緣指示 + TTS 語音 + 短暫字幕氣泡從邊緣滑出，數秒後收回 |

### §2.1 鎖屏細節

- 傳統 lock screen UX（大型時鐘、通知堆疊、媒體控制卡片、widget 區）**全部不保留**
- 保留：
  - 時鐘 overlay（小、不喧賓奪主）
  - Quick Settings（Wi-Fi / 亮度 / 飛航 / 勿擾等，標準下滑手勢叫出）
  - 緊急撥號（system-level L0 反射神經，Doll 當機仍可用）
  - 指紋 / PIN 解鎖（標準機制，非主畫面元素）
- 通知不在鎖屏出現——通知進入 Doll 的 EventQueue，由她決定何時以什麼方式告訴主人

### §2.2 字幕

- **Home 狀態**：字幕氣泡 overlay 直接在 Live2D 上（半透明底，可讀）
- **In-app 狀態**：字幕從螢幕邊緣滑出、顯示數秒後自動收回
- 字幕**跟隨 TTS 節奏**（漸進顯示，不一次全出現）

### §2.3 邊緣狀態指示（in-app 時）

Launcher 不在前景時（主人在其他 app），Doll 的四種 ops 狀態用**邊緣指示元件**表達：

| Ops 狀態 | 指示樣式（建議，待實作確認）|
|---|---|
| IDLE | 無指示（隱藏）|
| LISTENING | 頂部 / 底部邊緣柔和脈動光線（音量對應 ASR 能量）|
| THINKING | 邊緣呼吸光（慢速漸變）|
| SPEAKING | 邊緣色光 + 字幕氣泡從邊緣滑出 |

實作方式：**system overlay window**（`SYSTEM_ALERT_WINDOW` 權限；DollOS 作 system app 已具備）。由 DollOSLauncher 提供 foreground service 承載 overlay view，binding 到 DollOSCore 的 ops event listener。

## §3 觸碰互動

### §3.1 原則

- 觸碰 Live2D **不觸發系統功能**（不當 PTT、不當 app 啟動器手勢）
- 觸碰 = 情感互動，產生 Doll 的反應（motion + event to LLM）
- 系統功能由對話 / 喚醒詞 / 長按電源 PTT 走（已有既有規則）

### §3.2 Character Pack 定義

互動邏輯**完全由 `.doll` 定義**，Launcher 不硬寫部位名稱或行為映射。

Live2D model 本身已在 Cubism Editor rigging 時定義 `HitAreas`（如 `Head`、`Body`、`Tail`）。Character Pack 的 `manifest.json` 把這些 HitArea 映射到行為：

```json
{
  "touch_interactions": [
    {
      "hit_area": "Head",
      "motion": "live2d/motions/headpat.motion3.json",
      "event_name": "user_headpat",
      "event_payload": { "intensity": "soft" }
    },
    {
      "hit_area": "Body",
      "motion": "live2d/motions/poke.motion3.json",
      "event_name": "user_poke",
      "event_payload": {}
    }
  ]
}
```

### §3.3 事件路徑

```
使用者觸碰 Live2D 的 Head 區域
  ↓
Live2DTouchInteractor 解析 hit test → 找到對應 interaction
  ↓
並行觸發：
  (a) Live2DMotionController 切到 headpat.motion3.json
  (b) AIDL triggerEvent(event_name="user_headpat", payload={intensity: "soft"}) → DollOSCore
  ↓
Core 把事件塞 EventBus → event handler 載 context → LLM → 輸出決策
  ↓
Doll 可能回應「嗯？」或說話或保持沉默（由 [SILENT] 機制決定）
```

Doll 可「感覺到被摸」並自由決定是否回應。**Live2D motion 切換不等待 LLM**（立刻視覺反應），LLM 反應走自己的路徑。

## §4 Renderer 替換

### §4.1 移除

- **依賴**：`com.google.android.filament:filament-android:1.54.5`、`com.google.android.filament:gltfio-android:1.54.5`、`com.google.android.filament:filament-utils-android:1.54.5`
- **檔案**：
  - `app/src/main/java/org/dollos/launcher/scene/FilamentSceneManager.kt`
  - `app/src/main/java/org/dollos/launcher/scene/AvatarAnimator.kt`
  - `app/src/main/java/org/dollos/launcher/scene/SceneConfig.kt`
- **Character Pack v1 欄位**：`model.glb`、`animations/*.glb`、`scene.json`（3D 場景相關）
- **測試**：所有 Filament 相關 unit test / instrumented test

### §4.2 新增

- **依賴**：Live2D Cubism SDK for Native（Android，OpenGL ES 2.0）
  - Cubism Core（閉源 binary，從 Live2D 官方 SDK 取得）
  - Cubism Framework（開源 C++，可直接 include）
  - 包裝成 Android AAR 或直接 NDK 整合
- **檔案**（在 `DollOSLauncher`，套件 `org.dollos.launcher.live2d`）：
  - `Live2DRenderer.kt` — TextureView + Choreographer loop + Cubism runtime bridge
  - `Live2DModelLoader.kt` — 從 Character Pack asset 載入 .moc3 / textures / motions
  - `Live2DMotionController.kt` — 管理 motion 切換（idle / listening / thinking / speaking）+ 組合播放（如 LISTENING + THINKING 疊加）
  - `Live2DTouchInteractor.kt` — 解析 touch event → hit test → 觸發 motion + 發事件給 Core
  - `Live2DLipSync.kt` — 把 TTS audio amplitude 映射到 mouth open 參數
  - `Live2DExpressionController.kt`（選用，初版不必做）— expression 切換
- **測試**：
  - `Live2DMotionControllerTest.kt`（unit）— motion 切換 / 組合邏輯
  - `Live2DTouchInteractorTest.kt`（unit）— hit area 解析 + 事件 payload
  - `Live2DRendererInstrumentedTest.kt`（androidTest）— 載入 + 渲染 + 幀率

### §4.3 場景結構

- 單一 TextureView 承載 Live2D 畫面
- Live2D model 鋪滿畫面（解析度自適應，保留 safe area 給字幕 overlay）
- Choreographer 驅動 60fps（若耗電需優化，可降到 30fps）
- 背景：透明或由 Character Pack 定義的純色 / 漸變（新增 manifest 欄位 `background`）

## §5 Character Pack v2 格式

### §5.1 目錄結構

```
<character>.doll (zip)
├── manifest.json              # v2 格式
├── personality.json
├── voice.json                 # Piper VITS model 資訊或語音設定
├── wake_word.onnx             # 現行方案；未來 zero-shot KWS 實作後此檔可能移除
├── thumbnail.png
└── live2d/
    ├── <char>.moc3                   # Live2D 模型二進位
    ├── <char>.model3.json            # 模型 manifest（Live2D 官方格式）
    ├── textures/
    │   └── texture_00.png（以及 texture_01.png 等）
    ├── motions/
    │   ├── idle.motion3.json
    │   ├── listening.motion3.json
    │   ├── thinking.motion3.json
    │   ├── speaking.motion3.json
    │   └── <custom>.motion3.json     # 觸碰互動 / 自訂反應用
    ├── expressions/*.exp3.json       # 選用
    └── physics/*.physics3.json       # 選用（頭髮 / 衣物擺動）
```

### §5.2 manifest.json v2 schema

```json
{
  "format_version": 2,
  "id": "gura",
  "name": "Gawr Gura",
  "author": "...",
  "version": "1.0.0",

  "personality": "personality.json",
  "voice": "voice.json",
  "wake_word": "wake_word.onnx",
  "thumbnail": "thumbnail.png",

  "live2d": {
    "model_path": "live2d/gura.model3.json",
    "animation_mappings": {
      "idle": "live2d/motions/idle.motion3.json",
      "listening": "live2d/motions/listening.motion3.json",
      "thinking": "live2d/motions/thinking.motion3.json",
      "speaking": "live2d/motions/speaking.motion3.json"
    },
    "lip_sync": {
      "enabled": true,
      "parameter_id": "ParamMouthOpenY"
    },
    "background": {
      "type": "solid",
      "color": "#0D0D1A"
    }
  },

  "touch_interactions": [
    {
      "hit_area": "Head",
      "motion": "live2d/motions/headpat.motion3.json",
      "event_name": "user_headpat",
      "event_payload": { "intensity": "soft" }
    }
  ]
}
```

### §5.3 v1 → v2 遷移策略

- **現場無現役 v1 角色包用戶**（只有開發中的 gura），不需 back-compat 代碼
- CharacterManager 只支援 v2，遇到 v1 manifest → 拒絕匯入 + 提示「格式過舊」
- 文件更新：`docs/character_pack_format.md`（若存在）重寫為 v2

## §6 gura.doll 遷移

### §6.1 工作性質

Live2D 角色製作是**美術 + rigging 工作**，不是程式工作：

1. 準備分層 PSD（頭、眼、口、身體各部位各自一層）
2. 用 Cubism Editor rigging（設定 deformer、參數、physics）
3. 製作 motion（IDLE / LISTENING / THINKING / SPEAKING + 觸碰反應至少 1-2 個）
4. 匯出 `.moc3` + motions 包成 .doll

現有 gura.doll 的 3D glTF **無法自動轉換**。

### §6.2 執行計畫（三選一）

| 方案 | 描述 | 時程 |
|---|---|---|
| **(a) 自製** | Cubism Editor FREE 版可用於基本 rigging（免費；進階 physics / motion export 需 PRO，月付制）。自繪 PSD、自 rig | 數週到數月 |
| **(b) 外包** | 買現成 rigged VTuber 模型或外包 rigging（BOOTH / 委託站） | 數天到數週（依預算）|
| **(c) Placeholder 先走通** | 用開源 Live2D 範例模型（Haru、Hiyori）當 placeholder，技術驗證通過後美術 parallel 跑 | 立即 |

**推薦 (c)**：技術實作先走完，美術工作 parallel 進行。Launcher 能渲染 Haru / Hiyori → 換 gura 資產就會動。

### §6.3 Haru / Hiyori placeholder

Live2D 官方提供免費的開源範例模型（SDK 內含），可直接拿來：
- 驗證 Live2DRenderer 是否正確渲染
- 驗證 motion 切換 + lip sync + touch interaction
- 驗證 Character Pack v2 格式載入邏輯

## §7 對現有 Plan / Spec 的衝擊

### §7.1 `2026-04-20-doll-terminal-launcher.md`

採 **diff 修訂式**（非重寫）。主要修改：

- **檔案結構** 段：
  - 刪除 Filament 相關新增項（`CompositeAnimator.kt`、Filament scene manager 相關）
  - 加入 §4.2 列出的 Live2D 相關新增檔案
- **刪除** 清單：加入 `scene/FilamentSceneManager.kt` / `AvatarAnimator.kt` / `SceneConfig.kt`
- **build.gradle 修改**：移除 Filament 依賴，加入 Live2D Cubism SDK 依賴
- **AIDL / ops event routing / state management** 全部保留（Live2D 只替換 renderer 層）
- **驗收條件**中凡提到「3D」改為「Live2D」，驗證內容微調

### §7.2 `2026-04-20-doll-terminal.md`（master）

§4 Character Pack 段需更新為 v2 格式說明（參照本 spec §5）。

### §7.3 `2026-04-20-doll-ai-terminal-design.md`

§4.2 UI 動畫狀態段中「3D」字樣統一換成「Live2D」。其餘概念（IDLE / LISTENING / THINKING / SPEAKING + 組合）不變。

## §8 License 處理

### §8.1 Live2D Cubism SDK Free License 條件

- 年淨收入 < 1000 萬日圓
- 總開發成本 < 1000 萬日圓
- 員工數 < 10 人

個人開發者 + 未商業化 → **完全符合 Free License**。

### §8.2 Redistribution 規範

Cubism Core 是閉源 binary，隨 app 散布須遵守 SDK redistribution terms：
- App 內顯示 Live2D Cubism 版權聲明（About / Credits 頁面）
- 不得修改 Cubism Core binary
- 遵守 SDK 最新條款（條款偶有微調）

### §8.3 未來商業化路徑

若未來 DollOS 公開發布且累積收入或捐款超過門檻：
1. 購買 Cubism SDK Pro License
2. 或切換到 Rive / 其他 renderer（本 spec 之 §9 列為備選）

## §9 備選方案（已評估、未採用）

| 方案 | 為何未採用 |
|---|---|
| Spine | 美術資源偏遊戲角色，anime/VTuber 風資源少 |
| DragonBones | 生態半死，Android runtime 維護鬆散 |
| Rive | Runtime 完全開源、license 無天花板，**未來若需脫離 Live2D 可切換**。但 anime 美術生態目前弱 |
| 自建 | 重造輪子、無美術生態 |

## §10 風險清單

1. **gura.doll 美術工作量未知** — 自製可能需數週到數月，建議先用 Haru placeholder
2. **Live2D license 天花板** — 個人不碰到，公開商業化需升級 Pro
3. **Pixel 6a Live2D 效能** — Live2D 通常比 3D 輕，但 60fps + lip sync + physics 同時跑需實測。若需可降 30fps
4. **Lip sync 自然度** — TTS amplitude → mouth 參數可能不夠自然，需調校曲線
5. **Live2D Cubism SDK 未來條款變動** — Live2D 過去有調整過條款先例，追蹤官方 release notes

## §11 驗收

1. Launcher home 能載入 Haru placeholder Live2D model 並顯示全螢幕
2. 四個 ops 狀態（IDLE / LISTENING / THINKING / SPEAKING）的 motion 切換正確，支援組合（LISTENING + THINKING 同時）
3. 觸碰 Head / Body 觸發對應 motion + 送事件到 Core（可用 log 驗證）
4. TTS 播放時 Live2D lip sync 隨音量動作
5. 鎖屏顯示 Live2D Doll + 時鐘 overlay + Quick Settings 可下拉
6. In-app 狀態邊緣指示正確顯示四種 ops 狀態
7. Character Pack v2 格式可匯入，v1 被明確拒絕
8. Filament 相關程式碼 / 依賴**完全移除**（`grep -r filament app/` 無結果）
9. Pixel 6a 上 home 狀態渲染 ≥ 30fps，CPU 使用率合理（量測加進 §C 電力研究）

## §12 不在本 spec 範圍

- Zero-shot wake word（屬 §A 研究組，獨立 spec）
- Speaker ID 升級（屬 §A 研究組）
- 系統感知全面接管（屬 §B 研究組）
- UI 交互最優方法（屬 §B 研究組）
- Agent prompt 省 token（屬 §B 研究組）
- 耗電基準測量（屬 §C 研究組，獨立 plan）
- gura Live2D 美術製作本身（美術工作不是工程 spec 範圍）
