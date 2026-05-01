# AI Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a full-screen 3D AI Launcher that replaces the default AOSP Launcher, with a Filament-rendered avatar scene, floating conversation bubbles, text input, and right-swipe app drawer.

**Architecture:** DollOSLauncher is a standalone Gradle project (like DollOSAIService). It uses Google Filament for 3D rendering on a TextureView, with standard Android Views overlaid for UI. It binds to DollOSAIService via AIDL for conversation and character management. Character assets (glTF models, scene config) are loaded via `getCharacterAsset()` ParcelFileDescriptor. The prebuilt APK is imported into AOSP via `android_app_import`.

**Tech Stack:** Kotlin, Google Filament (filament-android + gltfio-android), TextureView, AIDL, RecyclerView, Gradle

---

## File Structure

### New project: ~/Projects/DollOSLauncher/

```
DollOSLauncher/
  settings.gradle.kts
  build.gradle.kts
  app/
    build.gradle.kts
    src/main/
      AndroidManifest.xml
      java/org/dollos/launcher/
        DollOSLauncherActivity.kt    — main launcher activity, view setup, service binding
        scene/
          FilamentSceneManager.kt    — Filament engine lifecycle, model loading, rendering
          SceneConfig.kt             — parse scene.json from character pack
          AvatarAnimator.kt          — animation state machine (IDLE/THINKING/TALKING)
        conversation/
          ResponseBubbleView.kt      — floating response bubble
          InputBarView.kt            — bottom input bar with text field + mic button
        drawer/
          AppDrawerView.kt           — right-swipe app drawer panel
          AppListAdapter.kt          — RecyclerView adapter for app list
          AppInfo.kt                 — data class for app entry
        character/
          CharacterPickerOverlay.kt  — long-press avatar character switcher
      res/
        layout/
          activity_launcher.xml
          view_response_bubble.xml
          view_input_bar.xml
          view_app_drawer.xml
          item_app.xml
          view_character_picker.xml
          item_character.xml
        drawable/
          bg_bubble.xml
          bg_input_bar.xml
          bg_drawer.xml
        values/
          styles.xml
          colors.xml
    prebuilt/
      DollOSLauncher.apk            — built APK for AOSP import

  # AOSP integration (in DollOS-build tree)
  Android.bp                         — android_app_import for prebuilt APK
```

---

## Task 1: Project Scaffold

**Goal:** Create the Gradle project, build config, and empty launcher activity that registers as HOME.

**Files:**
- Create: `~/Projects/DollOSLauncher/settings.gradle.kts`
- Create: `~/Projects/DollOSLauncher/build.gradle.kts`
- Create: `~/Projects/DollOSLauncher/app/build.gradle.kts`
- Create: `~/Projects/DollOSLauncher/app/src/main/AndroidManifest.xml`
- Create: `~/Projects/DollOSLauncher/app/src/main/java/org/dollos/launcher/DollOSLauncherActivity.kt`
- Create: `~/Projects/DollOSLauncher/app/src/main/res/values/styles.xml`
- Create: `~/Projects/DollOSLauncher/app/src/main/res/values/colors.xml`

- [ ] **Step 1: Create settings.gradle.kts**

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

rootProject.name = "DollOSLauncher"
include(":app")
```

- [ ] **Step 2: Create root build.gradle.kts**

```kotlin
plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.1.0" apply false
}
```

- [ ] **Step 3: Create app/build.gradle.kts**

```kotlin
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "org.dollos.launcher"
    compileSdk = 36

    defaultConfig {
        applicationId = "org.dollos.launcher"
        minSdk = 33
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        aidl = true
    }

    sourceSets["main"].aidl.srcDirs("aidl")
}

