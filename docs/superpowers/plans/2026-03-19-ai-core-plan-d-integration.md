# AI Core Plan D: Integration

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire together all components from Plan A (DollOSAIService, LLM, Personality), Plan B (Memory, Conversation, ContextCompressor), and Plan C (Agent Actions, Emergency Stop) into a working integrated system. This plan runs AFTER Plans A, B, and C are all complete.

**Architecture:** DollOSService binds to DollOSAIService on startup. ContextCompressor uses the foreground LLM client for compression. ConversationManager and MemoryManager are wired into sendMessage. All product makefile changes are consolidated. Data migration handles the transition from DollOSService-held API key/personality to DollOSAIService.

**Tech Stack:** Kotlin, AIDL (Binder IPC), Soong (DollOSService) + Gradle (DollOSAIService), SharedPreferences

---

## File Structure

### DollOSService (modify existing)

```
packages/apps/DollOSService/
  Android.bp                         -- add dollos-ai-aidl to static_libs
  src/org/dollos/service/
    DollOSService.kt                 -- add ServiceConnection to DollOSAIService
    DollOSServiceImpl.kt             -- wire showTaskManager to pauseAll/getActiveTasks
```

### DollOSAIService Gradle project (modify existing)

```
DollOSAIService/app/src/main/java/org/dollos/ai/
    DollOSAIServiceImpl.kt           -- wire compressor, conversation, memory into sendMessage
    DollOSAIApp.kt                   -- instantiate MemoryManager, ConversationManager
    migration/
      DataMigration.kt               -- migrate API key/personality from DollOSService
```

Note: DollOSAIService is a Gradle project, not AOSP Soong. After changes, rebuild with `./gradlew assembleRelease` and copy APK to `external/DollOSAIService/prebuilt/` in AOSP tree.

### Task Manager (modify existing)

```
packages/apps/DollOSService/
  src/org/dollos/service/taskmanager/
    TaskManagerActivity.kt           -- add "refreshing..." indicator on timeout
```

### Vendor Config (modify existing)

```
vendor/dollos/
  dollos_bluejay.mk                  -- consolidated product config for all plans
```

---

## Task 1: DollOSService Binds to DollOSAIService

**Goal:** DollOSService connects to DollOSAIService on startup via Binder, enabling showTaskManager to call pauseAll/getActiveTasks and TaskManagerActivity to call cancel/resume.

**Files:**
- Modify: `packages/apps/DollOSService/Android.bp`
- Modify: `packages/apps/DollOSService/src/org/dollos/service/DollOSService.kt`
- Modify: `packages/apps/DollOSService/src/org/dollos/service/DollOSServiceImpl.kt`

- [ ] **Step 1: Add dollos-ai-aidl to DollOSService's Android.bp**

In `packages/apps/DollOSService/Android.bp`, add `"dollos-ai-aidl"` to the `static_libs` array. This gives DollOSService access to `IDollOSAIService` and `IDollOSAICallback` interfaces.

```diff
 static_libs: [
     "androidx.recyclerview_recyclerview",
+    "dollos-ai-aidl",
 ],
```

- [ ] **Step 2: Add ServiceConnection in DollOSService.kt**

Add a `ServiceConnection` that binds to DollOSAIService on `onCreate()`:

```kotlin
// Add to DollOSService.kt

private var aiService: IDollOSAIService? = null

private val aiServiceConnection = object : ServiceConnection {
    override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
        aiService = IDollOSAIService.Stub.asInterface(binder)
        Log.i(TAG, "Connected to DollOSAIService")
    }

    override fun onServiceDisconnected(name: ComponentName?) {
        aiService = null
        Log.w(TAG, "Disconnected from DollOSAIService")
    }
}

// In onCreate(), after existing init:
val aiIntent = Intent().apply {
    component = ComponentName("org.dollos.ai", "org.dollos.ai.DollOSAIService")
}
bindService(aiIntent, aiServiceConnection, Context.BIND_AUTO_CREATE)
```

