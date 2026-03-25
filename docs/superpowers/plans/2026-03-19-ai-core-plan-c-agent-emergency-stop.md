# AI Core Plan C: Agent System + Emergency Stop

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the system control infrastructure for DollOS AI: agent Action interface for device operations, DollOSService updates for executing actions on behalf of AIService, and emergency stop mechanism (double-click power button -> AI Task Manager).

**Architecture:** Agent actions are defined as an extensible Action interface in DollOSService. The emergency stop uses AOSP's built-in `MULTI_PRESS_POWER_LAUNCH_TARGET_ACTIVITY` config (no framework modification needed) to launch a Task Manager Activity. DollOSService gains new AIDL methods for system action execution and task management. DollOSAIService integrates tool calling with provider-native formats (Anthropic tool_use, OpenAI/Grok function calling).

**Tech Stack:** Kotlin, AIDL (Binder IPC), AOSP resource overlay (config_doublePressOnPowerBehavior), Android System Services (AlarmManager, WifiManager, BluetoothManager)

**Status:** All implementation complete (2026-03-23). Pending: build + deploy + on-device verification.

---

## File Structure

### DollOSService (modify existing)

```
packages/apps/DollOSService/
  aidl/org/dollos/service/
    IDollOSService.aidl              -- add executeSystemAction, getAvailableActions, showTaskManager
  src/org/dollos/service/
    DollOSApp.kt                     -- add ActionRegistry init + instance
    DollOSService.kt                 -- service binding (unchanged)
    DollOSServiceImpl.kt             -- add new AIDL method implementations
    action/
      Action.kt                      -- Action interface definition
      ActionRegistry.kt              -- registers and looks up available actions
      OpenAppAction.kt               -- open app by package/name
      SetAlarmAction.kt              -- set alarm via AlarmManager
      ToggleWifiAction.kt            -- toggle WiFi on/off
      ToggleBluetoothAction.kt       -- toggle Bluetooth on/off
    taskmanager/
      TaskManagerActivity.kt         -- emergency stop UI (modal)
      AITask.kt                      -- data class for AI task info
  res/layout/
    activity_task_manager.xml        -- task manager modal layout
    item_ai_task.xml                 -- individual task item layout
  res/drawable/
    button_primary_background.xml    -- rounded ripple button
  AndroidManifest.xml                -- add TaskManagerActivity + permissions
  privapp-permissions-dollos-service.xml -- agent action permissions
```

### DollOSAIService (tool calling integration)

```
app/src/main/java/org/dollos/ai/
  llm/
    LLMClient.kt                    -- interface with tools parameter
    LLMResponse.kt                  -- ToolCall + ToolDefinition data classes
    ClaudeProvider.kt                -- Anthropic tool_use format
    OpenAIProvider.kt                -- OpenAI function calling format
    GrokProvider.kt                  -- extends OpenAIProvider
    CustomProvider.kt                -- extends OpenAIProvider
  DollOSAIServiceImpl.kt            -- sendMessage() with tool calling loop
```

### Resource Overlay

```
vendor/dollos/overlay/
  frameworks/base/core/res/res/values/
    config.xml                       -- doublePressOnPower + globalActionsList
```

---

## Task 1: Action Interface and Registry

**Status: COMPLETE**

**Files:**
- Create: `packages/apps/DollOSService/src/org/dollos/service/action/Action.kt`
- Create: `packages/apps/DollOSService/src/org/dollos/service/action/ActionRegistry.kt`

- [x] **Step 1: Create Action interface**
- [x] **Step 2: Create ActionRegistry**
- [x] **Step 3: Commit**

---

## Task 2: Four Action Implementations

**Status: COMPLETE**

**Files:**
- Create: `packages/apps/DollOSService/src/org/dollos/service/action/OpenAppAction.kt`
- Create: `packages/apps/DollOSService/src/org/dollos/service/action/SetAlarmAction.kt`
- Create: `packages/apps/DollOSService/src/org/dollos/service/action/ToggleWifiAction.kt`
- Create: `packages/apps/DollOSService/src/org/dollos/service/action/ToggleBluetoothAction.kt`

- [x] **Step 1: Create OpenAppAction**
- [x] **Step 2: Create SetAlarmAction**
- [x] **Step 3: Create ToggleWifiAction**
- [x] **Step 4: Create ToggleBluetoothAction**
- [x] **Step 5: Commit**

---

## Task 3: Update DollOSService AIDL and Implementation

**Status: COMPLETE**

**Files:**
- Modify: `packages/apps/DollOSService/aidl/org/dollos/service/IDollOSService.aidl`
- Modify: `packages/apps/DollOSService/src/org/dollos/service/DollOSServiceImpl.kt`
- Modify: `packages/apps/DollOSService/src/org/dollos/service/DollOSApp.kt`
- Modify: `packages/apps/DollOSService/AndroidManifest.xml`

