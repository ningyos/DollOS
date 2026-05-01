# AI Core Plan A: DollOSAIService + LLM Client + Personality System

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build DollOSAIService as a standalone Gradle Android project (compiled externally, imported into AOSP as a prebuilt APK) with AIDL interfaces for AI operations, a multi-model LLM client with OkHttp streaming support and budget controls, and a 5-field personality system. Update OOBE SetupWizard to bind to DollOSAIService for API key and personality configuration. Integrate with Plan C's DollOSService for agent action execution.

**Architecture:** DollOSAIService is a standard Android Gradle project living in its own GitHub repo (`ningyos/DollOSAIService`). It uses Maven dependencies (OkHttp, kotlinx.serialization, kotlinx-coroutines). The APK is signed with the platform cert and imported into AOSP via `android_app_import` in `external/DollOSAIService/Android.bp`. The AIDL interface definitions live in AOSP as a `dollos-ai-aidl` java_library so both the Gradle project and Soong-built clients (DollOSService, SetupWizard) can use them. DollOSAIService runs as a privileged app with normal app uid (no `sharedUserId=system`) to avoid storage issues. Config stored in SharedPreferences via device-protected storage. Agent actions delegated to DollOSService via Binder IPC (`executeSystemAction`).

**Tech Stack:** Kotlin, AIDL (Binder IPC), OkHttp 4.x (Maven), kotlinx.serialization (Maven), kotlinx-coroutines-android (Maven), SharedPreferences (device-protected storage), Android Notification API, Gradle build system (external) + Soong (AOSP integration)

**Design Reference:** Self-implemented LLM client referencing Koog's design patterns for provider abstraction, streaming callback pattern, and tool calling schema generation. No Koog dependency.

---

## File Structure

### DollOSAIService (Gradle project -- separate repo: ningyos/DollOSAIService)

```
DollOSAIService/                          # Root of ningyos/DollOSAIService repo
  build.gradle.kts                        # Root build file
  settings.gradle.kts                     # Settings
  gradle.properties                       # Gradle properties
  app/
    build.gradle.kts                      # App module with Maven dependencies
    src/main/
      AndroidManifest.xml
      aidl/org/dollos/ai/
        IDollOSAIService.aidl             # Copied from AOSP dollos-ai-aidl
        IDollOSAICallback.aidl            # Copied from AOSP dollos-ai-aidl
      java/org/dollos/ai/
        DollOSAIApp.kt
        DollOSAIService.kt
        DollOSAIServiceImpl.kt
        llm/
          LLMProvider.kt
          LLMClient.kt
          LLMResponse.kt
          StreamingCallback.kt
          SSEParser.kt
          ClaudeProvider.kt
          OpenAIProvider.kt
          GrokProvider.kt
          CustomProvider.kt
        personality/
          PersonalityManager.kt
          SystemPromptBuilder.kt
        usage/
          UsageTracker.kt
          BudgetManager.kt
```

### AOSP tree -- AIDL shared library (new)

```
packages/apps/DollOSAIService/            # AIDL-only directory in AOSP
  Android.bp                              # java_library: dollos-ai-aidl
  aidl/org/dollos/ai/
    IDollOSAIService.aidl                 # Source of truth for AIDL interfaces
    IDollOSAICallback.aidl
```

### AOSP tree -- Prebuilt APK integration (new)

```
external/DollOSAIService/
  Android.bp                              # android_app_import
  prebuilt/
    DollOSAIService.apk                   # Built by Gradle, copied here
  privapp-permissions-dollos-ai.xml
```

### DollOSSetupWizard (modify existing)

```
packages/apps/DollOSSetupWizard/
  Android.bp                              # Add dollos-ai-aidl dependency
  src/org/dollos/setup/
    SetupWizardActivity.kt                # Add DollOSAIService binding
    ApiKeyPage.kt                         # Switch to DollOSAIService calls
    PersonalityPage.kt                    # Switch to DollOSAIService calls, add 5 fields
  res/layout/
    page_api_key.xml                      # Add foreground/background model selection
    page_personality.xml                  # Add backstory, directive, dynamism, address, language
```

### Vendor Config (modify existing)

```
vendor/dollos/
  dollos_bluejay.mk                       # Add DollOSAIService to PRODUCT_PACKAGES + privapp perms
```

---

## Task 1: Create Gradle Project Skeleton

**Goal:** Initialize the DollOSAIService Android Gradle project with proper structure, Kotlin DSL build files, Maven dependencies (OkHttp, kotlinx.serialization, kotlinx-coroutines), and AndroidManifest.

**Files:**
- Create: `DollOSAIService/settings.gradle.kts`
- Create: `DollOSAIService/build.gradle.kts`
- Create: `DollOSAIService/gradle.properties`
- Create: `DollOSAIService/app/build.gradle.kts`
- Create: `DollOSAIService/app/src/main/AndroidManifest.xml`

- [ ] **Step 1: Create settings.gradle.kts**

Create `DollOSAIService/settings.gradle.kts`:

```kotlin
pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolution {
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "DollOSAIService"
include(":app")
```

- [ ] **Step 2: Create root build.gradle.kts**

Create `DollOSAIService/build.gradle.kts`:

```kotlin
plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.1.0" apply false
    id("org.jetbrains.kotlin.plugin.serialization") version "2.1.0" apply false
}
```

- [ ] **Step 3: Create gradle.properties**

Create `DollOSAIService/gradle.properties`:

```properties
android.useAndroidX=true
kotlin.code.style=official
org.gradle.jvmargs=-Xmx2048m
```

- [ ] **Step 4: Create app/build.gradle.kts**

Create `DollOSAIService/app/build.gradle.kts`:

```kotlin
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.serialization")
}

android {
    namespace = "org.dollos.ai"
    compileSdk = 35

    defaultConfig {
        applicationId = "org.dollos.ai"
        minSdk = 33
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // Platform signing is done during AOSP integration.
            // For local dev, use debug keystore.
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
        freeCompilerArgs += listOf("-Xjvm-default=all")
    }

    buildFeatures {
        aidl = true
    }
}

dependencies {
    // HTTP client
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.squareup.okhttp3:okhttp-sse:4.12.0")

    // JSON serialization
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")

    // Coroutines
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.9.0")

    // AndroidX
    implementation("androidx.core:core-ktx:1.15.0")

    // DollOSService AIDL stubs -- copied as JAR from AOSP build output
    // See Task 2 for details on how to obtain this JAR.
    compileOnly(files("libs/dollos-service-aidl.jar"))
}
```

- [ ] **Step 5: Create AndroidManifest.xml**

Create `DollOSAIService/app/src/main/AndroidManifest.xml`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="org.dollos.ai">

    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="android.permission.RECEIVE_BOOT_COMPLETED" />
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
    <uses-permission android:name="android.permission.POST_NOTIFICATIONS" />

    <application
        android:name=".DollOSAIApp"
        android:label="DollOS AI Service"
        android:persistent="true"
        android:directBootAware="true">

        <service
            android:name=".DollOSAIService"
            android:exported="true"
            android:enabled="true">
            <intent-filter>
                <action android:name="org.dollos.ai.IDollOSAIService" />
            </intent-filter>
        </service>

    </application>
</manifest>
```

- [ ] **Step 6: Create libs/ directory for compile-only JARs**

```bash
mkdir -p DollOSAIService/app/libs
# dollos-service-aidl.jar will be placed here in Task 2.
```

- [ ] **Step 7: Initialize git repo and commit**

```bash
cd DollOSAIService
git init
git add .
git commit -m "feat: initialize Gradle Android project skeleton with Maven dependencies"
```

---

## Task 2: AIDL Integration

**Goal:** Create the AIDL interfaces for DollOSAIService in two places: (1) the AOSP Soong `dollos-ai-aidl` java_library (source of truth), and (2) copied into the Gradle project's `src/main/aidl/` for local compilation. Also set up the `dollos-service-aidl.jar` for the Gradle project to call DollOSService.

**AIDL sharing strategy:** The AIDL `.aidl` files live canonically in the AOSP tree at `packages/apps/DollOSAIService/aidl/`. They are copied into the Gradle project's `app/src/main/aidl/` directory. When AIDL changes, update AOSP first, then copy to Gradle project.

**Files:**
- Create: `packages/apps/DollOSAIService/aidl/org/dollos/ai/IDollOSAICallback.aidl` (AOSP)
- Create: `packages/apps/DollOSAIService/aidl/org/dollos/ai/IDollOSAIService.aidl` (AOSP)
- Create: `packages/apps/DollOSAIService/Android.bp` (AOSP)
- Create: `DollOSAIService/app/src/main/aidl/org/dollos/ai/IDollOSAICallback.aidl` (Gradle copy)
- Create: `DollOSAIService/app/src/main/aidl/org/dollos/ai/IDollOSAIService.aidl` (Gradle copy)

- [ ] **Step 1: Create IDollOSAICallback.aidl (AOSP -- source of truth)**

Create `packages/apps/DollOSAIService/aidl/org/dollos/ai/IDollOSAICallback.aidl`:

```aidl
package org.dollos.ai;

interface IDollOSAICallback {
    // Streaming response -- token-by-token delivery
    void onToken(String token);
    void onResponseComplete(String fullResponse);
    void onResponseError(String errorCode, String message);

    // Agent -- confirmation and execution results
    void onActionConfirmRequired(String actionId, String actionName, String description);
    void onActionExecuted(String actionId, boolean success, String resultMessage);

    // Task updates -- JSON list of active tasks
    void onTaskListUpdated(String tasksJson);

    // Memory -- user confirmation for "remember this"
    void onMemoryConfirmRequired(String formattedMemory);
}
```

- [ ] **Step 2: Create IDollOSAIService.aidl (AOSP -- source of truth)**

Create `packages/apps/DollOSAIService/aidl/org/dollos/ai/IDollOSAIService.aidl`:

```aidl
package org.dollos.ai;

import org.dollos.ai.IDollOSAICallback;

interface IDollOSAIService {
    // Callback registration
    void registerCallback(IDollOSAICallback callback);
    void unregisterCallback(IDollOSAICallback callback);

    // Conversation
    void sendMessage(String message);
    void stopGeneration();
    boolean pauseAll();
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
    void setEmbeddingSource(String source);
    String getEmbeddingSource();

    // Memory
    String searchMemory(String query);
    void exportMemory(in ParcelFileDescriptor fd);
    void importMemory(in ParcelFileDescriptor fd);
    void confirmMemoryWrite(boolean approved);

    // Usage
    String getUsageStats();
    void setWarningThreshold(long tokens, String period);
    void setHardLimit(long tokens, String period);

    // Agent confirmation
    void confirmAction(String actionId, boolean approved);

    // Task Manager
    String getActiveTasks();
    void cancelTask(String taskId);
}
```

- [ ] **Step 3: Create Android.bp for dollos-ai-aidl (AOSP)**

Create `packages/apps/DollOSAIService/Android.bp`:

```
java_library {
    name: "dollos-ai-aidl",
    srcs: [
        "aidl/**/*.aidl",
    ],
    aidl: {
        local_include_dirs: ["aidl"],
    },
    platform_apis: true,
}
```

This only builds the AIDL as a java_library. The actual DollOSAIService app is the prebuilt APK in `external/DollOSAIService/`.

- [ ] **Step 4: Copy AIDL files into Gradle project**

```bash
mkdir -p DollOSAIService/app/src/main/aidl/org/dollos/ai/
cp packages/apps/DollOSAIService/aidl/org/dollos/ai/IDollOSAICallback.aidl \
   DollOSAIService/app/src/main/aidl/org/dollos/ai/