- [ ] **Step 3: Wire showTaskManager() to pauseAll + getActiveTasks**

Update `DollOSServiceImpl.kt` `showTaskManager()`:

```kotlin
override fun showTaskManager() {
    val ai = aiService
    if (ai == null) {
        Log.w(TAG, "showTaskManager: DollOSAIService not connected, launching with empty task list")
        launchTaskManager("[]")
        return
    }

    executor.submit {
        val paused = ai.pauseAll() // blocking, 3s timeout
        val tasksJson = if (paused) {
            ai.activeTasks ?: "[]"
        } else {
            // pauseAll timed out -- launch with empty, refresh in background
            "[]"
        }
        launchTaskManager(tasksJson, refreshing = !paused)
    }
}

private fun launchTaskManager(tasksJson: String, refreshing: Boolean = false) {
    val intent = Intent(DollOSApp.instance, TaskManagerActivity::class.java).apply {
        addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        putExtra("tasks_json", tasksJson)
        putExtra("refreshing", refreshing)
    }
    DollOSApp.instance.startActivity(intent)
}
```

- [ ] **Step 4: Wire TaskManagerActivity cancel/resume to DollOSAIService**

Update `TaskManagerActivity.kt` to get a reference to DollOSAIService through DollOSService, and wire the cancel/resume buttons:

```kotlin
// In TaskManagerActivity.kt, replace TODO comments:

private fun cancelTask(task: AITask) {
    tasks.remove(task)
    adapter.notifyDataSetChanged()
    // Call DollOSAIService.cancelTask via DollOSService's aiService reference
    Thread {
        try {
            val aiService = (application as DollOSApp).getAIService()
            aiService?.cancelTask(task.id)
        } catch (e: Exception) {
            Log.e(TAG, "Failed to cancel task: ${task.id}", e)
        }
    }.start()
    if (tasks.isEmpty()) {
        emptyText.visibility = View.VISIBLE
        taskList.visibility = View.GONE
        statusText.text = "All tasks cancelled"
    }
}

private fun resumeAndFinish() {
    Thread {
        try {
            val aiService = (application as DollOSApp).getAIService()
            aiService?.resumeAll()
        } catch (e: Exception) {
            Log.e(TAG, "Failed to resume all tasks", e)
        }
    }.start()
    finish()
}
```

Add `getAIService()` to `DollOSApp.kt`:

```kotlin
// In DollOSApp.kt
fun getAIService(): IDollOSAIService? {
    return (getSystemService(Context.BIND_AUTO_CREATE) as? DollOSService)?.aiService
}
```

Note: The actual mechanism depends on how DollOSService exposes the aiService reference. Since TaskManagerActivity runs in the same process as DollOSService, it can access the field directly through DollOSApp's singleton or a static reference on DollOSService.

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSService
git add -A
git commit -m "feat: bind DollOSService to DollOSAIService, wire task manager"
```

---

## Task 2: Wire ContextCompressor to LLM Client

**Goal:** Set `compressWithLLM` callback on ContextCompressor so it uses the foreground LLMClient for context compression. Record token usage from compression calls.

**Files:**
- Modify: `packages/apps/DollOSAIService/src/org/dollos/ai/DollOSAIServiceImpl.kt`

- [ ] **Step 1: Set compressWithLLM callback after foreground client init**

In `DollOSAIServiceImpl.kt`, after `initForegroundClient()` completes (or whenever foregroundClient is set), set the compressor callback:

```kotlin
// In initForegroundClient(), after foregroundClient is assigned:
contextCompressor.compressWithLLM = { messages ->
    val client = foregroundClient
        ?: throw IllegalStateException("Foreground client not available for compression")

    val prompt = "Summarize the following conversation concisely, preserving all important facts, decisions, and context:\n\n" +
        messages.joinToString("\n") { "${it.role}: ${it.content}" }

    val response = client.sendSync(prompt)

    usageTracker.record(
        inputTokens = response.inputTokens,
        outputTokens = response.outputTokens,
        model = response.model,
        provider = client.providerType.name,
        function = "compression",
        isForeground = true
    )

    CompressionResult(
        summary = response.content,
        inputTokens = response.inputTokens,
        outputTokens = response.outputTokens
    )
}
Log.i(TAG, "ContextCompressor wired to foreground LLM client")
```

- [ ] **Step 2: Commit**

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService
git add src/org/dollos/ai/DollOSAIServiceImpl.kt
git commit -m "feat: wire ContextCompressor to foreground LLM client"
```

