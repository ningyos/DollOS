# Plan D v2: UI Operation, Smart Notification, Programmable Events — Design Spec

## Overview

Detailed design for the "v2" features described in AI Core Design Spec Section 7 (UI Operation Rights, Smart Notification, Programmable Events). See `2026-03-19-ai-core-design.md`.

Plan D v2 extends the background work system (Plan D v1) with three capabilities:

1. **UI Operation** — AI can read and control the phone's UI via AccessibilityService, both on the physical screen (takeover mode) and a VirtualDisplay (background mode)
2. **Smart Notification** — Context-aware notification routing (silent/normal/voice) based on DND, time, screen state, and user preferences
3. **Programmable Events** — User-defined trigger rules combining system conditions, created via natural language or Settings UI

## Section 1: DollOSAccessibilityService

### Placement

Lives inside the DollOSService app (system uid, platform cert). Shares process, permissions, and Application context with DollOSService. This is a separate `Service` class but in the same APK.

### Auto-Enable

DollOSApp.onCreate() writes to `Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES` to activate the service automatically. Platform cert grants write access to secure settings — no user interaction needed. If the service is killed or disabled by the system, DollOSApp re-enables it on next onCreate() call.

### Permissions Note

`TYPE_ACCESSIBILITY_OVERLAY` is an AccessibilityService-exclusive window type — no `SYSTEM_ALERT_WINDOW` permission needed. TakeoverManager uses `WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY` (not `TYPE_APPLICATION_OVERLAY`).

### Internal Modules

```
DollOSAccessibilityService
  ├── NodeReader         — read accessibility node tree, output structured JSON
  ├── UIExecutor         — execute UI operations (click, swipe, type, gesture)
  ├── ScreenCapture      — capture screenshots from physical screen or VirtualDisplay
  ├── TakeoverManager    — manage takeover state, overlay, touch interception
  └── AppEventMonitor    — listen to window change events, detect app open/close
```

### Communication with DollOSAIService

DollOSAIService sends UI operation commands via existing DollOSService Binder IPC. DollOSService forwards to AccessibilityService (same process, direct method call).

AppEventMonitor (in DollOSService) pushes app open/close events to DollOSAIService via `IDollOSAICallback.onAppEvent(String packageName, String eventType)`. This enables RuleEngine (in DollOSAIService) to evaluate APP_FOREGROUND conditions without cross-process polling.

## Section 2: UI Operation Modes

### Physical Screen Mode (Takeover)

**Trigger flow:**

1. AI decides it needs to operate the physical screen
2. AI asks user for confirmation in conversation (existing action confirmation flow)
3. User approves → enter takeover state
4. Display top floating bar "AI operating: [task description]" + screen edge glow
5. Touch input intercepted (TYPE_ACCESSIBILITY_OVERLAY full-screen transparent layer consumes all touch events)
6. AI reads node tree / takes screenshots → decides actions → executes
7. Exits takeover on completion, or user interrupts via power button

**Power button interrupt:**

- Single press → AccessibilityService receives KEYCODE_POWER via `onKeyEvent()` (with `FLAG_REQUEST_FILTER_KEY_EVENTS` enabled in service config). During takeover state, AccessibilityService intercepts the single press and tells TakeoverManager to show interrupt modal. The modal is drawn as TYPE_ACCESSIBILITY_OVERLAY by TakeoverManager (same process).
  - Cancel → AI stops, remove overlay, release touch, notify DollOSAIService via callback
  - Continue → dismiss modal, AI continues
- Double press → existing emergency stop via `config_doublePressOnPowerBehavior` (stop all AI). Double press is handled by PhoneWindowManager before it reaches AccessibilityService, so no conflict.

**Visual feedback during takeover:**

- **Top floating bar:** TYPE_ACCESSIBILITY_OVERLAY, shows "AI 操作中：[task description]"
- **Screen edge glow:** Animated border overlay (similar to gaming mode indicators)
- Both drawn by TakeoverManager using AccessibilityService's overlay privilege

### VirtualDisplay Mode

**Trigger:** AI needs to operate an app while screen is off. Automatic, no user confirmation needed.

**Implementation:**

1. `DisplayManager.createVirtualDisplay()` — create virtual screen (same resolution as physical)
2. Launch target app on VirtualDisplay via `ActivityOptions.setLaunchDisplayId()`
3. AccessibilityService reads node tree from VirtualDisplay via `getWindows()` filtered by displayId (API 30+, available on AOSP 16 / API 36). Note: `getRootInActiveWindow()` only returns the focused display — use `getWindows()` for multi-display.
4. Screenshots via VirtualDisplay's Surface
5. Destroy VirtualDisplay on completion

**Constraint:** Same app cannot run simultaneously on physical screen and VirtualDisplay. Acceptable because VirtualDisplay mode is used when screen is off (no conflict).

## Section 3: UI Operation Capabilities

### NodeReader Output Format