cp packages/apps/DollOSAIService/aidl/org/dollos/ai/IDollOSAIService.aidl \
   DollOSAIService/app/src/main/aidl/org/dollos/ai/
```

The Gradle project compiles these AIDL files via `buildFeatures { aidl = true }` in `app/build.gradle.kts`.

- [ ] **Step 5: Obtain dollos-service-aidl.jar for Gradle project**

DollOSAIService needs to call `IDollOSService.executeSystemAction()` and `IDollOSService.getAvailableActions()`. The `dollos-service-aidl` library is built by Soong in AOSP.

Extract the JAR after building AOSP:

```bash
# After AOSP build, the JAR is at:
# out/soong/.intermediates/packages/apps/DollOSService/dollos-service-aidl/android_common/turbine-combined/dollos-service-aidl.jar
cp out/soong/.intermediates/packages/apps/DollOSService/dollos-service-aidl/android_common/turbine-combined/dollos-service-aidl.jar \
   DollOSAIService/app/libs/dollos-service-aidl.jar
```

This JAR is `compileOnly` -- it is not bundled into the APK because the actual classes are already in the system image from DollOSService.

- [ ] **Step 6: Commit AOSP AIDL**

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService
git add aidl/ Android.bp
git commit -m "feat: add dollos-ai-aidl java_library with IDollOSAIService and IDollOSAICallback"
```

- [ ] **Step 7: Commit Gradle AIDL copy**

```bash
cd DollOSAIService
git add app/src/main/aidl/ app/libs/
git commit -m "feat: add AIDL interface copies and dollos-service-aidl.jar for compilation"
```

---

## Task 3: Application + Service Skeleton

**Goal:** Create the Application class (SharedPreferences init, notification channel) and the Service class (Binder binding, DollOSService connection for agent delegation). Uses device-protected storage for SharedPreferences (learned from DollOSService issues with direct boot).

**Files:**
- Create: `DollOSAIService/app/src/main/java/org/dollos/ai/DollOSAIApp.kt`
- Create: `DollOSAIService/app/src/main/java/org/dollos/ai/DollOSAIService.kt`

- [ ] **Step 1: Create DollOSAIApp.kt**

Create `DollOSAIService/app/src/main/java/org/dollos/ai/DollOSAIApp.kt`:

```kotlin
package org.dollos.ai

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.SharedPreferences
import android.util.Log

class DollOSAIApp : Application() {

    companion object {
        private const val TAG = "DollOSAIApp"
        const val VERSION = "0.1.0"
        const val PREFS_NAME = "dollos_ai_config"
        const val NOTIFICATION_CHANNEL_ID = "dollos_ai_errors"

        lateinit var instance: DollOSAIApp
            private set
        lateinit var prefs: SharedPreferences
            private set
    }

    override fun onCreate() {
        super.onCreate()
        instance = this

        val deContext = createDeviceProtectedStorageContext()
        prefs = deContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        Log.i(TAG, "DollOS AI Application initialized, version $VERSION")
        Log.i(TAG, "SharedPreferences: ${prefs.all.size} entries")

        createNotificationChannel()
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            NOTIFICATION_CHANNEL_ID,
            "AI Service Errors",
            NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = "Notifications for API errors, rate limits, and budget alerts"
        }
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(channel)
        Log.i(TAG, "Notification channel created: $NOTIFICATION_CHANNEL_ID")
    }
}
```

- [ ] **Step 2: Create DollOSAIService.kt**

Create `DollOSAIService/app/src/main/java/org/dollos/ai/DollOSAIService.kt`:

```kotlin
package org.dollos.ai

import android.app.Service
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import android.util.Log
import org.dollos.service.IDollOSService

class DollOSAIService : Service() {

    companion object {
        private const val TAG = "DollOSAIService"
    }

    private lateinit var binder: DollOSAIServiceImpl
    var dollOSService: IDollOSService? = null
        private set
    private var isBoundToDollOS = false

    private val dollOSServiceConnection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, service: IBinder?) {
            dollOSService = IDollOSService.Stub.asInterface(service)
            Log.i(TAG, "Connected to DollOSService")
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            dollOSService = null
            Log.w(TAG, "Disconnected from DollOSService")
        }
    }

    override fun onCreate() {
        super.onCreate()
        binder = DollOSAIServiceImpl(this)
        bindToDollOSService()
        Log.i(TAG, "DollOS AI Service created")
    }

    override fun onBind(intent: Intent?): IBinder {
        Log.i(TAG, "DollOS AI Service bound")
        return binder
    }

    override fun onDestroy() {
        super.onDestroy()
        if (isBoundToDollOS) {
            unbindService(dollOSServiceConnection)
            isBoundToDollOS = false
        }
        Log.i(TAG, "DollOS AI Service destroyed")
    }

    private fun bindToDollOSService() {
        val intent = Intent("org.dollos.service.IDollOSService")
        intent.setPackage("org.dollos.service")
        isBoundToDollOS = bindService(intent, dollOSServiceConnection, Context.BIND_AUTO_CREATE)
        if (!isBoundToDollOS) {
            Log.e(TAG, "Failed to bind to DollOSService")
        }
    }
}
```

- [ ] **Step 3: Commit**

```bash
cd DollOSAIService
git add app/src/main/java/org/dollos/ai/DollOSAIApp.kt \
        app/src/main/java/org/dollos/ai/DollOSAIService.kt
git commit -m "feat: add DollOSAIApp and DollOSAIService entry point with DollOSService binding"
```

---

## Task 4: LLM Provider Abstraction

**Goal:** Define the LLM provider interface, provider enum with per-provider temperature clamping, response data classes (using kotlinx.serialization), streaming callback interface, and the LLMClient interface.

**Files:**
- Create: `DollOSAIService/app/src/main/java/org/dollos/ai/llm/LLMProvider.kt`
- Create: `DollOSAIService/app/src/main/java/org/dollos/ai/llm/LLMResponse.kt`
- Create: `DollOSAIService/app/src/main/java/org/dollos/ai/llm/StreamingCallback.kt`
- Create: `DollOSAIService/app/src/main/java/org/dollos/ai/llm/LLMClient.kt`

- [x] **Step 1: Create LLMProvider.kt**

Create `DollOSAIService/app/src/main/java/org/dollos/ai/llm/LLMProvider.kt`:

```kotlin
package org.dollos.ai.llm

import kotlinx.serialization.Serializable

enum class LLMProviderType(
    val displayName: String,
    val minTemperature: Float,
    val maxTemperature: Float
) {
    CLAUDE("Claude", 0.3f, 1.0f),
    GROK("Grok", 0.3f, 1.2f),
    OPENAI("OpenAI", 0.3f, 1.2f),
    CUSTOM("Custom", 0.0f, 2.0f);

    fun clampTemperature(dynamism: Float): Float {
        val temperature = minTemperature + (maxTemperature - minTemperature) * dynamism
        return temperature.coerceIn(minTemperature, maxTemperature)
    }

    companion object {
        fun fromString(name: String): LLMProviderType {
            return when (name.lowercase()) {
                "claude" -> CLAUDE
                "grok" -> GROK
                "openai" -> OPENAI
                "custom" -> CUSTOM
                else -> throw IllegalArgumentException("Unknown provider: $name")
            }
        }
    }
}

@Serializable
data class ModelConfig(
    val providerType: LLMProviderType,
    val apiKey: String,
    val model: String,
    val baseUrl: String = ""
)
```

- [x] **Step 2: Create LLMResponse.kt**

Create `DollOSAIService/app/src/main/java/org/dollos/ai/llm/LLMResponse.kt`:

```kotlin
package org.dollos.ai.llm

import kotlinx.serialization.Serializable

@Serializable
data class LLMResponse(
    val content: String,
    val inputTokens: Long,
    val outputTokens: Long,
    val model: String,
    val toolCalls: List<ToolCall> = emptyList(),
    val finishReason: String = "stop"
)

@Serializable
data class ToolCall(
    val id: String,
    val name: String,
    val arguments: String
)

@Serializable
data class LLMMessage(
    val role: String,
    val content: String
)

@Serializable
data class ToolDefinition(
    val name: String,
    val description: String,
    val parameters: String // JSON Schema string
)

class LLMException(val errorCode: String, message: String) : Exception(message)
```

- [x] **Step 3: Create StreamingCallback.kt**

Create `DollOSAIService/app/src/main/java/org/dollos/ai/llm/StreamingCallback.kt`:

```kotlin
package org.dollos.ai.llm

interface StreamingCallback {
    fun onToken(token: String)
    fun onComplete(response: LLMResponse)
    fun onError(errorCode: String, message: String)
}
```

- [x] **Step 4: Create LLMClient.kt**

Create `DollOSAIService/app/src/main/java/org/dollos/ai/llm/LLMClient.kt`:

```kotlin
package org.dollos.ai.llm

interface LLMClient {
    val providerType: LLMProviderType

    fun sendBlocking(
        messages: List<LLMMessage>,
        systemPrompt: String,
        temperature: Float,
        tools: List<ToolDefinition> = emptyList()
    ): LLMResponse

    fun sendStreaming(
        messages: List<LLMMessage>,
        systemPrompt: String,
        temperature: Float,
        callback: StreamingCallback,
        tools: List<ToolDefinition> = emptyList()
    )

    fun cancelCurrent()
}
```

- [x] **Step 5: Commit**

```bash
cd DollOSAIService
git add app/src/main/java/org/dollos/ai/llm/
git commit -m "feat: add LLM client interface, provider types, response models, and streaming callback"
```

---

## Task 5: SSE Streaming Parser

**Goal:** Implement a reusable SSE (Server-Sent Events) parser using OkHttp. This parser handles the `data:` line protocol, partial JSON assembly, connection drops, and delivers events to a callback. Used by all provider implementations.

**Files:**
- Create: `DollOSAIService/app/src/main/java/org/dollos/ai/llm/SSEParser.kt`

- [x] **Step 1: Create SSEParser.kt**

Create `DollOSAIService/app/src/main/java/org/dollos/ai/llm/SSEParser.kt`:

```kotlin
package org.dollos.ai.llm

import android.util.Log
import okhttp3.Response
import java.io.BufferedReader
import java.io.InputStreamReader

interface SSEEventHandler {
    fun onEvent(eventType: String, data: String)
    fun onDone()
    fun onError(error: Exception)
}

class SSEParser(private val handler: SSEEventHandler) {

    companion object {
        private const val TAG = "SSEParser"
    }

    fun parse(response: Response) {
        val body = response.body ?: run {
            handler.onError(LLMException("NO_BODY", "Response body is null"))
            return
        }

        val reader = BufferedReader(InputStreamReader(body.byteStream()))
        try {
            var currentEvent = ""
            var currentData = StringBuilder()

            var line: String?
            while (reader.readLine().also { line = it } != null) {
                val l = line ?: continue

                when {
                    l.startsWith("event: ") -> {
                        currentEvent = l.substring(7).trim()
                    }
                    l.startsWith("data: ") -> {
                        val data = l.substring(6)
                        if (data == "[DONE]") {
                            handler.onDone()
                            return
                        }
                        currentData.append(data)
                    }
                    l.isEmpty() -> {
                        // Empty line = end of event
                        if (currentData.isNotEmpty()) {
                            handler.onEvent(currentEvent, currentData.toString())
                            currentEvent = ""
                            currentData = StringBuilder()
                        }
                    }
                }
            }

            // Stream ended without [DONE] -- treat as done
            if (currentData.isNotEmpty()) {
                handler.onEvent(currentEvent, currentData.toString())
            }
            handler.onDone()
        } catch (e: Exception) {
            handler.onError(e)
        } finally {
            reader.close()
            body.close()
        }
    }
}
```

