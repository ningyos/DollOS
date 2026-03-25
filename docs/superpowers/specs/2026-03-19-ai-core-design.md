# AI Core -- Design Spec

## Overview

AI Core is the second sub-project of DollOS, providing the AI intelligence layer that powers the OS's companion experience. It includes an LLM client for multi-model API communication, a conversation engine with persistent memory, a personality system, and a basic agent system for device control.

AI Core runs as an independent system service (`DollOSAIService`) separate from the existing `DollOSService`.

## Architecture

### Service Architecture

```
DollOSAIService (normal app uid, platform cert)
  - LLM Client
  - Conversation Engine
  - Memory System
  - Personality System
  - Agent System (logic)

DollOSService (system uid)
  - System config (GMS, version)
  - Agent execution (WiFi, Bluetooth, alarms, open app -- on behalf of AIService)
  - AI Task Manager (emergency stop UI)
  - Power button interception
```

DollOSAIService uses normal app uid (NOT sharedUserId=system) to avoid storage issues. System-privileged operations (including starting Activities from background) are delegated to DollOSService via Binder IPC.

### Responsibility Split

| Concern | Owner |
|---------|-------|
| API key, personality, memory | DollOSAIService |
| GMS preference, DollOS version | DollOSService |
| LLM API calls | DollOSAIService |
| All agent actions (WiFi, BT, alarms, open app) | DollOSService (executed on behalf of AIService) |
| AI Task Manager + emergency stop | DollOSService |
| Conversation history, context management | DollOSAIService |

### Migration from DollOS Base

DollOSService currently holds `setApiKey`, `setPersonality`, `getPersonalityName` methods. These will be:
1. **Deprecated** in DollOSService AIDL (marked with comment, kept for backward compat during transition)
2. **Replaced** by DollOSAIService AIDL methods
3. OOBE updated to bind to both services: GMS page -> DollOSService, API key + personality pages -> DollOSAIService
4. DollOSService's old personality/API key data migrated to DollOSAIService on first boot after upgrade

### DollOS Base Spec Update

DollOS Base spec's `/data/dollos/` directory structure is superseded for AI data. Memory and AI config are stored in DollOSAIService's own app internal storage (`/data/user/0/org.dollos.ai/files/`). The `/data/dollos/` paths in the Base spec should be treated as reserved for future system-level AI features that require cross-service access.

## Section 1: LLM Client

### Multi-Model Support

Supports multiple LLM providers through a unified interface. Users configure in Settings:
- **Foreground model** -- used for active conversation and context compression (higher quality)
- **Background model** -- used for idle memory summarization, background tasks (cheaper)

Context compression uses the foreground model (async, non-blocking) because compression quality directly impacts all subsequent conversation accuracy. The cost trade-off (parallel foreground API call) is justified by the importance of accurate context summaries.

Supported providers (first version): Claude, Grok, OpenAI, custom endpoint.

### Streaming and Response Control

- Streaming responses by default (token-by-token display via callback interface)
- Non-streaming available as option
- **Emergency stop mechanism:**
  - Double-click power button -> pause ALL AI activity globally
  - Display AI Task Manager modal (system level, managed by DollOSService)
  - Modal shows: task name, description, start time, token usage, estimated cost, conversation context
  - User can cancel individual tasks or resume all
  - Modal has highest priority (same level as power menu), cannot be accessed by AI
  - Uses AOSP's built-in config_doublePressOnPowerBehavior resource overlay (value 3 = LAUNCH_TARGET_ACTIVITY) to launch TaskManagerActivity. No framework modification needed.

### Stop Triggers

- Manual: stop button in conversation UI
- Voice: user starts speaking during AI response -> stop current generation
- Hardware: double-click power button -> global emergency stop

### Usage Tracking

Tracks per API call:
- Token count (input + output)
- Estimated cost
- Timestamp
- Classification: foreground/background, model, function (conversation/agent/memory)