```json
{
  "package": "com.android.settings",
  "displayId": 0,
  "nodes": [
    {
      "id": "node_0",
      "class": "android.widget.TextView",
      "text": "Wi-Fi",
      "resourceId": "com.android.settings:id/wifi_item",
      "contentDescription": "",
      "bounds": [0, 200, 1080, 280],
      "clickable": true,
      "enabled": true,
      "focused": false,
      "scrollable": false,
      "children": ["node_1", "node_2"]
    }
  ]
}
```

### UIExecutor Operations

| Operation | API | Parameters |
|-----------|-----|------------|
| Click | `ACTION_CLICK` on node | node_id |
| Long press | `ACTION_LONG_CLICK` on node | node_id |
| Input text | `ACTION_SET_TEXT` on node | node_id, text |
| Swipe | `GestureDescription` | start_xy, end_xy, duration_ms |
| Drag | `GestureDescription` | start_xy, end_xy, duration_ms (longer) |
| Multi-finger gesture | `GestureDescription` multi-stroke | strokes[] |
| Back | `GLOBAL_ACTION_BACK` | — |
| Home | `GLOBAL_ACTION_HOME` | — |
| Recents | `GLOBAL_ACTION_RECENTS` | — |
| Screenshot | `takeScreenshot()` (API 30+) | — |

### AI Operation Loop

```
1. Read node tree (+ screenshot if needed for Canvas/WebView content)
2. Build context for LLM: "Current screen: [node tree JSON], Task: [user instruction]"
3. LLM responds with operation (tool call: click node_3)
4. Execute operation
5. Wait for screen update (TYPE_WINDOW_CONTENT_CHANGED event, max 3s timeout)
6. Return to step 1 until task complete or LLM signals done
```

Max turns limit per task (configurable, same mechanism as background worker). Accessibility tree is primary perception; screenshot sent to LLM vision only when node tree is insufficient (Canvas, games, images, WebView).

## Section 4: Smart Notification

### Notification Levels

| Level | Delivery | Use Case |
|-------|----------|----------|
| SILENT | Notification without sound or vibration | DND on, quiet hours, low priority |
| NORMAL | Standard notification (sound + vibration) | Standard priority |
| URGENT | TTS voice announcement + notification | High priority, user idle |

### Decision Logic

```
Inputs:
  - event.priority (HIGH / NORMAL / LOW)
  - DND state (on / off)
  - Screen state (on / off)
  - Time (user-configured quiet hours, default 23:00-07:00)
  - User per-event-type override

Rules (highest priority first):
  1. DND on → SILENT
  2. Quiet hours → SILENT
  3. User per-event-type override exists → use override
  4. HIGH + screen off → URGENT (TTS)
  5. HIGH + screen on → NORMAL
  6. NORMAL priority → NORMAL
  7. LOW priority → SILENT
```

### Implementation

- **NotificationRouter** — receives events, applies rules, selects delivery method
- Three Android `NotificationChannel`s: `dollos_silent`, `dollos_normal`, `dollos_urgent`
- **TTSInterface** — abstract interface for voice output, implementation deferred to voice pipeline
  - `fun speak(text: String, priority: Int)`
  - `fun stop()`
  - No-op default implementation until TTS is available
- **Settings:** quiet hours, per-event-type overrides, TTS enable/disable

### Placement

NotificationRouter lives in DollOSAIService (it needs access to event priority and AI context). Creates Android notifications via standard `NotificationManager`.

## Section 5: Programmable Events

### Rule Model

```kotlin
Rule {
    id: String
    name: String                    // "WiFi 斷線提醒"
    enabled: Boolean
    conditions: List<Condition>     // AND combination
    action: RuleAction
    actionParams: String            // JSON — worker task, event payload, etc.
    createdBy: String               // "user" or "ai"
    naturalLanguage: String         // original natural language (preserved when AI creates)
    debouncePeriodMs: Long          // minimum interval between triggers, default 60000
    createdAt: Long
}

Condition {
    type: ConditionType
    operator: Operator              // EQUALS, NOT_EQUALS, LESS_THAN, GREATER_THAN
    value: String
}

enum ConditionType {
    SCREEN_STATE,        // "on" / "off"
    CHARGING_STATE,      // "charging" / "discharging"
    WIFI_STATE,          // "connected" / "disconnected"
    WIFI_SSID,           // specific network name
    BLUETOOTH_STATE,     // "connected" / "disconnected"
    BLUETOOTH_DEVICE,    // specific device name
    BATTERY_LEVEL,       // 0-100
    APP_FOREGROUND,      // package name
    TIME_RANGE,          // "22:00-07:00"
    DAY_OF_WEEK          // "MON,TUE,WED..."
}

enum RuleAction {
    NOTIFY,              // use Smart Notification to notify user
    SPAWN_WORKER,        // launch background worker
    SEND_EVENT           // push event into EventQueue for AI processing
}
```

### Creation Methods