- [x] **Step 2: Commit**

```bash
cd DollOSAIService
git add app/src/main/java/org/dollos/ai/llm/SSEParser.kt
git commit -m "feat: add SSE streaming parser using OkHttp response body"
```

---

## Task 6: Provider Implementations

**Goal:** Implement concrete LLM providers for Claude, OpenAI, Grok, and Custom endpoints using OkHttp. Each provider handles its own API format, SSE streaming, tool call extraction, and retry with exponential backoff. Grok and Custom use OpenAI-compatible format.

**Files:**
- Create: `DollOSAIService/app/src/main/java/org/dollos/ai/llm/ClaudeProvider.kt`
- Create: `DollOSAIService/app/src/main/java/org/dollos/ai/llm/OpenAIProvider.kt`
- Create: `DollOSAIService/app/src/main/java/org/dollos/ai/llm/GrokProvider.kt`
- Create: `DollOSAIService/app/src/main/java/org/dollos/ai/llm/CustomProvider.kt`

- [x] **Step 1: Create ClaudeProvider.kt**

Create `DollOSAIService/app/src/main/java/org/dollos/ai/llm/ClaudeProvider.kt`:

```kotlin
package org.dollos.ai.llm

import android.util.Log
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

class ClaudeProvider(private val config: ModelConfig) : LLMClient {

    companion object {
        private const val TAG = "ClaudeProvider"
        private const val BASE_URL = "https://api.anthropic.com/v1/messages"
        private const val API_VERSION = "2023-06-01"
        private const val MAX_RETRIES = 3
        private val JSON_MEDIA_TYPE = "application/json".toMediaType()
    }

    override val providerType = LLMProviderType.CLAUDE
    private val cancelled = AtomicBoolean(false)
    private val json = Json { ignoreUnknownKeys = true }

    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()

    private var currentCall: okhttp3.Call? = null

    override fun sendBlocking(
        messages: List<LLMMessage>,
        systemPrompt: String,
        temperature: Float,
        tools: List<ToolDefinition>
    ): LLMResponse {
        cancelled.set(false)
        val body = buildRequestBody(messages, systemPrompt, temperature, tools, stream = false)
        return executeWithRetry { doBlockingRequest(body) }
    }

    override fun sendStreaming(
        messages: List<LLMMessage>,
        systemPrompt: String,
        temperature: Float,
        callback: StreamingCallback,
        tools: List<ToolDefinition>
    ) {
        cancelled.set(false)
        val body = buildRequestBody(messages, systemPrompt, temperature, tools, stream = true)

        val request = Request.Builder()
            .url(BASE_URL)
            .header("Content-Type", "application/json")
            .header("x-api-key", config.apiKey)
            .header("anthropic-version", API_VERSION)
            .post(body.toRequestBody(JSON_MEDIA_TYPE))
            .build()

        val call = httpClient.newCall(request)
        currentCall = call

        try {
            val response = call.execute()
            if (!response.isSuccessful) {
                val errorBody = response.body?.string() ?: "Unknown error"
                response.close()
                callback.onError("HTTP_${response.code}", errorBody)
                return
            }

            val fullContent = StringBuilder()
            var inputTokens = 0L
            var outputTokens = 0L
            val toolCalls = mutableListOf<ToolCall>()
            var currentToolId = ""
            var currentToolName = ""
            val currentToolArgs = StringBuilder()
            var inToolUse = false

            val parser = SSEParser(object : SSEEventHandler {
                override fun onEvent(eventType: String, data: String) {
                    if (cancelled.get()) return
                    try {
                        val event = Json.parseToJsonElement(data).jsonObject
                        val type = event["type"]?.jsonPrimitive?.content ?: ""

                        when (type) {
                            "message_start" -> {
                                val usage = event["message"]?.jsonObject?.get("usage")?.jsonObject
                                inputTokens = usage?.get("input_tokens")?.jsonPrimitive?.long ?: 0
                            }
                            "content_block_start" -> {
                                val block = event["content_block"]?.jsonObject
                                val blockType = block?.get("type")?.jsonPrimitive?.content ?: ""
                                if (blockType == "tool_use") {
                                    inToolUse = true
                                    currentToolId = block["id"]?.jsonPrimitive?.content ?: ""
                                    currentToolName = block["name"]?.jsonPrimitive?.content ?: ""
                                    currentToolArgs.clear()
                                }
                            }
                            "content_block_delta" -> {
                                val delta = event["delta"]?.jsonObject
                                val deltaType = delta?.get("type")?.jsonPrimitive?.content ?: ""
                                if (deltaType == "text_delta") {
                                    val text = delta["text"]?.jsonPrimitive?.content ?: ""
                                    if (text.isNotEmpty()) {
                                        fullContent.append(text)
                                        callback.onToken(text)
                                    }
                                } else if (deltaType == "input_json_delta") {
                                    val partial = delta["partial_json"]?.jsonPrimitive?.content ?: ""
                                    currentToolArgs.append(partial)
                                }
                            }
                            "content_block_stop" -> {
                                if (inToolUse) {
                                    toolCalls.add(ToolCall(
                                        id = currentToolId,
                                        name = currentToolName,
                                        arguments = currentToolArgs.toString()
                                    ))
                                    inToolUse = false
                                }
                            }
                            "message_delta" -> {
                                val usage = event["usage"]?.jsonObject
                                outputTokens = usage?.get("output_tokens")?.jsonPrimitive?.long ?: 0
                            }
                        }
                    } catch (e: Exception) {
                        Log.e(TAG, "Failed to parse SSE event", e)
                    }
                }

                override fun onDone() {
                    // Handled below after parser returns
                }

                override fun onError(error: Exception) {
                    if (!cancelled.get()) {
                        callback.onError("STREAM_ERROR", error.message ?: "Unknown streaming error")
                    }
                }
            })

            parser.parse(response)

            if (!cancelled.get()) {
                val llmResponse = LLMResponse(
                    content = fullContent.toString(),
                    inputTokens = inputTokens,
                    outputTokens = outputTokens,
                    model = config.model,
                    toolCalls = toolCalls
                )
                callback.onComplete(llmResponse)
            }
        } catch (e: Exception) {
            if (!cancelled.get()) {
                Log.e(TAG, "Streaming error", e)
                callback.onError("STREAM_ERROR", e.message ?: "Unknown streaming error")
            }
        } finally {
            currentCall = null
        }
    }

    override fun cancelCurrent() {
        cancelled.set(true)
        currentCall?.cancel()
    }

    private fun doBlockingRequest(body: String): LLMResponse {
        val request = Request.Builder()
            .url(BASE_URL)
            .header("Content-Type", "application/json")
            .header("x-api-key", config.apiKey)
            .header("anthropic-version", API_VERSION)
            .post(body.toRequestBody(JSON_MEDIA_TYPE))
            .build()

        val call = httpClient.newCall(request)
        currentCall = call

        try {
            val response = call.execute()
            if (!response.isSuccessful) {
                val errorBody = response.body?.string() ?: "Unknown error"
                response.close()
                throw LLMException("HTTP_${response.code}", errorBody)
            }

            val responseText = response.body?.string() ?: throw LLMException("NO_BODY", "Empty response")
            response.close()
            return parseBlockingResponse(responseText)
        } finally {
            currentCall = null
        }
    }

    private fun <T> executeWithRetry(block: () -> T): T {
        var lastException: Exception? = null
        for (attempt in 0 until MAX_RETRIES) {
            try {
                return block()
            } catch (e: LLMException) {
                lastException = e
                if (e.errorCode.startsWith("HTTP_4") && e.errorCode != "HTTP_429") {
                    throw e // Client error (except rate limit) -- do not retry
                }
                if (attempt < MAX_RETRIES - 1) {
                    val delayMs = (1000L * (1 shl attempt)) // Exponential backoff: 1s, 2s, 4s
                    Log.w(TAG, "Retry attempt ${attempt + 1} after ${delayMs}ms: ${e.errorCode}")
                    Thread.sleep(delayMs)
                }
            } catch (e: Exception) {
                lastException = e
                if (attempt < MAX_RETRIES - 1) {
                    val delayMs = (1000L * (1 shl attempt))
                    Log.w(TAG, "Retry attempt ${attempt + 1} after ${delayMs}ms: ${e.message}")
                    Thread.sleep(delayMs)
                }
            }
        }
        throw lastException ?: LLMException("RETRY_EXHAUSTED", "All retries failed")
    }

    private fun buildRequestBody(
        messages: List<LLMMessage>,
        systemPrompt: String,
        temperature: Float,
        tools: List<ToolDefinition>,
        stream: Boolean
    ): String {
        val body = buildJsonObject {
            put("model", config.model)
            put("max_tokens", 4096)
            put("temperature", temperature.toDouble())
            put("stream", stream)

            if (systemPrompt.isNotBlank()) {
                put("system", systemPrompt)
            }

            put("messages", buildJsonArray {
                for (msg in messages) {
                    add(buildJsonObject {
                        put("role", msg.role)
                        put("content", msg.content)
                    })
                }
            })

            if (tools.isNotEmpty()) {
                put("tools", buildJsonArray {
                    for (tool in tools) {
                        add(buildJsonObject {
                            put("name", tool.name)
                            put("description", tool.description)
                            put("input_schema", Json.parseToJsonElement(tool.parameters))
                        })
                    }
                })
            }
        }
        return body.toString()
    }

    private fun parseBlockingResponse(responseText: String): LLMResponse {
        val jsonObj = Json.parseToJsonElement(responseText).jsonObject
        val content = jsonObj["content"]?.jsonArray
        val textBuilder = StringBuilder()
        val toolCalls = mutableListOf<ToolCall>()

        content?.forEach { block ->
            val obj = block.jsonObject
            when (obj["type"]?.jsonPrimitive?.content) {
                "text" -> textBuilder.append(obj["text"]?.jsonPrimitive?.content ?: "")
                "tool_use" -> toolCalls.add(ToolCall(
                    id = obj["id"]?.jsonPrimitive?.content ?: "",
                    name = obj["name"]?.jsonPrimitive?.content ?: "",
                    arguments = obj["input"]?.toString() ?: "{}"
                ))
            }
        }

        val usage = jsonObj["usage"]?.jsonObject
        return LLMResponse(
            content = textBuilder.toString(),
            inputTokens = usage?.get("input_tokens")?.jsonPrimitive?.long ?: 0,
            outputTokens = usage?.get("output_tokens")?.jsonPrimitive?.long ?: 0,
            model = jsonObj["model"]?.jsonPrimitive?.content ?: config.model,
            toolCalls = toolCalls,
            finishReason = jsonObj["stop_reason"]?.jsonPrimitive?.content ?: "end_turn"
        )
    }
}
```

- [x] **Step 2: Create OpenAIProvider.kt**

Create `DollOSAIService/app/src/main/java/org/dollos/ai/llm/OpenAIProvider.kt`:

```kotlin
package org.dollos.ai.llm

import android.util.Log
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

open class OpenAIProvider(
    private val config: ModelConfig,
    private val baseUrl: String = "https://api.openai.com/v1/chat/completions"
) : LLMClient {

    companion object {
        private const val TAG = "OpenAIProvider"
        private const val MAX_RETRIES = 3
        private val JSON_MEDIA_TYPE = "application/json".toMediaType()
    }

    override val providerType = config.providerType
    private val cancelled = AtomicBoolean(false)

    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()

    private var currentCall: okhttp3.Call? = null

    override fun sendBlocking(
        messages: List<LLMMessage>,
        systemPrompt: String,
        temperature: Float,
        tools: List<ToolDefinition>
    ): LLMResponse {
        cancelled.set(false)
        val body = buildRequestBody(messages, systemPrompt, temperature, tools, stream = false)
        return executeWithRetry { doBlockingRequest(body) }
    }

    override fun sendStreaming(
        messages: List<LLMMessage>,
        systemPrompt: String,
        temperature: Float,
        callback: StreamingCallback,
        tools: List<ToolDefinition>
    ) {
        cancelled.set(false)
        val body = buildRequestBody(messages, systemPrompt, temperature, tools, stream = true)

        val request = Request.Builder()
            .url(baseUrl)
            .header("Content-Type", "application/json")
            .header("Authorization", "Bearer ${config.apiKey}")
            .post(body.toRequestBody(JSON_MEDIA_TYPE))
            .build()

        val call = httpClient.newCall(request)
        currentCall = call

        try {
            val response = call.execute()
            if (!response.isSuccessful) {
                val errorBody = response.body?.string() ?: "Unknown error"
                response.close()
                callback.onError("HTTP_${response.code}", errorBody)
                return
            }

            val fullContent = StringBuilder()
            var inputTokens = 0L
            var outputTokens = 0L
            val toolCalls = mutableListOf<ToolCall>()
            val toolCallArgs = mutableMapOf<Int, StringBuilder>()
            val toolCallIds = mutableMapOf<Int, String>()
            val toolCallNames = mutableMapOf<Int, String>()

            val parser = SSEParser(object : SSEEventHandler {
                override fun onEvent(eventType: String, data: String) {
                    if (cancelled.get()) return
                    try {
                        val chunk = Json.parseToJsonElement(data).jsonObject
                        val choices = chunk["choices"]?.jsonArray
                        if (choices != null && choices.isNotEmpty()) {
                            val delta = choices[0].jsonObject["delta"]?.jsonObject
                            if (delta != null) {
                                val content = delta["content"]?.jsonPrimitive?.content ?: ""
                                if (content.isNotEmpty()) {
                                    fullContent.append(content)
                                    callback.onToken(content)
                                }

                                val deltaToolCalls = delta["tool_calls"]?.jsonArray
                                deltaToolCalls?.forEach { tc ->
                                    val tcObj = tc.jsonObject
                                    val index = tcObj["index"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0
                                    val id = tcObj["id"]?.jsonPrimitive?.content ?: ""
                                    val function = tcObj["function"]?.jsonObject

                                    if (id.isNotEmpty()) toolCallIds[index] = id
                                    if (function != null) {
                                        val name = function["name"]?.jsonPrimitive?.content ?: ""
                                        if (name.isNotEmpty()) toolCallNames[index] = name
                                        val args = function["arguments"]?.jsonPrimitive?.content ?: ""
                                        if (args.isNotEmpty()) {
                                            toolCallArgs.getOrPut(index) { StringBuilder() }.append(args)
                                        }
                                    }
                                }
                            }
                        }

                        val usage = chunk["usage"]?.jsonObject
                        if (usage != null) {
                            inputTokens = usage["prompt_tokens"]?.jsonPrimitive?.long ?: inputTokens
                            outputTokens = usage["completion_tokens"]?.jsonPrimitive?.long ?: outputTokens
                        }
                    } catch (e: Exception) {
                        Log.e(TAG, "Failed to parse SSE chunk", e)
                    }
                }

                override fun onDone() {}
                override fun onError(error: Exception) {
                    if (!cancelled.get()) {
                        callback.onError("STREAM_ERROR", error.message ?: "Unknown streaming error")
                    }
                }
            })

            parser.parse(response)

            if (!cancelled.get()) {
                for ((index, id) in toolCallIds) {
                    toolCalls.add(ToolCall(
                        id = id,
                        name = toolCallNames[index] ?: "",
                        arguments = toolCallArgs[index]?.toString() ?: "{}"
                    ))
                }

                val llmResponse = LLMResponse(
                    content = fullContent.toString(),
                    inputTokens = inputTokens,
                    outputTokens = outputTokens,
                    model = config.model,
                    toolCalls = toolCalls
                )
                callback.onComplete(llmResponse)
            }
        } catch (e: Exception) {
            if (!cancelled.get()) {
                Log.e(TAG, "Streaming error", e)
                callback.onError("STREAM_ERROR", e.message ?: "Unknown streaming error")
            }
        } finally {
            currentCall = null
        }
    }

    override fun cancelCurrent() {
        cancelled.set(true)
        currentCall?.cancel()
    }

    private fun doBlockingRequest(body: String): LLMResponse {
        val request = Request.Builder()
            .url(baseUrl)
            .header("Content-Type", "application/json")
            .header("Authorization", "Bearer ${config.apiKey}")
            .post(body.toRequestBody(JSON_MEDIA_TYPE))
            .build()

        val call = httpClient.newCall(request)
        currentCall = call

        try {
            val response = call.execute()
            if (!response.isSuccessful) {
                val errorBody = response.body?.string() ?: "Unknown error"
                response.close()
                throw LLMException("HTTP_${response.code}", errorBody)
            }

            val responseText = response.body?.string() ?: throw LLMException("NO_BODY", "Empty response")
            response.close()
            return parseBlockingResponse(responseText)
        } finally {
            currentCall = null
        }
    }

    private fun <T> executeWithRetry(block: () -> T): T {
        var lastException: Exception? = null
        for (attempt in 0 until MAX_RETRIES) {
            try {
                return block()
            } catch (e: LLMException) {
                lastException = e
                if (e.errorCode.startsWith("HTTP_4") && e.errorCode != "HTTP_429") {
                    throw e
                }
                if (attempt < MAX_RETRIES - 1) {
                    val delayMs = (1000L * (1 shl attempt))
                    Log.w(TAG, "Retry attempt ${attempt + 1} after ${delayMs}ms: ${e.errorCode}")
                    Thread.sleep(delayMs)
                }
            } catch (e: Exception) {
                lastException = e
                if (attempt < MAX_RETRIES - 1) {
                    val delayMs = (1000L * (1 shl attempt))
                    Log.w(TAG, "Retry attempt ${attempt + 1} after ${delayMs}ms: ${e.message}")
                    Thread.sleep(delayMs)
                }
            }
        }
        throw lastException ?: LLMException("RETRY_EXHAUSTED", "All retries failed")
    }

    private fun buildRequestBody(
        messages: List<LLMMessage>,
        systemPrompt: String,
        temperature: Float,
        tools: List<ToolDefinition>,
        stream: Boolean
    ): String {
        val body = buildJsonObject {
            put("model", config.model)
            put("temperature", temperature.toDouble())
            put("stream", stream)
            if (stream) {
                put("stream_options", buildJsonObject { put("include_usage", true) })
            }

            put("messages", buildJsonArray {
                if (systemPrompt.isNotBlank()) {
                    add(buildJsonObject {
                        put("role", "system")
                        put("content", systemPrompt)
                    })
                }
                for (msg in messages) {
                    add(buildJsonObject {
                        put("role", msg.role)
                        put("content", msg.content)
                    })
                }
            })

            if (tools.isNotEmpty()) {
                put("tools", buildJsonArray {
                    for (tool in tools) {
                        add(buildJsonObject {
                            put("type", "function")
                            put("function", buildJsonObject {
                                put("name", tool.name)
                                put("description", tool.description)
                                put("parameters", Json.parseToJsonElement(tool.parameters))
                            })
                        })
                    }
                })
            }
        }
        return body.toString()
    }

    private fun parseBlockingResponse(responseText: String): LLMResponse {
        val jsonObj = Json.parseToJsonElement(responseText).jsonObject
        val choices = jsonObj["choices"]?.jsonArray
        val message = choices?.get(0)?.jsonObject?.get("message")?.jsonObject
        val content = message?.get("content")?.jsonPrimitive?.content ?: ""

        val toolCalls = mutableListOf<ToolCall>()
        message?.get("tool_calls")?.jsonArray?.forEach { tc ->
            val tcObj = tc.jsonObject
            val function = tcObj["function"]?.jsonObject
            toolCalls.add(ToolCall(
                id = tcObj["id"]?.jsonPrimitive?.content ?: "",
                name = function?.get("name")?.jsonPrimitive?.content ?: "",
                arguments = function?.get("arguments")?.jsonPrimitive?.content ?: "{}"
            ))
        }

        val usage = jsonObj["usage"]?.jsonObject
        return LLMResponse(
            content = content,
            inputTokens = usage?.get("prompt_tokens")?.jsonPrimitive?.long ?: 0,
            outputTokens = usage?.get("completion_tokens")?.jsonPrimitive?.long ?: 0,
            model = jsonObj["model"]?.jsonPrimitive?.content ?: config.model,
            toolCalls = toolCalls,
            finishReason = choices?.get(0)?.jsonObject?.get("finish_reason")?.jsonPrimitive?.content ?: "stop"
        )
    }
}
```

- [x] **Step 3: Create GrokProvider.kt**

Grok uses OpenAI-compatible API format with a different base URL. Extends OpenAIProvider.

Create `DollOSAIService/app/src/main/java/org/dollos/ai/llm/GrokProvider.kt`:

```kotlin
package org.dollos.ai.llm

class GrokProvider(config: ModelConfig) : OpenAIProvider(
    config = config,
    baseUrl = "https://api.x.ai/v1/chat/completions"
)
```

- [x] **Step 4: Create CustomProvider.kt**

Custom provider uses OpenAI-compatible format with user-configurable base URL.

Create `DollOSAIService/app/src/main/java/org/dollos/ai/llm/CustomProvider.kt`:

```kotlin
package org.dollos.ai.llm

class CustomProvider(config: ModelConfig) : OpenAIProvider(
    config = config,
    baseUrl = if (config.baseUrl.isNotBlank()) {
        config.baseUrl.trimEnd('/') + "/chat/completions"
    } else {
        throw LLMException("CONFIG_ERROR", "Custom provider requires a base URL")
    }
)
```

- [x] **Step 5: Commit**

```bash
cd DollOSAIService
git add app/src/main/java/org/dollos/ai/llm/
git commit -m "feat: add Claude, OpenAI, Grok, and Custom LLM provider implementations with OkHttp"
```

---

## Task 7: Personality System

**Goal:** Implement the 5-field personality system (backstory, response directive, dynamism, address, language preference) with SharedPreferences persistence and system prompt construction.

**Files:**
- Create: `DollOSAIService/app/src/main/java/org/dollos/ai/personality/PersonalityManager.kt`
- Create: `DollOSAIService/app/src/main/java/org/dollos/ai/personality/SystemPromptBuilder.kt`

- [x] **Step 1: Create PersonalityManager.kt**

Create `DollOSAIService/app/src/main/java/org/dollos/ai/personality/PersonalityManager.kt`:

```kotlin
package org.dollos.ai.personality

import android.content.SharedPreferences
import android.util.Log

class PersonalityManager(private val prefs: SharedPreferences) {

    companion object {
        private const val TAG = "PersonalityManager"
        private const val KEY_BACKSTORY = "personality_backstory"
        private const val KEY_RESPONSE_DIRECTIVE = "personality_response_directive"
        private const val KEY_DYNAMISM = "personality_dynamism"
        private const val KEY_ADDRESS = "personality_address"
        private const val KEY_LANGUAGE_PREFERENCE = "personality_language_preference"

        const val BACKSTORY_MAX_LENGTH = 2500
        const val RESPONSE_DIRECTIVE_MAX_LENGTH = 150
        const val ADDRESS_MAX_LENGTH = 50
    }

    var backstory: String
        get() = prefs.getString(KEY_BACKSTORY, "") ?: ""
        set(value) {
            val trimmed = value.take(BACKSTORY_MAX_LENGTH)
            prefs.edit().putString(KEY_BACKSTORY, trimmed).apply()
            Log.i(TAG, "Backstory updated (${trimmed.length} chars)")
        }

    var responseDirective: String
        get() = prefs.getString(KEY_RESPONSE_DIRECTIVE, "") ?: ""
        set(value) {
            val trimmed = value.take(RESPONSE_DIRECTIVE_MAX_LENGTH)
            prefs.edit().putString(KEY_RESPONSE_DIRECTIVE, trimmed).apply()
            Log.i(TAG, "Response directive updated (${trimmed.length} chars)")
        }

    var dynamism: Float
        get() = prefs.getFloat(KEY_DYNAMISM, 0.5f)
        set(value) {
            val clamped = value.coerceIn(0.0f, 1.0f)
            prefs.edit().putFloat(KEY_DYNAMISM, clamped).apply()
            Log.i(TAG, "Dynamism updated: $clamped")
        }

    var address: String
        get() = prefs.getString(KEY_ADDRESS, "") ?: ""
        set(value) {
            val trimmed = value.take(ADDRESS_MAX_LENGTH)
            prefs.edit().putString(KEY_ADDRESS, trimmed).apply()
            Log.i(TAG, "Address updated: $trimmed")
        }

    var languagePreference: String
        get() = prefs.getString(KEY_LANGUAGE_PREFERENCE, "") ?: ""
        set(value) {
            prefs.edit().putString(KEY_LANGUAGE_PREFERENCE, value).apply()
            Log.i(TAG, "Language preference updated: $value")
        }

    fun isConfigured(): Boolean {
        return backstory.isNotBlank() || responseDirective.isNotBlank()
    }
}
```

- [x] **Step 2: Create SystemPromptBuilder.kt**

Create `DollOSAIService/app/src/main/java/org/dollos/ai/personality/SystemPromptBuilder.kt`:

```kotlin
package org.dollos.ai.personality

class SystemPromptBuilder(private val personalityManager: PersonalityManager) {

    fun build(): String {
        val sections = mutableListOf<String>()

        val directive = personalityManager.responseDirective
        if (directive.isNotBlank()) {
            sections.add("[Response Directive]\n$directive")
        }

        val language = personalityManager.languagePreference
        if (language.isNotBlank()) {
            sections.add("[Language Preference]\nRespond in $language.")
        }

        val address = personalityManager.address
        if (address.isNotBlank()) {
            sections.add("[How to Address User]\nAddress the user as \"$address\".")
        }

        val backstory = personalityManager.backstory
        if (backstory.isNotBlank()) {
            sections.add("[Backstory]\n$backstory")
        }

        val dynamism = personalityManager.dynamism
        val creativityNote = when {
            dynamism < 0.3f -> "Be consistent and predictable in your responses. Avoid creative embellishments."
            dynamism < 0.7f -> "Balance consistency with occasional creative expression."
            else -> "Be creative and expressive. Vary your responses and show personality."
        }
        sections.add("[Creativity Level]\n$creativityNote")

        return sections.joinToString("\n\n")
    }

    fun buildWithMemory(tier1Memory: String, tier2Memory: String, searchResults: String): String {
        val sections = mutableListOf<String>()

        sections.add(build())

        if (tier1Memory.isNotBlank()) {
            sections.add("[Core Memories]\n$tier1Memory")
        }

        if (tier2Memory.isNotBlank()) {
            sections.add("[Recent Memories]\n$tier2Memory")
        }

        if (searchResults.isNotBlank()) {
            sections.add("[Relevant Memories]\n$searchResults")
        }

        return sections.joinToString("\n\n")
    }
}
```

- [x] **Step 3: Commit**

```bash
cd DollOSAIService
git add app/src/main/java/org/dollos/ai/personality/
git commit -m "feat: add PersonalityManager with 5-field config and SystemPromptBuilder"
```

---

## Task 8: Usage Tracking and Budget Controls

**Goal:** Implement token usage tracking with per-call recording, and budget controls with warning threshold and hard limit (daily/monthly periods). Budget violations trigger Android system notifications and can block API calls.

**Files:**
- Create: `DollOSAIService/app/src/main/java/org/dollos/ai/usage/UsageTracker.kt`
- Create: `DollOSAIService/app/src/main/java/org/dollos/ai/usage/BudgetManager.kt`

- [x] **Step 1: Create UsageTracker.kt**

Create `DollOSAIService/app/src/main/java/org/dollos/ai/usage/UsageTracker.kt`:

```kotlin
package org.dollos.ai.usage

import android.content.SharedPreferences
import android.util.Log
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

@Serializable
data class UsageRecord(
    val timestamp: Long,
    val inputTokens: Long,
    val outputTokens: Long,
    val model: String,
    val provider: String,
    val function: String,
    val isForeground: Boolean
) {
    val totalTokens: Long get() = inputTokens + outputTokens
}

class UsageTracker(private val prefs: SharedPreferences) {

    companion object {
        private const val TAG = "UsageTracker"
        private const val KEY_USAGE_RECORDS = "usage_records"
        private const val MAX_RECORDS = 10000
    }

    private val json = Json { ignoreUnknownKeys = true }
    private val records = mutableListOf<UsageRecord>()
    private val lock = Any()

    init {
        loadRecords()
    }

    fun record(
        inputTokens: Long,
        outputTokens: Long,
        model: String,
        provider: String,
        function: String,
        isForeground: Boolean
    ) {
        synchronized(lock) {
            val record = UsageRecord(
                timestamp = System.currentTimeMillis(),
                inputTokens = inputTokens,
                outputTokens = outputTokens,
                model = model,
                provider = provider,
                function = function,
                isForeground = isForeground
            )
            records.add(record)

            if (records.size > MAX_RECORDS) {
                records.removeAt(0)
            }

            saveRecords()
            Log.i(TAG, "Usage recorded: ${record.totalTokens} tokens ($provider/$model, $function)")
        }
    }

    fun getTotalTokensForPeriod(period: String): Long {
        synchronized(lock) {
            val cutoff = getPeriodCutoff(period)
            return records.filter { it.timestamp >= cutoff }.sumOf { it.totalTokens }
        }
    }

    fun getStatsJson(): String {
        synchronized(lock) {
            val dailyCutoff = getPeriodCutoff("daily")
            val monthlyCutoff = getPeriodCutoff("monthly")

            val dailyRecords = records.filter { it.timestamp >= dailyCutoff }
            val monthlyRecords = records.filter { it.timestamp >= monthlyCutoff }

            return buildString {
                append("{")
                append("\"daily\":{")
                append("\"totalTokens\":${dailyRecords.sumOf { it.totalTokens }},")
                append("\"inputTokens\":${dailyRecords.sumOf { it.inputTokens }},")
                append("\"outputTokens\":${dailyRecords.sumOf { it.outputTokens }},")
                append("\"callCount\":${dailyRecords.size}")
                append("},")
                append("\"monthly\":{")
                append("\"totalTokens\":${monthlyRecords.sumOf { it.totalTokens }},")
                append("\"inputTokens\":${monthlyRecords.sumOf { it.inputTokens }},")
                append("\"outputTokens\":${monthlyRecords.sumOf { it.outputTokens }},")
                append("\"callCount\":${monthlyRecords.size}")
                append("},")
                append("\"allTime\":{")
                append("\"totalTokens\":${records.sumOf { it.totalTokens }},")
                append("\"inputTokens\":${records.sumOf { it.inputTokens }},")
                append("\"outputTokens\":${records.sumOf { it.outputTokens }},")
                append("\"callCount\":${records.size}")
                append("}")
                append("}")
            }
        }
    }

    private fun getPeriodCutoff(period: String): Long {
        val cal = java.util.Calendar.getInstance()
        when (period) {
            "daily" -> {
                cal.set(java.util.Calendar.HOUR_OF_DAY, 0)
                cal.set(java.util.Calendar.MINUTE, 0)
                cal.set(java.util.Calendar.SECOND, 0)
                cal.set(java.util.Calendar.MILLISECOND, 0)
            }
            "monthly" -> {
                cal.set(java.util.Calendar.DAY_OF_MONTH, 1)
                cal.set(java.util.Calendar.HOUR_OF_DAY, 0)
                cal.set(java.util.Calendar.MINUTE, 0)
                cal.set(java.util.Calendar.SECOND, 0)
                cal.set(java.util.Calendar.MILLISECOND, 0)
            }
        }
        return cal.timeInMillis
    }

    private fun loadRecords() {
        val data = prefs.getString(KEY_USAGE_RECORDS, null) ?: return
        try {
            val loaded: List<UsageRecord> = json.decodeFromString(data)
            records.clear()
            records.addAll(loaded)
            Log.i(TAG, "Loaded ${records.size} usage records")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to load usage records", e)
        }
    }

    private fun saveRecords() {
        val data = json.encodeToString(records.toList())
        prefs.edit().putString(KEY_USAGE_RECORDS, data).apply()
    }
}
```

- [x] **Step 2: Create BudgetManager.kt**

Create `DollOSAIService/app/src/main/java/org/dollos/ai/usage/BudgetManager.kt`:

```kotlin
package org.dollos.ai.usage

import android.app.NotificationManager
import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import androidx.core.app.NotificationCompat
import org.dollos.ai.DollOSAIApp

class BudgetManager(
    private val prefs: SharedPreferences,
    private val usageTracker: UsageTracker,
    private val context: Context
) {

    companion object {
        private const val TAG = "BudgetManager"
        private const val KEY_WARNING_TOKENS = "budget_warning_tokens"
        private const val KEY_WARNING_PERIOD = "budget_warning_period"
        private const val KEY_HARD_LIMIT_TOKENS = "budget_hard_limit_tokens"
        private const val KEY_HARD_LIMIT_PERIOD = "budget_hard_limit_period"
        private const val KEY_WARNING_NOTIFIED = "budget_warning_notified"
        private const val NOTIFICATION_ID_WARNING = 1001
        private const val NOTIFICATION_ID_HARD_LIMIT = 1002
    }

    var warningTokens: Long
        get() = prefs.getLong(KEY_WARNING_TOKENS, 0)
        private set(value) { prefs.edit().putLong(KEY_WARNING_TOKENS, value).apply() }

    var warningPeriod: String
        get() = prefs.getString(KEY_WARNING_PERIOD, "daily") ?: "daily"
        private set(value) { prefs.edit().putString(KEY_WARNING_PERIOD, value).apply() }

    var hardLimitTokens: Long
        get() = prefs.getLong(KEY_HARD_LIMIT_TOKENS, 0)
        private set(value) { prefs.edit().putLong(KEY_HARD_LIMIT_TOKENS, value).apply() }

    var hardLimitPeriod: String
        get() = prefs.getString(KEY_HARD_LIMIT_PERIOD, "daily") ?: "daily"
        private set(value) { prefs.edit().putString(KEY_HARD_LIMIT_PERIOD, value).apply() }

    fun setWarningThreshold(tokens: Long, period: String) {
        warningTokens = tokens
        warningPeriod = period
        prefs.edit().putBoolean(KEY_WARNING_NOTIFIED, false).apply()
        Log.i(TAG, "Warning threshold set: $tokens tokens per $period")
    }

    fun setHardLimit(tokens: Long, period: String) {
        hardLimitTokens = tokens
        hardLimitPeriod = period
        Log.i(TAG, "Hard limit set: $tokens tokens per $period")
    }

    fun isHardLimitReached(): Boolean {
        if (hardLimitTokens <= 0) return false
        val used = usageTracker.getTotalTokensForPeriod(hardLimitPeriod)
        return used >= hardLimitTokens
    }

    fun checkBudgetAfterCall() {
        checkWarning()
        checkHardLimit()
    }

    private fun checkWarning() {
        if (warningTokens <= 0) return

        val used = usageTracker.getTotalTokensForPeriod(warningPeriod)
        val alreadyNotified = prefs.getBoolean(KEY_WARNING_NOTIFIED, false)

        if (used >= warningTokens && !alreadyNotified) {
            prefs.edit().putBoolean(KEY_WARNING_NOTIFIED, true).apply()
            sendNotification(
                NOTIFICATION_ID_WARNING,
                "AI Usage Warning",
                "You have used $used tokens this $warningPeriod (threshold: $warningTokens)."
            )
            Log.w(TAG, "Warning threshold reached: $used / $warningTokens ($warningPeriod)")
        }
    }

    private fun checkHardLimit() {
        if (hardLimitTokens <= 0) return

        val used = usageTracker.getTotalTokensForPeriod(hardLimitPeriod)
        if (used >= hardLimitTokens) {
            sendNotification(
                NOTIFICATION_ID_HARD_LIMIT,
                "AI Usage Limit Reached",
                "All AI functions stopped. Used $used tokens this $hardLimitPeriod (limit: $hardLimitTokens)."
            )
            Log.w(TAG, "Hard limit reached: $used / $hardLimitTokens ($hardLimitPeriod)")
        }
    }

    private fun sendNotification(id: Int, title: String, text: String) {
        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val notification = NotificationCompat.Builder(context, DollOSAIApp.NOTIFICATION_CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_dialog_alert)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .build()
        nm.notify(id, notification)
    }
}
```