Budget controls:
- User sets **warning threshold** -- notification when exceeded
- User sets **hard limit** -- all AI functions stop when exceeded
- Both configurable with period (per day or per month) and token amount

### Error Handling

API errors (invalid key, rate limit, network failure) are surfaced via Android system notifications, not in the conversation UI. This keeps the conversation clean and the notification system handles retries/alerts.

### Offline Mode

- First version: AI conversation fully stops when offline, local small model fallback for future consideration
- Agent operations that don't need LLM (toggle WiFi/BT) remain functional offline via voice/text command parsing
- Memory search (ObjectBox local) remains functional offline

### No Cache

Caching deferred to future versions.

## Section 2: Conversation Engine

### Conversation Structure

Conversations are automatically segmented by date. Each day is a separate conversation segment (like date dividers in a chat app). Users scroll up to see history. No manual "new conversation" action needed.

### Context Window Management

**Proactive compression:**
1. Monitor context usage continuously
2. At 70-80% capacity, snapshot current conversation for background compression
3. Immediately truncate old messages in foreground, keep recent N messages, continue responding
4. Background compression runs in parallel using foreground model (async API call, non-blocking)
5. When compression completes, merge: summary + new messages accumulated during compression

**Compression produces:**
- A summary of the compressed conversation
- Triggers memory write (important facts extracted before compression)

### Memory Architecture

Inspired by OpenClaw/memsearch. **Markdown files are the single source of truth.** ObjectBox is a pure search index rebuilt from Markdown if corrupted.

**Three-tier structure:**

| Tier | Path | Loading |
|------|------|---------|
| 1 | `MEMORY.md` | Always loaded into context |
| 2 | `memory/YYYY-MM-DD.md` | Today and yesterday auto-loaded |
| 3 | `memory/people/`, `topics/`, `decisions/` | Loaded via search when relevant |

All paths relative to DollOSAIService's internal storage: `/data/user/0/org.dollos.ai/files/memory/`.

**Search:** Hybrid search using ObjectBox:
- Vector search (semantic similarity)
- BM25 keyword search
- Results merged with configurable weights

**Embedding source:** User-configurable in Settings:
- Cloud API (OpenAI, Voyage, etc.) -- higher quality, requires network
- Local ONNX model (e.g., all-MiniLM-L6-v2, ~23MB) -- offline capable, lower quality

**Storage:** App internal storage, with export/import functionality via `ParcelFileDescriptor` (Android scoped storage compliant) for user access and backup.

### Memory Write Triggers

Three trigger sources, all feeding into a **single serialized write queue**:

1. **Context threshold** -- when context reaches 70-80%, extract important facts before compression
2. **Idle background** -- when conversation is idle for N minutes, background model reviews recent conversation and extracts memories
3. **Event-based** -- screen lock, app switch, user says "remember this"

**"Remember this" flow:**
1. User says "remember this" (or similar)
2. AI extracts and organizes the information
3. AI presents formatted memory to user: "I'll remember: [content]. Is this correct?"
4. User confirms -> write to memory
5. User corrects -> AI adjusts and re-confirms

**Write failure handling:** Retry 3 times, then persist to `pending_writes.json` in app internal storage. On next service startup, pending writes are replayed before normal operation. If a pending write conflicts with current index state, the Markdown file content takes precedence.

### Index Sync

Markdown is source of truth. ObjectBox index rebuilds from Markdown when:
- Files are added/modified/deleted
- Index is corrupted or missing
- Service startup detects inconsistency

## Section 3: Personality System

### Configuration Fields

Inspired by Kindroid. Five user-configurable fields, all injected into the system prompt:

| Field | Limit | Description |
|-------|-------|-------------|
| Backstory | 2500 chars | AI's background, personality, motivations. Written in 3rd person |
| Response Directive | 150 chars | Forced tone and style guide (strongest influence) |
| Dynamism | 0.0 - 1.0 slider | Stability (0) to creativity (1). Maps to LLM temperature and prompt wording |
| Address | 50 chars | How AI addresses the user (name, nickname, honorific) |
| Language Preference | selection | AI response language, whether to mix languages |