**Natural language:** User tells AI "notify me whenever WiFi disconnects" → AI parses into Rule → confirms in conversation "I'll create a rule: notify you when WiFi disconnects, OK?" → user confirms → save

**Settings UI:** Rule list page — view, edit, delete, enable/disable. Add new rule via form (select conditions + select action).

### Persistence

Room `@Entity`, same database as ScheduleEntry from v1. On boot, reload all enabled rules and register corresponding listeners.

### Rule Evaluation

`RuleEngine` listens to all condition sources:
- App events from AccessibilityService (via AppEventMonitor)
- System events from SystemEventReceiver (screen, charging, WiFi, Bluetooth)
- Battery level from BroadcastReceiver (ACTION_BATTERY_CHANGED)
- Time conditions evaluated on each event (not polled)

On each incoming event, RuleEngine iterates enabled rules. If all conditions of a rule are satisfied, trigger the rule's action. Debounce: same rule cannot fire more often than its `debouncePeriodMs` (default 60s).

### AIDL Additions

Add to `IDollOSAIService` (note: `IDollOSAIService.aidl` is defined in the AI Core design spec Section 6 and will be created as part of DollOSAIService app implementation; it does not exist yet in the committed codebase):

```
// Programmable Events
String getRules();                    // JSON list of all rules
void addRule(String ruleJson);
void updateRule(String ruleJson);
void removeRule(String ruleId);
void setRuleEnabled(String ruleId, boolean enabled);
```

## Section 6: AIDL Additions for UI Operation

### IDollOSService (add to existing)

```
// UI Operation
String readScreen(int displayId);                    // returns node tree JSON
String executeUIAction(String actionJson);           // click, swipe, type, etc.
oneway void captureScreen(int displayId, ICaptureCallback callback);  // async — takeScreenshot() is callback-based
void startTakeover(String taskDescription);          // enter takeover mode
void stopTakeover();                                 // exit takeover mode
int createVirtualDisplay(int width, int height);     // returns displayId
void destroyVirtualDisplay(int displayId);
void launchAppOnDisplay(String packageName, int displayId);

// Note: takeover interrupt modal is triggered internally by AccessibilityService
// on power button press — no AIDL call needed from AIService
```

### IDollOSAICallback (add to existing)

```
// UI Operation
void onTakeoverApproved();           // user approved takeover in conversation
void onTakeoverCancelled();          // user cancelled (power button modal)
void onScreenReady(int displayId);   // VirtualDisplay ready or screen content changed
void onAppEvent(String packageName, String eventType);  // app open/close from AppEventMonitor
void onCaptureResult(int displayId, in byte[] pngBytes); // screenshot result callback
```

## Section 7: File Structure

### DollOSService (new files)

```
src/org/dollos/service/
  accessibility/
    DollOSAccessibilityService.kt    — main AccessibilityService
    NodeReader.kt                    — read and serialize node tree
    UIExecutor.kt                    — execute UI operations
    ScreenCapture.kt                 — screenshot capture
    TakeoverManager.kt              — takeover overlay + touch interception
    AppEventMonitor.kt              — detect app open/close from accessibility events
res/
  xml/accessibility_service_config.xml   — module-level res/, not inside src/
```

### DollOSService (modify existing)

```
  AndroidManifest.xml               — register DollOSAccessibilityService
  DollOSApp.kt                      — auto-enable accessibility service
  DollOSServiceImpl.kt              — add UI operation AIDL methods, forward to AccessibilityService
  aidl/.../IDollOSService.aidl      — add UI operation methods
```

### DollOSAIService (new files)

```
app/src/main/java/org/dollos/ai/
  notification/
    NotificationRouter.kt           — decision logic + notification dispatch
    NotificationLevel.kt            — SILENT / NORMAL / URGENT enum
    TTSInterface.kt                 — abstract TTS interface (impl deferred)
    QuietHoursConfig.kt             — quiet hours configuration
  rule/
    Rule.kt                         — Rule data class + ConditionType + RuleAction
    RuleEntity.kt                   — Room @Entity
    RuleDao.kt                      — Room DAO
    RuleEngine.kt                   — evaluate rules on events, trigger actions
```

### DollOSAIService (modify existing)

```
  DollOSAIServiceImpl.kt            — add rule AIDL methods
  DollOSAIApp.kt                    — init NotificationRouter, RuleEngine
  conversation/ConversationDatabase.kt — add RuleEntity + RuleDao
  aidl/.../IDollOSAIService.aidl    — add rule management methods
  aidl/.../IDollOSAICallback.aidl   — add takeover + screen callbacks
  AndroidManifest.xml               — notification channels
```

## Implementation Status Update

After Plan D v2 implementation, update the AI Core design spec status table:

| Plan | Status | Description |
|------|--------|-------------|
| Plan D v2 | Complete | UI operation (AccessibilityService + VirtualDisplay + takeover), smart notification, programmable events |
