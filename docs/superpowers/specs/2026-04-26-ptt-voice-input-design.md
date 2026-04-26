# PTT 語音輸入架構

**Date:** 2026-04-26
**Replaces:** Wake word as primary voice trigger
**Status:** Approved (brainstorm 2026-04-26)

## 動機

Wake word（「嗨 Doll」之類的喚醒詞）破壞 immersion，效果差。改用 push-to-talk（長按電源鍵）為唯一 voice input 觸發路徑。自動過濾（always-listening + intent classifier）是長期目標，**不在本 spec 範圍**。

## 範圍

**做**
- 拿掉 wake word pipeline（openWakeWord 三層 ONNX：mel / embedding / classifier）
- 註冊 DollOSAIService 為系統 Assist provider
- 長按電源鍵 → launchAssist → PTT session
- 認證 / 權限模型：sensitive 標記 + 指紋確認
- 鎖屏 / 解鎖完全跟 PTT 解耦
- Speaker ID 退化為 personalization hint，不再當 auth gate

**不做**
- 自動過濾（always-on ASR + 意圖判斷）
- Press-and-hold 自訂手勢（用 launchAssist 一次性 intent）
- 改 PTT 鍵位（power button 是定論）
- 修改既有 emergency call、shutdown menu 行為以外的電源鍵手勢

## 認證 / 權限模型

分線是 **敏感 vs 非敏感**，標記在 skill / action / data type 層級，靜態定義：

**敏感** — 必須指紋
- 系統 / 第三方 app 操作（開銀行 / messenger / 改設定）
- 機敏 data：SMS / email / 聯絡人 / 檔案
- 鏡頭 / 螢幕擷取
- character pack 標 `private` 的 memory entries
- 安裝 / 卸載 app

**非敏感** — 任何人都能用
- 對話 / Q&A
- DollOS 內部 self-contained 動作：鬧鐘（Android `AlarmClock.ACTION_SET_ALARM`）、to-do、語音備忘、切換 character pack
- 公開資訊查詢：時間、天氣
- 查 Doll 自己的非私人 memory

**Speaker ID 角色**
- 識別誰在講，用來 personalization（招呼、語氣、引用過去對話）
- 不當 auth gate
- 主人 / 非主人之分只反映在 *互動風格*，不反映在 *權限*

**鎖屏 / 解鎖**
- 跟 PTT 完全解耦 — 任何狀態都能講
- 鎖屏不是權限分線，sensitive 才是
- Pickup wake（拿起手機 / 從口袋拿出）讓螢幕亮 + Haru 可見，這已是 Plan 3 完成的行為

## 觸發架構

```
Power button long-press
  → AOSP framework 路由 launchAssist intent
  → DollOSVoiceInteractionService.onShow()
  → PTTSessionController.startSession()
  → AudioRecord open + ASR start
  → State = LISTENING + chime + edge vignette 青藍 + 字幕「在聽…」
  → ASR partial → 字幕氣泡更新
  → VAD silence 1.5s → ASR final
  → SpeakerID identify (personalization hint, not gate)
  → State = THINKING + 紫 vignette
  → LLM stream
  → 若 LLM 要 sensitive action → 跳指紋 prompt → 過 / 取消
  → State = SPEAKING + 暖橙 vignette + 字幕 + Live2D lip sync
  → TTS 結束 / VAD 結束 → State = IDLE
```

## 狀態機

| 狀態 | 視覺 | 聲音 | 行為 / 退出條件 |
|---|---|---|---|
| IDLE | 無 vignette | 無 | 等 PTT 觸發 |
| LISTENING | 青藍 vignette + 字幕「在聽…」 | 進入 chime（短上揚 "叮-"） | ASR 串流；VAD 1.5s 靜音結束；最長 30s 強制結束；長按電源 = 取消（雙音 "咚咚"，回 IDLE） |
| THINKING | 紫 vignette + 字幕「…」 | 無 | LLM 處理；最長 20s（hang 自動 abort）；長按電源 = 中斷回 IDLE；若 sensitive → 暫停跳指紋 prompt |
| SPEAKING | 暖橙 vignette + 字幕內容 + Live2D lip sync | TTS 播放 | TTS 結束 → IDLE；長按電源 = 中斷 TTS 立即進 LISTENING + chime |

**Audio focus 序列**（中斷 TTS 進 LISTENING）
1. `tts.stop()` 並等待 audio sink release（max 100ms）
2. 短延遲（50ms）讓 audio 系統 quiet
3. 開 AudioRecord
4. 進 LISTENING

## 元件 / 改動