- [x] **Step 3: Commit**

```bash
cd DollOSAIService
git add app/src/main/java/org/dollos/ai/usage/
git commit -m "feat: add UsageTracker and BudgetManager with warning/hard limit controls"
```

---

## Task 9: AIDL Service Implementation

**Goal:** Implement the full `IDollOSAIService.Stub` with all AIDL methods. This ties together the LLM client, personality manager, usage tracker, budget manager, and DollOSService delegation. Includes pauseAll() with 3s synchronous blocking, callback management, tool call handling with confirmation flow (pending map, 60s timeout), and error notification.

**Files:**
- Create: `DollOSAIService/app/src/main/java/org/dollos/ai/DollOSAIServiceImpl.kt`

- [x] **Step 1: Create DollOSAIServiceImpl.kt**

Create `DollOSAIService/app/src/main/java/org/dollos/ai/DollOSAIServiceImpl.kt`:

```kotlin
package org.dollos.ai

import android.app.NotificationManager
import android.content.Context
import android.os.ParcelFileDescriptor
import android.os.RemoteCallbackList
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.dollos.ai.llm.ClaudeProvider
import org.dollos.ai.llm.CustomProvider
import org.dollos.ai.llm.GrokProvider
import org.dollos.ai.llm.LLMClient
import org.dollos.ai.llm.LLMException
import org.dollos.ai.llm.LLMMessage
import org.dollos.ai.llm.LLMProviderType
import org.dollos.ai.llm.LLMResponse
import org.dollos.ai.llm.ModelConfig
import org.dollos.ai.llm.OpenAIProvider
import org.dollos.ai.llm.StreamingCallback
import org.dollos.ai.llm.ToolCall
import org.dollos.ai.llm.ToolDefinition
import org.dollos.ai.personality.PersonalityManager
import org.dollos.ai.personality.SystemPromptBuilder
import org.dollos.ai.usage.BudgetManager
import org.dollos.ai.usage.UsageTracker
import org.dollos.service.IDollOSService
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

class DollOSAIServiceImpl(private val service: DollOSAIService) : IDollOSAIService.Stub() {

    companion object {
        private const val TAG = "DollOSAIServiceImpl"
        private const val KEY_FG_PROVIDER = "fg_provider"
        private const val KEY_FG_API_KEY = "fg_api_key"
        private const val KEY_FG_MODEL = "fg_model"
        private const val KEY_FG_BASE_URL = "fg_base_url"
        private const val KEY_BG_PROVIDER = "bg_provider"
        private const val KEY_BG_API_KEY = "bg_api_key"
        private const val KEY_BG_MODEL = "bg_model"
        private const val KEY_BG_BASE_URL = "bg_base_url"
        private const val PAUSE_TIMEOUT_MS = 3000L
        private const val CONFIRM_TIMEOUT_MS = 60_000L
    }

    private val callbacks = RemoteCallbackList<IDollOSAICallback>()
    private val executor = Executors.newCachedThreadPool()
    private val personalityManager = PersonalityManager(DollOSAIApp.prefs)
    private val systemPromptBuilder = SystemPromptBuilder(personalityManager)
    private val usageTracker = UsageTracker(DollOSAIApp.prefs)
    private val budgetManager = BudgetManager(DollOSAIApp.prefs, usageTracker, DollOSAIApp.instance)

    private var foregroundClient: LLMClient? = null
    private var backgroundClient: LLMClient? = null
    private val paused = AtomicBoolean(false)
    private val activeTasks = ConcurrentHashMap<String, TaskInfo>()

    data class TaskInfo(
        val id: String,
        val name: String,
        val description: String,
        val startTime: Long,
        var tokenUsage: Long = 0,
        var status: String = "RUNNING"
    )

    private data class PendingAction(
        val toolCall: ToolCall,
        val timeoutRunnable: Runnable
    )
    private val pendingConfirmations = ConcurrentHashMap<String, PendingAction>()
    private val confirmTimeoutHandler = android.os.Handler(android.os.Looper.getMainLooper())

    init {
        initForegroundClient()
        initBackgroundClient()
    }

    // --- Callback registration ---

    override fun registerCallback(callback: IDollOSAICallback?) {
        if (callback != null) {
            callbacks.register(callback)
            Log.i(TAG, "Callback registered")
        }
    }

    override fun unregisterCallback(callback: IDollOSAICallback?) {
        if (callback != null) {
            callbacks.unregister(callback)
            Log.i(TAG, "Callback unregistered")
        }
    }

    // --- Conversation ---

    override fun sendMessage(message: String?) {
        if (message.isNullOrBlank()) return

        if (paused.get()) {
            broadcastError("PAUSED", "AI is paused")
            return
        }

        if (budgetManager.isHardLimitReached()) {
            broadcastError("BUDGET_LIMIT", "Token budget limit reached")
            return
        }

        val client = foregroundClient
        if (client == null) {
            broadcastError("NOT_CONFIGURED", "No foreground model configured")
            return
        }

        val taskId = UUID.randomUUID().toString()
        val task = TaskInfo(
            id = taskId,
            name = "Conversation",
            description = "Processing: ${message.take(50)}",
            startTime = System.currentTimeMillis()
        )
        activeTasks[taskId] = task

        executor.submit {
            try {
                val systemPrompt = systemPromptBuilder.build()
                val temperature = client.providerType.clampTemperature(personalityManager.dynamism)
                val messages = listOf(LLMMessage(role = "user", content = message))

                val tools = loadToolDefinitions()

                client.sendStreaming(messages, systemPrompt, temperature, object : StreamingCallback {
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
            } catch (e: LLMException) {
                activeTasks.remove(taskId)
                sendErrorNotification(e.errorCode, e.message ?: "Unknown error")
                broadcastError(e.errorCode, e.message ?: "Unknown error")
            } catch (e: Exception) {
                activeTasks.remove(taskId)
                sendErrorNotification("UNKNOWN", e.message ?: "Unknown error")
                broadcastError("UNKNOWN", e.message ?: "Unknown error")
            }
        }
    }

    override fun stopGeneration() {
        foregroundClient?.cancelCurrent()
        backgroundClient?.cancelCurrent()
        Log.i(TAG, "Generation stopped")
    }

    override fun pauseAll(): Boolean {
        Log.i(TAG, "pauseAll() called")
        paused.set(true)

        foregroundClient?.cancelCurrent()
        backgroundClient?.cancelCurrent()

        val latch = CountDownLatch(1)

        executor.submit {
            for (task in activeTasks.values) {
                task.status = "PAUSED"
            }
            latch.countDown()
        }

        val completed = latch.await(PAUSE_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        Log.i(TAG, "pauseAll() completed: $completed, active tasks: ${activeTasks.size}")
        return completed
    }

    override fun resumeAll() {
        Log.i(TAG, "resumeAll() called")
        paused.set(false)
        for (task in activeTasks.values) {
            task.status = "RUNNING"
        }
    }

    // --- Personality ---

    override fun setBackstory(backstory: String?) {
        personalityManager.backstory = backstory ?: ""
    }

    override fun setResponseDirective(directive: String?) {
        personalityManager.responseDirective = directive ?: ""
    }

    override fun setDynamism(value: Float) {
        personalityManager.dynamism = value
    }

    override fun setAddress(address: String?) {
        personalityManager.address = address ?: ""
    }

    override fun setLanguagePreference(language: String?) {
        personalityManager.languagePreference = language ?: ""
    }

    override fun getBackstory(): String = personalityManager.backstory

    override fun getResponseDirective(): String = personalityManager.responseDirective

    override fun getDynamism(): Float = personalityManager.dynamism

    override fun getAddress(): String = personalityManager.address

    override fun getLanguagePreference(): String = personalityManager.languagePreference

    // --- API Configuration ---

    override fun setForegroundModel(provider: String?, apiKey: String?, model: String?) {
        if (provider == null || apiKey == null || model == null) return
        DollOSAIApp.prefs.edit()
            .putString(KEY_FG_PROVIDER, provider)
            .putString(KEY_FG_API_KEY, apiKey)
            .putString(KEY_FG_MODEL, model)
            .apply()
        initForegroundClient()
        Log.i(TAG, "Foreground model set: $provider / $model")
    }

    override fun setBackgroundModel(provider: String?, apiKey: String?, model: String?) {
        if (provider == null || apiKey == null || model == null) return
        DollOSAIApp.prefs.edit()
            .putString(KEY_BG_PROVIDER, provider)
            .putString(KEY_BG_API_KEY, apiKey)
            .putString(KEY_BG_MODEL, model)
            .apply()
        initBackgroundClient()
        Log.i(TAG, "Background model set: $provider / $model")
    }

    // --- Embedding Configuration ---

    override fun setEmbeddingSource(source: String?) {
        DollOSAIApp.prefs.edit().putString("embedding_source", source ?: "local").apply()
        Log.i(TAG, "Embedding source set: $source")
    }

    override fun getEmbeddingSource(): String {
        return DollOSAIApp.prefs.getString("embedding_source", "local") ?: "local"
    }

    // --- Memory (stubs for Plan B) ---

    override fun searchMemory(query: String?): String {
        // Plan B will implement full memory search
        return "[]"
    }

    override fun exportMemory(fd: ParcelFileDescriptor?) {
        // Plan B will implement memory export
        Log.i(TAG, "exportMemory called (not yet implemented, see Plan B)")
    }

    override fun importMemory(fd: ParcelFileDescriptor?) {
        // Plan B will implement memory import
        Log.i(TAG, "importMemory called (not yet implemented, see Plan B)")
    }

    override fun confirmMemoryWrite(approved: Boolean) {
        // Plan B will implement memory confirmation
        Log.i(TAG, "confirmMemoryWrite called: $approved (not yet implemented, see Plan B)")
    }

    // --- Usage ---

    override fun getUsageStats(): String {
        return usageTracker.getStatsJson()
    }

    override fun setWarningThreshold(tokens: Long, period: String?) {
        budgetManager.setWarningThreshold(tokens, period ?: "daily")
    }

    override fun setHardLimit(tokens: Long, period: String?) {
        budgetManager.setHardLimit(tokens, period ?: "daily")
    }

    // --- Agent confirmation ---

    override fun confirmAction(actionId: String?, approved: Boolean) {
        if (actionId == null) return

        val pending = pendingConfirmations.remove(actionId)
        if (pending == null) {
            Log.w(TAG, "confirmAction called for unknown or expired actionId: $actionId")
            return
        }
        confirmTimeoutHandler.removeCallbacks(pending.timeoutRunnable)

        if (approved) {
            val dollOSService = service.dollOSService
            if (dollOSService == null) {
                broadcastActionExecuted(actionId, false, "DollOSService not connected")
                return
            }
            executor.submit {
                executeToolCall(dollOSService, pending.toolCall)
            }
        } else {
            broadcastActionExecuted(actionId, false, "Action denied by user")
            Log.i(TAG, "Action denied by user: ${pending.toolCall.name}")
        }
    }

    // --- Task Manager ---

    override fun getActiveTasks(): String {
        val tasks = activeTasks.values.map { task ->
            buildString {
                append("{")
                append("\"id\":\"${task.id}\",")
                append("\"name\":\"${task.name}\",")
                append("\"description\":\"${task.description}\",")
                append("\"startTime\":${task.startTime},")
                append("\"tokenUsage\":${task.tokenUsage},")
                append("\"estimatedCost\":0.0,")
                append("\"conversationContext\":\"\",")
                append("\"status\":\"${task.status}\"")
                append("}")
            }
        }
        return "[${tasks.joinToString(",")}]"
    }

    override fun cancelTask(taskId: String?) {
        if (taskId == null) return
        activeTasks.remove(taskId)
        Log.i(TAG, "Task cancelled: $taskId")
    }

    // --- Internal helpers ---

    private fun initForegroundClient() {
        val provider = DollOSAIApp.prefs.getString(KEY_FG_PROVIDER, null) ?: return
        val apiKey = DollOSAIApp.prefs.getString(KEY_FG_API_KEY, null) ?: return
        val model = DollOSAIApp.prefs.getString(KEY_FG_MODEL, null) ?: return
        val baseUrl = DollOSAIApp.prefs.getString(KEY_FG_BASE_URL, "") ?: ""

        foregroundClient = createClient(provider, apiKey, model, baseUrl)
        Log.i(TAG, "Foreground client initialized: $provider / $model")
    }

    private fun initBackgroundClient() {
        val provider = DollOSAIApp.prefs.getString(KEY_BG_PROVIDER, null) ?: return
        val apiKey = DollOSAIApp.prefs.getString(KEY_BG_API_KEY, null) ?: return
        val model = DollOSAIApp.prefs.getString(KEY_BG_MODEL, null) ?: return
        val baseUrl = DollOSAIApp.prefs.getString(KEY_BG_BASE_URL, "") ?: ""

        backgroundClient = createClient(provider, apiKey, model, baseUrl)
        Log.i(TAG, "Background client initialized: $provider / $model")
    }

    private fun createClient(provider: String, apiKey: String, model: String, baseUrl: String): LLMClient {
        val providerType = LLMProviderType.fromString(provider)
        val config = ModelConfig(
            providerType = providerType,
            apiKey = apiKey,
            model = model,
            baseUrl = baseUrl
        )
        return when (providerType) {
            LLMProviderType.CLAUDE -> ClaudeProvider(config)
            LLMProviderType.OPENAI -> OpenAIProvider(config)
            LLMProviderType.GROK -> GrokProvider(config)
            LLMProviderType.CUSTOM -> CustomProvider(config)
        }
    }

    private fun loadToolDefinitions(): List<ToolDefinition> {
        val dollOSService = service.dollOSService ?: return emptyList()
        try {
            val actionsJson = dollOSService.availableActions
            if (actionsJson.isNullOrBlank()) return emptyList()

            val arr = Json.parseToJsonElement(actionsJson).jsonArray
            val tools = mutableListOf<ToolDefinition>()
            for (element in arr) {
                val action = element.jsonObject
                tools.add(ToolDefinition(
                    name = action["id"]?.jsonPrimitive?.content ?: "",
                    description = action["description"]?.jsonPrimitive?.content ?: "",
                    parameters = action["parameters"]?.toString()
                        ?: """{"type":"object","properties":{},"required":[]}"""
                ))
            }
            return tools
        } catch (e: Exception) {
            Log.e(TAG, "Failed to load tool definitions from DollOSService", e)
            return emptyList()
        }
    }

    private fun handleToolCalls(toolCalls: List<ToolCall>) {
        val dollOSService = service.dollOSService
        if (dollOSService == null) {
            Log.e(TAG, "Cannot execute tool calls: DollOSService not connected")
            return
        }

        // Parse available actions to check confirmRequired per action
        val confirmMap = mutableMapOf<String, Boolean>()
        try {
            val actionsJson = dollOSService.availableActions
            if (!actionsJson.isNullOrBlank()) {
                val arr = Json.parseToJsonElement(actionsJson).jsonArray
                for (element in arr) {
                    val action = element.jsonObject
                    val id = action["id"]?.jsonPrimitive?.content ?: ""
                    val confirm = action["confirmRequired"]?.jsonPrimitive?.content?.toBoolean() ?: true
                    confirmMap[id] = confirm
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to parse available actions for confirm check", e)
        }

        for (toolCall in toolCalls) {
            val needsConfirm = confirmMap.getOrDefault(toolCall.name, true)

            if (needsConfirm) {
                val timeoutRunnable = Runnable {
                    val removed = pendingConfirmations.remove(toolCall.id)
                    if (removed != null) {
                        Log.i(TAG, "Confirmation timed out for action: ${toolCall.name}")
                        broadcastActionExecuted(toolCall.id, false, "Confirmation timed out (60s)")
                    }
                }
                pendingConfirmations[toolCall.id] = PendingAction(toolCall, timeoutRunnable)
                confirmTimeoutHandler.postDelayed(timeoutRunnable, CONFIRM_TIMEOUT_MS)
                broadcastActionConfirm(toolCall.id, toolCall.name, "Execute ${toolCall.name}?")
                Log.i(TAG, "Awaiting confirmation for action: ${toolCall.name} (id=${toolCall.id})")
            } else {
                executor.submit {
                    executeToolCall(dollOSService, toolCall)
                }
            }
        }
    }

    private fun executeToolCall(dollOSService: IDollOSService, toolCall: ToolCall) {
        try {
            val resultJson = dollOSService.executeSystemAction(toolCall.name, toolCall.arguments)
            val result = Json.parseToJsonElement(resultJson).jsonObject
            val success = result["success"]?.jsonPrimitive?.content?.toBoolean() ?: false
            val message = result["message"]?.jsonPrimitive?.content ?: ""

            broadcastActionExecuted(toolCall.id, success, message)
            Log.i(TAG, "Tool call executed: ${toolCall.name} -> success=$success")
        } catch (e: Exception) {
            Log.e(TAG, "Tool call failed: ${toolCall.name}", e)
            broadcastActionExecuted(toolCall.id, false, e.message ?: "Execution failed")
        }
    }

    // --- Broadcast helpers ---

    private fun broadcastToken(token: String) {
        val n = callbacks.beginBroadcast()
        try {
            for (i in 0 until n) {
                try {
                    callbacks.getBroadcastItem(i).onToken(token)
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to broadcast token to callback $i", e)
                }
            }
        } finally {
            callbacks.finishBroadcast()
        }
    }

    private fun broadcastComplete(fullResponse: String) {
        val n = callbacks.beginBroadcast()
        try {
            for (i in 0 until n) {
                try {
                    callbacks.getBroadcastItem(i).onResponseComplete(fullResponse)
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to broadcast complete to callback $i", e)
                }
            }
        } finally {
            callbacks.finishBroadcast()
        }
    }

    private fun broadcastError(errorCode: String, message: String) {
        val n = callbacks.beginBroadcast()
        try {
            for (i in 0 until n) {
                try {
                    callbacks.getBroadcastItem(i).onResponseError(errorCode, message)
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to broadcast error to callback $i", e)
                }
            }
        } finally {
            callbacks.finishBroadcast()
        }
    }

    private fun broadcastActionConfirm(actionId: String, actionName: String, description: String) {
        val n = callbacks.beginBroadcast()
        try {
            for (i in 0 until n) {
                try {
                    callbacks.getBroadcastItem(i).onActionConfirmRequired(actionId, actionName, description)
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to broadcast action confirm to callback $i", e)
                }
            }
        } finally {
            callbacks.finishBroadcast()
        }
    }

    private fun broadcastActionExecuted(actionId: String, success: Boolean, resultMessage: String) {
        val n = callbacks.beginBroadcast()
        try {
            for (i in 0 until n) {
                try {
                    callbacks.getBroadcastItem(i).onActionExecuted(actionId, success, resultMessage)
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to broadcast action result to callback $i", e)
                }
            }
        } finally {
            callbacks.finishBroadcast()
        }
    }

    private fun sendErrorNotification(errorCode: String, message: String) {
        val nm = DollOSAIApp.instance.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val notification = NotificationCompat.Builder(DollOSAIApp.instance, DollOSAIApp.NOTIFICATION_CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_dialog_alert)
            .setContentTitle("AI Service Error: $errorCode")
            .setContentText(message)
            .setStyle(NotificationCompat.BigTextStyle().bigText(message))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .build()
        nm.notify(errorCode.hashCode(), notification)
    }
}
```

