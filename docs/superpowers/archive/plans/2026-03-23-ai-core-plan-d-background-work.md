# AI Core Plan D v1: Background Work System

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the foreground AI from a passive chat responder into an event-driven agent with background worker support, unified event queue, scheduling, and system event triggers.

**Architecture:** All inputs (user messages, worker results, system events, schedules, AI internal events) flow through a unified EventQueue. The foreground AI processes events either piggybacked on sendMessage() or autonomously during idle periods. Background workers are independent coroutines using the background LLM model, with per-task action whitelists and skill templates.

**Tech Stack:** Kotlin, Coroutines (Channel), Room (schedule persistence, worker result persistence), AlarmManager (scheduling), BroadcastReceiver (system events), AIDL (Binder IPC)

---

## File Structure

### DollOSAIService (new files)

```
app/src/main/java/org/dollos/ai/
  event/
    Event.kt                         -- Event data class + EventType enum
    EventQueue.kt                    -- Thread-safe queue with Channel notify
    EventProcessor.kt                -- Drains queue, builds context injection, triggers idle processing
  worker/
    BackgroundWorker.kt              -- Single worker: coroutine + background LLM + action whitelist
    WorkerManager.kt                 -- Spawns/tracks/cancels workers, collects results
    WorkerResultEntity.kt            -- Room @Entity for persisted worker results (survives restart)
    WorkerResultDao.kt               -- Room DAO for worker results
    Skill.kt                         -- Skill data class + SkillRegistry (predefined whitelist templates)
  schedule/
    ScheduleEntry.kt                 -- Room @Entity for persisted schedules
    ScheduleDao.kt                   -- Room DAO
    ScheduleManager.kt               -- AlarmManager registration + boot replay
    ScheduleReceiver.kt              -- BroadcastReceiver for alarm triggers
  trigger/
    SystemEventReceiver.kt           -- Dynamically registered receiver for screen/charging/WiFi (screen/WiFi cannot use manifest on Android 8+)
    IdleDetector.kt                  -- Tracks user activity, triggers idle processing
```

### DollOSAIService (modify existing)

```
  DollOSAIServiceImpl.kt             -- integrate EventQueue, piggyback on sendMessage, idle timer
  DollOSAIApp.kt                     -- init EventQueue, WorkerManager, ScheduleManager
  conversation/ConversationDatabase.kt -- add ScheduleEntry + ScheduleDao
  AndroidManifest.xml                -- add ScheduleReceiver + SystemEventReceiver
```

### AIDL (modify existing)

```
  aidl/org/dollos/ai/
    IDollOSAIService.aidl            -- add spawnWorker, getWorkers, cancelWorker, addSchedule, removeSchedule, getSchedules
    IDollOSAICallback.aidl           -- add onWorkerComplete, onEventProcessed
```

---

## Task 1: Event Data Class + EventQueue

**Goal:** Define the Event model and a thread-safe queue with wake-up notification.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/event/Event.kt`
- Create: `app/src/main/java/org/dollos/ai/event/EventQueue.kt`

- [ ] **Step 1: Create Event.kt**

```kotlin
package org.dollos.ai.event

import java.util.UUID

enum class EventType {
    TEXT_MESSAGE,
    VOICE_MESSAGE,
    WORKER_RESULT,
    SCHEDULE,
    SYSTEM_EVENT,
    INTERNAL
}

enum class EventPriority {
    HIGH,    // process immediately (wake idle processor)
    NORMAL,  // process on next cycle
    LOW      // process when convenient
}

data class Event(
    val id: String = UUID.randomUUID().toString(),
    val type: EventType,
    val priority: EventPriority = EventPriority.NORMAL,
    val payload: String,          // JSON
    val timestamp: Long = System.currentTimeMillis(),
    val source: String            // "user" / "worker:task_id" / "system:wifi" / "schedule:morning_routine"
)
```

- [ ] **Step 2: Create EventQueue.kt**

```kotlin
package org.dollos.ai.event

import android.util.Log
import kotlinx.coroutines.channels.Channel
import java.util.concurrent.ConcurrentLinkedQueue

class EventQueue {

    companion object {
        private const val TAG = "EventQueue"
    }

    private val queue = ConcurrentLinkedQueue<Event>()
    val notify = Channel<Unit>(Channel.CONFLATED)

    fun push(event: Event) {
        queue.add(event)
        notify.trySend(Unit)
        Log.d(TAG, "Event pushed: ${event.type} from ${event.source} (priority=${event.priority})")
    }

    fun drainAll(): List<Event> {
        val events = mutableListOf<Event>()
        while (true) {
            val event = queue.poll() ?: break
            events.add(event)
        }
        return events.sortedWith(
            compareBy<Event> { it.priority.ordinal }
                .thenBy { it.timestamp }
        )
    }

    /**
     * Drain only non-user-message events. User messages stay in queue.
     * Used by sendMessage() piggyback — we don't want to accidentally discard
     * TEXT_MESSAGE/VOICE_MESSAGE events that haven't been processed yet.
     */
    fun drainNonUserEvents(): List<Event> {
        val drained = mutableListOf<Event>()
        val kept = mutableListOf<Event>()
        while (true) {
            val event = queue.poll() ?: break
            if (event.type == EventType.TEXT_MESSAGE || event.type == EventType.VOICE_MESSAGE) {
                kept.add(event)
            } else {
                drained.add(event)
            }
        }
        // Put user messages back
        kept.forEach { queue.add(it) }
        return drained.sortedWith(
            compareBy<Event> { it.priority.ordinal }
                .thenBy { it.timestamp }
        )
    }

    fun peek(): Event? = queue.peek()

    fun isEmpty(): Boolean = queue.isEmpty()