---

## Task 3: Wire ConversationManager + MemoryManager into sendMessage

**Goal:** Replace sendMessage's simple message list with ConversationManager and MemoryManager integration. System prompt includes memory context.

**Files:**
- Modify: `packages/apps/DollOSAIService/src/org/dollos/ai/DollOSAIApp.kt`
- Modify: `packages/apps/DollOSAIService/src/org/dollos/ai/DollOSAIServiceImpl.kt`

- [ ] **Step 1: Instantiate MemoryManager and ConversationManager in DollOSAIApp**

```kotlin
// In DollOSAIApp.kt, add to companion object:
lateinit var memoryManager: MemoryManager
    private set
lateinit var conversationManager: ConversationManager
    private set

// In onCreate():
val memoryDir = File(filesDir, "memory")
memoryDir.mkdirs()
memoryManager = MemoryManager(memoryDir)
conversationManager = ConversationManager()
Log.i(TAG, "MemoryManager and ConversationManager initialized")
```

- [ ] **Step 2: Update sendMessage to use ConversationManager**

In `DollOSAIServiceImpl.kt`, replace the current sendMessage implementation's message building:

```kotlin
override fun sendMessage(message: String?) {
    if (message == null) return

    val client = foregroundClient
    if (client == null) {
        broadcastError("NO_CLIENT", "No foreground model configured")
        return
    }

    if (!budgetManager.canMakeCall()) {
        broadcastError("BUDGET_EXCEEDED", "Token budget exceeded")
        return
    }

    val taskId = UUID.randomUUID().toString()
    activeTasks[taskId] = ActiveTask(
        id = taskId,
        name = "Conversation",
        description = "Processing: ${message.take(50)}",
        startTime = System.currentTimeMillis(),
        status = "RUNNING"
    )

    // Add user message to conversation history
    DollOSAIApp.conversationManager.addUserMessage(message)

    // Build context using ConversationManager and MemoryManager
    val conversationContext = DollOSAIApp.conversationManager.buildConversationContext()
    val memoryContext = DollOSAIApp.memoryManager.buildMemoryContext()
    val systemPrompt = buildSystemPrompt(memoryContext)

    val messages = mutableListOf<Message>()
    messages.add(Message(role = "system", content = systemPrompt))
    messages.addAll(conversationContext)

    val tools = loadToolDefinitions()

    client.sendStreaming(messages, object : StreamingCallback {
        override fun onToken(token: String) {
            if (paused.get()) return
            broadcastToken(token)
        }

        override fun onComplete(response: LLMResponse) {
            activeTasks.remove(taskId)

            usageTracker.record(
                inputTokens = response.inputTokens,
                outputTokens = response.outputTokens,
                model = response.model,
                provider = client.providerType.name,
                function = "conversation",
                isForeground = true
            )
            budgetManager.checkBudgetAfterCall()

            // Record assistant response in conversation history
            DollOSAIApp.conversationManager.addAssistantMessage(response.content)

            if (response.toolCalls.isNotEmpty()) {
                handleToolCalls(response.toolCalls)
            }

            broadcastComplete(response.content)
        }

        override fun onError(errorCode: String, message: String) {
            activeTasks.remove(taskId)
            sendErrorNotification(errorCode, message)
            broadcastError(errorCode, message)
        }
    }, tools)
}
```

