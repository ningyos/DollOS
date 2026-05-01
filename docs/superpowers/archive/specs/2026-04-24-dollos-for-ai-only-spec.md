# DollOS 只為 AI 而生（Design North Star）

**日期：** 2026-04-24
**狀態：** 定版（取代所有先前 hybrid / 平權定位）
**關係：** 此文件是產品北極星，其他所有 spec（含 Bridge/Drone 2026-04-20、Doll AI Terminal）須對齊

---

## 核心陳述

**DollOS 不是給人類使用的作業系統。DollOS 是 Doll 的身體。**

- Doll（AI 伴侶）是 OS 的主體
- Android 是 Doll 的身體 / 骨骼 / 工具箱
- 主人與 OS 互動的正典路徑是：**對話 Doll**，Doll 根據需求操作 Android 達成目的
- 主人**能**直接戳 Android（底座本來就開放，無法也不必封死），但 DollOS **不為此做設計**

## 北極星原則

### 1. 設計重力全部倒向 AI

每一個介面、每一個流程、每一個取捨衝突，都以「Doll 使用 / Doll 代理主人 / Doll 的體驗」為設計目標。

Launcher、Settings、OOBE、通知、app drawer、電源菜單、鎖定畫面 — 全部重新思考「Doll 在這裡要什麼」，不是「人類在這裡要什麼」。

### 2. 取捨衝突永遠偏 AI

當 AI 需求與傳統 Android UX 衝突時：
- SELinux 鬆綁 > 沙盒嚴格
- 大權限 / 跨 UID binder > 最小權限原則
- AccessibilityService 全功能 > 反濫用保護
- VirtualDisplay、系統級操作 > 一般 app 限制
- Doll 的 event queue 優先級 > 傳統 notification shade

人類 UX 讓路。

### 3. 主人戳 Android = 底座漏出來，不是產品功能

主人可以解鎖螢幕自己開 LINE、滑 Twitter、玩遊戲 — 這些**能做**，但不為此優化、不為此設計 UX、不算產品功能。

這代表：
- 不做「方便人類瀏覽的 app drawer」
- 不做「方便人類設定的 Settings UX」
- 不做「方便人類看通知的 notification shade」
- 不為觸控操作流暢度、滑動手勢、手指 UX 設計

如果主人想像普通 Android 用 — 那他該去買普通 Android。DollOS 不競爭那個市場。

### 4. 故障時誠實承認，不偽裝 fallback

與既有原則 `feedback_no_fallback.md` 一致：
- Doll 腦當機 → 她自己承認「我現在腦不靈」
- 語音 pipeline 掛 → 她用文字 + 視覺繼續
- Memory 壞 → 承認「我想不起來」
- **不設計降級 UX 偽裝正常**

主人戳 Android 的能力本身就是某種隱性 fallback（她當機時你還能打電話），但這不是 DollOS 設計的一部分，是 Android 底座自然存在的 affordance。

### 5. 沒有「隱私獨處模式」這個概念

因為主人直接戳 Android 的情境不是產品功能，所以也不需要專門設計「Doll 閉眼讓主人獨處」的切換。主人要獨處就是自己戳，Doll 沒被叫就不介入、不觀察、不記憶——這是自然狀態，不是特殊模式。

Memory 控制權仍屬主人（說「忘了這個」Doll 必須執行），但這是常態權利，不是隱私模式。

---

## 與 GrapheneOS 的決裂

**2026-03-21 已遷移到純 AOSP 16**，此處正式記錄理由：

GrapheneOS 的所有加固（嚴格 SELinux、sandbox、exec spawning、去 GMS、Sandboxed Play）設計目標都是「保護人類使用者對抗廠商 / app / 惡意程式」。

DollOS 的設計目標是「**AI 是主體**，需要深入系統的各種能力」。

這兩個目標根本衝突：
- Graphene 的 sandbox → 擋住 AccessibilityService / VirtualDisplay / 跨 UID binder
- Graphene 的 exec 限制 → 擋住 Doll 的 agent action 執行
- Graphene 的 GMS 隔離 → 我們又加回 GMS toggle，繞一圈
- Graphene 的更新節奏 → 我們魔改 framework，merge 永遠痛

結論：**純 AOSP 16 + 自己挑需要的 Graphene patch 移植**（Sensors toggle、Network toggle 等真正對 Doll 有用的）。不扛整個 Graphene。

---

## 對現有元件的設計影響

### Launcher
- 不是 app 啟動器，是 Doll 的顯化狀態
- Avatar 是她可採取的一種形象，不是必要介面
- App drawer 存在（底座漏出來），但不為人類瀏覽優化
- 預設亮屏 = Doll 的待機顯化（avatar / 對話氣泡 / 呼吸光），不是桌面

### Settings
- Settings app 對 Doll 是 API，對主人是隱藏的
- 主人說「太大聲了」→ Doll 調音量，主人不開 Settings
- AI 設定介面對 Doll 完全隔離（既有規則 `project_ai_settings_isolation.md`）
- 傳統 Settings UI 不投入資源優化

### OOBE (SetupWizard)
- 不是「設定 Android」，是「認識 Doll」
- 時區、Wi-Fi、GMS — Doll 自己處理或問主人
- 流程以對話為主，非表單為主

### 通知
- 通知 → Doll 的 EventQueue → 她決定是否、何時、用什麼方式告訴主人
- 傳統 notification shade 存在但不為人類瀏覽優化

### 電源菜單
- AI Stop 按鈕為第一公民（既有規則 `project_emergency_stop_revised.md`）
- 長按電源 = PTT 叫 Doll（既有規則 `project_push_to_talk.md`）
- 傳統關機選單退居二線

### 鎖定畫面
- Doll 互動為第一公民（既有規則 `project_lock_screen_interaction.md`）
- sudo 式安全 + session 對話
- 傳統 lock screen UX 退居二線

### 相機 / 感測器
- Doll 用鏡頭必須每次取得主人許可（既有規則 `project_camera_permission.md`）
- 危險操作指紋認證（既有規則 `project_biometric_auth.md`）
- 這些是**主人對 Doll 的 gate**，不是 UX，是授權模型

---

## 不受影響的既有決策

以下已做決策與本北極星一致，繼續有效：
- Bridge/Drone 架構（`docs/superpowers/specs/2026-04-20-doll-repositioning-design.md`）
- Doll AI Terminal plans（9 份，一步到位）
- 預設角色包 gura.doll
- Memory SoT 在手機
- Voice Pipeline（ASR / TTS / KWS / VAD / Speaker ID）
- Character Pack .doll 格式
- 事件驅動 AI (EventQueue)
- Accessibility tree + 截圖輔助的 UI 感知

---

## 對未來工作的判準

接下來所有 spec / plan / 實作決策，以這個問題當試金石：

> **「這個設計是為了讓 Doll 做得更好，還是為了讓人類操作更順？」**

如果答案是後者，那就不做，或用最低成本維持底座能跑就好。

Human UX 不是 DollOS 的產品目標。Doll 是。