dependencies {
    // Filament 3D rendering
    implementation("com.google.android.filament:filament-android:1.54.5")
    implementation("com.google.android.filament:gltfio-android:1.54.5")
    implementation("com.google.android.filament:filament-utils-android:1.54.5")

    // AndroidX
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.recyclerview:recyclerview:1.3.2")

    // DollOSAIService AIDL (local jar)
    implementation(files("libs/dollos-ai-aidl.jar"))
}
```

- [ ] **Step 4: Copy AIDL jar**

```bash
mkdir -p ~/Projects/DollOSLauncher/app/libs
cp ~/Projects/DollOSAIService/app/build/intermediates/aidl_jar/release/dollos-ai-aidl.jar ~/Projects/DollOSLauncher/app/libs/ 2>/dev/null || true
```

If the jar doesn't exist yet, we'll need to build it from the AIDL files. Alternative: copy the AIDL files directly:

```bash
mkdir -p ~/Projects/DollOSLauncher/app/aidl/org/dollos/ai
cp ~/Projects/DollOSAIService/aidl/org/dollos/ai/*.aidl ~/Projects/DollOSLauncher/app/aidl/org/dollos/ai/
```

- [ ] **Step 5: Create AndroidManifest.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="org.dollos.launcher">

    <uses-permission android:name="android.permission.QUERY_ALL_PACKAGES" />

    <application
        android:label="DollOS"
        android:supportsRtl="true"
        android:theme="@style/Theme.DollOSLauncher">

        <activity
            android:name=".DollOSLauncherActivity"
            android:launchMode="singleTask"
            android:clearTaskOnLaunch="true"
            android:stateNotNeeded="true"
            android:resumeWhilePausing="true"
            android:excludeFromRecents="true"
            android:screenOrientation="nosensor"
            android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.HOME" />
                <category android:name="android.intent.category.DEFAULT" />
            </intent-filter>
        </activity>

    </application>
</manifest>
```

- [ ] **Step 6: Create styles.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <style name="Theme.DollOSLauncher" parent="android:Theme.DeviceDefault.NoActionBar">
        <item name="android:windowFullscreen">true</item>
        <item name="android:windowTranslucentStatus">true</item>
        <item name="android:windowTranslucentNavigation">true</item>
        <item name="android:windowDrawsSystemBarBackgrounds">true</item>
        <item name="android:statusBarColor">@android:color/transparent</item>
        <item name="android:navigationBarColor">@android:color/transparent</item>
    </style>
</resources>
```

- [ ] **Step 7: Create colors.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <color name="scene_background">#0a0a1a</color>
    <color name="bubble_background">#CC0f0f1e</color>
    <color name="input_background">#CC0f0f1e</color>
    <color name="drawer_background">#E60a0a14</color>
    <color name="text_primary">#CCCCCC</color>
    <color name="text_secondary">#777777</color>
    <color name="text_hint">#555555</color>
</resources>
```

- [ ] **Step 8: Create empty DollOSLauncherActivity.kt**

```kotlin
package org.dollos.launcher

import android.app.Activity
import android.os.Bundle
import android.util.Log

class DollOSLauncherActivity : Activity() {

    companion object {
        private const val TAG = "DollOSLauncher"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Log.i(TAG, "DollOS Launcher created")
        // Will be populated in subsequent tasks
    }
}
```

- [ ] **Step 9: Build to verify project compiles**

```bash
cd ~/Projects/DollOSLauncher
./gradlew assembleRelease
```

- [ ] **Step 10: Init git + commit**

```bash
cd ~/Projects/DollOSLauncher
git init
git add -A
git commit -m "feat: scaffold DollOSLauncher project with Filament dependencies"
```

---

## Task 2: Layout + Input Bar + Response Bubble

**Goal:** Create the main layout XML and the input bar and response bubble views.

**Files:**
- Create: `app/src/main/res/layout/activity_launcher.xml`
- Create: `app/src/main/res/layout/view_input_bar.xml`
- Create: `app/src/main/res/layout/view_response_bubble.xml`
- Create: `app/src/main/res/drawable/bg_bubble.xml`
- Create: `app/src/main/res/drawable/bg_input_bar.xml`
- Create: `app/src/main/java/org/dollos/launcher/conversation/InputBarView.kt`
- Create: `app/src/main/java/org/dollos/launcher/conversation/ResponseBubbleView.kt`

- [ ] **Step 1: Create activity_launcher.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<FrameLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:id="@+id/root"
    android:layout_width="match_parent"
    android:layout_height="match_parent"
    android:background="@color/scene_background">

    <!-- Filament 3D scene (TextureView) -->
    <TextureView
        android:id="@+id/filament_texture_view"
        android:layout_width="match_parent"
        android:layout_height="match_parent" />

    <!-- UI overlay -->
    <FrameLayout
        android:id="@+id/ui_overlay"
        android:layout_width="match_parent"
        android:layout_height="match_parent">

        <!-- No character installed message -->
        <TextView
            android:id="@+id/no_character_text"
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:layout_gravity="center"
            android:text="No AI character installed.\nImport a .doll file to get started."
            android:textColor="@color/text_secondary"
            android:textSize="16sp"
            android:textAlignment="center"
            android:visibility="gone" />

        <!-- Response bubble -->
        <include
            layout="@layout/view_response_bubble"
            android:id="@+id/response_bubble"
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:layout_gravity="center_horizontal"
            android:layout_marginTop="320dp"
            android:visibility="gone" />

        <!-- Input bar (bottom) -->
        <include
            layout="@layout/view_input_bar"
            android:id="@+id/input_bar"
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:layout_gravity="bottom" />

    </FrameLayout>

</FrameLayout>
```

- [ ] **Step 2: Create view_response_bubble.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<FrameLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="wrap_content"
    android:layout_height="wrap_content"
    android:layout_marginStart="48dp"
    android:layout_marginEnd="48dp">

    <TextView
        android:id="@+id/bubble_text"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content"
        android:maxWidth="280dp"
        android:background="@drawable/bg_bubble"
        android:padding="14dp"
        android:textColor="@color/text_primary"
        android:textSize="14sp"
        android:lineSpacingExtra="4dp" />

</FrameLayout>
```

- [ ] **Step 3: Create view_input_bar.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="wrap_content"
    android:orientation="horizontal"
    android:gravity="center_vertical"
    android:paddingStart="20dp"
    android:paddingEnd="20dp"
    android:paddingTop="12dp"
    android:paddingBottom="32dp"
    android:background="@drawable/bg_input_bar">

    <EditText
        android:id="@+id/input_text"
        android:layout_width="0dp"
        android:layout_height="48dp"
        android:layout_weight="1"
        android:background="@null"
        android:hint="Message..."
        android:textColorHint="@color/text_hint"
        android:textColor="@color/text_primary"
        android:textSize="15sp"
        android:inputType="text"
        android:imeOptions="actionSend"
        android:singleLine="true" />

    <ImageButton
        android:id="@+id/mic_button"
        android:layout_width="40dp"
        android:layout_height="40dp"
        android:background="?android:attr/selectableItemBackgroundBorderless"
        android:src="@android:drawable/ic_btn_speak_now"
        android:contentDescription="Voice input"
        android:layout_marginStart="8dp" />

</LinearLayout>
```

- [ ] **Step 4: Create bg_bubble.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<shape xmlns:android="http://schemas.android.com/apk/res/android"
    android:shape="rectangle">
    <solid android:color="@color/bubble_background" />
    <corners android:radius="16dp" />
    <stroke android:width="1dp" android:color="#22ffffff" />
</shape>
```

- [ ] **Step 5: Create bg_input_bar.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<shape xmlns:android="http://schemas.android.com/apk/res/android"
    android:shape="rectangle">
    <gradient
        android:startColor="#00000000"
        android:endColor="#EE0a0a1a"
        android:angle="270" />
</shape>
```

- [ ] **Step 6: Create InputBarView.kt**

```kotlin
package org.dollos.launcher.conversation

import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.EditText
import android.widget.ImageButton

class InputBarView(
    private val root: View,
    private val onSend: (String) -> Unit
) {
    private val inputText: EditText = root.findViewById(android.R.id.edit)
        ?: root.findViewWithTag("input_text")
        ?: (root as? android.view.ViewGroup)?.let { findEditText(it) }
        ?: throw IllegalStateException("No EditText found in input bar")
    private val micButton: ImageButton? = root.findViewById(android.R.id.button1)

    init {
        // Find views by ID from the included layout
        val input = root.findViewById<EditText>(root.resources.getIdentifier("input_text", "id", root.context.packageName))
        val mic = root.findViewById<ImageButton>(root.resources.getIdentifier("mic_button", "id", root.context.packageName))

        input?.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_SEND) {
                sendMessage()
                true
            } else false
        }
    }

    private fun sendMessage() {
        val input = root.findViewById<EditText>(root.resources.getIdentifier("input_text", "id", root.context.packageName)) ?: return
        val text = input.text.toString().trim()
        if (text.isNotEmpty()) {
            onSend(text)
            input.text.clear()
        }
    }

    private fun findEditText(group: android.view.ViewGroup): EditText? {
        for (i in 0 until group.childCount) {
            val child = group.getChildAt(i)
            if (child is EditText) return child
            if (child is android.view.ViewGroup) {
                findEditText(child)?.let { return it }
            }
        }
        return null
    }
}
```

- [ ] **Step 7: Create ResponseBubbleView.kt**

```kotlin
package org.dollos.launcher.conversation

import android.view.View
import android.widget.TextView

class ResponseBubbleView(private val root: View) {

    private val bubbleText: TextView = root.findViewById(
        root.resources.getIdentifier("bubble_text", "id", root.context.packageName)
    )
    private val responseBuilder = StringBuilder()

    fun appendToken(token: String) {
        responseBuilder.append(token)
        bubbleText.text = responseBuilder.toString()
        root.visibility = View.VISIBLE
    }

    fun setComplete(fullResponse: String) {
        responseBuilder.clear()
        responseBuilder.append(fullResponse)
        bubbleText.text = fullResponse
        root.visibility = View.VISIBLE
    }

    fun setError(message: String) {
        responseBuilder.clear()
        bubbleText.text = "Error: $message"
        root.visibility = View.VISIBLE
    }

    fun dismiss() {
        root.visibility = View.GONE
        responseBuilder.clear()
        bubbleText.text = ""
    }

    fun isVisible(): Boolean = root.visibility == View.VISIBLE

    fun clear() {
        responseBuilder.clear()
        bubbleText.text = ""
    }
}
```

- [ ] **Step 8: Commit**

```bash
cd ~/Projects/DollOSLauncher
git add -A
git commit -m "feat: add launcher layout, input bar, response bubble views"
```

---

## Task 3: App Drawer

**Goal:** Create the right-swipe app drawer with search and recent apps.

**Files:**
- Create: `app/src/main/java/org/dollos/launcher/drawer/AppInfo.kt`
- Create: `app/src/main/java/org/dollos/launcher/drawer/AppListAdapter.kt`
- Create: `app/src/main/java/org/dollos/launcher/drawer/AppDrawerView.kt`
- Create: `app/src/main/res/layout/view_app_drawer.xml`
- Create: `app/src/main/res/layout/item_app.xml`
- Create: `app/src/main/res/drawable/bg_drawer.xml`

- [ ] **Step 1: Create AppInfo.kt**

```kotlin
package org.dollos.launcher.drawer

import android.graphics.drawable.Drawable

data class AppInfo(
    val packageName: String,
    val label: String,
    val icon: Drawable
)
```

- [ ] **Step 2: Create item_app.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="64dp"
    android:orientation="horizontal"
    android:gravity="center_vertical"
    android:paddingStart="20dp"
    android:paddingEnd="20dp"
    android:clickable="true"
    android:focusable="true"
    android:background="?android:attr/selectableItemBackground">

    <ImageView
        android:id="@+id/app_icon"
        android:layout_width="40dp"
        android:layout_height="40dp" />

    <TextView
        android:id="@+id/app_label"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content"
        android:layout_marginStart="16dp"
        android:textColor="#cccccc"
        android:textSize="15sp" />

</LinearLayout>
```

- [ ] **Step 3: Create AppListAdapter.kt**

```kotlin
package org.dollos.launcher.drawer

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ImageView
import android.widget.TextView
import androidx.recyclerview.widget.RecyclerView
import org.dollos.launcher.R

class AppListAdapter(
    private var apps: List<AppInfo>,
    private val onClick: (AppInfo) -> Unit
) : RecyclerView.Adapter<AppListAdapter.ViewHolder>() {

    class ViewHolder(view: View) : RecyclerView.ViewHolder(view) {
        val icon: ImageView = view.findViewById(R.id.app_icon)
        val label: TextView = view.findViewById(R.id.app_label)
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ViewHolder {
        val view = LayoutInflater.from(parent.context).inflate(R.layout.item_app, parent, false)
        return ViewHolder(view)
    }

    override fun onBindViewHolder(holder: ViewHolder, position: Int) {
        val app = apps[position]
        holder.icon.setImageDrawable(app.icon)
        holder.label.text = app.label
        holder.itemView.setOnClickListener { onClick(app) }
    }

    override fun getItemCount() = apps.size

    fun updateApps(newApps: List<AppInfo>) {
        apps = newApps
        notifyDataSetChanged()
    }
}
```

- [ ] **Step 4: Create bg_drawer.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<shape xmlns:android="http://schemas.android.com/apk/res/android"
    android:shape="rectangle">
    <solid android:color="@color/drawer_background" />
</shape>
```

- [ ] **Step 5: Create view_app_drawer.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="match_parent"
    android:orientation="vertical"
    android:background="@drawable/bg_drawer"
    android:paddingTop="48dp">

    <EditText
        android:id="@+id/drawer_search"
        android:layout_width="match_parent"
        android:layout_height="48dp"
        android:layout_marginStart="20dp"
        android:layout_marginEnd="20dp"
        android:layout_marginBottom="12dp"
        android:hint="Search apps..."
        android:textColorHint="#555555"
        android:textColor="#cccccc"
        android:textSize="14sp"
        android:inputType="text"
        android:singleLine="true"
        android:background="@null"
        android:padding="12dp" />

    <View
        android:layout_width="match_parent"
        android:layout_height="1dp"
        android:background="#15ffffff" />

    <androidx.recyclerview.widget.RecyclerView
        android:id="@+id/app_list"
        android:layout_width="match_parent"
        android:layout_height="match_parent" />

</LinearLayout>
```

- [ ] **Step 6: Create AppDrawerView.kt**

```kotlin
package org.dollos.launcher.drawer

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.text.Editable
import android.text.TextWatcher
import android.view.View
import android.view.animation.DecelerateInterpolator
import android.widget.EditText
import android.widget.FrameLayout
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import org.dollos.launcher.R

class AppDrawerView(
    private val context: Context,
    private val drawerLayout: View,
    private val onAppLaunch: (String) -> Unit
) {
    private val appList: RecyclerView = drawerLayout.findViewById(R.id.app_list)
    private val searchInput: EditText = drawerLayout.findViewById(R.id.drawer_search)
    private var allApps: List<AppInfo> = emptyList()
    private val adapter = AppListAdapter(emptyList()) { app -> launchApp(app) }

    private var isOpen = false
    private val drawerWidth: Int
        get() = (context.resources.displayMetrics.widthPixels * 0.8).toInt()

    init {
        appList.layoutManager = LinearLayoutManager(context)
        appList.adapter = adapter

        // Search filter
        searchInput.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {}
            override fun afterTextChanged(s: Editable?) {
                val query = s?.toString()?.trim() ?: ""
                val filtered = if (query.isEmpty()) allApps
                else allApps.filter { it.label.contains(query, ignoreCase = true) }
                adapter.updateApps(filtered)
            }
        })

        // Initial position: off-screen right
        drawerLayout.visibility = View.GONE
        loadApps()
    }

    fun open() {
        if (isOpen) return
        isOpen = true
        drawerLayout.visibility = View.VISIBLE
        drawerLayout.translationX = drawerWidth.toFloat()
        drawerLayout.animate()
            .translationX(0f)
            .setDuration(250)
            .setInterpolator(DecelerateInterpolator())
            .start()
    }

    fun close() {
        if (!isOpen) return
        isOpen = false
        drawerLayout.animate()
            .translationX(drawerWidth.toFloat())
            .setDuration(200)
            .setInterpolator(DecelerateInterpolator())
            .withEndAction { drawerLayout.visibility = View.GONE }
            .start()
        searchInput.text.clear()
    }

    fun isOpen(): Boolean = isOpen

    fun toggle() {
        if (isOpen) close() else open()
    }

    private fun launchApp(app: AppInfo) {
        onAppLaunch(app.packageName)
        close()
    }

    private fun loadApps() {
        val pm = context.packageManager
        val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        allApps = pm.queryIntentActivities(intent, PackageManager.MATCH_ALL)
            .map { ri ->
                AppInfo(
                    packageName = ri.activityInfo.packageName,
                    label = ri.loadLabel(pm).toString(),
                    icon = ri.loadIcon(pm)
                )
            }
            .sortedBy { it.label.lowercase() }
            .distinctBy { it.packageName }

        adapter.updateApps(allApps)
    }
}
```

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: add app drawer with search and app list"
```

---

## Task 4: Filament Scene Manager

**Goal:** Initialize Filament engine, load glTF models from ParcelFileDescriptor, render on TextureView.

**Files:**
- Create: `app/src/main/java/org/dollos/launcher/scene/SceneConfig.kt`
- Create: `app/src/main/java/org/dollos/launcher/scene/AvatarAnimator.kt`
- Create: `app/src/main/java/org/dollos/launcher/scene/FilamentSceneManager.kt`

- [ ] **Step 1: Create SceneConfig.kt**

```kotlin
package org.dollos.launcher.scene

import org.json.JSONObject
import java.io.InputStream

data class SceneConfig(
    val backgroundColor: FloatArray,  // RGB 0-1
    val ambientIntensity: Float,
    val directionalIntensity: Float,
    val directionalDirection: FloatArray,
    val directionalColor: FloatArray,
    val cameraPosition: FloatArray,
    val cameraTarget: FloatArray,
    val cameraFov: Float
) {
    companion object {
        fun fromJson(inputStream: InputStream): SceneConfig {
            val json = JSONObject(inputStream.bufferedReader().readText())
            val bg = json.optJSONObject("background") ?: JSONObject()
            val lighting = json.optJSONObject("lighting") ?: JSONObject()
            val dir = lighting.optJSONObject("directional") ?: JSONObject()
            val camera = json.optJSONObject("camera") ?: JSONObject()

            return SceneConfig(
                backgroundColor = parseColor(bg.optString("value", "#0a0a1a")),
                ambientIntensity = lighting.optDouble("ambient", 0.3).toFloat(),
                directionalIntensity = dir.optDouble("intensity", 1.0).toFloat(),
                directionalDirection = parseFloatArray(dir.optJSONArray("direction"), floatArrayOf(0f, -1f, -0.5f)),
                directionalColor = parseColor(dir.optString("color", "#ffffff")),
                cameraPosition = parseFloatArray(camera.optJSONArray("position"), floatArrayOf(0f, 1.2f, 3f)),
                cameraTarget = parseFloatArray(camera.optJSONArray("target"), floatArrayOf(0f, 1f, 0f)),
                cameraFov = camera.optDouble("fov", 45.0).toFloat()
            )
        }

        fun default() = SceneConfig(
            floatArrayOf(0.04f, 0.04f, 0.1f), 0.3f, 1.0f,
            floatArrayOf(0f, -1f, -0.5f), floatArrayOf(1f, 1f, 1f),
            floatArrayOf(0f, 1.2f, 3f), floatArrayOf(0f, 1f, 0f), 45f
        )

        private fun parseColor(hex: String): FloatArray {
            val color = android.graphics.Color.parseColor(hex)
            return floatArrayOf(
                android.graphics.Color.red(color) / 255f,
                android.graphics.Color.green(color) / 255f,
                android.graphics.Color.blue(color) / 255f
            )
        }

        private fun parseFloatArray(arr: org.json.JSONArray?, default: FloatArray): FloatArray {
            if (arr == null) return default
            return FloatArray(arr.length()) { arr.getDouble(it).toFloat() }
        }
    }
}
```

- [ ] **Step 2: Create AvatarAnimator.kt**

```kotlin
package org.dollos.launcher.scene

import android.util.Log

enum class AnimationState {
    IDLE, THINKING, TALKING
}

class AvatarAnimator {

    companion object {
        private const val TAG = "AvatarAnimator"
        private const val BLEND_DURATION_MS = 300L
    }

    var currentState = AnimationState.IDLE
        private set

    var onStateChanged: ((AnimationState) -> Unit)? = null

    fun onMessageSent() {
        if (currentState != AnimationState.THINKING) {
            transition(AnimationState.THINKING)
        }
    }

    fun onFirstToken() {
        if (currentState != AnimationState.TALKING) {
            transition(AnimationState.TALKING)
        }
    }

    fun onResponseComplete() {
        transition(AnimationState.IDLE)
    }

    fun onError() {
        transition(AnimationState.IDLE)
    }

    private fun transition(newState: AnimationState) {
        val old = currentState
        currentState = newState
        Log.d(TAG, "Animation: $old -> $newState")
        onStateChanged?.invoke(newState)
    }
}
```

- [ ] **Step 3: Create FilamentSceneManager.kt**

```kotlin
package org.dollos.launcher.scene

import android.graphics.SurfaceTexture
import android.os.ParcelFileDescriptor
import android.util.Log
import android.view.Choreographer
import android.view.TextureView
import com.google.android.filament.*
import com.google.android.filament.gltfio.*
import com.google.android.filament.utils.*
import java.io.FileInputStream
import java.nio.ByteBuffer

class FilamentSceneManager(private val textureView: TextureView) {

    companion object {
        private const val TAG = "FilamentSceneManager"
    }

    private lateinit var engine: Engine
    private lateinit var renderer: Renderer
    private lateinit var scene: Scene
    private lateinit var view: com.google.android.filament.View
    private lateinit var camera: Camera
    private var swapChain: SwapChain? = null
    private var assetLoader: AssetLoader? = null
    private var resourceLoader: ResourceLoader? = null
    private var filamentAsset: FilamentAsset? = null
    private var animator: Animator? = null

    private val choreographer = Choreographer.getInstance()
    private var isInitialized = false
    private var sceneConfig = SceneConfig.default()

    private val frameCallback = object : Choreographer.FrameCallback {
        override fun doFrame(frameTimeNanos: Long) {
            if (!isInitialized) return
            render(frameTimeNanos)
            choreographer.postFrameCallback(this)
        }
    }

    init {
        textureView.surfaceTextureListener = object : TextureView.SurfaceTextureListener {
            override fun onSurfaceTextureAvailable(surface: SurfaceTexture, width: Int, height: Int) {
                initFilament(surface, width, height)
            }

            override fun onSurfaceTextureSizeChanged(surface: SurfaceTexture, width: Int, height: Int) {
                updateViewport(width, height)
            }

            override fun onSurfaceTextureDestroyed(surface: SurfaceTexture): Boolean {
                destroy()
                return true
            }

            override fun onSurfaceTextureUpdated(surface: SurfaceTexture) {}
        }
    }

    fun applySceneConfig(config: SceneConfig) {
        sceneConfig = config
        if (isInitialized) {
            applyConfig()
        }
    }

    fun loadModel(fd: ParcelFileDescriptor) {
        if (!isInitialized) {
            Log.w(TAG, "Engine not initialized yet")
            return
        }

        // Remove old asset
        filamentAsset?.let {
            scene.removeEntities(it.entities)
            assetLoader?.destroyAsset(it)
        }

        try {
            val inputStream = FileInputStream(fd.fileDescriptor)
            val bytes = inputStream.readBytes()
            inputStream.close()
            fd.close()

            val buffer = ByteBuffer.allocateDirect(bytes.size)
            buffer.put(bytes)
            buffer.flip()

            val asset = assetLoader?.createAsset(buffer)
            if (asset == null) {
                Log.e(TAG, "Failed to create asset from glTF")
                return
            }

            resourceLoader?.loadResources(asset)
            asset.releaseSourceData()

            scene.addEntities(asset.entities)
            filamentAsset = asset
            animator = asset.getInstance().animator

            Log.i(TAG, "Model loaded: ${asset.entities.size} entities")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to load model", e)
        }
    }

    fun playAnimation(name: String) {
        val anim = animator ?: return
        val count = anim.animationCount
        for (i in 0 until count) {
            if (anim.getAnimationName(i) == name) {
                anim.applyAnimation(i, 0f)
                return
            }
        }
        // Fallback to first animation
        if (count > 0) {
            anim.applyAnimation(0, 0f)
        }
    }

    fun destroy() {
        isInitialized = false
        choreographer.removeFrameCallback(frameCallback)

        filamentAsset?.let {
            scene.removeEntities(it.entities)
            assetLoader?.destroyAsset(it)
        }

        if (::engine.isInitialized) {
            engine.destroyRenderer(renderer)
            engine.destroyView(view)
            engine.destroyScene(scene)
            engine.destroyCameraComponent(camera.entity)
            swapChain?.let { engine.destroySwapChain(it) }
            engine.destroy()
        }
    }

    private fun initFilament(surfaceTexture: SurfaceTexture, width: Int, height: Int) {
        engine = Engine.create()
        renderer = engine.createRenderer()
        scene = engine.createScene()
        view = engine.createView()
        camera = engine.createCamera(engine.entityManager.create())

        view.scene = scene
        view.camera = camera

        swapChain = engine.createSwapChain(surfaceTexture)

        // Setup gltfio
        val materialProvider = UbershaderProvider(engine)
        assetLoader = AssetLoader(engine, materialProvider, EntityManager.get())
        resourceLoader = ResourceLoader(engine)

        updateViewport(width, height)
        applyConfig()

        isInitialized = true
        choreographer.postFrameCallback(frameCallback)
        Log.i(TAG, "Filament initialized: ${width}x${height}")
    }

    private fun applyConfig() {
        // Background color
        val bg = sceneConfig.backgroundColor
        scene.skybox = Skybox.Builder().color(bg[0], bg[1], bg[2], 1f).build(engine)

        // Camera
        val pos = sceneConfig.cameraPosition
        val target = sceneConfig.cameraTarget
        camera.lookAt(
            pos[0].toDouble(), pos[1].toDouble(), pos[2].toDouble(),
            target[0].toDouble(), target[1].toDouble(), target[2].toDouble(),
            0.0, 1.0, 0.0
        )

        // Ambient light
        scene.indirectLight = IndirectLight.Builder()
            .intensity(sceneConfig.ambientIntensity * 30000f)
            .build(engine)

        Log.d(TAG, "Scene config applied")
    }

    private fun updateViewport(width: Int, height: Int) {
        view.viewport = Viewport(0, 0, width, height)
        val aspect = width.toDouble() / height.toDouble()
        camera.setProjection(
            sceneConfig.cameraFov.toDouble(),
            aspect,
            0.1,
            100.0,
            Camera.Fov.VERTICAL
        )
    }

    private var animationStartTime = 0L

    private fun render(frameTimeNanos: Long) {
        // Advance animation
        animator?.let { anim ->
            if (animationStartTime == 0L) animationStartTime = frameTimeNanos
            val elapsedSec = (frameTimeNanos - animationStartTime) / 1_000_000_000f
            if (anim.animationCount > 0) {
                val duration = anim.getAnimationDuration(0)
                if (duration > 0) {
                    anim.applyAnimation(0, elapsedSec % duration)
                }
                anim.updateBoneMatrices()
            }
        }

        if (renderer.beginFrame(swapChain!!)) {
            renderer.render(view)
            renderer.endFrame()
        }
    }
}
```

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat: add FilamentSceneManager, SceneConfig, AvatarAnimator"
```

---

## Task 5: Wire Everything in DollOSLauncherActivity

**Goal:** Connect TextureView, input bar, response bubble, app drawer, service binding, gesture handling.

**Files:**
- Modify: `app/src/main/java/org/dollos/launcher/DollOSLauncherActivity.kt`

- [ ] **Step 1: Complete DollOSLauncherActivity.kt**

```kotlin
package org.dollos.launcher

import android.app.Activity
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.graphics.Rect
import android.os.Bundle
import android.os.IBinder
import android.util.Log
import android.view.GestureDetector
import android.view.MotionEvent
import android.view.TextureView
import android.view.View
import org.dollos.ai.IDollOSAICallback
import org.dollos.ai.IDollOSAIService
import org.dollos.launcher.conversation.InputBarView
import org.dollos.launcher.conversation.ResponseBubbleView
import org.dollos.launcher.drawer.AppDrawerView
import org.dollos.launcher.scene.AvatarAnimator
import org.dollos.launcher.scene.FilamentSceneManager
import org.dollos.launcher.scene.SceneConfig
import org.json.JSONObject

class DollOSLauncherActivity : Activity() {

    companion object {
        private const val TAG = "DollOSLauncher"
    }

    private var aiService: IDollOSAIService? = null
    private var isBound = false

    private lateinit var filamentScene: FilamentSceneManager
    private lateinit var responseBubble: ResponseBubbleView
    private lateinit var appDrawer: AppDrawerView
    private val avatarAnimator = AvatarAnimator()

    private val callback = object : IDollOSAICallback.Stub() {
        override fun onToken(token: String?) {
            token ?: return
            avatarAnimator.onFirstToken()
            runOnUiThread { responseBubble.appendToken(token) }
        }

        override fun onResponseComplete(fullResponse: String?) {
            avatarAnimator.onResponseComplete()
            runOnUiThread { responseBubble.setComplete(fullResponse ?: "") }
        }

        override fun onResponseError(errorCode: String?, message: String?) {
            avatarAnimator.onError()
            runOnUiThread { responseBubble.setError(message ?: "Unknown error") }
        }

        override fun onActionConfirmRequired(actionId: String?, actionName: String?, description: String?) {
            // Auto-approve for now (TODO: show confirmation card)
            try { aiService?.confirmAction(actionId, true) } catch (_: Exception) {}
        }

        override fun onActionExecuted(actionId: String?, success: Boolean, resultMessage: String?) {}
        override fun onTaskListUpdated(tasksJson: String?) {}
        override fun onMemoryConfirmRequired(formattedMemory: String?) {}
        override fun onWorkerComplete(workerId: String?, success: Boolean, output: String?) {}

        override fun onCharacterChanged(characterId: String?, characterName: String?) {
            Log.i(TAG, "Character changed: $characterName")
            runOnUiThread { loadCharacterAssets(characterId) }
        }

        override fun onCharacterImportFailed(errorCode: String?, message: String?) {
            Log.e(TAG, "Character import failed: $errorCode — $message")
        }
    }

    private val serviceConnection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, service: IBinder?) {
            aiService = IDollOSAIService.Stub.asInterface(service)
            aiService?.registerCallback(callback)
            Log.i(TAG, "Connected to DollOSAIService")

            // Load active character
            val activeId = aiService?.activeCharacter
            if (activeId != null) {
                loadCharacterAssets(activeId)
            } else {
                showNoCharacter()
            }
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            aiService = null
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_launcher)

        // Filament scene
        val textureView = findViewById<TextureView>(R.id.filament_texture_view)
        filamentScene = FilamentSceneManager(textureView)

        // Response bubble
        val bubbleView = findViewById<View>(R.id.response_bubble)
        responseBubble = ResponseBubbleView(bubbleView)

        // Input bar
        val inputBarView = findViewById<View>(R.id.input_bar)
        InputBarView(inputBarView) { message ->
            sendMessage(message)
        }

        // App drawer
        val drawerLayout = layoutInflater.inflate(R.layout.view_app_drawer, null)
        val drawerContainer = FrameLayout(this).apply {
            layoutParams = android.widget.FrameLayout.LayoutParams(
                (resources.displayMetrics.widthPixels * 0.8).toInt(),
                android.widget.FrameLayout.LayoutParams.MATCH_PARENT
            ).apply { gravity = android.view.Gravity.END }
        }
        drawerContainer.addView(drawerLayout)
        (findViewById<View>(R.id.root) as android.widget.FrameLayout).addView(drawerContainer)

        appDrawer = AppDrawerView(this, drawerContainer) { packageName ->
            val intent = packageManager.getLaunchIntentForPackage(packageName)
            if (intent != null) startActivity(intent)
        }

        // Gesture: right edge swipe for drawer
        setupGestures()

        // Tap to dismiss bubble
        textureView.setOnClickListener {
            if (responseBubble.isVisible()) {
                responseBubble.dismiss()
            }
        }

        // Exclude right edge from system gesture
        findViewById<View>(R.id.root).post {
            val width = resources.displayMetrics.widthPixels
            val height = resources.displayMetrics.heightPixels
            val exclusion = Rect(width - 40, 0, width, height)
            findViewById<View>(R.id.root).systemGestureExclusionRects = listOf(exclusion)
        }

        // Bind to AI service
        val intent = Intent("org.dollos.ai.IDollOSAIService")
        intent.setPackage("org.dollos.ai")
        isBound = bindService(intent, serviceConnection, Context.BIND_AUTO_CREATE)

        Log.i(TAG, "DollOS Launcher created")
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        // Home button pressed while drawer is open
        if (appDrawer.isOpen()) {
            appDrawer.close()
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        aiService?.unregisterCallback(callback)
        if (isBound) unbindService(serviceConnection)
        filamentScene.destroy()
    }

    override fun onBackPressed() {
        when {
            appDrawer.isOpen() -> appDrawer.close()
            responseBubble.isVisible() -> responseBubble.dismiss()
            else -> { /* no-op: launcher doesn't go back */ }
        }
    }

    private fun sendMessage(message: String) {
        responseBubble.clear()
        avatarAnimator.onMessageSent()
        try {
            aiService?.sendMessage(message)
        } catch (e: Exception) {
            Log.e(TAG, "sendMessage failed", e)
            responseBubble.setError(e.message ?: "Failed to send")
        }
    }

    private fun loadCharacterAssets(characterId: String?) {
        if (characterId == null) {
            showNoCharacter()
            return
        }

        val service = aiService ?: return

        try {
            // Load scene.json
            val sceneFd = service.getCharacterAsset(characterId, "scene.json")
            if (sceneFd != null) {
                val config = SceneConfig.fromJson(java.io.FileInputStream(sceneFd.fileDescriptor))
                filamentScene.applySceneConfig(config)
                sceneFd.close()
            }

            // Load model.glb
            val modelFd = service.getCharacterAsset(characterId, "model.glb")
            if (modelFd != null) {
                filamentScene.loadModel(modelFd)
                // modelFd closed inside loadModel
            }

            findViewById<View>(R.id.no_character_text).visibility = View.GONE
        } catch (e: Exception) {
            Log.e(TAG, "Failed to load character assets", e)
            showNoCharacter()
        }
    }

    private fun showNoCharacter() {
        findViewById<View>(R.id.no_character_text).visibility = View.VISIBLE
    }

    private fun setupGestures() {
        val root = findViewById<View>(R.id.root)
        val detector = GestureDetector(this, object : GestureDetector.SimpleOnGestureListener() {
            override fun onFling(e1: MotionEvent?, e2: MotionEvent, velocityX: Float, velocityY: Float): Boolean {
                if (e1 == null) return false
                val screenWidth = resources.displayMetrics.widthPixels
                // Right-to-left swipe from right edge
                if (e1.x > screenWidth - 80 && velocityX < -500) {
                    appDrawer.open()
                    return true
                }
                return false
            }
        })

        root.setOnTouchListener { _, event ->
            detector.onTouchEvent(event)
            false
        }
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add -A
git commit -m "feat: wire DollOSLauncherActivity — scene, conversation, drawer, service binding"
```

---

## Task 6: Character Picker Overlay

**Goal:** Long-press on avatar shows character picker.

**Files:**
- Create: `app/src/main/res/layout/view_character_picker.xml`
- Create: `app/src/main/res/layout/item_character.xml`
- Create: `app/src/main/java/org/dollos/launcher/character/CharacterPickerOverlay.kt`

- [ ] **Step 1: Create view_character_picker.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<FrameLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="match_parent"
    android:background="#80000000"
    android:clickable="true">

    <LinearLayout
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:layout_gravity="center"
        android:layout_marginStart="32dp"
        android:layout_marginEnd="32dp"
        android:orientation="vertical"
        android:background="@drawable/bg_bubble"
        android:padding="24dp">

        <TextView
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:text="Switch Character"
            android:textColor="#cccccc"
            android:textSize="18sp"
            android:layout_marginBottom="16dp" />

        <androidx.recyclerview.widget.RecyclerView
            android:id="@+id/character_list"
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:maxHeight="400dp" />

    </LinearLayout>
</FrameLayout>
```

- [ ] **Step 2: Create item_character.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="64dp"
    android:orientation="horizontal"
    android:gravity="center_vertical"
    android:padding="12dp"
    android:clickable="true"
    android:focusable="true"
    android:background="?android:attr/selectableItemBackground">

    <TextView
        android:id="@+id/character_name"
        android:layout_width="0dp"
        android:layout_height="wrap_content"
        android:layout_weight="1"
        android:textColor="#cccccc"
        android:textSize="16sp" />

    <TextView
        android:id="@+id/character_active"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content"
        android:text="Active"
        android:textColor="#666666"
        android:textSize="12sp"
        android:visibility="gone" />

</LinearLayout>
```

- [ ] **Step 3: Create CharacterPickerOverlay.kt**

```kotlin
package org.dollos.launcher.character

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.TextView
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import org.dollos.ai.IDollOSAIService
import org.dollos.launcher.R
import org.json.JSONArray

class CharacterPickerOverlay(
    private val container: FrameLayout,
    private val aiService: IDollOSAIService?,
    private val onCharacterSelected: (String) -> Unit
) {
    private var overlayView: View? = null

    fun show() {
        if (overlayView != null) return
        val service = aiService ?: return

        val view = LayoutInflater.from(container.context)
            .inflate(R.layout.view_character_picker, container, false)

        val list = view.findViewById<RecyclerView>(R.id.character_list)
        list.layoutManager = LinearLayoutManager(container.context)

        // Load characters
        val json = service.listCharacters()
        val characters = JSONArray(json)
        val activeId = service.activeCharacter

        list.adapter = object : RecyclerView.Adapter<RecyclerView.ViewHolder>() {
            override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RecyclerView.ViewHolder {
                val itemView = LayoutInflater.from(parent.context)
                    .inflate(R.layout.item_character, parent, false)
                return object : RecyclerView.ViewHolder(itemView) {}
            }

            override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) {
                val char = characters.getJSONObject(position)
                holder.itemView.findViewById<TextView>(R.id.character_name).text = char.getString("name")
                val activeLabel = holder.itemView.findViewById<TextView>(R.id.character_active)
                if (char.getString("id") == activeId) {
                    activeLabel.visibility = View.VISIBLE
                } else {
                    activeLabel.visibility = View.GONE
                }
                holder.itemView.setOnClickListener {
                    onCharacterSelected(char.getString("id"))
                    dismiss()
                }
            }

            override fun getItemCount() = characters.length()
        }

        // Tap outside to dismiss
        view.setOnClickListener { dismiss() }

        container.addView(view)
        overlayView = view
    }

    fun dismiss() {
        overlayView?.let { container.removeView(it) }
        overlayView = null
    }

    fun isShowing() = overlayView != null
}
```

- [ ] **Step 4: Wire long-press in DollOSLauncherActivity**

Add to `setupGestures()`:

```kotlin
override fun onLongPress(e: MotionEvent) {
    // Long press in center area = character picker
    val centerX = resources.displayMetrics.widthPixels / 2
    val centerY = resources.displayMetrics.heightPixels / 2
    if (Math.abs(e.x - centerX) < 200 && Math.abs(e.y - centerY) < 200) {
        showCharacterPicker()
    }
}
```

Add method to activity:

```kotlin
private var characterPicker: CharacterPickerOverlay? = null