No presets. Users write their own personality configuration.

### System Prompt Construction

```
[Response Directive]
[Language Preference]
[Address instruction]
[Backstory]
[Relevant memories from Tier 1 + Tier 2]
[Search results from Tier 3 if applicable]
[Conversation history]
[User message]
```

Dynamism maps to:
- LLM temperature parameter with **per-provider clamping**: Claude (0.3-1.0), OpenAI/Grok (0.3-1.2)
- Additional prompt wording adjusting creativity level
- Provider-specific limits applied automatically to prevent API errors

## Section 4: Agent System

### Action Interface

Each agent operation implements a unified `Action` interface:

```
Action {
    id: String              // "open_app"
    name: String            // "Open App"
    description: String     // For LLM tool description
    parameters: Schema      // What parameters the action needs
    confirmRequired: Boolean // Default confirmation requirement
    execute(params): Result // Execute the action
}
```

### LLM Integration

Uses each provider's native tool calling format to invoke actions. All registered Actions are provided to the LLM as available tools. LLM decides when to call which tool based on conversation context.

**Provider-native tool calling:**
- Anthropic (Claude): `tools[]` in request, `tool_use` content block in response
- OpenAI / Grok: `tools[]` with `function` type in request, `tool_calls` in response
- Each provider's LLM client handles format conversion to/from internal `ToolCall` representation

**DollOSAIService-side components:**
- `ToolCallFormatter`: converts ActionRegistry tool descriptions into provider-specific `tools[]` format
- `ToolCallParser`: parses provider-specific tool call responses into internal `ToolCall` data class
- `AgentExecutor`: orchestrates tool call loop — check confirmRequired, call DollOSService.executeSystemAction(), feed result back to LLM
- `sendMessage()` modified to support multi-turn tool calling (LLM may chain multiple tool calls)

For models that don't support tool calling: not supported in v1.

### First Version Actions

| Action | Parameters | Default Confirm | Executor |
|--------|-----------|----------------|----------|
| Open App | package name or app name | No | DollOSService (via Binder, needs system uid for background activity start) |
| Set Alarm | time, label | Yes | DollOSService (via Binder) |
| Toggle WiFi | on/off | Yes | DollOSService (via Binder) |
| Toggle Bluetooth | on/off | Yes | DollOSService (via Binder) |

All actions are executed by DollOSService (system uid) on behalf of DollOSAIService, because system uid is needed for background activity launches and system setting changes.

### Confirmation Flow

1. LLM decides to call a tool
2. AIService checks if `confirmRequired` is true for that action
3. If yes: display confirmation to user in conversation ("Should I turn on WiFi?")
4. User confirms -> execute via DollOSService Binder call
5. User denies -> cancel and inform LLM
6. **Timeout:** If user doesn't respond within 60 seconds, auto-cancel and inform LLM. Pending confirmation does not block other tasks.

Users can change confirmation settings per action in Settings.

## Section 5: Emergency Stop (AI Task Manager)

### Trigger

Double-click power button. Uses AOSP's built-in config_doublePressOnPowerBehavior resource overlay (value 3 = LAUNCH_TARGET_ACTIVITY) to launch TaskManagerActivity. No framework modification needed.

### Behavior

1. Double-click detected -> DollOSService sends **synchronous** pause signal to DollOSAIService
2. `pauseAll()` returns only after all tasks are confirmed paused (blocking Binder call with 3s timeout)
3. DollOSService then reads task list via `getActiveTasks()` (guaranteed consistent after pause confirmation)
4. DollOSService displays Task Manager modal (same z-order as power menu)
5. Modal shows all AI tasks with:
   - Task name and description
   - Start time
   - Token usage and estimated cost
   - Related conversation context
6. User can:
   - Cancel individual tasks
   - Resume all (dismiss modal)
   - Dismiss modal (tapping outside) -> resume all
7. AI cannot access or dismiss this modal

### Implementation