- [x] **Step 2: Commit**

```bash
cd DollOSAIService
git add app/src/main/java/org/dollos/ai/DollOSAIServiceImpl.kt
git commit -m "feat: add DollOSAIServiceImpl with full AIDL implementation, tool calling, and budget controls"
```

---

## Task 10: AOSP Integration (Prebuilt APK)

**Goal:** Create the AOSP integration for DollOSAIService as a prebuilt APK import. This includes the `android_app_import` in `external/DollOSAIService/`, privapp permissions, and additions to `dollos_bluejay.mk`.

**Files:**
- Create: `external/DollOSAIService/Android.bp`
- Create: `external/DollOSAIService/privapp-permissions-dollos-ai.xml`
- Modify: `vendor/dollos/dollos_bluejay.mk`

- [ ] **Step 1: Create external/DollOSAIService/Android.bp**

Create `external/DollOSAIService/Android.bp`:

```
android_app_import {
    name: "DollOSAIService",
    apk: "prebuilt/DollOSAIService.apk",
    privileged: true,
    certificate: "platform",
    presigned: false,
    dex_preopt: {
        enabled: true,
    },
    required: [
        "privapp-permissions-dollos-ai.xml",
    ],
}

prebuilt_etc {
    name: "privapp-permissions-dollos-ai.xml",
    src: "privapp-permissions-dollos-ai.xml",
    sub_dir: "permissions",
}
```

- [ ] **Step 2: Create privapp-permissions-dollos-ai.xml**

Create `external/DollOSAIService/privapp-permissions-dollos-ai.xml`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<permissions>
    <privapp-permissions package="org.dollos.ai">
        <permission name="android.permission.INTERNET" />
        <permission name="android.permission.RECEIVE_BOOT_COMPLETED" />
        <permission name="android.permission.FOREGROUND_SERVICE" />
        <permission name="android.permission.POST_NOTIFICATIONS" />
    </privapp-permissions>