    fun size(): Int = queue.size
}
```

- [ ] **Step 3: Commit**

```bash
cd ~/Projects/DollOSAIService
git add app/src/main/java/org/dollos/ai/event/
git commit -m "feat: add Event data class and EventQueue"
```

---

## Task 2: Skill and SkillRegistry

**Goal:** Define skill templates (predefined action whitelists for background workers).

**Files:**
- Create: `app/src/main/java/org/dollos/ai/worker/Skill.kt`

- [ ] **Step 1: Create Skill.kt**

```kotlin
package org.dollos.ai.worker

import android.util.Log

data class Skill(
    val id: String,
    val name: String,
    val description: String,
    val allowedActions: Set<String>,   // action IDs this skill can use
    val systemPrompt: String           // additional instructions for the worker
)

class SkillRegistry {

    companion object {
        private const val TAG = "SkillRegistry"
    }

    private val skills = mutableMapOf<String, Skill>()

    fun register(skill: Skill) {
        skills[skill.id] = skill
        Log.i(TAG, "Registered skill: ${skill.id} (${skill.allowedActions.size} actions)")
    }

    fun get(id: String): Skill? = skills[id]

    fun getAll(): List<Skill> = skills.values.toList()

    init {
        // Built-in skills
        register(Skill(
            id = "general",
            name = "General",
            description = "General purpose background task with no action access",
            allowedActions = emptySet(),
            systemPrompt = "You are a background worker. Complete the assigned task and return the result as concise text."
        ))
        register(Skill(
            id = "device_control",
            name = "Device Control",
            description = "Can toggle WiFi, Bluetooth, and set alarms",
            allowedActions = setOf("toggle_wifi", "toggle_bluetooth", "set_alarm"),
            systemPrompt = "You are a background worker with device control capabilities. Execute the requested device operations."
        ))
        register(Skill(
            id = "app_launcher",
            name = "App Launcher",
            description = "Can open applications",
            allowedActions = setOf("open_app"),
            systemPrompt = "You are a background worker that can open applications on behalf of the user."
        ))
        register(Skill(
            id = "full_system",
            name = "Full System",
            description = "Can use all non-UI system actions",
            allowedActions = setOf("open_app", "set_alarm", "toggle_wifi", "toggle_bluetooth"),
            systemPrompt = "You are a background worker with full system action access. Execute the requested task using available tools."
        ))
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/org/dollos/ai/worker/
git commit -m "feat: add Skill and SkillRegistry for worker action whitelists"
```

---

## Task 3: BackgroundWorker

**Goal:** A single background worker that runs an LLM conversation with the background model, can call whitelisted actions, and reports results.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/worker/BackgroundWorker.kt`

- [ ] **Step 1: Create BackgroundWorker.kt**

```kotlin
package org.dollos.ai.worker

import android.util.Log
import kotlinx.coroutines.*
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.dollos.ai.event.Event
import org.dollos.ai.event.EventPriority
import org.dollos.ai.event.EventQueue
import org.dollos.ai.event.EventType
import org.dollos.ai.llm.*
import org.dollos.service.IDollOSService
import java.util.UUID

data class WorkerConfig(
    val id: String = UUID.randomUUID().toString(),
    val task: String,               // what the worker should do
    val skill: Skill,               // skill template (defines allowed actions)
    val maxTurns: Int = 10,         // max LLM turns before forced completion
    val timeoutMs: Long = 120_000   // 2 minute timeout
)

data class WorkerResult(
    val workerId: String,
    val success: Boolean,
    val output: String,
    val tokensUsed: Long,
    val actionsExecuted: List<String>
)

class BackgroundWorker(
    private val config: WorkerConfig,
    private val client: LLMClient,
    private val dollOSService: IDollOSService?,
    private val eventQueue: EventQueue
) {

    companion object {
        private const val TAG = "BackgroundWorker"
    }

    var status: String = "RUNNING"
        private set
    private var job: Job? = null
    private var tokensUsed: Long = 0
    private val actionsExecuted = mutableListOf<String>()

    fun start(scope: CoroutineScope) {
        job = scope.launch {
            try {
                withTimeout(config.timeoutMs) {
                    run()
                }
            } catch (e: TimeoutCancellationException) {
                Log.w(TAG, "Worker ${config.id} timed out")
                postResult(false, "Worker timed out after ${config.timeoutMs}ms")
            } catch (e: CancellationException) {
                Log.i(TAG, "Worker ${config.id} cancelled")
                status = "CANCELLED"
            } catch (e: Exception) {
                Log.e(TAG, "Worker ${config.id} failed", e)
                postResult(false, "Error: ${e.message}")
            }
        }
    }

    fun cancel() {
        job?.cancel()
        status = "CANCELLED"
        Log.i(TAG, "Worker ${config.id} cancelled")
    }

    // v1: no pause/resume for workers, only cancel.
    // Pausing a coroutine mid-LLM-call requires Mutex-based suspension
    // which adds complexity. Workers are cheap to re-spawn.

    private suspend fun run() {
        Log.i(TAG, "Worker ${config.id} started: ${config.task}")

        val systemPrompt = "${config.skill.systemPrompt}\n\nYour task: ${config.task}"
        val messages = mutableListOf(LLMMessage("user", config.task))

        // Build tool definitions filtered by skill whitelist
        val tools = loadFilteredTools()

        var turns = 0
        while (turns < config.maxTurns && status == "RUNNING") {
            turns++

            val response = client.send(messages, systemPrompt, 0.3f, tools)
            tokensUsed += response.inputTokens + response.outputTokens

            if (response.toolCalls.isNotEmpty()) {
                // Execute tool calls
                for (toolCall in response.toolCalls) {
                    if (toolCall.name !in config.skill.allowedActions) {
                        Log.w(TAG, "Worker ${config.id} tried disallowed action: ${toolCall.name}")
                        messages.add(LLMMessage("assistant", response.content))
                        messages.add(LLMMessage("user", "Error: action '${toolCall.name}' is not allowed by your skill. Allowed: ${config.skill.allowedActions}"))
                        continue
                    }

                    val result = executeAction(toolCall)
                    actionsExecuted.add(toolCall.name)
                    messages.add(LLMMessage("assistant", response.content))
                    messages.add(LLMMessage("user", "Tool result for ${toolCall.name}: $result"))
                }
            } else {
                // No tool calls — worker is done
                postResult(true, response.content)
                return
            }
        }

        // Max turns reached
        postResult(true, "Completed after $turns turns")
    }

    private fun loadFilteredTools(): List<ToolDefinition> {
        if (config.skill.allowedActions.isEmpty()) return emptyList()
        val service = dollOSService ?: return emptyList()

        return try {
            val actionsJson = service.availableActions ?: return emptyList()
            val arr = Json.parseToJsonElement(actionsJson).jsonArray
            arr.mapNotNull { element ->
                val action = element.jsonObject
                val id = action["id"]?.jsonPrimitive?.content ?: return@mapNotNull null
                if (id !in config.skill.allowedActions) return@mapNotNull null
                ToolDefinition(
                    name = id,
                    description = action["description"]?.jsonPrimitive?.content ?: "",
                    parameters = action["parameters"]?.toString()
                        ?: """{"type":"object","properties":{},"required":[]}"""
                )
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to load tools for worker", e)
            emptyList()
        }
    }

    private fun executeAction(toolCall: ToolCall): String {
        val service = dollOSService ?: return """{"success":false,"message":"DollOSService not connected"}"""
        return try {
            service.executeSystemAction(toolCall.name, toolCall.arguments)
        } catch (e: Exception) {
            """{"success":false,"message":"${e.message}"}"""
        }
    }

    private fun postResult(success: Boolean, output: String) {
        status = "COMPLETED"
        val result = WorkerResult(config.id, success, output, tokensUsed, actionsExecuted.toList())
        val payload = """{"workerId":"${result.workerId}","success":${result.success},"output":"${result.output.replace("\"", "\\\"")}","tokensUsed":${result.tokensUsed}}"""

        eventQueue.push(Event(
            type = EventType.WORKER_RESULT,
            priority = EventPriority.NORMAL,
            payload = payload,
            source = "worker:${config.id}"
        ))
        Log.i(TAG, "Worker ${config.id} completed: success=$success, tokens=$tokensUsed")
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/org/dollos/ai/worker/BackgroundWorker.kt
git commit -m "feat: add BackgroundWorker with skill-based action whitelisting"
```

---

## Task 4: WorkerManager

**Goal:** Manages worker lifecycle: spawn, track, cancel, collect results.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/worker/WorkerManager.kt`

- [ ] **Step 1: Create WorkerManager.kt**

```kotlin
package org.dollos.ai.worker

import android.util.Log
import kotlinx.coroutines.CoroutineScope
import org.dollos.ai.event.EventQueue
import org.dollos.ai.llm.LLMClient
import org.dollos.service.IDollOSService
import java.util.concurrent.ConcurrentHashMap

class WorkerManager(
    private val eventQueue: EventQueue,
    private val scope: CoroutineScope
) {

    companion object {
        private const val TAG = "WorkerManager"
        private const val MAX_CONCURRENT_WORKERS = 5
    }

    val skillRegistry = SkillRegistry()
    private val workers = ConcurrentHashMap<String, BackgroundWorker>()

    fun spawn(
        task: String,
        skillId: String,
        client: LLMClient,
        dollOSService: IDollOSService?,
        maxTurns: Int = 10,
        timeoutMs: Long = 120_000
    ): String? {
        if (workers.size >= MAX_CONCURRENT_WORKERS) {
            Log.w(TAG, "Max concurrent workers reached ($MAX_CONCURRENT_WORKERS)")
            return null
        }

        val skill = skillRegistry.get(skillId)
        if (skill == null) {
            Log.e(TAG, "Unknown skill: $skillId")
            return null
        }

        val config = WorkerConfig(
            task = task,
            skill = skill,
            maxTurns = maxTurns,
            timeoutMs = timeoutMs
        )

        val worker = BackgroundWorker(config, client, dollOSService, eventQueue)
        workers[config.id] = worker
        worker.start(scope)

        Log.i(TAG, "Spawned worker ${config.id}: skill=$skillId, task=${task.take(50)}")
        return config.id
    }

    fun cancel(workerId: String) {
        workers[workerId]?.cancel()
        workers.remove(workerId)
        Log.i(TAG, "Cancelled worker: $workerId")
    }

    fun cancelAll() {
        workers.values.forEach { it.cancel() }
        val count = workers.size
        workers.clear()
        Log.i(TAG, "Cancelled all workers ($count)")
    }

    fun getActiveWorkers(): List<Pair<String, BackgroundWorker>> {
        // Clean up completed workers
        workers.entries.removeIf { it.value.status in listOf("COMPLETED", "CANCELLED") }
        return workers.entries.map { it.key to it.value }
    }

    fun getWorkerInfoJson(): String {
        val infos = workers.entries.map { (id, worker) ->
            """{"id":"$id","status":"${worker.status}"}"""
        }
        return "[${infos.joinToString(",")}]"
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/org/dollos/ai/worker/WorkerManager.kt
git commit -m "feat: add WorkerManager for background worker lifecycle"
```

---

## Task 5: Worker Result Persistence

**Goal:** Persist worker results to Room so they survive service restarts. Replay unprocessed results on startup.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/worker/WorkerResultEntity.kt`
- Create: `app/src/main/java/org/dollos/ai/worker/WorkerResultDao.kt`

- [ ] **Step 1: Create WorkerResultEntity.kt**

```kotlin
package org.dollos.ai.worker

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "worker_results")
data class WorkerResultEntity(
    @PrimaryKey val workerId: String,
    val success: Boolean,
    val output: String,
    val tokensUsed: Long,
    val timestamp: Long = System.currentTimeMillis(),
    val processed: Boolean = false
)
```

- [ ] **Step 2: Create WorkerResultDao.kt**

```kotlin
package org.dollos.ai.worker

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query

@Dao
interface WorkerResultDao {
    @Query("SELECT * FROM worker_results WHERE processed = 0")
    suspend fun getUnprocessed(): List<WorkerResultEntity>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insert(result: WorkerResultEntity)

    @Query("UPDATE worker_results SET processed = 1 WHERE workerId = :workerId")
    suspend fun markProcessed(workerId: String)

    @Query("DELETE FROM worker_results WHERE processed = 1 AND timestamp < :beforeMs")
    suspend fun cleanOld(beforeMs: Long)
}
```

- [ ] **Step 3: Update BackgroundWorker.postResult() to persist**

In BackgroundWorker, add a `workerResultDao` parameter and persist before pushing to EventQueue:

```kotlin
// In postResult():
scope.launch {
    workerResultDao.insert(WorkerResultEntity(config.id, success, output, tokensUsed))
}
// Then push to EventQueue as before
```

- [ ] **Step 4: Replay unprocessed results on WorkerManager init**

```kotlin
// In WorkerManager, add init method:
fun init(workerResultDao: WorkerResultDao) {
    scope.launch {
        val unprocessed = workerResultDao.getUnprocessed()
        for (result in unprocessed) {
            eventQueue.push(Event(
                type = EventType.WORKER_RESULT,
                priority = EventPriority.NORMAL,
                payload = """{"workerId":"${result.workerId}","success":${result.success},"output":"${result.output.replace("\"", "\\\"")}","tokensUsed":${result.tokensUsed}}""",
                source = "worker:${result.workerId}"
            ))
        }
        if (unprocessed.isNotEmpty()) {
            Log.i(TAG, "Replayed ${unprocessed.size} unprocessed worker results")
        }
        // Clean old processed results (older than 7 days)
        workerResultDao.cleanOld(System.currentTimeMillis() - 7 * 86400000L)
    }
}
```

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/org/dollos/ai/worker/
git commit -m "feat: add worker result Room persistence and replay"
```

---

## Task 6: Schedule System

**Goal:** Persist schedules to Room, register with AlarmManager, replay on boot.
(Note: was Task 5 before Worker Result Persistence was added)

**Files:**
- Create: `app/src/main/java/org/dollos/ai/schedule/ScheduleEntry.kt`
- Create: `app/src/main/java/org/dollos/ai/schedule/ScheduleDao.kt`
- Create: `app/src/main/java/org/dollos/ai/schedule/ScheduleManager.kt`
- Create: `app/src/main/java/org/dollos/ai/schedule/ScheduleReceiver.kt`

- [ ] **Step 1: Create ScheduleEntry.kt**

```kotlin
package org.dollos.ai.schedule

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "schedules")
data class ScheduleEntry(
    @PrimaryKey val id: String,
    val task: String,              // what to do
    val skillId: String,           // which skill template
    val triggerTimeMs: Long,       // next trigger time (epoch ms)
    val intervalMs: Long = 0,      // 0 = one-shot, >0 = repeating
    val enabled: Boolean = true
)
```

- [ ] **Step 2: Create ScheduleDao.kt**

```kotlin
package org.dollos.ai.schedule

import androidx.room.Dao
import androidx.room.Delete
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query

@Dao
interface ScheduleDao {
    @Query("SELECT * FROM schedules WHERE enabled = 1")
    suspend fun getEnabled(): List<ScheduleEntry>

    @Query("SELECT * FROM schedules")
    suspend fun getAll(): List<ScheduleEntry>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insert(entry: ScheduleEntry)

    @Query("DELETE FROM schedules WHERE id = :id")
    suspend fun delete(id: String)

    @Query("UPDATE schedules SET triggerTimeMs = :nextTrigger WHERE id = :id")
    suspend fun updateNextTrigger(id: String, nextTrigger: Long)
}
```

- [ ] **Step 3: Create ScheduleManager.kt**

```kotlin
package org.dollos.ai.schedule

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import org.dollos.ai.event.Event
import org.dollos.ai.event.EventPriority
import org.dollos.ai.event.EventQueue
import org.dollos.ai.event.EventType
import java.util.UUID

class ScheduleManager(
    private val context: Context,
    private val dao: ScheduleDao,
    private val eventQueue: EventQueue,
    private val scope: CoroutineScope
) {

    companion object {
        private const val TAG = "ScheduleManager"
        const val ACTION_SCHEDULE_TRIGGER = "org.dollos.ai.SCHEDULE_TRIGGER"
        const val EXTRA_SCHEDULE_ID = "schedule_id"
    }

    private val alarmManager = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager

    fun init() {
        scope.launch {
            val schedules = dao.getEnabled()
            schedules.forEach { registerAlarm(it) }
            Log.i(TAG, "Initialized: ${schedules.size} schedules registered")
        }
    }

    suspend fun addSchedule(task: String, skillId: String, triggerTimeMs: Long, intervalMs: Long = 0): String {
        val id = UUID.randomUUID().toString()
        val entry = ScheduleEntry(id, task, skillId, triggerTimeMs, intervalMs)
        dao.insert(entry)
        registerAlarm(entry)
        Log.i(TAG, "Added schedule $id: trigger=${triggerTimeMs}, interval=${intervalMs}")
        return id
    }

    suspend fun removeSchedule(id: String) {
        cancelAlarm(id)
        dao.delete(id)
        Log.i(TAG, "Removed schedule: $id")
    }

    suspend fun getSchedules(): List<ScheduleEntry> = dao.getAll()

    fun onScheduleTriggered(scheduleId: String) {
        scope.launch {
            val schedules = dao.getEnabled()
            val entry = schedules.find { it.id == scheduleId } ?: return@launch

            Log.i(TAG, "Schedule triggered: ${entry.id} — ${entry.task}")

            eventQueue.push(Event(
                type = EventType.SCHEDULE,
                priority = EventPriority.NORMAL,
                payload = """{"scheduleId":"${entry.id}","task":"${entry.task.replace("\"", "\\\"")}","skillId":"${entry.skillId}"}""",
                source = "schedule:${entry.id}"
            ))

            // Update next trigger for repeating schedules
            if (entry.intervalMs > 0) {
                val next = entry.triggerTimeMs + entry.intervalMs
                dao.updateNextTrigger(entry.id, next)
                registerAlarm(entry.copy(triggerTimeMs = next))
            }
        }
    }

    private fun registerAlarm(entry: ScheduleEntry) {
        val intent = Intent(ACTION_SCHEDULE_TRIGGER).apply {
            setPackage(context.packageName)
            putExtra(EXTRA_SCHEDULE_ID, entry.id)
        }
        val pi = PendingIntent.getBroadcast(
            context, entry.id.hashCode(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        alarmManager.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, entry.triggerTimeMs, pi)
    }

    private fun cancelAlarm(scheduleId: String) {
        val intent = Intent(ACTION_SCHEDULE_TRIGGER).apply {
            setPackage(context.packageName)
            putExtra(EXTRA_SCHEDULE_ID, scheduleId)
        }
        val pi = PendingIntent.getBroadcast(
            context, scheduleId.hashCode(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        alarmManager.cancel(pi)
    }
}
```

- [ ] **Step 4: Create ScheduleReceiver.kt**

```kotlin
package org.dollos.ai.schedule

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import org.dollos.ai.DollOSAIApp

class ScheduleReceiver : BroadcastReceiver() {

    companion object {
        private const val TAG = "ScheduleReceiver"
    }

    override fun onReceive(context: Context, intent: Intent) {
        val scheduleId = intent.getStringExtra(ScheduleManager.EXTRA_SCHEDULE_ID) ?: return
        Log.i(TAG, "Schedule alarm received: $scheduleId")
        DollOSAIApp.scheduleManager?.onScheduleTriggered(scheduleId)
    }
}
```

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/org/dollos/ai/schedule/
git commit -m "feat: add schedule system with AlarmManager and Room persistence"
```

---

## Task 7: System Event Receiver + Idle Detector

**Goal:** Listen for system events (screen, charging, WiFi) and detect user idle state.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/trigger/SystemEventReceiver.kt`
- Create: `app/src/main/java/org/dollos/ai/trigger/IdleDetector.kt`

- [ ] **Step 1: Create SystemEventReceiver.kt**

```kotlin
package org.dollos.ai.trigger

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.util.Log
import org.dollos.ai.DollOSAIApp
import org.dollos.ai.event.Event
import org.dollos.ai.event.EventPriority
import org.dollos.ai.event.EventType

class SystemEventReceiver : BroadcastReceiver() {

    companion object {
        private const val TAG = "SystemEventReceiver"
    }

    override fun onReceive(context: Context, intent: Intent) {
        val queue = DollOSAIApp.eventQueue ?: return

        val (payload, source) = when (intent.action) {
            Intent.ACTION_SCREEN_ON ->
                """{"event":"screen_on"}""" to "system:screen"
            Intent.ACTION_SCREEN_OFF ->
                """{"event":"screen_off"}""" to "system:screen"
            Intent.ACTION_POWER_CONNECTED ->
                """{"event":"power_connected"}""" to "system:charging"
            Intent.ACTION_POWER_DISCONNECTED ->
                """{"event":"power_disconnected"}""" to "system:charging"
            WifiManager.NETWORK_STATE_CHANGED_ACTION -> {
                val info = intent.getParcelableExtra<android.net.NetworkInfo>(WifiManager.EXTRA_NETWORK_INFO)
                val connected = info?.isConnected ?: false
                """{"event":"wifi_${if (connected) "connected" else "disconnected"}"}""" to "system:wifi"
            }
            Intent.ACTION_BOOT_COMPLETED ->
                """{"event":"boot_completed"}""" to "system:boot"
            else -> return
        }

        Log.d(TAG, "System event: $source — $payload")
        queue.push(Event(
            type = EventType.SYSTEM_EVENT,
            priority = EventPriority.LOW,
            payload = payload,
            source = source
        ))
    }
}
```

- [ ] **Step 2: Create IdleDetector.kt**

```kotlin
package org.dollos.ai.trigger

import android.os.Handler
import android.os.Looper
import android.util.Log
import org.dollos.ai.event.EventQueue

class IdleDetector(
    private val eventQueue: EventQueue,
    private val onIdle: () -> Unit
) {

    companion object {
        private const val TAG = "IdleDetector"
        private const val IDLE_TIMEOUT_MS = 30_000L           // 30s after last user interaction
        private const val SCREEN_OFF_IDLE_MS = 5_000L         // 5s after screen off
        private const val HEARTBEAT_INTERVAL_MS = 300_000L    // 5 min heartbeat when deep idle
    }

    private val handler = Handler(Looper.getMainLooper())
    private var lastUserActivity = System.currentTimeMillis()
    private var screenOn = true
    private var isIdle = false

    private val idleCheck = Runnable { checkIdle() }

    fun onUserActivity() {
        lastUserActivity = System.currentTimeMillis()
        if (isIdle) {
            isIdle = false
            Log.d(TAG, "User active — exiting idle")
        }
        scheduleIdleCheck()
    }

    fun onScreenOn() {
        screenOn = true
        onUserActivity()
    }

    fun onScreenOff() {
        screenOn = false
        // Shorter idle timeout when screen is off
        handler.removeCallbacks(idleCheck)
        handler.postDelayed(idleCheck, SCREEN_OFF_IDLE_MS)
    }

    fun start() {
        scheduleIdleCheck()
    }

    fun stop() {
        handler.removeCallbacks(idleCheck)
    }

    private fun scheduleIdleCheck() {
        handler.removeCallbacks(idleCheck)
        handler.postDelayed(idleCheck, IDLE_TIMEOUT_MS)
    }

    private fun checkIdle() {
        val elapsed = System.currentTimeMillis() - lastUserActivity

        if (!isIdle && (elapsed >= IDLE_TIMEOUT_MS || (!screenOn && elapsed >= SCREEN_OFF_IDLE_MS))) {
            isIdle = true
            Log.d(TAG, "User idle detected (${elapsed}ms)")

            if (!eventQueue.isEmpty()) {
                onIdle()
            }
        }

        // Heartbeat for long idle periods
        if (isIdle) {
            handler.postDelayed(idleCheck, HEARTBEAT_INTERVAL_MS)
        }
    }
}
```

- [ ] **Step 3: Commit**

```bash
git add app/src/main/java/org/dollos/ai/trigger/
git commit -m "feat: add SystemEventReceiver and IdleDetector"
```

---

## Task 8: EventProcessor

**Goal:** Drains the EventQueue, builds context injection for LLM, handles idle-triggered autonomous processing.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/event/EventProcessor.kt`

- [ ] **Step 1: Create EventProcessor.kt**

```kotlin
package org.dollos.ai.event

import android.util.Log

class EventProcessor(private val eventQueue: EventQueue) {

    companion object {
        private const val TAG = "EventProcessor"
    }

    /**
     * Build a context injection string from pending events (for piggybacking on sendMessage).
     * Only drains non-user-message events. User messages stay in queue.
     */
    fun buildEventContext(): String? {
        val events = eventQueue.drainNonUserEvents()
        if (events.isEmpty()) return null

        val lines = events.map { event ->
            when (event.type) {
                EventType.WORKER_RESULT -> "[Background Worker Result] ${event.payload}"
                EventType.SCHEDULE -> "[Scheduled Task Triggered] ${event.payload}"
                EventType.SYSTEM_EVENT -> "[System Event] ${event.payload}"
                EventType.INTERNAL -> "[Internal Event] ${event.payload}"
                else -> "[Event] ${event.payload}"
            }
        }

        Log.d(TAG, "Built event context: ${events.size} events")
        return "The following events occurred since your last response. Process them as appropriate:\n" +
                lines.joinToString("\n")
    }

    /**
     * Get all pending events for autonomous idle processing.
     * Returns the events and a combined prompt for the LLM.
     */
    fun buildIdleProcessingPrompt(): Pair<List<Event>, String>? {
        val events = eventQueue.drainAll()
        if (events.isEmpty()) return null

        val lines = events.map { event ->
            when (event.type) {
                EventType.WORKER_RESULT -> "[Background Worker Result] ${event.payload}"
                EventType.SCHEDULE -> "[Scheduled Task] ${event.payload}"
                EventType.SYSTEM_EVENT -> "[System Event] ${event.payload}"
                EventType.INTERNAL -> "[Internal Event] ${event.payload}"
                EventType.TEXT_MESSAGE -> "[Pending User Message] ${event.payload}"
                EventType.VOICE_MESSAGE -> "[Pending Voice Message] ${event.payload}"
            }
        }

        Log.d(TAG, "Built idle processing prompt: ${events.size} events")
        val prompt = "You are processing events during idle time. For each event, decide the appropriate action:\n" +
                "- If the user should be notified, output: NOTIFY: <message>\n" +
                "- If a background worker should be spawned, output: SPAWN_WORKER: <skill_id> <task>\n" +
                "- If memory should be updated, output: UPDATE_MEMORY: <content>\n" +
                "- If no action needed, output: SILENT: <reason>\n\n" +
                "Events:\n${lines.joinToString("\n")}"

        return events to prompt
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/org/dollos/ai/event/EventProcessor.kt
git commit -m "feat: add EventProcessor for event context injection and idle processing"
```

---

## Task 9: Integrate into DollOSAIServiceImpl + DollOSAIApp

**Goal:** Wire everything together: EventQueue in App, piggyback on sendMessage, idle processing, AIDL methods.

**Files:**
- Modify: `app/src/main/java/org/dollos/ai/DollOSAIApp.kt`
- Modify: `app/src/main/java/org/dollos/ai/DollOSAIServiceImpl.kt`
- Modify: `app/src/main/java/org/dollos/ai/conversation/ConversationDatabase.kt`

- [ ] **Step 1: Update DollOSAIApp.kt**

Add static references for EventQueue, WorkerManager, ScheduleManager:

```kotlin
// Add to companion object:
var eventQueue: EventQueue? = null
    private set
var scheduleManager: ScheduleManager? = null
    private set

// Add to onCreate():
eventQueue = EventQueue()
```

Note: WorkerManager and ScheduleManager are initialized in DollOSAIServiceImpl (they need the coroutine scope and database).

- [ ] **Step 2: Update ConversationDatabase.kt**

Add ScheduleEntry, WorkerResultEntity and their DAOs to the existing Room database:

```kotlin
// Add to @Database entities list:
entities = [MessageEntity::class, SegmentSummaryEntity::class, ScheduleEntry::class, WorkerResultEntity::class]

// Add version bump and fallbackToDestructiveMigration (dev phase)

// Add abstract DAOs:
abstract fun scheduleDao(): ScheduleDao
abstract fun workerResultDao(): WorkerResultDao
```

- [ ] **Step 3: Update DollOSAIServiceImpl.kt — add fields and init**

Add to the class fields (after conversationManager):

```kotlin
private val eventQueue = DollOSAIApp.eventQueue!!
private val eventProcessor = EventProcessor(eventQueue)
private val workerManager = WorkerManager(eventQueue, scope)
private val scheduleManager: ScheduleManager
private val idleDetector: IdleDetector
```

Add to init block:

```kotlin
// Initialize schedule manager
val scheduleDao = ConversationDatabase.getInstance(DollOSAIApp.instance).scheduleDao()
scheduleManager = ScheduleManager(DollOSAIApp.instance, scheduleDao, eventQueue, scope)
scheduleManager.init()
DollOSAIApp.scheduleManager = scheduleManager

// Initialize idle detector
idleDetector = IdleDetector(eventQueue) { processEventsIdle() }
idleDetector.start()
```

- [ ] **Step 4: Update sendMessage() — piggyback event context**

After building memoryContext and before LLM call, inject pending events:

```kotlin
// After line: val searchResults = memoryManager.searchRelevantMemory(message)
// Add:
val eventContext = eventProcessor.buildEventContext() ?: ""
// Append event context to searchResults so it doesn't overwrite tier2Memory
val enrichedSearch = if (eventContext.isNotEmpty()) {
    "$searchResults\n\n$eventContext"
} else {
    searchResults
}
val systemPrompt = systemPromptBuilder.buildWithMemory(memoryContext, "", enrichedSearch)

// Also notify idle detector of user activity:
idleDetector.onUserActivity()
```

- [ ] **Step 5: Add idle processing method**

```kotlin
private fun processEventsIdle() {
    // Use background model for idle processing (cheaper)
    val client = backgroundClient ?: foregroundClient ?: return
    val (events, prompt) = eventProcessor.buildIdleProcessingPrompt() ?: return

    Log.i(TAG, "Processing ${events.size} events during idle")

    scope.launch {
        try {
            val response = client.send(
                listOf(LLMMessage("user", prompt)),
                systemPromptBuilder.build(),
                0.3f
            )

            // Parse response lines and act
            response.content.lines().forEach { line ->
                when {
                    line.startsWith("NOTIFY:") -> {
                        val message = line.removePrefix("NOTIFY:").trim()
                        sendIdleNotification(message)
                    }
                    line.startsWith("SPAWN_WORKER:") -> {
                        val parts = line.removePrefix("SPAWN_WORKER:").trim().split(" ", limit = 2)
                        if (parts.size == 2) {
                            val bgClient = backgroundClient ?: return@forEach
                            workerManager.spawn(parts[1], parts[0], bgClient, service.dollOSService)
                        }
                    }
                    line.startsWith("UPDATE_MEMORY:") -> {
                        val content = line.removePrefix("UPDATE_MEMORY:").trim()
                        // Write to daily memory
                        memoryManager.writeToDaily(content)
                    }
                    line.startsWith("SILENT:") -> {
                        Log.d(TAG, "Idle processing silent: ${line.removePrefix("SILENT:").trim()}")
                    }
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Idle processing failed", e)
        }
    }
}

private fun sendIdleNotification(message: String) {
    val nm = DollOSAIApp.instance.getSystemService(Context.NOTIFICATION_SERVICE) as android.app.NotificationManager
    val notification = androidx.core.app.NotificationCompat.Builder(DollOSAIApp.instance, DollOSAIApp.NOTIFICATION_CHANNEL_ID)
        .setSmallIcon(android.R.drawable.ic_dialog_info)
        .setContentTitle("DollOS AI")
        .setContentText(message)
        .setStyle(androidx.core.app.NotificationCompat.BigTextStyle().bigText(message))
        .setPriority(androidx.core.app.NotificationCompat.PRIORITY_DEFAULT)
        .setAutoCancel(true)
        .build()
    nm.notify(System.currentTimeMillis().toInt(), notification)
}
```

- [ ] **Step 6: Add AIDL worker/schedule methods to DollOSAIServiceImpl**

```kotlin
// Worker management
fun spawnWorker(task: String, skillId: String): String? {
    val bgClient = backgroundClient ?: return null
    return workerManager.spawn(task, skillId, bgClient, service.dollOSService)
}

fun getWorkers(): String = workerManager.getWorkerInfoJson()

fun cancelWorker(workerId: String) = workerManager.cancel(workerId)

// Schedule management
fun addSchedule(task: String, skillId: String, triggerTimeMs: Long, intervalMs: Long): String {
    return runBlocking { scheduleManager.addSchedule(task, skillId, triggerTimeMs, intervalMs) }
}

fun removeSchedule(scheduleId: String) {
    scope.launch { scheduleManager.removeSchedule(scheduleId) }
}

fun getSchedules(): String {
    return runBlocking {
        val entries = scheduleManager.getSchedules()
        val json = entries.map { """{"id":"${it.id}","task":"${it.task}","skillId":"${it.skillId}","triggerTimeMs":${it.triggerTimeMs},"intervalMs":${it.intervalMs},"enabled":${it.enabled}}""" }
        "[${json.joinToString(",")}]"
    }
}
```

- [ ] **Step 7: Update pauseAll to cancel workers (v1: no worker pause, only cancel)**

```kotlin
// In pauseAll():
workerManager.cancelAll()  // v1: workers are cancelled on emergency stop, not paused
```

- [ ] **Step 8: Wire screen events to IdleDetector**

In DollOSAIServiceImpl init or via SystemEventReceiver, ensure screen on/off events also notify IdleDetector. Add a static reference:

```kotlin
// In DollOSAIServiceImpl companion:
var idleDetectorRef: WeakReference<IdleDetector>? = null

// In init:
idleDetectorRef = WeakReference(idleDetector)
```

Register SystemEventReceiver dynamically in DollOSAIServiceImpl init (screen/WiFi events require dynamic registration on Android 8+):

```kotlin
// In init block:
val systemReceiver = SystemEventReceiver()
val filter = android.content.IntentFilter().apply {
    addAction(Intent.ACTION_SCREEN_ON)
    addAction(Intent.ACTION_SCREEN_OFF)
    addAction(Intent.ACTION_POWER_CONNECTED)
    addAction(Intent.ACTION_POWER_DISCONNECTED)
    addAction(android.net.wifi.WifiManager.NETWORK_STATE_CHANGED_ACTION)
}
DollOSAIApp.instance.registerReceiver(systemReceiver, filter)
```

In SystemEventReceiver.onReceive, also notify IdleDetector:

```kotlin
when (intent.action) {
    Intent.ACTION_SCREEN_ON -> DollOSAIServiceImpl.idleDetectorRef?.get()?.onScreenOn()
    Intent.ACTION_SCREEN_OFF -> DollOSAIServiceImpl.idleDetectorRef?.get()?.onScreenOff()
}
```

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat: integrate event queue, worker manager, schedule into DollOSAIServiceImpl"
```

---

## Task 10: Update AIDL Interfaces

**Goal:** Expose worker and schedule management through AIDL.

**Files:**
- Modify: `aidl/org/dollos/ai/IDollOSAIService.aidl`
- Modify: `aidl/org/dollos/ai/IDollOSAICallback.aidl`

- [ ] **Step 1: Update IDollOSAIService.aidl**

Add after existing methods:

```aidl
    // Background workers
    String spawnWorker(String task, String skillId);
    String getWorkers();
    void cancelWorker(String workerId);

    // Schedules
    String addSchedule(String task, String skillId, long triggerTimeMs, long intervalMs);
    void removeSchedule(String scheduleId);
    String getSchedules();
```

- [ ] **Step 2: Update IDollOSAICallback.aidl**

Add:

```aidl
    void onWorkerComplete(String workerId, boolean success, String output);
```

- [ ] **Step 3: Commit**

```bash
git add aidl/
git commit -m "feat: add worker and schedule AIDL methods"
```

---

## Task 11: Update AndroidManifest.xml

**Goal:** Register receivers for schedule alarms and system events.

**Files:**
- Modify: `app/src/main/AndroidManifest.xml`

- [ ] **Step 1: Add receivers and permissions**

Add permissions:

```xml
<uses-permission android:name="android.permission.SCHEDULE_EXACT_ALARM" />
<uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
```

Add inside `<application>`:

```xml
<receiver
    android:name=".schedule.ScheduleReceiver"
    android:exported="false">
    <intent-filter>
        <action android:name="org.dollos.ai.SCHEDULE_TRIGGER" />
    </intent-filter>
</receiver>
```

Note: SystemEventReceiver is registered dynamically in DollOSAIServiceImpl (screen/WiFi events require dynamic registration on Android 8+). Only ScheduleReceiver needs manifest declaration.

- [ ] **Step 2: Commit**

```bash
git add app/src/main/AndroidManifest.xml
git commit -m "feat: register schedule and system event receivers"
```

---

## Task 12: Build, Deploy, and Verify

**Goal:** Build and verify on device.

- [ ] **Step 1: Build**

```bash
cd ~/Projects/DollOSAIService
./gradlew assembleRelease
```

- [ ] **Step 2: Deploy**

```bash
cp app/build/outputs/apk/release/app-release-unsigned.apk prebuilt/DollOSAIService.apk
rsync -av --delete . ~/Projects/DollOS-build/external/DollOSAIService/
cd ~/Projects/DollOS-build
source build/envsetup.sh && lunch dollos_bluejay-bp2a-userdebug
m DollOSAIService -j$(nproc)
```

Push to device and reboot.

- [ ] **Step 3: Verify EventQueue and idle detection**

```bash
adb logcat -d | grep -iE "EventQueue|IdleDetector|ScheduleManager|WorkerManager|EventProcessor" | head -30
```

Expected: EventQueue created, IdleDetector started, ScheduleManager initialized.

- [ ] **Step 4: Verify event piggybacking**

Send a message via TestActivity. Check logcat for EventProcessor building event context (if any system events accumulated).

- [ ] **Step 5: Verify system events**

Toggle screen on/off, connect/disconnect charger. Check logcat for SystemEventReceiver events.

- [ ] **Step 6: Test worker spawn (via adb or TestActivity)**

If possible, send a message like "run a background task to check available apps" and verify worker spawning in logcat.

---

## Notes

### What's NOT in v1

- UI operation (VirtualDisplay, accessibility tree, takeover mechanism)
- Smart notification (context-aware delivery method selection)
- Programmable event definitions (user-defined event conditions)
- Voice message handling (requires Voice Pipeline / STT)

### Integration with existing systems

- Workers contribute to UsageTracker (background model usage)
- Worker results can trigger memory writes
- Schedules survive reboot (Room + AlarmManager re-registration)
- Workers visible in TaskManager via getActiveTasks() (need to merge worker info)

### Event flow summary

```
User sends message
  → sendMessage()
  → eventProcessor.buildEventContext() drains non-user events
  → inject into LLM context alongside user message
  → AI processes everything in one response
  → idleDetector.onUserActivity() resets idle timer

User goes idle (30s / screen off 5s)
  → IdleDetector fires onIdle
  → processEventsIdle() drains all events
  → foreground LLM decides: notify / spawn worker / update memory / silent
  → actions executed accordingly

Schedule triggers (AlarmManager)
  → ScheduleReceiver → ScheduleManager.onScheduleTriggered()
  → Event pushed to queue
  → processed on next sendMessage or idle cycle

Background worker completes
  → BackgroundWorker posts WORKER_RESULT event
  → processed on next sendMessage or idle cycle
```