- DollOSService owns the modal UI and power button interception
- DollOSAIService reports task list to DollOSService via Binder
- `pauseAll()` is synchronous with 3s timeout -- if AIService doesn't confirm in time, DollOSService shows modal with stale data and a "refreshing..." indicator
- Pause/resume signals flow from DollOSService to DollOSAIService via Binder

## Section 6: AIDL Interfaces

### IDollOSAICallback (new -- for streaming and async events)

```
interface IDollOSAICallback {
    // Streaming response
    void onToken(String token);
    void onResponseComplete(String fullResponse);
    void onResponseError(String errorCode, String message);

    // Agent
    void onActionConfirmRequired(String actionId, String actionName, String description);
    void onActionExecuted(String actionId, boolean success, String resultMessage);

    // Task updates
    void onTaskListUpdated(String tasksJson);

    // Memory
    void onMemoryConfirmRequired(String formattedMemory);
}
```

### IDollOSAIService (new)

```
interface IDollOSAIService {
    // Callback registration
    void registerCallback(IDollOSAICallback callback);
    void unregisterCallback(IDollOSAICallback callback);

    // Conversation
    void sendMessage(String message);
    void stopGeneration();
    boolean pauseAll(); // returns true when all tasks paused, blocking with 3s timeout
    void resumeAll();

    // Personality
    void setBackstory(String backstory);
    void setResponseDirective(String directive);
    void setDynamism(float value);
    void setAddress(String address);
    void setLanguagePreference(String language);
    String getBackstory();
    String getResponseDirective();
    float getDynamism();
    String getAddress();
    String getLanguagePreference();

    // API Configuration
    void setForegroundModel(String provider, String apiKey, String model);
    void setBackgroundModel(String provider, String apiKey, String model);

    // Embedding Configuration
    void setEmbeddingSource(String source); // "cloud" or "local"
    String getEmbeddingSource();

    // Memory
    String searchMemory(String query);
    void exportMemory(in ParcelFileDescriptor fd);
    void importMemory(in ParcelFileDescriptor fd);
    void confirmMemoryWrite(boolean approved); // user confirms "remember this"

    // Usage
    String getUsageStats();
    void setWarningThreshold(long tokens, String period); // period: "daily" or "monthly"
    void setHardLimit(long tokens, String period);

    // Task Manager
    String getActiveTasks(); // JSON list of active tasks
    void cancelTask(String taskId);
}
```

### IDollOSService (updated)

Add to existing interface:

```
// Agent execution (on behalf of AIService) -- returns JSON result
String executeSystemAction(String actionId, String paramsJson);

// Available actions query
String getAvailableActions();

// Emergency stop
void showTaskManager();
```

Note: `hideTaskManager()` removed -- modal is only dismissed by user interaction (tap resume/outside), never programmatically.

## Section 7: Background Work System (Event-Driven Architecture)

### Overview

The foreground AI transitions from a passive chat responder to an event-driven agent. All inputs (user messages, worker results, system events, schedules, internal events) flow through a unified Event Queue. The foreground AI processes events either piggybacked on user messages or autonomously during idle periods.

### Event Queue

```
Event {
    id: String
    type: TEXT_MESSAGE | VOICE_MESSAGE | WORKER_RESULT | SCHEDULE | SYSTEM_EVENT | INTERNAL
    priority: HIGH | NORMAL | LOW
    payload: String (JSON)
    timestamp: Long
    source: String  // "user" / "worker:task_id" / "system:wifi" / "alarm:morning_routine"
}
```

- TEXT_MESSAGE: user typed text, response via text UI
- VOICE_MESSAGE: user spoke via STT, response via TTS + text display, AI adjusts style (shorter, conversational)
- Queue is in-memory (ConcurrentLinkedQueue), not persisted to disk on restart
- Exception: WORKER_RESULT persisted to Room (worker may complete before service restart)

### Event Processing Model

**Not a polling loop.** Two consumption paths:

1. **User is chatting** -- events piggyback on sendMessage(). Before LLM call, inject pending events into context ("Background task X completed: [result]", "System event: WiFi disconnected"). AI handles everything in one response. Zero latency for user messages.

2. **User is idle** -- idle timer triggers autonomous processing. AI reads pending events, decides actions (notify user, execute action, update memory, spawn worker). Triggered by: new HIGH priority event arriving, or idle timeout (configurable, e.g. 30s after last user interaction).

### Background Worker Agent

Workers are like Claude's subagents: spawned by foreground AI, schedule, or event trigger. They run independently, cannot interact with users, and report results back.

**Characteristics:**
- Uses background model (cheaper)
- Cannot operate UI (no accessibility tree, no screenshots, no taps)
- Can call system API actions (WiFi, Bluetooth, alarm, open app)
- Visible in TaskManager (can be paused/cancelled)
- confirmRequired actions execute without confirmation (task assignment implies authorization)

**Authorization:**
- Per-task action whitelist: foreground AI specifies allowed actions when spawning
- Skill templates: predefined whitelist + behavior bundles (e.g. "morning_routine" skill allows set_alarm + open_app)
- Actions outside whitelist are rejected, not queued for confirmation

### Trigger Mechanisms

| Trigger | Mechanism | v1 |
|---------|-----------|-----|
| Foreground AI dispatch | Direct spawn via API | Yes |
| Schedule | AlarmManager (setExactAndAllowWhileIdle) | Yes |
| AI internal event | Memory write complete, context compression complete | Yes |
| System event | BroadcastReceiver (screen on/off, charging, WiFi) | Yes |
| Programmable event | User-defined conditions combining system events | v2 |

Schedule persistence: stored in Room, re-registered with AlarmManager on boot.

### UI Operation Rights (v2)

| State | AI UI Access | Mechanism |
|-------|-------------|-----------|
| User active (screen on + interacting) | Must ask permission first, user approves → AI takes over | Takeover overlay showing what AI is doing |
| Screen locked / off | Free to operate | VirtualDisplay (no physical screen wake) |
| User wakes screen during AI operation | Show "AI operating" overlay + current task description | User can interrupt or wait |

v1: workers cannot operate UI. v2: full UI operation with VirtualDisplay and takeover mechanism.

### Smart Notification (v2)

AI proactively notifies users with context-aware delivery:
- Silent notification (no sound/vibration) -- low priority, user is busy
- Normal notification -- standard priority
- Voice announcement (TTS) -- high priority, user is idle / driving
- Delivery method chosen based on: Do Not Disturb state, location, user preferences, time of day

## Implementation Status (2026-03-24)

| Plan | Status | Description |
|------|--------|-------------|
| Plan A | Complete | LLM client, personality, usage tracking, settings |
| Plan B | Complete | Memory system (ObjectBox + FTS4), conversation engine, context compression |
| Plan C | Complete | Agent system, action execution, emergency stop, tool calling |
| Plan D v1 | Complete | Background work system: event queue, workers, triggers |
| Embedding | Complete | Cloud (configurable endpoint) + Local (ONNX Runtime), per-model vector store, retrieval modes |
| Settings UI | Complete | Restructured: Stats + Personality main page, LLM / Memory / Budget sub-pages |
| Plan D v2 | Complete | UI operation (AccessibilityService + VirtualDisplay + takeover), smart notification, programmable events |
| Character Pack | Complete | .doll format, import/export/switch, security validation, AIDL asset access |
| AI Launcher | Complete | Full-screen 3D Launcher (Filament + TextureView), conversation bubble, app drawer, character picker |

## Out of Scope

- AI Launcher (separate sub-project -- System UI)
- Avatar system (separate sub-project)
- Voice pipeline / STT / TTS (separate sub-project, but VOICE_MESSAGE event type reserved)
- Wake word (replaces original power-key PTT plan)
- API key encryption (deferred)
- GMS auto-installation in OOBE
- Cache system
- Advanced agent actions beyond the four listed
- Models that don't support tool calling