- [x] **Step 1: Update IDollOSService.aidl** — added executeSystemAction, getAvailableActions, showTaskManager
- [x] **Step 2: Update DollOSApp.kt** — ActionRegistry init + instance
- [x] **Step 3: Update DollOSServiceImpl.kt** — AIDL method implementations
- [x] **Step 4: Add permissions to AndroidManifest.xml**
- [x] **Step 5: Commit**

---

## Task 4: AI Task Manager Data Model

**Status: COMPLETE**

- [x] **Step 1: Create AITask data class**
- [x] **Step 2: Commit**

---

## Task 5: Task Manager Activity UI

**Status: COMPLETE**

- [x] **Step 1: Create activity_task_manager.xml**
- [x] **Step 2: Create item_ai_task.xml**
- [x] **Step 3: Create TaskManagerActivity.kt**
- [x] **Step 4: Add TaskManagerActivity to AndroidManifest.xml**
- [x] **Step 5: Create button_primary_background.xml drawable**
- [x] **Step 6: Add RecyclerView dependency to Android.bp**
- [x] **Step 7: Commit**

---

## Task 6: Power Button Double-Click Configuration

**Status: COMPLETE**

- [x] **Step 1: Create resource overlay** — config_doublePressOnPowerBehavior = 3
- [x] **Step 2: Add overlay to product makefile**
- [x] **Step 3: Commit**

---

## Task 7: Update DollOSService Permissions

**Status: COMPLETE**

- [x] **Step 1: Update privapp-permissions** — BT, WiFi, alarm, overlay
- [x] **Step 2: Commit**

---

## Task 8: DollOSAIService Tool Calling Integration

**Status: COMPLETE**

DollOSAIService already has full tool calling support integrated:

- [x] **LLMClient interface** — accepts `tools: List<ToolDefinition>` parameter
- [x] **ToolCall / ToolDefinition** — data classes in LLMResponse.kt
- [x] **ClaudeProvider** — Anthropic native tool_use format (streaming + blocking)
- [x] **OpenAIProvider** — OpenAI function calling format (streaming + blocking)
- [x] **GrokProvider / CustomProvider** — inherit OpenAIProvider tool calling
- [x] **sendMessage()** — loads tool definitions from DollOSService, passes to LLM
- [x] **loadToolDefinitions()** — maps DollOSService.getAvailableActions() to ToolDefinition list
- [x] **handleToolCalls()** — routes based on confirmRequired, 60s timeout
- [x] **executeToolCall()** — calls DollOSService.executeSystemAction() via Binder

---

## Task 9: Build, Deploy, and Verify

**Status: PENDING**

- [ ] **Step 1: Build DollOSService**

```bash
cd ~/Projects/DollOS-build
source build/envsetup.sh
lunch dollos_bluejay-bp2a-userdebug
m DollOSService -j$(nproc)
```

- [ ] **Step 2: Build DollOSAIService**

```bash
cd ~/Projects/DollOSAIService
./gradlew assembleRelease
cp app/build/outputs/apk/release/app-release-unsigned.apk prebuilt/DollOSAIService.apk
rsync -av --delete . ~/Projects/DollOS-build/external/DollOSAIService/
cd ~/Projects/DollOS-build
m DollOSAIService -j$(nproc)
```

- [ ] **Step 3: Deploy to device**

Push updated modules to device via adb, reboot.

- [ ] **Step 4: Verify ActionRegistry**

```bash
adb logcat -d | grep "ActionRegistry\|DollOSApp"
```

Expected: 4 actions registered on startup.

- [ ] **Step 5: Verify power button double-click**

Double-click power button → TaskManagerActivity should appear with "No active AI tasks".

- [ ] **Step 6: Verify tool calling**

Send message via test app: "open the Settings app"
Expected: LLM returns tool_call for open_app → DollOSAIService calls DollOSService.executeSystemAction() → Settings app opens.

- [ ] **Step 7: Verify confirmation flow**

Send message: "set an alarm for 7:30 AM"
Expected: onActionConfirmRequired callback fires → confirmation displayed → user approves → alarm set.

---

## Notes

### Integration Status (2026-03-23)

Plan A (LLM client, personality, usage, settings) and Plan B (memory system, conversation engine) are both complete and verified on device. Plan C completes the agent system integration:

- DollOSAIService ↔ DollOSService Binder IPC for action execution
- Provider-native tool calling (Claude tool_use, OpenAI/Grok function calling)
- Emergency stop via power button double-click → TaskManagerActivity

### TaskManagerActivity TODOs

The current TaskManagerActivity has TODO comments for live integration:
- `loadTasks()`: currently reads from intent extra, should call `pauseAll()` + `getActiveTasks()` via AIService binder
- `cancelTask()`: should call `IDollOSAIService.cancelTask(taskId)`
- `resumeAndFinish()`: should call `IDollOSAIService.resumeAll()`

These will be connected once the full task tracking system is exercised in real usage.