- [ ] **Step 3: Update buildSystemPrompt to include memory context**

```kotlin
private fun buildSystemPrompt(memoryContext: String): String {
    val directive = DollOSAIApp.prefs.getString(KEY_RESPONSE_DIRECTIVE, "") ?: ""
    val backstory = DollOSAIApp.prefs.getString(KEY_BACKSTORY, "") ?: ""
    val address = DollOSAIApp.prefs.getString(KEY_ADDRESS, "") ?: ""
    val language = DollOSAIApp.prefs.getString(KEY_LANGUAGE, "") ?: ""

    val sb = StringBuilder()
    if (directive.isNotBlank()) sb.appendLine(directive)
    if (language.isNotBlank()) sb.appendLine("Respond in: $language")
    if (address.isNotBlank()) sb.appendLine("Address the user as: $address")
    if (backstory.isNotBlank()) sb.appendLine(backstory)
    if (memoryContext.isNotBlank()) {
        sb.appendLine()
        sb.appendLine("## Memories")
        sb.appendLine(memoryContext)
    }
    return sb.toString()
}
```

- [ ] **Step 4: Commit**

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService
git add -A
git commit -m "feat: wire ConversationManager and MemoryManager into sendMessage"
```

---

## Task 4: dollos_bluejay.mk Coordination

**Goal:** Consolidate all Plan A, B, C product config changes into a single canonical version of `dollos_bluejay.mk`. This task is the single source of truth for this file -- Plans A, B, C must NOT modify it in parallel.

**Files:**
- Modify: `vendor/dollos/dollos_bluejay.mk`

- [ ] **Step 1: Write consolidated dollos_bluejay.mk**

The final `vendor/dollos/dollos_bluejay.mk` must contain all of:

```makefile
# DollOS product configuration for bluejay (Pixel 6a)

# --- Base (from DollOS Base plan) ---
PRODUCT_PACKAGES += \
    DollOSService \
    DollOSSetupWizard

# DollOSService privapp permissions
PRODUCT_COPY_FILES += \
    packages/apps/DollOSService/privapp-permissions-dollos-service.xml:$(TARGET_COPY_OUT_PRODUCT)/etc/permissions/privapp-permissions-dollos-service.xml

# DollOSSetupWizard privapp permissions
PRODUCT_COPY_FILES += \
    packages/apps/DollOSSetupWizard/privapp-permissions-dollos-setupwizard.xml:$(TARGET_COPY_OUT_PRODUCT)/etc/permissions/privapp-permissions-dollos-setupwizard.xml

# --- Plan A: DollOSAIService ---
PRODUCT_PACKAGES += \
    DollOSAIService

PRODUCT_COPY_FILES += \
    packages/apps/DollOSAIService/privapp-permissions-dollos-ai.xml:$(TARGET_COPY_OUT_PRODUCT)/etc/permissions/privapp-permissions-dollos-ai.xml

# --- Plan C: Framework overlay (power button double-click -> Task Manager) ---
PRODUCT_PACKAGE_OVERLAYS += vendor/dollos/overlay
```

- [ ] **Step 2: Commit**

```bash
cd ~/Desktop/DollOS-build/vendor/dollos
git add dollos_bluejay.mk
git commit -m "feat: consolidate dollos_bluejay.mk for Plans A, B, C integration"
```

**IMPORTANT:** This file must not be modified by Plans A, B, or C individually. All product config changes go through this task.

---

## Task 5: Data Migration from DollOSService

**Goal:** On DollOSAIService first startup, migrate API key and personality data that was previously stored in DollOSService to DollOSAIService's own SharedPreferences.

**Files:**
- Create: `packages/apps/DollOSAIService/src/org/dollos/ai/migration/DataMigration.kt`
- Modify: `packages/apps/DollOSAIService/src/org/dollos/ai/DollOSAIApp.kt`

- [ ] **Step 1: Create DataMigration.kt**

```kotlin
package org.dollos.ai.migration

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import android.util.Log
import org.dollos.service.IDollOSService