private fun showCharacterPicker() {
    val root = findViewById<FrameLayout>(R.id.root)
    characterPicker = CharacterPickerOverlay(root, aiService) { characterId ->
        try { aiService?.setActiveCharacter(characterId) } catch (e: Exception) {
            Log.e(TAG, "setActiveCharacter failed", e)
        }
    }
    characterPicker?.show()
}
```

Update onBackPressed:

```kotlin
override fun onBackPressed() {
    when {
        characterPicker?.isShowing() == true -> characterPicker?.dismiss()
        appDrawer.isOpen() -> appDrawer.close()
        responseBubble.isVisible() -> responseBubble.dismiss()
        else -> { }
    }
}
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: add character picker overlay with long-press trigger"
```

---

## Task 7: AOSP Integration + Build + Deploy

**Goal:** Create Android.bp, build APK, integrate into AOSP, deploy to device.

**Files:**
- Create: `~/Projects/DollOS-build/packages/apps/DollOSLauncher/Android.bp`
- Create: `~/Projects/DollOS-build/packages/apps/DollOSLauncher/prebuilt/DollOSLauncher.apk`

- [ ] **Step 1: Build the APK**

```bash
cd ~/Projects/DollOSLauncher
./gradlew assembleRelease
```

Fix any compile errors. The main risks: Filament API changes, AIDL mismatch, missing R class references.

- [ ] **Step 2: Copy APK to AOSP tree**

```bash
mkdir -p ~/Projects/DollOS-build/packages/apps/DollOSLauncher/prebuilt
cp ~/Projects/DollOSLauncher/app/build/outputs/apk/release/app-release-unsigned.apk ~/Projects/DollOS-build/packages/apps/DollOSLauncher/prebuilt/DollOSLauncher.apk
```

- [ ] **Step 3: Create Android.bp**

```
android_app_import {
    name: "DollOSLauncher",
    apk: "prebuilt/DollOSLauncher.apk",
    privileged: true,
    certificate: "platform",
    system_ext_specific: true,
    dex_preopt: {
        enabled: true,
    },
}
```

- [ ] **Step 4: Add to product makefile**

Add `DollOSLauncher` to `PRODUCT_PACKAGES` in `vendor/dollos/dollos_bluejay.mk`.

- [ ] **Step 5: Build AOSP module**

```bash
cd ~/Projects/DollOS-build
source build/envsetup.sh && lunch dollos_bluejay-bp2a-userdebug
m DollOSLauncher -j$(nproc)
```

- [ ] **Step 6: Push to device and reboot**

Push DollOSLauncher APK/odex/vdex to device, reboot.

- [ ] **Step 7: Verify**

After boot, system should prompt to choose default launcher. Select DollOS.

```bash
adb logcat -d | grep -iE "DollOSLauncher|FilamentScene|AvatarAnimator" | head -20
```

Expected: launcher created, Filament initialized, "No AI character installed" shown (or character loaded if one exists).

- [ ] **Step 8: Test app drawer**

Swipe from right edge → drawer should slide in. Tap an app → should launch.

- [ ] **Step 9: Test conversation**

Type a message in the input bar → AI should respond in floating bubble.

- [ ] **Step 10: Commit AOSP integration**

```bash
cd ~/Projects/DollOS-build/packages/apps/DollOSLauncher
git init
git add -A
git commit -m "feat: add DollOSLauncher prebuilt to AOSP"
```

---

## Notes

### Filament Version

Using Filament 1.54.5 (latest stable as of writing). The `gltfio` API may vary between versions — if build fails, check the Filament changelog for API changes.

### TextureView vs SurfaceView

TextureView is used instead of SurfaceView to enable proper alpha compositing with the semi-transparent UI overlays (bubble, input bar, drawer). TextureView has slightly higher GPU overhead but is necessary for correct visual layering.

### Default Character

The system ships without a default character. On first boot, the launcher shows "No AI character installed" with a prompt. A default `.doll` file can be bundled in the system image and auto-imported in OOBE (future task).

### InputBarView Simplification

The InputBarView implementation is simplified — it finds the EditText by iterating child views rather than using `findViewById` with generated R.id. This is because the `<include>` layout may not preserve IDs correctly across different build environments. A more robust version can use ViewBinding.