**新增**
- `org.dollos.ai.voice.PTTSessionController` — 持有狀態機、協調 ASR / VAD / SpeakerID / LLM / TTS / EdgeOverlayState / Live2D lip sync 餵 wallpaper
- `org.dollos.ai.voice.DollOSVoiceInteractionService` — Android `VoiceInteractionService` 子類，註冊為系統 Assist provider，`onShow` → `PTTSessionController.startSession()`
- `org.dollos.ai.voice.DollOSVoiceInteractionSession` — `VoiceInteractionSession` 子類（framework 要求）
- `res/xml/voice_interaction_service.xml` — 設 `supportsLaunchVoiceAssistFromKeyguard=true`、`sessionService` 指向上面的 session class

**改動**
- `VoiceController` / `VoicePipeline` — 移除 KWS engine、ASR 改成「PTT 才開」（不再 always-on）
- `EdgeOverlayState` — 已支援 LISTENING / THINKING / SPEAKING / IDLE，本 spec 不加新狀態
- `IDollOSAIService.aidl` — 移除 wake word 相關方法（`setWakeWordEnabled`、`isWakeWordEnabled`、`setWakeWord`）+ callback `onWakeWordDetected`
- AndroidManifest — 加 `DollOSVoiceInteractionService` + `BIND_VOICE_INTERACTION` permission
- `privapp-permissions-dollos-ai.xml` — 加 `BIND_VOICE_INTERACTION`
- AOSP framework — 確認 / 必要時 patch `PhoneWindowManager` 把長按電源鍵綁 `launchAssist`（GrapheneOS 預設行為需實測）
- Settings UI — 移除 wake word 區
- agent skill manifest schema — 加 `sensitive: boolean` 欄位；agent runtime 在執行前檢查，sensitive 走指紋 prompt 路徑

**移除**
- `WakeWordEngine` 整個（mel / embedding / classifier 三層 ONNX 載入路徑）
- `wake_word.onnx` 從 `.doll` character pack manifest 拿掉
- 訓練工具（`~/Projects/DollOS/wake_word_training/`）保留 — 純 dev 工具，不影響 runtime

**ASR / TTS / VAD / Speaker ID** 完全保留，只是觸發點變

## 風險

1. **長按電源鍵綁定** — Android 預設長按 = 電源選單 / 緊急 / shutdown。GrapheneOS 可能已改 / 鎖死。需 framework 實測，必要時 patch `PhoneWindowManager`（Plan 4 已示範可改）。
2. **VoiceInteractionService 鎖屏觸發** — `supportsLaunchVoiceAssistFromKeyguard` 在某些 ROM 行為不一致。需鎖屏實測 mic capture + UI overlay 都能正常。
3. **Audio focus race**（中斷 TTS 進 LISTENING）— sequence 已定義（stop → wait → record），實作要嚴格遵守。
4. **指紋 prompt mid-flow** — `BiometricPrompt` 要求 caller activity，但 PTTSession 跑在 service 裡。需用 `BiometricManager` + 透明 activity proxy，或現有 `TaskManagerActivity` 模式。
5. **敏感標記漏標** — 任何 skill 沒明確標 `sensitive` 預設視為 sensitive（fail-safe）。
6. **多人同住環境的 personalization 誤判** — Speaker ID 不當 auth 後，誤認 OK（只影響語氣）。

## 驗收

- 鎖屏 → 長按電源 → 聽 chime → 「現在幾點」→ Doll 用語音回答
- 桌面 → 長按電源 → 同上
- 鎖屏 → 「設個 7 點的鬧鐘」→ Doll 直接設好（無指紋）
- 桌面 → 「打開銀行 app」→ 跳指紋 prompt → 過 → 開
- 桌面 → 「打開銀行 app」→ 跳指紋 prompt → 取消 → Doll 說「好那就先這樣」回 IDLE
- 開機後 logcat 沒有 `WakeWordEngine` / `openWakeWord` / KWS ONNX 載入訊息
- `pm dump org.dollos.ai` 不包含 wake word ONNX 資產
- 移除前後實機 RSS / 耗電對照（KWS 砍掉應省 always-on 推論成本）

## 不在範圍 / 後續

- **自動過濾**（always-on ASR + 意圖分類）— Phase 2，需要先做基礎效能 / 隱私評估
- **視覺身份識別**（front camera face ID）— Phase 2 或更後
- **Press-and-hold 自訂手勢**（按住講、放開停） — 不做，launchAssist 一次性 intent + VAD 結束已足夠
- **多 PTT 鍵位**（音量鍵 / 自訂鍵）— 不做，唯一觸發點是長按電源
- **緊急 / fullscreen intent 整合**（來電、鬧鐘 fullscreen 蓋掉 PTT session）— 跟 Plan 4 緊急路徑驗證合併處理
- **離線模式 LLM**（無網路時走 local model）— 獨立議題
