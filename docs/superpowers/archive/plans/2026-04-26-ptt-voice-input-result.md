# PTT 語音輸入 — 驗證結果（Task 15）

**狀態：** 待人工 E2E 驗證

## 已完成 commits（implementation + wiring）

| SHA | 訊息 |
|-----|------|
| 96c1c5b | feat(voice): wire PTTSessionController into app lifecycle |
| c9fc799 | feat(voice): TTS interrupt → LISTENING audio focus sequence |
| e62ead6 | refactor: remove WakeWordEngine + drop wakeWord from character pack manifest |
| 7880a58 | refactor(aidl): drop wake word methods + onWakeWordDetected callback |
| 5c5cd03 | refactor(voice): PTT-only ASR, remove always-on streaming + KWS hooks |
| 786f962 | feat(agent): gate sensitive actions via SensitiveActionGate |
| 43ccdc2 | feat(voice): grant BIND_VOICE_INTERACTION to DollOSAIService |
| ...    | (Task 1-6 of plan, earlier) |

Launcher: `c2704ea refactor: drop onWakeWordDetected callback stub`
Wallpaper: `b971395 refactor: drop onWakeWordDetected callback stub`

## 部署步驟

```bash
# 1. DollOSAIService
cd ~/Projects/DollOSAIService && ./gradlew assembleRelease
cp app/build/outputs/apk/release/app-release-unsigned.apk prebuilt/DollOSAIService.apk
rsync -av --delete . ~/Projects/DollOS-build/external/DollOSAIService/

# 2. Launcher（如有改動）
cd ~/Projects/DollOSLauncher-avatar-live2d && ./gradlew assembleRelease
cp app/build/outputs/apk/release/app-release-unsigned.apk \
   ~/Projects/DollOS-build/packages/apps/DollOSLauncher/prebuilt/DollOSLauncher.apk

# 3. AOSP build
cd ~/Projects/DollOS-build && source build/envsetup.sh && lunch dollos_bluejay-bp2a-userdebug
m DollOSAIService DollOSLauncher Settings -j$(nproc)
m services.core framework -j$(nproc)   # 若 Task 9 動到 framework 才需

# 4. Push
export PATH="$HOME/Android/Sdk/platform-tools:$PATH"
adb root && adb remount
adb push out/target/product/bluejay/system_ext/priv-app/DollOSAIService/* /system_ext/priv-app/DollOSAIService/
adb push out/target/product/bluejay/system_ext/priv-app/DollOSLauncher/* /system_ext/priv-app/DollOSLauncher/
adb push out/target/product/bluejay/system_ext/priv-app/Settings/* /system_ext/priv-app/Settings/
# framework（若有改）
adb push out/target/product/bluejay/system/framework/services.jar /system/framework/
adb push out/target/product/bluejay/system/framework/framework.jar /system/framework/
adb shell "rm -rf /data/dalvik-cache/arm64/system_ext@priv-app@DollOSAIService@*"
adb reboot
```

## 驗證 Checklist

| # | 場景 | 預期 | 結果 |
|---|------|------|------|
| 1 | 解鎖 → 桌面 → 長按電源 | chime + listening UI | ☐ |
| 2 | 解鎖狀態說「現在幾點」 | Doll 回時間 | ☐ |
| 3 | 鎖屏 → 長按電源（不解鎖） | chime + listening | ☐ |
| 4 | 鎖屏 → 「設個 1 分鐘後的鬧鐘」 | 直接設好（無指紋）；Clock app 有鬧鐘 | ☐ |
| 5 | 桌面 → 「打開銀行 app」 | 跳指紋 prompt → 過 → 開 | ☐ |
| 6 | 「打開銀行 app」→ 取消指紋 | Doll 回拒並回 IDLE | ☐ |
| 7 | TTS 講長故事中 → 長按電源 | TTS 停 + 進 listening + chime | ☐ |
| 8 | 進 listening 立即再長按電源 | 取消雙音 + 回 IDLE | ☐ |
| 9 | `adb logcat -d \| grep -iE "WakeWord\|openWakeWord\|KWS"` | 空（KWS 完全移除） | ☐ |

驗完把這檔案的結果欄填上 PASS/FAIL + 備註，並 commit `verify: PTT voice input E2E`。

## 已知 deferred 議題

- **Reviewer 觀察（Task 11 out-of-scope）**：launcher 與 AIService 的 `IDollOSAICallback.aidl` 在 4 個既有 method 上不一致（`onOpsEvent` / `onTtsAmplitude` / `onSubtitle` / `onVisionCaptureStateChanged`）。本 plan 不處理，但會在 binder 跨 process 觸發 crash — 建議下一輪修。
- **Code review 觀察（Task 10 I2）**：`processAudio` 對 `state` 是 lock-free 讀取，理論上仍有 TOCTOU window。Task 14 的 stop sequence 在實務上應已抑制最大可觀察影響，但若驗證階段聽到 ASR 抓到 TTS 尾巴，需回頭加 `feedAudio` 鎖。
- **Code review 觀察（Task 10 M3）**：`MODEL_BASE = /system_ext/dollos/models/voice` 寫死，VoicePipeline 無法 unit test。