</permissions>
```

- [ ] **Step 3: Create placeholder directory for prebuilt APK**

```bash
mkdir -p external/DollOSAIService/prebuilt/
# The APK will be placed here after building the Gradle project (Task 12).
```

- [ ] **Step 4: Update dollos_bluejay.mk**

Add DollOSAIService to `vendor/dollos/dollos_bluejay.mk`:

```makefile
# DollOS packages
PRODUCT_PACKAGES += \
    DollOSService \
    DollOSSetupWizard \
    DollOSAIService

# Privapp permissions
PRODUCT_COPY_FILES += \
    packages/apps/DollOSService/privapp-permissions-dollos-service.xml:$(TARGET_COPY_OUT_SYSTEM)/etc/permissions/privapp-permissions-dollos-service.xml \
    packages/apps/DollOSSetupWizard/privapp-permissions-dollos-setup.xml:$(TARGET_COPY_OUT_SYSTEM)/etc/permissions/privapp-permissions-dollos-setup.xml
```

Note: The `privapp-permissions-dollos-ai.xml` is handled by the `prebuilt_etc` module in `external/DollOSAIService/Android.bp` via the `required` field, so it does not need a `PRODUCT_COPY_FILES` entry.

- [ ] **Step 5: Add to platform_manifest (local_manifests)**

Create or update `~/Desktop/DollOS-build/.repo/local_manifests/dollos.xml` to include the external directory if needed. Since `external/DollOSAIService/` is a directory within the AOSP tree (not a separate git repo for the build), it can simply be committed to the AOSP tree directly.

- [ ] **Step 6: Add dollos-ai-aidl dependency to DollOSSetupWizard**

Modify `packages/apps/DollOSSetupWizard/Android.bp` to add `dollos-ai-aidl` to static_libs:

```
android_app {
    name: "DollOSSetupWizard",
    ...
    static_libs: [
        "androidx.core_core-ktx",
        "dollos-service-aidl",
        "dollos-ai-aidl",
    ],
    ...
}
```

- [ ] **Step 7: Commit AOSP changes**

```bash
cd ~/Desktop/DollOS-build
git -C external/DollOSAIService add .
git -C external/DollOSAIService commit -m "feat: add DollOSAIService prebuilt APK integration with android_app_import"

git -C vendor/dollos add dollos_bluejay.mk
git -C vendor/dollos commit -m "feat: add DollOSAIService to PRODUCT_PACKAGES"

git -C packages/apps/DollOSSetupWizard add Android.bp
git -C packages/apps/DollOSSetupWizard commit -m "feat: add dollos-ai-aidl dependency for DollOSAIService binding"
```

---

## Task 11: OOBE Migration

**Goal:** Update DollOSSetupWizard to bind to DollOSAIService for API key configuration and personality setup. The ApiKeyPage now calls `setForegroundModel` on DollOSAIService. The PersonalityPage now has 5 fields (backstory, directive, dynamism, address, language) instead of the old single-field personality.

**Files:**
- Modify: `packages/apps/DollOSSetupWizard/src/org/dollos/setup/SetupWizardActivity.kt`
- Modify: `packages/apps/DollOSSetupWizard/src/org/dollos/setup/ApiKeyPage.kt`
- Modify: `packages/apps/DollOSSetupWizard/src/org/dollos/setup/PersonalityPage.kt`
- Modify: `packages/apps/DollOSSetupWizard/res/layout/page_api_key.xml`
- Modify: `packages/apps/DollOSSetupWizard/res/layout/page_personality.xml`

- [ ] **Step 1: Add DollOSAIService binding to SetupWizardActivity.kt**

Add a ServiceConnection for DollOSAIService alongside the existing DollOSService connection:

```kotlin
// Add to SetupWizardActivity class body:

private var aiService: IDollOSAIService? = null
private var isAIBound = false

private val aiServiceConnection = object : ServiceConnection {
    override fun onServiceConnected(name: ComponentName?, service: IBinder?) {
        aiService = IDollOSAIService.Stub.asInterface(service)
        Log.i(TAG, "Connected to DollOSAIService")
    }

    override fun onServiceDisconnected(name: ComponentName?) {
        aiService = null
        Log.w(TAG, "Disconnected from DollOSAIService")
    }
}

// In onCreate(), add:
val aiIntent = Intent("org.dollos.ai.IDollOSAIService")
aiIntent.setPackage("org.dollos.ai")
isAIBound = bindService(aiIntent, aiServiceConnection, Context.BIND_AUTO_CREATE)

// In onDestroy(), add:
if (isAIBound) {
    unbindService(aiServiceConnection)
}

// Expose getter for pages:
fun getAIService(): IDollOSAIService? = aiService
```

- [ ] **Step 2: Update ApiKeyPage to call DollOSAIService**

Replace DollOSService API key calls with DollOSAIService calls:

```kotlin
// In ApiKeyPage, when user saves API key configuration:
// OLD: activity.getDollOSService()?.setApiKey(apiKey)
// NEW:
val provider = providerSpinner.selectedItem.toString().lowercase()
val apiKey = apiKeyEditText.text.toString()
val model = modelEditText.text.toString()
activity.getAIService()?.setForegroundModel(provider, apiKey, model)
```

Layout changes for `page_api_key.xml`:
- Add Spinner for provider selection (Claude, OpenAI, Grok, Custom)
- Add EditText for model name
- Keep existing EditText for API key
- Add optional section for background model (same 3 fields)

- [ ] **Step 3: Update PersonalityPage with 5 fields**

Replace the old single personality field with 5 fields:

```kotlin
// In PersonalityPage, when user saves personality:
val aiService = activity.getAIService() ?: return

aiService.setBackstory(backstoryEditText.text.toString())
aiService.setResponseDirective(directiveEditText.text.toString())
aiService.setDynamism(dynamismSlider.value)
aiService.setAddress(addressEditText.text.toString())
aiService.setLanguagePreference(languageSpinner.selectedItem.toString())
```

Layout changes for `page_personality.xml`:
- EditText: Backstory (multiline, max 2500 chars, hint: "Describe your AI companion's personality in 3rd person")
- EditText: Response Directive (single line, max 150 chars, hint: "How should the AI respond? e.g., 'Be concise and witty'")
- Slider: Dynamism (0.0 to 1.0, labels: "Stable" to "Creative")
- EditText: Address (single line, max 50 chars, hint: "How should the AI address you?")
- Spinner: Language Preference (English, Chinese, Japanese, Mixed, Custom)

- [ ] **Step 4: Commit**

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSSetupWizard
git add .
git commit -m "feat: update OOBE to bind DollOSAIService for API key and 5-field personality setup"
```

---

## Task 12: Build + Flash + Verify

**Goal:** Build the Gradle project, copy the APK to the AOSP tree, build AOSP, flash, and verify DollOSAIService is running.

- [ ] **Step 1: Build Gradle project**

```bash
cd DollOSAIService
./gradlew assembleRelease
```

Output APK at: `DollOSAIService/app/build/outputs/apk/release/app-release.apk`

Note: This APK is debug-signed. Platform signing happens during AOSP build because `android_app_import` has `certificate: "platform"` and `presigned: false`.

- [ ] **Step 2: Copy APK to AOSP tree**

```bash
cp DollOSAIService/app/build/outputs/apk/release/app-release.apk \
   ~/Desktop/DollOS-build/external/DollOSAIService/prebuilt/DollOSAIService.apk
```

- [ ] **Step 3: Build AOSP**

```bash
cd ~/Desktop/DollOS-build
source build/envsetup.sh
lunch dollos_bluejay-userdebug
m -j$(nproc)
```

- [ ] **Step 4: Flash**

```bash
cd ~/Desktop/DollOS-build
adb reboot bootloader
fastboot flashall -w
```

- [ ] **Step 5: Verify DollOSAIService is installed and running**

```bash
# Check app is installed
adb shell pm list packages | grep org.dollos.ai

# Check service is running
adb shell dumpsys activity services org.dollos.ai

# Check privapp permissions
adb shell cat /system/etc/permissions/privapp-permissions-dollos-ai.xml

# Check logs
adb logcat -s DollOSAIApp:I DollOSAIService:I DollOSAIServiceImpl:I
```

- [ ] **Step 6: Verify AIDL binding works**

```bash
# Check that DollOSSetupWizard can bind to DollOSAIService
adb logcat -s DollOSSetupWizard:I | grep "DollOSAIService"

# Check that DollOSAIService can bind to DollOSService
adb logcat -s DollOSAIService:I | grep "DollOSService"
```

---

## Architecture Summary

```
+-----------------------------------------------------------+
|                  AOSP Build Tree                          |
|                                                           |
|  packages/apps/DollOSAIService/                           |
|    Android.bp (dollos-ai-aidl java_library)               |
|    aidl/  (AIDL source of truth)                          |
|                                                           |
|  packages/apps/DollOSService/                             |
|    Android.bp (dollos-service-aidl + DollOSService app)   |
|    depends on: dollos-ai-aidl (to receive AI callbacks)   |
|                                                           |
|  packages/apps/DollOSSetupWizard/                         |
|    Android.bp                                             |
|    depends on: dollos-service-aidl, dollos-ai-aidl        |
|                                                           |
|  external/DollOSAIService/                                |
|    Android.bp (android_app_import)                        |
|    prebuilt/DollOSAIService.apk  <--+                     |
|    privapp-permissions-dollos-ai.xml |                    |
|                                      |                    |
|  vendor/dollos/dollos_bluejay.mk     |                    |
|    PRODUCT_PACKAGES += DollOSAIService                    |
+-----------------------------------------------------------+
                                       |
                  Built externally     |
                                       |
+-----------------------------------------------------------+
|           ningyos/DollOSAIService (Gradle)                |
|                                                           |
|  app/build.gradle.kts                                     |
|    - OkHttp 4.12.0 (Maven)                               |
|    - kotlinx-serialization-json 1.7.3 (Maven)            |
|    - kotlinx-coroutines-android 1.9.0 (Maven)            |
|    - dollos-service-aidl.jar (compileOnly, from AOSP)     |
|                                                           |
|  app/src/main/aidl/  (copied from AOSP)                  |
|  app/src/main/java/org/dollos/ai/                         |
|    DollOSAIApp.kt                                         |
|    DollOSAIService.kt                                     |
|    DollOSAIServiceImpl.kt                                 |
|    llm/ (LLM providers with OkHttp)                       |
|    personality/ (5-field system)                           |
|    usage/ (tracking + budget)                             |
|                                                           |
|  ./gradlew assembleRelease                                |
|    -> app/build/outputs/apk/release/app-release.apk   ---+
+-----------------------------------------------------------+
```

**Data flow for AIDL sharing:**

```
AOSP packages/apps/DollOSAIService/aidl/  (source of truth)
    |
    +-- Soong builds dollos-ai-aidl java_library
    |     |
    |     +-- DollOSService static_libs (to call DollOSAIService)
    |     +-- DollOSSetupWizard static_libs (to call DollOSAIService)
    |
    +-- Manually copied to Gradle project src/main/aidl/
          |
          +-- Gradle AIDL plugin generates Java stubs
                |
                +-- DollOSAIServiceImpl implements IDollOSAIService.Stub
```

**Build workflow:**

```
1. Edit AIDL in AOSP packages/apps/DollOSAIService/aidl/ (if changed)
2. Copy AIDL to Gradle project (if changed)
3. Edit Kotlin source in Gradle project
4. ./gradlew assembleRelease
5. cp APK to external/DollOSAIService/prebuilt/
6. m -j$(nproc) in AOSP tree
7. fastboot flashall
```