class DataMigration(private val context: Context) {

    companion object {
        private const val TAG = "DataMigration"
        private const val KEY_MIGRATION_COMPLETE = "migration_from_dollos_service_complete"
    }

    fun migrateIfNeeded() {
        val prefs = context.createDeviceProtectedStorageContext()
            .getSharedPreferences("dollos_ai_config", Context.MODE_PRIVATE)

        if (prefs.getBoolean(KEY_MIGRATION_COMPLETE, false)) {
            Log.i(TAG, "Migration already complete, skipping")
            return
        }

        Log.i(TAG, "Starting data migration from DollOSService")

        val intent = Intent().apply {
            component = ComponentName("org.dollos.service", "org.dollos.service.DollOSService")
        }

        context.bindService(intent, object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
                try {
                    val dollOSService = IDollOSService.Stub.asInterface(binder)

                    // Read old data from DollOSService
                    val oldApiKey = dollOSService.apiKey
                    val oldPersonality = dollOSService.personalityName

                    if (!oldApiKey.isNullOrBlank()) {
                        prefs.edit().putString("fg_api_key", oldApiKey).apply()
                        Log.i(TAG, "Migrated API key from DollOSService")
                    }

                    if (!oldPersonality.isNullOrBlank()) {
                        prefs.edit().putString("backstory", oldPersonality).apply()
                        Log.i(TAG, "Migrated personality from DollOSService")
                    }

                    prefs.edit().putBoolean(KEY_MIGRATION_COMPLETE, true).apply()
                    Log.i(TAG, "Migration complete")
                } catch (e: Exception) {
                    Log.e(TAG, "Migration failed", e)
                } finally {
                    context.unbindService(this)
                }
            }

            override fun onServiceDisconnected(name: ComponentName?) {
                // no-op
            }
        }, Context.BIND_AUTO_CREATE)
    }
}
```

- [ ] **Step 2: Call migration on DollOSAIApp startup**

In `DollOSAIApp.kt` `onCreate()`, add before other init:

```kotlin
// Migrate data from DollOSService (one-time, on first boot after upgrade)
DataMigration(this).migrateIfNeeded()
```

- [ ] **Step 3: Commit**

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService
git add -A
git commit -m "feat: add data migration from DollOSService on first startup"
```

---

## Task 6: Task Manager "refreshing..." Indicator

**Goal:** When `pauseAll()` times out (3s), show TaskManagerActivity immediately with a "refreshing..." indicator. Retry `getActiveTasks()` in background and update UI when data arrives.

**Files:**
- Modify: `packages/apps/DollOSService/src/org/dollos/service/taskmanager/TaskManagerActivity.kt`

- [ ] **Step 1: Add refreshing state to TaskManagerActivity**

Update `TaskManagerActivity.kt` to handle the `refreshing` extra:

```kotlin
// In onCreate(), after loadTasks():
val refreshing = intent.getBooleanExtra("refreshing", false)
if (refreshing) {
    statusText.text = "Refreshing task list..."
    retryLoadTasks()
}

private fun retryLoadTasks() {
    Thread {
        try {
            val aiService = (application as DollOSApp).getAIService()
            if (aiService == null) {
                Log.w(TAG, "retryLoadTasks: DollOSAIService not available")
                return@Thread
            }

            // Retry getActiveTasks -- AIService should have finished pausing by now
            val tasksJson = aiService.activeTasks ?: "[]"
            val newTasks = AITask.listFromJson(tasksJson)

            runOnUiThread {
                tasks.clear()
                tasks.addAll(newTasks)
                adapter.notifyDataSetChanged()

                if (tasks.isEmpty()) {
                    emptyText.visibility = View.VISIBLE
                    taskList.visibility = View.GONE
                    statusText.text = "No active AI tasks"
                } else {
                    emptyText.visibility = View.GONE
                    taskList.visibility = View.VISIBLE
                    statusText.text = "${tasks.size} task(s) paused"
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "retryLoadTasks failed", e)
            runOnUiThread {
                statusText.text = "Failed to load task list"
            }
        }
    }.start()
}
```

- [ ] **Step 2: Commit**

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSService
git add src/org/dollos/service/taskmanager/TaskManagerActivity.kt
git commit -m "feat: add refreshing indicator to Task Manager on pauseAll timeout"
```

---

## Task 7: Build + Flash + Full Integration Test

**Goal:** Build the full DollOS image with all Plans A-D integrated, flash to device, and run end-to-end verification.

**Files:** (no new code changes -- verification only)

- [ ] **Step 1: Build**

```bash
cd ~/Desktop/DollOS-build
source build/envsetup.sh
lunch dollos_bluejay-cur-userdebug
m -j$(nproc)
```

- [ ] **Step 2: Flash**

```bash
adb reboot bootloader
# wait for bootloader
cd out/target/product/bluejay
fastboot flashall -w
```

- [ ] **Step 3: Verify streaming conversation**

After boot + OOBE (configure API key + personality):

```bash
# Check DollOSAIService is running
adb shell dumpsys activity services org.dollos.ai

# Send a test message via adb (or use OOBE conversation UI)
# Verify streaming tokens arrive in logcat
adb shell logcat -s DollOSAIServiceImpl:I
```

- [ ] **Step 4: Verify memory writes**

After a conversation:

```bash
# Check memory directory
adb shell run-as org.dollos.ai ls -la files/memory/

# Check MEMORY.md exists
adb shell run-as org.dollos.ai cat files/memory/MEMORY.md
```

- [ ] **Step 5: Verify emergency stop**

Double-click power button:

```bash
# Task Manager should appear
# Check logcat for pause signal
adb shell logcat -s DollOSServiceImpl:I TaskManagerActivity:I

# Verify task list shows current tasks (if any running)
# Tap "Resume All" to dismiss
```

- [ ] **Step 6: Verify agent action with confirmation**

Ask the AI to set an alarm:

```bash
# Should see confirmation prompt in conversation
# Confirm -> alarm is set
# Check logcat
adb shell logcat -s SetAlarmAction:I DollOSAIServiceImpl:I
```

- [ ] **Step 7: Verify agent action without confirmation**

Ask the AI to open an app:

```bash
# Should execute immediately (no confirmation)
adb shell logcat -s OpenAppAction:I
```

- [ ] **Step 8: Verify data migration**

If DollOSService had old API key data:

```bash
adb shell logcat -s DataMigration:I
# Should see "Migration complete" or "Migration already complete"
```

- [ ] **Step 9: Verify Task Manager cancel**

Start a long conversation, double-click power:

```bash
# Cancel a task in Task Manager
# Verify task is removed from DollOSAIService
adb shell logcat -s DollOSAIServiceImpl:I
```

- [ ] **Step 10: Commit verification**

```bash
cd ~/Desktop/DollOS
# Record test results if needed
```

---

## Notes

### Dependency Order

Plan D must run AFTER Plans A, B, C are all complete. The dependency graph:

```
Plan A (AI Service) ---\
Plan B (Memory)     ----+--> Plan D (Integration)
Plan C (Agent/Stop) ---/
```

### dollos_bluejay.mk Ownership

Task 4 of this plan is the single source of truth for `dollos_bluejay.mk`. During implementation of Plans A, B, C, do NOT commit changes to this file -- defer them all to Plan D Task 4.

### What Plan D does NOT do

- No new features -- only wiring existing components together
- No new AIDL methods -- all interfaces are defined in Plans A and C
- No new UI beyond the "refreshing..." indicator
- No architecture changes
