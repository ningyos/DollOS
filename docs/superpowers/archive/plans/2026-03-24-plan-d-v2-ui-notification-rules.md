# Plan D v2: UI Operation, Smart Notification, Programmable Events — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add UI operation capabilities (AccessibilityService + VirtualDisplay + takeover), smart notification routing, and programmable event rules to DollOS.

**Architecture:** DollOSAccessibilityService lives in the DollOSService APK (system uid). It provides node tree reading, UI execution, screenshot capture, takeover overlay, and app event monitoring. DollOSAIService (separate APK, normal uid) communicates via Binder IPC through IDollOSService AIDL. Smart Notification and Programmable Events logic lives in DollOSAIService but DollOSService provides the event sources (app events via AccessibilityService, system events via existing SystemEventReceiver in Plan D v1).

**Tech Stack:** Kotlin, AccessibilityService API, VirtualDisplay (DisplayManager), GestureDescription, Room (rule persistence), NotificationManager, BroadcastReceiver

**Spec:** `docs/superpowers/specs/2026-03-24-plan-d-v2-design.md`

**Note on DollOSAIService:** DollOSAIService app does not exist in the committed codebase yet (it's designed in the AI Core spec but not yet built). Tasks that modify DollOSAIService files (notification, rule engine) create the files in the planned locations. These will be integrated when DollOSAIService is scaffolded.

---

## File Structure

### DollOSService — New Files

All paths relative to `aosp/packages/apps/DollOSService/`:

```
src/org/dollos/service/accessibility/
  DollOSAccessibilityService.kt    — main service, delegates to modules
  NodeReader.kt                    — read node tree → JSON
  UIExecutor.kt                    — click, swipe, type, gesture, global actions
  ScreenCapture.kt                 — takeScreenshot() + VirtualDisplay surface capture
  TakeoverManager.kt               — overlay bar + edge glow + touch interception + interrupt modal
  AppEventMonitor.kt               — track foreground app from TYPE_WINDOW_STATE_CHANGED
  VirtualDisplayManager.kt         — create/destroy VirtualDisplay, launch app on display

res/xml/accessibility_service_config.xml
res/layout/takeover_bar.xml
res/layout/takeover_interrupt_modal.xml
res/drawable/edge_glow.xml
```

### DollOSService — Modified Files

```
AndroidManifest.xml                — register AccessibilityService + permissions
Android.bp                         — (no changes needed, src/**/*.kt glob catches new files)
DollOSApp.kt                       — auto-enable accessibility service
DollOSServiceImpl.kt               — add UI operation AIDL methods
aidl/org/dollos/service/IDollOSService.aidl — add UI operation methods
privapp-permissions-dollos-service.xml — add WRITE_SECURE_SETTINGS
```

### DollOSAIService — New Files (future integration)

All paths relative to planned `aosp/packages/apps/DollOSAIService/`:

```
src/org/dollos/ai/notification/
  NotificationLevel.kt             — enum SILENT / NORMAL / URGENT
  QuietHoursConfig.kt              — quiet hours data class + time check
  TTSInterface.kt                  — abstract TTS interface (no-op default)
  NotificationRouter.kt            — decision logic + dispatch

src/org/dollos/ai/rule/
  Rule.kt                          — Rule + Condition + ConditionType + RuleAction + Operator
  RuleEntity.kt                    — Room @Entity + type converters
  RuleDao.kt                       — Room DAO
  RuleEngine.kt                    — evaluate rules, trigger actions, debounce
```

---

## Task 1: AccessibilityService Config + Manifest Registration

**Goal:** Register DollOSAccessibilityService in the manifest and create the service config XML.

**Files:**
- Create: `aosp/packages/apps/DollOSService/res/xml/accessibility_service_config.xml`
- Modify: `aosp/packages/apps/DollOSService/AndroidManifest.xml`
- Modify: `aosp/packages/apps/DollOSService/privapp-permissions-dollos-service.xml`

- [ ] **Step 1: Create accessibility_service_config.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<accessibility-service xmlns:android="http://schemas.android.com/apk/res/android"
    android:description="@string/accessibility_service_description"
    android:accessibilityEventTypes="typeWindowStateChanged|typeWindowContentChanged"
    android:accessibilityFeedbackType="feedbackGeneric"
    android:notificationTimeout="100"
    android:canRetrieveWindowContent="true"
    android:canPerformGestures="true"
    android:canTakeScreenshot="true"
    android:accessibilityFlags="flagRequestFilterKeyEvents|flagRetrieveInteractiveWindows|flagIncludeNotImportantViews"
    android:canRequestFilterKeyEvents="true" />
```

Note: `flagRetrieveInteractiveWindows` is required for `getWindows()` to work (multi-display node tree access). `flagRequestFilterKeyEvents` enables `onKeyEvent()` for power button interception during takeover.

- [ ] **Step 2: Add string resource for accessibility service description**

Create `aosp/packages/apps/DollOSService/res/values/strings.xml`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="accessibility_service_description">DollOS AI screen reading and UI automation service</string>
    <string name="takeover_bar_text">AI 操作中：%s</string>
    <string name="takeover_interrupt_title">AI 正在操作</string>
    <string name="takeover_interrupt_cancel">取消</string>
    <string name="takeover_interrupt_continue">繼續</string>
</resources>
```

- [ ] **Step 3: Register AccessibilityService in AndroidManifest.xml**

Add inside `<application>`, after the existing `<activity>` block:

```xml
<service
    android:name=".accessibility.DollOSAccessibilityService"
    android:exported="true"
    android:permission="android.permission.BIND_ACCESSIBILITY_SERVICE">
    <intent-filter>
        <action android:name="android.accessibilityservice.AccessibilityService" />
    </intent-filter>
    <meta-data
        android:name="android.accessibilityservice"
        android:resource="@xml/accessibility_service_config" />
</service>
```

- [ ] **Step 4: Add WRITE_SECURE_SETTINGS to privapp-permissions**

Add to `privapp-permissions-dollos-service.xml`:

```xml
<permission name="android.permission.WRITE_SECURE_SETTINGS" />
```

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/DollOS
git add aosp/packages/apps/DollOSService/res/xml/accessibility_service_config.xml \
        aosp/packages/apps/DollOSService/res/values/strings.xml \
        aosp/packages/apps/DollOSService/AndroidManifest.xml \
        aosp/packages/apps/DollOSService/privapp-permissions-dollos-service.xml
git commit -m "feat: register DollOSAccessibilityService with config and permissions"
```

---

## Task 2: DollOSAccessibilityService Skeleton + Auto-Enable

**Goal:** Create the main AccessibilityService class and auto-enable it on app startup.

**Files:**
- Create: `aosp/packages/apps/DollOSService/src/org/dollos/service/accessibility/DollOSAccessibilityService.kt`
- Modify: `aosp/packages/apps/DollOSService/src/org/dollos/service/DollOSApp.kt`

- [ ] **Step 1: Create DollOSAccessibilityService.kt**

```kotlin
package org.dollos.service.accessibility

import android.accessibilityservice.AccessibilityService
import android.util.Log
import android.view.KeyEvent
import android.view.accessibility.AccessibilityEvent

class DollOSAccessibilityService : AccessibilityService() {

    companion object {
        private const val TAG = "DollOSA11yService"
        var instance: DollOSAccessibilityService? = null
            private set
    }

    lateinit var nodeReader: NodeReader
        private set
    lateinit var uiExecutor: UIExecutor
        private set
    lateinit var screenCapture: ScreenCapture
        private set
    lateinit var takeoverManager: TakeoverManager
        private set
    lateinit var appEventMonitor: AppEventMonitor
        private set

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        nodeReader = NodeReader(this)
        uiExecutor = UIExecutor(this)
        screenCapture = ScreenCapture(this)
        takeoverManager = TakeoverManager(this)
        appEventMonitor = AppEventMonitor()
        Log.i(TAG, "AccessibilityService connected")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent) {
        when (event.eventType) {
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED -> {
                appEventMonitor.onWindowStateChanged(event)
            }
            AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED -> {
                // Used by AI operation loop to detect screen updates
            }
        }
    }

    override fun onKeyEvent(event: KeyEvent): Boolean {
        if (event.keyCode == KeyEvent.KEYCODE_POWER
            && event.action == KeyEvent.ACTION_DOWN
            && takeoverManager.isActive
        ) {
            takeoverManager.showInterruptModal()
            return true // consume the key event
        }
        return false
    }

    override fun onInterrupt() {
        Log.w(TAG, "AccessibilityService interrupted")
    }

    override fun onDestroy() {
        instance = null
        takeoverManager.cleanup()
        Log.i(TAG, "AccessibilityService destroyed")
        super.onDestroy()
    }
}
```

- [ ] **Step 2: Add auto-enable to DollOSApp.kt**

Add to DollOSApp.kt after the actionRegistry initialization:

```kotlin
import android.provider.Settings
import android.content.ComponentName

// In onCreate(), after actionRegistry init:
enableAccessibilityService()
```

Add method:

```kotlin
private fun enableAccessibilityService() {
    val componentName = ComponentName(
        this,
        "org.dollos.service.accessibility.DollOSAccessibilityService"
    ).flattenToString()

    val enabledServices = Settings.Secure.getString(
        contentResolver,
        Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
    ) ?: ""

    if (!enabledServices.contains(componentName)) {
        val newValue = if (enabledServices.isEmpty()) {
            componentName
        } else {
            "$enabledServices:$componentName"
        }
        Settings.Secure.putString(
            contentResolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
            newValue
        )
        Settings.Secure.putInt(
            contentResolver,
            Settings.Secure.ACCESSIBILITY_ENABLED,
            1
        )
        Log.i(TAG, "Auto-enabled AccessibilityService: $componentName")
    } else {
        Log.d(TAG, "AccessibilityService already enabled")
    }
}
```

- [ ] **Step 3: Commit**

```bash
git add aosp/packages/apps/DollOSService/src/org/dollos/service/accessibility/DollOSAccessibilityService.kt \
        aosp/packages/apps/DollOSService/src/org/dollos/service/DollOSApp.kt
git commit -m "feat: add DollOSAccessibilityService skeleton with auto-enable"
```

---

## Task 3: NodeReader

**Goal:** Read accessibility node tree from any display and serialize to JSON.

**Files:**
- Create: `aosp/packages/apps/DollOSService/src/org/dollos/service/accessibility/NodeReader.kt`

- [ ] **Step 1: Create NodeReader.kt**

```kotlin
package org.dollos.service.accessibility

import android.util.Log
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import org.json.JSONArray
import org.json.JSONObject

class NodeReader(private val service: DollOSAccessibilityService) {

    companion object {
        private const val TAG = "NodeReader"
        private const val MAX_DEPTH = 30
    }

    /**
     * Read the node tree from a specific display.
     * Returns JSON string matching the spec format.
     */
    fun readScreen(displayId: Int): String {
        val windows = service.windows
        val targetWindows = windows.filter { it.displayId == displayId }

        if (targetWindows.isEmpty()) {
            Log.w(TAG, "No windows found on display $displayId")
            return JSONObject().apply {
                put("package", "")
                put("displayId", displayId)
                put("nodes", JSONArray())
            }.toString()
        }

        // Use the focused window, or first app window
        val window = targetWindows.firstOrNull {
            it.type == AccessibilityWindowInfo.TYPE_APPLICATION && it.isFocused
        } ?: targetWindows.firstOrNull {
            it.type == AccessibilityWindowInfo.TYPE_APPLICATION
        } ?: targetWindows.first()

        val root = window.root ?: return JSONObject().apply {
            put("package", "")
            put("displayId", displayId)
            put("nodes", JSONArray())
        }.toString()

        val result = JSONObject()
        result.put("package", root.packageName?.toString() ?: "")
        result.put("displayId", displayId)

        val nodes = JSONArray()
        var nodeCounter = 0
        val nodeMap = mutableMapOf<AccessibilityNodeInfo, String>()

        fun traverse(node: AccessibilityNodeInfo, depth: Int) {
            if (depth > MAX_DEPTH) return

            val nodeId = "node_$nodeCounter"
            nodeCounter++
            nodeMap[node] = nodeId

            val rect = android.graphics.Rect()
            node.getBoundsInScreen(rect)

            val childIds = JSONArray()
            for (i in 0 until node.childCount) {
                val child = node.getChild(i) ?: continue
                val childId = "node_$nodeCounter"
                childIds.put(childId)
                traverse(child, depth + 1)
            }

            val obj = JSONObject()
            obj.put("id", nodeId)
            obj.put("class", node.className?.toString() ?: "")
            obj.put("text", node.text?.toString() ?: "")
            obj.put("resourceId", node.viewIdResourceName ?: "")
            obj.put("contentDescription", node.contentDescription?.toString() ?: "")
            obj.put("bounds", JSONArray().apply {
                put(rect.left); put(rect.top); put(rect.right); put(rect.bottom)
            })
            obj.put("clickable", node.isClickable)
            obj.put("enabled", node.isEnabled)
            obj.put("focused", node.isFocused)
            obj.put("scrollable", node.isScrollable)
            obj.put("children", childIds)

            nodes.put(obj)
        }

        traverse(root, 0)
        result.put("nodes", nodes)

        Log.d(TAG, "Read ${nodeCounter} nodes from display $displayId")
        return result.toString()
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add aosp/packages/apps/DollOSService/src/org/dollos/service/accessibility/NodeReader.kt
git commit -m "feat: add NodeReader — accessibility tree to JSON serializer"
```

---

## Task 4: UIExecutor

**Goal:** Execute UI operations (click, swipe, type, gesture, global actions) via AccessibilityService.

**Files:**
- Create: `aosp/packages/apps/DollOSService/src/org/dollos/service/accessibility/UIExecutor.kt`

- [ ] **Step 1: Create UIExecutor.kt**

```kotlin
package org.dollos.service.accessibility

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.os.Bundle
import android.util.Log
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONObject

class UIExecutor(private val service: DollOSAccessibilityService) {

    companion object {
        private const val TAG = "UIExecutor"
    }

    /**
     * Execute a UI action from JSON command.
     * Returns JSON result: {"success": bool, "message": string}
     */
    fun execute(actionJson: String): String {
        return try {
            val cmd = JSONObject(actionJson)
            val type = cmd.getString("type")
            // displayId defaults to 0 (physical screen) if not specified
            val displayId = cmd.optInt("displayId", 0)

            when (type) {
                "click" -> performClick(cmd, displayId)
                "long_press" -> performLongPress(cmd, displayId)
                "input_text" -> performInputText(cmd, displayId)
                "swipe" -> performSwipe(cmd)
                "drag" -> performDrag(cmd)
                "multi_gesture" -> performMultiGesture(cmd)
                "back" -> performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK, "back")
                "home" -> performGlobalAction(AccessibilityService.GLOBAL_ACTION_HOME, "home")
                "recents" -> performGlobalAction(AccessibilityService.GLOBAL_ACTION_RECENTS, "recents")
                else -> result(false, "Unknown action type: $type")
            }
        } catch (e: Exception) {
            Log.e(TAG, "Action execution failed", e)
            result(false, "Error: ${e.message}")
        }
    }

    private fun performClick(cmd: JSONObject, displayId: Int): String {
        val node = findNodeById(cmd.getString("node_id"), displayId) ?: return result(false, "Node not found")
        val success = node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        return result(success, if (success) "Clicked" else "Click failed")
    }

    private fun performLongPress(cmd: JSONObject, displayId: Int): String {
        val node = findNodeById(cmd.getString("node_id"), displayId) ?: return result(false, "Node not found")
        val success = node.performAction(AccessibilityNodeInfo.ACTION_LONG_CLICK)
        return result(success, if (success) "Long pressed" else "Long press failed")
    }

    private fun performInputText(cmd: JSONObject, displayId: Int): String {
        val node = findNodeById(cmd.getString("node_id"), displayId) ?: return result(false, "Node not found")
        val text = cmd.getString("text")
        val args = Bundle().apply { putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text) }
        val success = node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
        return result(success, if (success) "Text set" else "Set text failed")
    }

    private fun performSwipe(cmd: JSONObject): String {
        val startX = cmd.getDouble("start_x").toFloat()
        val startY = cmd.getDouble("start_y").toFloat()
        val endX = cmd.getDouble("end_x").toFloat()
        val endY = cmd.getDouble("end_y").toFloat()
        val durationMs = cmd.optLong("duration_ms", 300)

        val path = Path().apply {
            moveTo(startX, startY)
            lineTo(endX, endY)
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()

        service.dispatchGesture(gesture, object : AccessibilityService.GestureResultCallback() {
            override fun onCompleted(gestureDescription: GestureDescription) {
                Log.d(TAG, "Swipe completed")
            }
            override fun onCancelled(gestureDescription: GestureDescription) {
                Log.w(TAG, "Swipe cancelled")
            }
        }, null)

        return result(true, "Swipe dispatched")
    }

    private fun performDrag(cmd: JSONObject): String {
        val startX = cmd.getDouble("start_x").toFloat()
        val startY = cmd.getDouble("start_y").toFloat()
        val endX = cmd.getDouble("end_x").toFloat()
        val endY = cmd.getDouble("end_y").toFloat()
        val durationMs = cmd.optLong("duration_ms", 1000)

        val path = Path().apply {
            moveTo(startX, startY)
            lineTo(endX, endY)
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()

        service.dispatchGesture(gesture, null, null)
        return result(true, "Drag dispatched")
    }

    private fun performMultiGesture(cmd: JSONObject): String {
        val strokes = cmd.getJSONArray("strokes")
        val builder = GestureDescription.Builder()

        for (i in 0 until strokes.length()) {
            val s = strokes.getJSONObject(i)
            val path = Path().apply {
                moveTo(s.getDouble("start_x").toFloat(), s.getDouble("start_y").toFloat())
                lineTo(s.getDouble("end_x").toFloat(), s.getDouble("end_y").toFloat())
            }
            val startTime = s.optLong("start_time", 0)
            val duration = s.optLong("duration_ms", 300)
            builder.addStroke(GestureDescription.StrokeDescription(path, startTime, duration))
        }

        service.dispatchGesture(builder.build(), null, null)
        return result(true, "Multi-gesture dispatched")
    }

    private fun performGlobalAction(action: Int, name: String): String {
        val success = service.performGlobalAction(action)
        return result(success, if (success) "$name executed" else "$name failed")
    }

    /**
     * Find a node by its "node_N" ID on a specific display.
     * Uses getWindows() filtered by displayId for multi-display support.
     */
    private fun findNodeById(nodeId: String, displayId: Int): AccessibilityNodeInfo? {
        val index = nodeId.removePrefix("node_").toIntOrNull() ?: return null

        val windows = service.windows
        val window = windows.firstOrNull {
            it.displayId == displayId && it.type == android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION && it.isFocused
        } ?: windows.firstOrNull {
            it.displayId == displayId && it.type == android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION
        } ?: return null

        val root = window.root ?: return null

        var counter = 0
        fun find(node: AccessibilityNodeInfo): AccessibilityNodeInfo? {
            if (counter == index) return node
            counter++
            for (i in 0 until node.childCount) {
                val child = node.getChild(i) ?: continue
                val result = find(child)
                if (result != null) return result
            }
            return null
        }

        return find(root)
    }

    private fun result(success: Boolean, message: String): String {
        return JSONObject().apply {
            put("success", success)
            put("message", message)
        }.toString()
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add aosp/packages/apps/DollOSService/src/org/dollos/service/accessibility/UIExecutor.kt
git commit -m "feat: add UIExecutor — click, swipe, type, gesture, global actions"
```

---

## Task 5: ScreenCapture

**Goal:** Capture screenshots from physical screen (AccessibilityService API) and VirtualDisplay (Surface).

**Files:**
- Create: `aosp/packages/apps/DollOSService/src/org/dollos/service/accessibility/ScreenCapture.kt`

- [ ] **Step 1: Create ScreenCapture.kt**

```kotlin
package org.dollos.service.accessibility

import android.accessibilityservice.AccessibilityService
import android.graphics.Bitmap
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.os.Handler
import android.os.Looper
import android.util.Log
import java.io.ByteArrayOutputStream

class ScreenCapture(private val service: DollOSAccessibilityService) {

    companion object {
        private const val TAG = "ScreenCapture"
    }

    interface CaptureCallback {
        fun onCaptureResult(displayId: Int, pngBytes: ByteArray?)
    }

    /**
     * Capture screenshot from physical display using AccessibilityService.takeScreenshot().
     * API 30+ only. Async — result delivered via callback.
     */
    fun capturePhysicalScreen(displayId: Int, callback: CaptureCallback) {
        service.takeScreenshot(
            displayId,
            service.mainExecutor,
            object : AccessibilityService.TakeScreenshotCallback() {
                override fun onSuccess(screenshot: ScreenshotResult) {
                    val bitmap = Bitmap.wrapHardwareBuffer(
                        screenshot.hardwareBuffer,
                        screenshot.colorSpace
                    )
                    if (bitmap != null) {
                        val stream = ByteArrayOutputStream()
                        bitmap.compress(Bitmap.CompressFormat.PNG, 90, stream)
                        callback.onCaptureResult(displayId, stream.toByteArray())
                        bitmap.recycle()
                        screenshot.hardwareBuffer.close()
                    } else {
                        Log.e(TAG, "Failed to create bitmap from screenshot")
                        callback.onCaptureResult(displayId, null)
                    }
                }

                override fun onFailure(errorCode: Int) {
                    Log.e(TAG, "Screenshot failed with error code: $errorCode")
                    callback.onCaptureResult(displayId, null)
                }
            }
        )
    }

    /**
     * Capture screenshot from a VirtualDisplay's ImageReader.
     * VirtualDisplayManager provides the ImageReader for each display.
     */
    fun captureVirtualDisplay(displayId: Int, imageReader: ImageReader, callback: CaptureCallback) {
        val image = imageReader.acquireLatestImage()
        if (image == null) {
            Log.w(TAG, "No image available from VirtualDisplay $displayId")
            callback.onCaptureResult(displayId, null)
            return
        }

        try {
            val plane = image.planes[0]
            val buffer = plane.buffer
            val pixelStride = plane.pixelStride
            val rowStride = plane.rowStride
            val rowPadding = rowStride - pixelStride * image.width

            val bitmap = Bitmap.createBitmap(
                image.width + rowPadding / pixelStride,
                image.height,
                Bitmap.Config.ARGB_8888
            )
            bitmap.copyPixelsFromBuffer(buffer)

            // Crop to actual width (remove padding)
            val cropped = Bitmap.createBitmap(bitmap, 0, 0, image.width, image.height)
            bitmap.recycle()

            val stream = ByteArrayOutputStream()
            cropped.compress(Bitmap.CompressFormat.PNG, 90, stream)
            callback.onCaptureResult(displayId, stream.toByteArray())
            cropped.recycle()
        } catch (e: Exception) {
            Log.e(TAG, "VirtualDisplay capture failed", e)
            callback.onCaptureResult(displayId, null)
        } finally {
            image.close()
        }
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add aosp/packages/apps/DollOSService/src/org/dollos/service/accessibility/ScreenCapture.kt
git commit -m "feat: add ScreenCapture — physical and VirtualDisplay capture"
```

---

## Task 6: VirtualDisplayManager

**Goal:** Create and manage VirtualDisplays for background AI UI operation.

**Files:**
- Create: `aosp/packages/apps/DollOSService/src/org/dollos/service/accessibility/VirtualDisplayManager.kt`

- [ ] **Step 1: Create VirtualDisplayManager.kt**

```kotlin
package org.dollos.service.accessibility

import android.app.ActivityOptions
import android.content.Context
import android.content.Intent
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.util.DisplayMetrics
import android.util.Log

class VirtualDisplayManager(private val context: Context) {

    companion object {
        private const val TAG = "VirtualDisplayManager"
        private const val DISPLAY_NAME = "DollOS-AI-Display"
    }

    data class ManagedDisplay(
        val virtualDisplay: VirtualDisplay,
        val imageReader: ImageReader,
        val displayId: Int
    )

    private val displays = mutableMapOf<Int, ManagedDisplay>()
    private val displayManager = context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager

    /**
     * Create a new VirtualDisplay with the same resolution as the physical screen.
     * Returns the displayId.
     */
    fun create(width: Int, height: Int): Int {
        val metrics = DisplayMetrics()
        val defaultDisplay = displayManager.getDisplay(android.view.Display.DEFAULT_DISPLAY)
        defaultDisplay.getRealMetrics(metrics)

        val w = if (width > 0) width else metrics.widthPixels
        val h = if (height > 0) height else metrics.heightPixels
        val dpi = metrics.densityDpi

        val imageReader = ImageReader.newInstance(w, h, android.graphics.PixelFormat.RGBA_8888, 2)

        val virtualDisplay = displayManager.createVirtualDisplay(
            DISPLAY_NAME,
            w, h, dpi,
            imageReader.surface,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_PUBLIC
                or DisplayManager.VIRTUAL_DISPLAY_FLAG_OWN_CONTENT_ONLY
        )

        val displayId = virtualDisplay.display.displayId
        displays[displayId] = ManagedDisplay(virtualDisplay, imageReader, displayId)

        Log.i(TAG, "Created VirtualDisplay $displayId (${w}x${h})")
        return displayId
    }

    /**
     * Launch an app on the specified VirtualDisplay.
     */
    fun launchApp(packageName: String, displayId: Int) {
        val pm = context.packageManager
        val launchIntent = pm.getLaunchIntentForPackage(packageName)
        if (launchIntent == null) {
            Log.e(TAG, "No launch intent for package: $packageName")
            return
        }

        launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_MULTIPLE_TASK)
        val options = ActivityOptions.makeBasic()
        options.launchDisplayId = displayId

        context.startActivity(launchIntent, options.toBundle())
        Log.i(TAG, "Launched $packageName on display $displayId")
    }

    /**
     * Get the ImageReader for a managed display (for screenshot capture).
     */
    fun getImageReader(displayId: Int): ImageReader? = displays[displayId]?.imageReader

    /**
     * Destroy a VirtualDisplay and clean up resources.
     */
    fun destroy(displayId: Int) {
        val managed = displays.remove(displayId)
        if (managed != null) {
            managed.virtualDisplay.release()
            managed.imageReader.close()
            Log.i(TAG, "Destroyed VirtualDisplay $displayId")
        }
    }

    /**
     * Destroy all managed VirtualDisplays.
     */
    fun destroyAll() {
        displays.keys.toList().forEach { destroy(it) }
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add aosp/packages/apps/DollOSService/src/org/dollos/service/accessibility/VirtualDisplayManager.kt
git commit -m "feat: add VirtualDisplayManager — create/destroy/launch on virtual displays"
```

---

## Task 7: TakeoverManager

**Goal:** Manage takeover state — floating bar overlay, edge glow, touch interception, and interrupt modal.

**Files:**
- Create: `aosp/packages/apps/DollOSService/res/layout/takeover_bar.xml`
- Create: `aosp/packages/apps/DollOSService/res/layout/takeover_interrupt_modal.xml`
- Create: `aosp/packages/apps/DollOSService/res/drawable/edge_glow.xml`
- Create: `aosp/packages/apps/DollOSService/src/org/dollos/service/accessibility/TakeoverManager.kt`

- [ ] **Step 1: Create takeover_bar.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="48dp"
    android:background="#CC1A1A2E"
    android:gravity="center"
    android:orientation="horizontal"
    android:paddingHorizontal="16dp">

    <ImageView
        android:layout_width="24dp"
        android:layout_height="24dp"
        android:src="@android:drawable/ic_menu_info_details"
        android:tint="#7C4DFF"
        android:layout_marginEnd="8dp" />

    <TextView
        android:id="@+id/takeover_text"
        android:layout_width="0dp"
        android:layout_height="wrap_content"
        android:layout_weight="1"
        android:textColor="#FFFFFF"
        android:textSize="14sp"
        android:singleLine="true"
        android:ellipsize="end" />
</LinearLayout>
```

- [ ] **Step 2: Create takeover_interrupt_modal.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<FrameLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="match_parent"
    android:background="#80000000">

    <LinearLayout
        android:layout_width="300dp"
        android:layout_height="wrap_content"
        android:layout_gravity="center"
        android:background="@android:drawable/dialog_holo_dark_frame"
        android:orientation="vertical"
        android:padding="24dp">

        <TextView
            android:id="@+id/interrupt_title"
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:text="@string/takeover_interrupt_title"
            android:textColor="#FFFFFF"
            android:textSize="18sp"
            android:textStyle="bold"
            android:layout_marginBottom="8dp" />

        <TextView
            android:id="@+id/interrupt_task_desc"
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:textColor="#CCCCCC"
            android:textSize="14sp"
            android:layout_marginBottom="24dp" />

        <LinearLayout
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:orientation="horizontal"
            android:gravity="end">

            <Button
                android:id="@+id/btn_cancel"
                android:layout_width="wrap_content"
                android:layout_height="wrap_content"
                android:text="@string/takeover_interrupt_cancel"
                android:layout_marginEnd="8dp"
                style="?android:attr/buttonBarNegativeButtonStyle" />

            <Button
                android:id="@+id/btn_continue"
                android:layout_width="wrap_content"
                android:layout_height="wrap_content"
                android:text="@string/takeover_interrupt_continue"
                style="?android:attr/buttonBarPositiveButtonStyle" />
        </LinearLayout>
    </LinearLayout>
</FrameLayout>
```

- [ ] **Step 3: Create edge_glow.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<shape xmlns:android="http://schemas.android.com/apk/res/android"
    android:shape="rectangle">
    <stroke
        android:width="3dp"
        android:color="#7C4DFF" />
    <corners android:radius="0dp" />
</shape>
```

- [ ] **Step 4: Create TakeoverManager.kt**

```kotlin
package org.dollos.service.accessibility

import android.content.Context
import android.graphics.PixelFormat
import android.util.Log
import android.view.Gravity
import android.view.LayoutInflater
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.FrameLayout
import android.widget.TextView
import org.dollos.service.R

class TakeoverManager(private val service: DollOSAccessibilityService) {

    companion object {
        private const val TAG = "TakeoverManager"
    }

    var isActive: Boolean = false
        private set

    private var taskDescription: String = ""
    private var barView: View? = null
    private var touchBlocker: View? = null
    private var edgeGlow: View? = null
    private var interruptModal: View? = null
    private var onCancelListener: (() -> Unit)? = null

    private val windowManager: WindowManager
        get() = service.getSystemService(Context.WINDOW_SERVICE) as WindowManager

    /**
     * Start takeover mode. Shows floating bar + edge glow + touch blocker.
     */
    fun start(description: String, onCancel: () -> Unit) {
        if (isActive) return
        isActive = true
        taskDescription = description
        onCancelListener = onCancel

        addTouchBlocker()
        addEdgeGlow()
        addBar(description)

        Log.i(TAG, "Takeover started: $description")
    }

    /**
     * Stop takeover mode. Remove all overlays.
     */
    fun stop() {
        if (!isActive) return
        isActive = false
        removeAll()
        Log.i(TAG, "Takeover stopped")
    }

    /**
     * Show interrupt modal (triggered by power button press).
     */
    fun showInterruptModal() {
        if (interruptModal != null) return

        val inflater = LayoutInflater.from(service)
        interruptModal = inflater.inflate(R.layout.takeover_interrupt_modal, null)

        interruptModal!!.findViewById<TextView>(R.id.interrupt_task_desc).text = taskDescription

        interruptModal!!.findViewById<Button>(R.id.btn_cancel).setOnClickListener {
            dismissInterruptModal()
            stop()
            onCancelListener?.invoke()
        }

        interruptModal!!.findViewById<Button>(R.id.btn_continue).setOnClickListener {
            dismissInterruptModal()
        }

        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
            PixelFormat.TRANSLUCENT
        )

        windowManager.addView(interruptModal, params)
        Log.d(TAG, "Interrupt modal shown")
    }

    fun cleanup() {
        removeAll()
    }

    private fun addTouchBlocker() {
        touchBlocker = FrameLayout(service)
        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
            PixelFormat.TRANSLUCENT
        )
        // This transparent overlay consumes all touch events
        touchBlocker!!.setOnTouchListener { _, _ -> true }
        windowManager.addView(touchBlocker, params)
    }

    private fun addEdgeGlow() {
        edgeGlow = FrameLayout(service).apply {
            setBackgroundResource(R.drawable.edge_glow)
        }
        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE,
            PixelFormat.TRANSLUCENT
        )
        windowManager.addView(edgeGlow, params)
    }

    private fun addBar(description: String) {
        val inflater = LayoutInflater.from(service)
        barView = inflater.inflate(R.layout.takeover_bar, null)
        barView!!.findViewById<TextView>(R.id.takeover_text).text =
            service.getString(R.string.takeover_bar_text, description)

        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE,
            PixelFormat.TRANSLUCENT
        )
        params.gravity = Gravity.TOP
        windowManager.addView(barView, params)
    }

    private fun dismissInterruptModal() {
        interruptModal?.let {
            windowManager.removeView(it)
            interruptModal = null
        }
    }

    private fun removeAll() {
        dismissInterruptModal()
        barView?.let { windowManager.removeView(it); barView = null }
        edgeGlow?.let { windowManager.removeView(it); edgeGlow = null }
        touchBlocker?.let { windowManager.removeView(it); touchBlocker = null }
    }
}
```

- [ ] **Step 5: Commit**

```bash
git add aosp/packages/apps/DollOSService/res/layout/takeover_bar.xml \
        aosp/packages/apps/DollOSService/res/layout/takeover_interrupt_modal.xml \
        aosp/packages/apps/DollOSService/res/drawable/edge_glow.xml \
        aosp/packages/apps/DollOSService/src/org/dollos/service/accessibility/TakeoverManager.kt
git commit -m "feat: add TakeoverManager — overlay bar, edge glow, touch block, interrupt modal"
```

---

## Task 8: AppEventMonitor

**Goal:** Track foreground app changes from accessibility events. Push to DollOSAIService via callback.

**Files:**
- Create: `aosp/packages/apps/DollOSService/src/org/dollos/service/accessibility/AppEventMonitor.kt`

- [ ] **Step 1: Create AppEventMonitor.kt**

```kotlin
package org.dollos.service.accessibility

import android.util.Log
import android.view.accessibility.AccessibilityEvent

class AppEventMonitor {

    companion object {
        private const val TAG = "AppEventMonitor"
    }

    var currentForegroundPackage: String = ""
        private set

    interface AppEventListener {
        fun onAppChanged(packageName: String, eventType: String)
    }

    private var listener: AppEventListener? = null

    fun setListener(listener: AppEventListener?) {
        this.listener = listener
    }

    fun onWindowStateChanged(event: AccessibilityEvent) {
        val packageName = event.packageName?.toString() ?: return

        // Filter out system UI and keyboard packages
        if (packageName == "com.android.systemui") return
        if (packageName.contains("inputmethod")) return

        if (packageName != currentForegroundPackage) {
            val previousPackage = currentForegroundPackage
            currentForegroundPackage = packageName

            if (previousPackage.isNotEmpty()) {
                listener?.onAppChanged(previousPackage, "closed")
                Log.d(TAG, "App closed: $previousPackage")
            }
            listener?.onAppChanged(packageName, "opened")
            Log.d(TAG, "App opened: $packageName")
        }
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add aosp/packages/apps/DollOSService/src/org/dollos/service/accessibility/AppEventMonitor.kt
git commit -m "feat: add AppEventMonitor — track foreground app changes"
```

---

## Task 9: AIDL + DollOSServiceImpl — UI Operation Methods

**Goal:** Add UI operation methods to IDollOSService AIDL and implement them in DollOSServiceImpl.

**Files:**
- Modify: `aosp/packages/apps/DollOSService/aidl/org/dollos/service/IDollOSService.aidl`
- Modify: `aosp/packages/apps/DollOSService/src/org/dollos/service/DollOSServiceImpl.kt`

- [ ] **Step 0: Create ICaptureCallback.aidl**

Create `aosp/packages/apps/DollOSService/aidl/org/dollos/service/ICaptureCallback.aidl`:

```aidl
package org.dollos.service;

interface ICaptureCallback {
    void onCaptureResult(int displayId, in byte[] pngBytes);
}
```

- [ ] **Step 1: Update IDollOSService.aidl**

Add import at top: `import org.dollos.service.ICaptureCallback;`

Add after `showTaskManager()`:

```aidl
    /** Read the accessibility node tree from a display as JSON */
    String readScreen(int displayId);

    /** Execute a UI action (click, swipe, type, etc.) from JSON command */
    String executeUIAction(String actionJson);

    /** Capture screenshot asynchronously - result delivered via ICaptureCallback */
    oneway void captureScreen(int displayId, ICaptureCallback callback);

    /** Enter takeover mode with task description */
    void startTakeover(String taskDescription);

    /** Exit takeover mode */
    void stopTakeover();

    /** Create a VirtualDisplay, returns displayId */
    int createVirtualDisplay(int width, int height);

    /** Destroy a VirtualDisplay */
    void destroyVirtualDisplay(int displayId);

    /** Launch an app on a specific display */
    void launchAppOnDisplay(String packageName, int displayId);
```

- [ ] **Step 2: Initialize VirtualDisplayManager in DollOSApp.kt**

Add to DollOSApp companion object:

```kotlin
lateinit var virtualDisplayManager: VirtualDisplayManager
    private set
```

Add to onCreate():

```kotlin
virtualDisplayManager = VirtualDisplayManager(this)
```

- [ ] **Step 3: Implement UI methods in DollOSServiceImpl.kt**

Add imports and implementations:

```kotlin
import org.dollos.service.accessibility.DollOSAccessibilityService
import org.dollos.service.accessibility.ScreenCapture

override fun readScreen(displayId: Int): String {
    val a11y = DollOSAccessibilityService.instance
        ?: return """{"package":"","displayId":$displayId,"nodes":[],"error":"AccessibilityService not running"}"""
    return a11y.nodeReader.readScreen(displayId)
}

override fun executeUIAction(actionJson: String): String {
    val a11y = DollOSAccessibilityService.instance
        ?: return """{"success":false,"message":"AccessibilityService not running"}"""
    return a11y.uiExecutor.execute(actionJson)
}

override fun captureScreen(displayId: Int, callback: ICaptureCallback) {
    val a11y = DollOSAccessibilityService.instance ?: run {
        callback.onCaptureResult(displayId, null)
        return
    }
    val vdImageReader = DollOSApp.virtualDisplayManager.getImageReader(displayId)

    val captureCallback = object : ScreenCapture.CaptureCallback {
        override fun onCaptureResult(displayId: Int, pngBytes: ByteArray?) {
            callback.onCaptureResult(displayId, pngBytes)
        }
    }

    if (vdImageReader != null) {
        a11y.screenCapture.captureVirtualDisplay(displayId, vdImageReader, captureCallback)
    } else {
        a11y.screenCapture.capturePhysicalScreen(displayId, captureCallback)
    }
}

override fun startTakeover(taskDescription: String) {
    val a11y = DollOSAccessibilityService.instance ?: return
    a11y.takeoverManager.start(taskDescription) {
        // onCancel callback — notify AIService
        Log.i("DollOSServiceImpl", "Takeover cancelled by user")
        // TODO: notify AIService via callback when callback is registered
    }
}

override fun stopTakeover() {
    DollOSAccessibilityService.instance?.takeoverManager?.stop()
}

override fun createVirtualDisplay(width: Int, height: Int): Int {
    return DollOSApp.virtualDisplayManager.create(width, height)
}

override fun destroyVirtualDisplay(displayId: Int) {
    DollOSApp.virtualDisplayManager.destroy(displayId)
}

override fun launchAppOnDisplay(packageName: String, displayId: Int) {
    DollOSApp.virtualDisplayManager.launchApp(packageName, displayId)
}
```

- [ ] **Step 4: Commit**

```bash
git add aosp/packages/apps/DollOSService/aidl/org/dollos/service/ICaptureCallback.aidl \
        aosp/packages/apps/DollOSService/aidl/org/dollos/service/IDollOSService.aidl \
        aosp/packages/apps/DollOSService/src/org/dollos/service/DollOSApp.kt \
        aosp/packages/apps/DollOSService/src/org/dollos/service/DollOSServiceImpl.kt
git commit -m "feat: add UI operation AIDL methods and service implementation"
```

---

## Task 10: Smart Notification — NotificationLevel + QuietHoursConfig + TTSInterface

**Goal:** Create the notification data model and utility classes.

**Files:**
- Create: `aosp/packages/apps/DollOSService/src/org/dollos/service/notification/NotificationLevel.kt`
- Create: `aosp/packages/apps/DollOSService/src/org/dollos/service/notification/QuietHoursConfig.kt`
- Create: `aosp/packages/apps/DollOSService/src/org/dollos/service/notification/TTSInterface.kt`

Note: The spec says these live in DollOSAIService, but since that app doesn't exist yet, we place them in DollOSService for now. They can be moved when DollOSAIService is scaffolded (both share `platform_apis: true`).

- [ ] **Step 1: Create NotificationLevel.kt**

```kotlin
package org.dollos.service.notification

enum class NotificationLevel {
    SILENT,   // no sound, no vibration
    NORMAL,   // sound + vibration
    URGENT    // TTS + notification
}
```

- [ ] **Step 2: Create QuietHoursConfig.kt**

```kotlin
package org.dollos.service.notification

import java.time.LocalTime

data class QuietHoursConfig(
    val enabled: Boolean = true,
    val startTime: LocalTime = LocalTime.of(23, 0),
    val endTime: LocalTime = LocalTime.of(7, 0)
) {
    fun isQuietNow(): Boolean {
        if (!enabled) return false
        val now = LocalTime.now()
        return if (startTime <= endTime) {
            // Same day range (e.g., 09:00 - 17:00)
            now in startTime..endTime
        } else {
            // Overnight range (e.g., 23:00 - 07:00)
            now >= startTime || now <= endTime
        }
    }
}
```

- [ ] **Step 3: Create TTSInterface.kt**

```kotlin
package org.dollos.service.notification

/**
 * Abstract TTS interface. No-op default until voice pipeline is implemented.
 */
interface TTSInterface {
    fun speak(text: String, priority: Int)
    fun stop()
    val isAvailable: Boolean
}

class NoOpTTS : TTSInterface {
    override fun speak(text: String, priority: Int) {
        // No-op: TTS not yet available
    }
    override fun stop() {}
    override val isAvailable: Boolean = false
}
```

- [ ] **Step 4: Commit**

```bash
git add aosp/packages/apps/DollOSService/src/org/dollos/service/notification/
git commit -m "feat: add NotificationLevel, QuietHoursConfig, TTSInterface"
```

---

## Task 11: Smart Notification — NotificationRouter

**Goal:** Implement notification routing decision logic.

**Files:**
- Create: `aosp/packages/apps/DollOSService/src/org/dollos/service/notification/NotificationRouter.kt`

- [ ] **Step 1: Create NotificationRouter.kt**

```kotlin
package org.dollos.service.notification

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.media.AudioAttributes
import android.os.Build
import android.provider.Settings
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat

class NotificationRouter(private val context: Context) {

    companion object {
        private const val TAG = "NotificationRouter"
        const val CHANNEL_SILENT = "dollos_silent"
        const val CHANNEL_NORMAL = "dollos_normal"
        const val CHANNEL_URGENT = "dollos_urgent"
    }

    var quietHours = QuietHoursConfig()
    var tts: TTSInterface = NoOpTTS()
    private val notificationManager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
    private val perEventOverrides = mutableMapOf<String, NotificationLevel>()
    private var notificationId = 1000

    init {
        createChannels()
    }

    /**
     * Set per-event-type notification level override.
     * eventType examples: "WORKER_RESULT", "SCHEDULE", "SYSTEM_EVENT"
     */
    fun setOverride(eventType: String, level: NotificationLevel) {
        perEventOverrides[eventType] = level
    }

    fun removeOverride(eventType: String) {
        perEventOverrides.remove(eventType)
    }

    /**
     * Route a notification based on event priority, type, and context.
     * priority: "HIGH", "NORMAL", "LOW"
     * eventType: matches EventType from Plan D v1
     */
    fun route(title: String, message: String, priority: String, eventType: String) {
        val level = decide(priority, eventType)
        dispatch(title, message, level)
        Log.d(TAG, "Routed notification: level=$level, priority=$priority, eventType=$eventType")
    }

    private fun decide(priority: String, eventType: String): NotificationLevel {
        // Rule 1: DND on → SILENT
        if (isDndOn()) return NotificationLevel.SILENT

        // Rule 2: Quiet hours → SILENT
        if (quietHours.isQuietNow()) return NotificationLevel.SILENT

        // Rule 3: Per-event-type override
        perEventOverrides[eventType]?.let { return it }

        // Rules 4-7: Priority-based
        return when (priority) {
            "HIGH" -> if (isScreenOff()) NotificationLevel.URGENT else NotificationLevel.NORMAL
            "NORMAL" -> NotificationLevel.NORMAL
            "LOW" -> NotificationLevel.SILENT
            else -> NotificationLevel.NORMAL
        }
    }

    private fun dispatch(title: String, message: String, level: NotificationLevel) {
        val channelId = when (level) {
            NotificationLevel.SILENT -> CHANNEL_SILENT
            NotificationLevel.NORMAL -> CHANNEL_NORMAL
            NotificationLevel.URGENT -> CHANNEL_URGENT
        }

        val notification = NotificationCompat.Builder(context, channelId)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(title)
            .setContentText(message)
            .setAutoCancel(true)
            .build()

        notificationManager.notify(notificationId++, notification)

        if (level == NotificationLevel.URGENT && tts.isAvailable) {
            tts.speak("$title. $message", 1)
        }
    }

    private fun isDndOn(): Boolean {
        return notificationManager.currentInterruptionFilter != NotificationManager.INTERRUPTION_FILTER_ALL
    }

    private fun isScreenOff(): Boolean {
        val pm = context.getSystemService(Context.POWER_SERVICE) as android.os.PowerManager
        return !pm.isInteractive
    }

    private fun createChannels() {
        val silent = NotificationChannel(CHANNEL_SILENT, "DollOS Silent", NotificationManager.IMPORTANCE_LOW).apply {
            setSound(null, null)
            enableVibration(false)
        }

        val normal = NotificationChannel(CHANNEL_NORMAL, "DollOS Normal", NotificationManager.IMPORTANCE_DEFAULT)

        val urgent = NotificationChannel(CHANNEL_URGENT, "DollOS Urgent", NotificationManager.IMPORTANCE_HIGH).apply {
            enableVibration(true)
            setSound(
                Settings.System.DEFAULT_NOTIFICATION_URI,
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_NOTIFICATION)
                    .build()
            )
        }

        notificationManager.createNotificationChannel(silent)
        notificationManager.createNotificationChannel(normal)
        notificationManager.createNotificationChannel(urgent)

        Log.i(TAG, "Notification channels created")
    }
}
```

- [ ] **Step 2: Initialize NotificationRouter in DollOSApp.kt**

Add to companion object:

```kotlin
lateinit var notificationRouter: NotificationRouter
    private set
```

Add to onCreate():

```kotlin
notificationRouter = NotificationRouter(this)
```

- [ ] **Step 3: Commit**

```bash
git add aosp/packages/apps/DollOSService/src/org/dollos/service/notification/NotificationRouter.kt \
        aosp/packages/apps/DollOSService/src/org/dollos/service/DollOSApp.kt
git commit -m "feat: add NotificationRouter — context-aware notification routing"
```

---

## Task 12: Programmable Events — Rule Data Model

**Goal:** Create Rule, Condition, enums, and Room entity/DAO.

**Files:**
- Create: `aosp/packages/apps/DollOSService/src/org/dollos/service/rule/Rule.kt`
- Create: `aosp/packages/apps/DollOSService/src/org/dollos/service/rule/RuleEntity.kt`
- Create: `aosp/packages/apps/DollOSService/src/org/dollos/service/rule/RuleDao.kt`

Note: Room persistence is placed in DollOSService for now. The spec says DollOSAIService, but that doesn't exist yet. The data model is portable.

- [ ] **Step 1: Create Rule.kt**

```kotlin
package org.dollos.service.rule

import org.json.JSONArray
import org.json.JSONObject

enum class ConditionType {
    SCREEN_STATE,
    CHARGING_STATE,
    WIFI_STATE,
    WIFI_SSID,
    BLUETOOTH_STATE,
    BLUETOOTH_DEVICE,
    BATTERY_LEVEL,
    APP_FOREGROUND,
    TIME_RANGE,
    DAY_OF_WEEK
}

enum class Operator {
    EQUALS,
    NOT_EQUALS,
    LESS_THAN,
    GREATER_THAN
}

enum class RuleAction {
    NOTIFY,
    SPAWN_WORKER,
    SEND_EVENT
}

data class Condition(
    val type: ConditionType,
    val operator: Operator,
    val value: String
) {
    fun toJson(): JSONObject = JSONObject().apply {
        put("type", type.name)
        put("operator", operator.name)
        put("value", value)
    }

    companion object {
        fun fromJson(json: JSONObject): Condition = Condition(
            type = ConditionType.valueOf(json.getString("type")),
            operator = Operator.valueOf(json.getString("operator")),
            value = json.getString("value")
        )
    }
}

data class Rule(
    val id: String,
    val name: String,
    val enabled: Boolean = true,
    val conditions: List<Condition>,
    val action: RuleAction,
    val actionParams: String = "{}",
    val createdBy: String = "user",
    val naturalLanguage: String = "",
    val debouncePeriodMs: Long = 60_000,
    val createdAt: Long = System.currentTimeMillis()
) {
    fun toJson(): JSONObject = JSONObject().apply {
        put("id", id)
        put("name", name)
        put("enabled", enabled)
        put("conditions", JSONArray().apply {
            conditions.forEach { put(it.toJson()) }
        })
        put("action", action.name)
        put("actionParams", actionParams)
        put("createdBy", createdBy)
        put("naturalLanguage", naturalLanguage)
        put("debouncePeriodMs", debouncePeriodMs)
        put("createdAt", createdAt)
    }

    companion object {
        fun fromJson(json: JSONObject): Rule {
            val conditionsArray = json.getJSONArray("conditions")
            val conditions = (0 until conditionsArray.length()).map {
                Condition.fromJson(conditionsArray.getJSONObject(it))
            }
            return Rule(
                id = json.getString("id"),
                name = json.getString("name"),
                enabled = json.optBoolean("enabled", true),
                conditions = conditions,
                action = RuleAction.valueOf(json.getString("action")),
                actionParams = json.optString("actionParams", "{}"),
                createdBy = json.optString("createdBy", "user"),
                naturalLanguage = json.optString("naturalLanguage", ""),
                debouncePeriodMs = json.optLong("debouncePeriodMs", 60_000),
                createdAt = json.optLong("createdAt", System.currentTimeMillis())
            )
        }
    }
}
```

- [ ] **Step 2: Create RuleEntity.kt**

```kotlin
package org.dollos.service.rule

import android.content.ContentValues
import android.database.Cursor

/**
 * SQLite-backed entity for Rule persistence.
 * Uses raw SQLite (not Room) since DollOSService doesn't have Room dependency.
 * Room version will live in DollOSAIService when it's scaffolded.
 */
object RuleTable {
    const val TABLE_NAME = "rules"

    const val COL_ID = "id"
    const val COL_NAME = "name"
    const val COL_ENABLED = "enabled"
    const val COL_CONDITIONS_JSON = "conditions_json"
    const val COL_ACTION = "action"
    const val COL_ACTION_PARAMS = "action_params"
    const val COL_CREATED_BY = "created_by"
    const val COL_NATURAL_LANGUAGE = "natural_language"
    const val COL_DEBOUNCE_MS = "debounce_period_ms"
    const val COL_CREATED_AT = "created_at"

    const val CREATE_TABLE = """
        CREATE TABLE IF NOT EXISTS $TABLE_NAME (
            $COL_ID TEXT PRIMARY KEY,
            $COL_NAME TEXT NOT NULL,
            $COL_ENABLED INTEGER NOT NULL DEFAULT 1,
            $COL_CONDITIONS_JSON TEXT NOT NULL,
            $COL_ACTION TEXT NOT NULL,
            $COL_ACTION_PARAMS TEXT NOT NULL DEFAULT '{}',
            $COL_CREATED_BY TEXT NOT NULL DEFAULT 'user',
            $COL_NATURAL_LANGUAGE TEXT NOT NULL DEFAULT '',
            $COL_DEBOUNCE_MS INTEGER NOT NULL DEFAULT 60000,
            $COL_CREATED_AT INTEGER NOT NULL
        )
    """

    fun toContentValues(rule: Rule): ContentValues = ContentValues().apply {
        put(COL_ID, rule.id)
        put(COL_NAME, rule.name)
        put(COL_ENABLED, if (rule.enabled) 1 else 0)
        put(COL_CONDITIONS_JSON, org.json.JSONArray().apply { rule.conditions.forEach { put(it.toJson()) } }.toString())
        put(COL_ACTION, rule.action.name)
        put(COL_ACTION_PARAMS, rule.actionParams)
        put(COL_CREATED_BY, rule.createdBy)
        put(COL_NATURAL_LANGUAGE, rule.naturalLanguage)
        put(COL_DEBOUNCE_MS, rule.debouncePeriodMs)
        put(COL_CREATED_AT, rule.createdAt)
    }

    fun fromCursor(cursor: Cursor): Rule {
        val conditionsRaw = cursor.getString(cursor.getColumnIndexOrThrow(COL_CONDITIONS_JSON))
        val conditions = if (conditionsRaw.isEmpty() || conditionsRaw == "[]") emptyList() else {
            val arr = org.json.JSONArray(conditionsRaw)
            (0 until arr.length()).map { Condition.fromJson(arr.getJSONObject(it)) }
        }
        return Rule(
            id = cursor.getString(cursor.getColumnIndexOrThrow(COL_ID)),
            name = cursor.getString(cursor.getColumnIndexOrThrow(COL_NAME)),
            enabled = cursor.getInt(cursor.getColumnIndexOrThrow(COL_ENABLED)) == 1,
            conditions = conditions,
            action = RuleAction.valueOf(cursor.getString(cursor.getColumnIndexOrThrow(COL_ACTION))),
            actionParams = cursor.getString(cursor.getColumnIndexOrThrow(COL_ACTION_PARAMS)),
            createdBy = cursor.getString(cursor.getColumnIndexOrThrow(COL_CREATED_BY)),
            naturalLanguage = cursor.getString(cursor.getColumnIndexOrThrow(COL_NATURAL_LANGUAGE)),
            debouncePeriodMs = cursor.getLong(cursor.getColumnIndexOrThrow(COL_DEBOUNCE_MS)),
            createdAt = cursor.getLong(cursor.getColumnIndexOrThrow(COL_CREATED_AT))
        )
    }
}
```

- [ ] **Step 3: Create RuleDao.kt**

```kotlin
package org.dollos.service.rule

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.util.Log

class RuleDao(context: Context) : SQLiteOpenHelper(context, DB_NAME, null, DB_VERSION) {

    companion object {
        private const val TAG = "RuleDao"
        private const val DB_NAME = "dollos_rules.db"
        private const val DB_VERSION = 1
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(RuleTable.CREATE_TABLE)
        Log.i(TAG, "Rules database created")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        // Future migrations
    }

    fun insert(rule: Rule) {
        writableDatabase.insertWithOnConflict(
            RuleTable.TABLE_NAME,
            null,
            RuleTable.toContentValues(rule),
            SQLiteDatabase.CONFLICT_REPLACE
        )
    }

    fun update(rule: Rule) {
        writableDatabase.update(
            RuleTable.TABLE_NAME,
            RuleTable.toContentValues(rule),
            "${RuleTable.COL_ID} = ?",
            arrayOf(rule.id)
        )
    }

    fun delete(ruleId: String) {
        writableDatabase.delete(
            RuleTable.TABLE_NAME,
            "${RuleTable.COL_ID} = ?",
            arrayOf(ruleId)
        )
    }

    fun setEnabled(ruleId: String, enabled: Boolean) {
        val cv = android.content.ContentValues().apply {
            put(RuleTable.COL_ENABLED, if (enabled) 1 else 0)
        }
        writableDatabase.update(
            RuleTable.TABLE_NAME,
            cv,
            "${RuleTable.COL_ID} = ?",
            arrayOf(ruleId)
        )
    }

    fun getAll(): List<Rule> {
        val rules = mutableListOf<Rule>()
        val cursor = readableDatabase.query(
            RuleTable.TABLE_NAME, null, null, null, null, null,
            "${RuleTable.COL_CREATED_AT} DESC"
        )
        cursor.use {
            while (it.moveToNext()) {
                rules.add(RuleTable.fromCursor(it))
            }
        }
        return rules
    }

    fun getEnabled(): List<Rule> {
        val rules = mutableListOf<Rule>()
        val cursor = readableDatabase.query(
            RuleTable.TABLE_NAME, null,
            "${RuleTable.COL_ENABLED} = 1",
            null, null, null, null
        )
        cursor.use {
            while (it.moveToNext()) {
                rules.add(RuleTable.fromCursor(it))
            }
        }
        return rules
    }
}
```

- [ ] **Step 4: Commit**

```bash
git add aosp/packages/apps/DollOSService/src/org/dollos/service/rule/
git commit -m "feat: add Rule data model, RuleEntity, RuleDao with SQLite persistence"
```

---

## Task 13: Programmable Events — RuleEngine

**Goal:** Evaluate rules against incoming events, trigger actions with debounce.

**Files:**
- Create: `aosp/packages/apps/DollOSService/src/org/dollos/service/rule/RuleEngine.kt`

- [ ] **Step 1: Create RuleEngine.kt**

```kotlin
package org.dollos.service.rule

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.PowerManager
import android.util.Log
import org.dollos.service.accessibility.AppEventMonitor
import org.dollos.service.notification.NotificationRouter
import java.time.DayOfWeek
import java.time.LocalTime
import java.time.format.TextStyle
import java.util.Locale
import java.util.concurrent.ConcurrentHashMap

class RuleEngine(
    private val context: Context,
    private val ruleDao: RuleDao,
    private val notificationRouter: NotificationRouter
) : AppEventMonitor.AppEventListener {

    companion object {
        private const val TAG = "RuleEngine"
    }

    private var rules = listOf<Rule>()
    private val lastFired = ConcurrentHashMap<String, Long>()

    // Current state cache
    private var screenState = "on"
    private var chargingState = "discharging"
    private var wifiState = "disconnected"
    private var wifiSsid = ""
    private var bluetoothState = "disconnected"
    private var bluetoothDevice = ""
    private var batteryLevel = 100
    private var foregroundApp = ""

    private val systemReceiver = object : BroadcastReceiver() {
        override fun onReceive(ctx: Context, intent: Intent) {
            when (intent.action) {
                Intent.ACTION_SCREEN_ON -> {
                    screenState = "on"
                    evaluate("SCREEN_STATE")
                }
                Intent.ACTION_SCREEN_OFF -> {
                    screenState = "off"
                    evaluate("SCREEN_STATE")
                }
                Intent.ACTION_POWER_CONNECTED -> {
                    chargingState = "charging"
                    evaluate("CHARGING_STATE")
                }
                Intent.ACTION_POWER_DISCONNECTED -> {
                    chargingState = "discharging"
                    evaluate("CHARGING_STATE")
                }
                WifiManager.NETWORK_STATE_CHANGED_ACTION -> {
                    val networkInfo = intent.getParcelableExtra<android.net.NetworkInfo>(WifiManager.EXTRA_NETWORK_INFO)
                    val wasConnected = wifiState == "connected"
                    wifiState = if (networkInfo?.isConnected == true) "connected" else "disconnected"

                    // Update SSID when connected
                    if (wifiState == "connected") {
                        val wifiManager = context.getSystemService(Context.WIFI_SERVICE) as WifiManager
                        wifiSsid = wifiManager.connectionInfo?.ssid?.removeSurrounding("\"") ?: ""
                    } else {
                        wifiSsid = ""
                    }

                    evaluate("WIFI_STATE")
                    if (wifiSsid.isNotEmpty()) evaluate("WIFI_SSID")
                }
                Intent.ACTION_BATTERY_CHANGED -> {
                    val level = intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
                    val scale = intent.getIntExtra(BatteryManager.EXTRA_SCALE, -1)
                    if (level >= 0 && scale > 0) {
                        batteryLevel = (level * 100) / scale
                        evaluate("BATTERY_LEVEL")
                    }
                }
                android.bluetooth.BluetoothAdapter.ACTION_CONNECTION_STATE_CHANGED -> {
                    val state = intent.getIntExtra(android.bluetooth.BluetoothAdapter.EXTRA_CONNECTION_STATE, -1)
                    bluetoothState = if (state == android.bluetooth.BluetoothAdapter.STATE_CONNECTED) "connected" else "disconnected"
                    evaluate("BLUETOOTH_STATE")
                }
            }
        }
    }

    fun start() {
        rules = ruleDao.getEnabled()
        Log.i(TAG, "Loaded ${rules.size} enabled rules")

        val filter = IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_SCREEN_OFF)
            addAction(Intent.ACTION_POWER_CONNECTED)
            addAction(Intent.ACTION_POWER_DISCONNECTED)
            addAction(WifiManager.NETWORK_STATE_CHANGED_ACTION)
            addAction(Intent.ACTION_BATTERY_CHANGED)
            addAction(android.bluetooth.BluetoothAdapter.ACTION_CONNECTION_STATE_CHANGED)
        }
        context.registerReceiver(systemReceiver, filter)

        // Init current state
        val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        screenState = if (pm.isInteractive) "on" else "off"
    }

    fun stop() {
        try {
            context.unregisterReceiver(systemReceiver)
        } catch (_: Exception) {}
    }

    fun reloadRules() {
        rules = ruleDao.getEnabled()
        Log.d(TAG, "Reloaded ${rules.size} enabled rules")
    }

    // AppEventMonitor.AppEventListener
    override fun onAppChanged(packageName: String, eventType: String) {
        if (eventType == "opened") {
            foregroundApp = packageName
        }
        evaluate("APP_FOREGROUND")
    }

    private fun evaluate(triggerSource: String) {
        val now = System.currentTimeMillis()

        for (rule in rules) {
            if (!rule.enabled) continue

            // Debounce check
            val lastTime = lastFired[rule.id] ?: 0
            if (now - lastTime < rule.debouncePeriodMs) continue

            // Check if any condition uses the trigger source
            val relevant = rule.conditions.any { it.type.name == triggerSource }
            if (!relevant) continue

            // Evaluate all conditions (AND)
            val allMet = rule.conditions.all { checkCondition(it) }
            if (!allMet) continue

            lastFired[rule.id] = now
            triggerAction(rule)
        }
    }

    private fun checkCondition(condition: Condition): Boolean {
        val currentValue = when (condition.type) {
            ConditionType.SCREEN_STATE -> screenState
            ConditionType.CHARGING_STATE -> chargingState
            ConditionType.WIFI_STATE -> wifiState
            ConditionType.WIFI_SSID -> wifiSsid
            ConditionType.BLUETOOTH_STATE -> bluetoothState
            ConditionType.BLUETOOTH_DEVICE -> bluetoothDevice
            ConditionType.BATTERY_LEVEL -> batteryLevel.toString()
            ConditionType.APP_FOREGROUND -> foregroundApp
            ConditionType.TIME_RANGE -> "check_special"
            ConditionType.DAY_OF_WEEK -> "check_special"
        }

        if (condition.type == ConditionType.TIME_RANGE) {
            return checkTimeRange(condition.value)
        }
        if (condition.type == ConditionType.DAY_OF_WEEK) {
            return checkDayOfWeek(condition.value)
        }

        return when (condition.operator) {
            Operator.EQUALS -> currentValue == condition.value
            Operator.NOT_EQUALS -> currentValue != condition.value
            Operator.LESS_THAN -> {
                val a = currentValue.toIntOrNull() ?: return false
                val b = condition.value.toIntOrNull() ?: return false
                a < b
            }
            Operator.GREATER_THAN -> {
                val a = currentValue.toIntOrNull() ?: return false
                val b = condition.value.toIntOrNull() ?: return false
                a > b
            }
        }
    }

    private fun checkTimeRange(value: String): Boolean {
        // value format: "HH:mm-HH:mm"
        val parts = value.split("-")
        if (parts.size != 2) return false
        val start = LocalTime.parse(parts[0].trim())
        val end = LocalTime.parse(parts[1].trim())
        val now = LocalTime.now()
        return if (start <= end) now in start..end else (now >= start || now <= end)
    }

    private fun checkDayOfWeek(value: String): Boolean {
        // value format: "MON,TUE,WED"
        val days = value.split(",").map { it.trim().uppercase() }
        val today = DayOfWeek.from(java.time.LocalDate.now())
            .getDisplayName(TextStyle.SHORT, Locale.ENGLISH).uppercase()
        return today in days
    }

    private fun triggerAction(rule: Rule) {
        Log.i(TAG, "Rule triggered: ${rule.name} (${rule.id})")

        when (rule.action) {
            RuleAction.NOTIFY -> {
                notificationRouter.route(
                    title = rule.name,
                    message = rule.naturalLanguage.ifEmpty { rule.name },
                    priority = "NORMAL",
                    eventType = "RULE"
                )
            }
            RuleAction.SPAWN_WORKER -> {
                // TODO: integrate with DollOSAIService WorkerManager when available
                Log.i(TAG, "SPAWN_WORKER action for rule ${rule.id}: ${rule.actionParams}")
            }
            RuleAction.SEND_EVENT -> {
                // TODO: integrate with DollOSAIService EventQueue when available
                Log.i(TAG, "SEND_EVENT action for rule ${rule.id}: ${rule.actionParams}")
            }
        }
    }
}
```

- [ ] **Step 2: Initialize RuleEngine in DollOSApp.kt**

Add to companion object:

```kotlin
lateinit var ruleDao: RuleDao
    private set
lateinit var ruleEngine: RuleEngine
    private set
```

Add to onCreate():

```kotlin
ruleDao = RuleDao(this)
ruleEngine = RuleEngine(this, ruleDao, notificationRouter)
ruleEngine.start()
```

- [ ] **Step 3: Wire AppEventMonitor to RuleEngine in DollOSAccessibilityService**

In `DollOSAccessibilityService.onServiceConnected()`, add after `appEventMonitor = AppEventMonitor()`:

```kotlin
appEventMonitor.setListener(DollOSApp.ruleEngine)
```

- [ ] **Step 4: Commit**

```bash
git add aosp/packages/apps/DollOSService/src/org/dollos/service/rule/RuleEngine.kt \
        aosp/packages/apps/DollOSService/src/org/dollos/service/DollOSApp.kt \
        aosp/packages/apps/DollOSService/src/org/dollos/service/accessibility/DollOSAccessibilityService.kt
git commit -m "feat: add RuleEngine — programmable event evaluation with debounce"
```

---

## Task 14: AIDL — Rule Management Methods

**Goal:** Expose rule CRUD operations via IDollOSService AIDL for DollOSAIService to call.

**Files:**
- Modify: `aosp/packages/apps/DollOSService/aidl/org/dollos/service/IDollOSService.aidl`
- Modify: `aosp/packages/apps/DollOSService/src/org/dollos/service/DollOSServiceImpl.kt`

Note: The spec puts rule management in IDollOSAIService, but since that doesn't exist yet, we expose it through IDollOSService. DollOSAIService will call these when it's built.

- [ ] **Step 1: Add rule methods to IDollOSService.aidl**

```aidl
    /** Get all programmable event rules as JSON array */
    String getRules();

    /** Add a new rule from JSON */
    void addRule(String ruleJson);

    /** Update an existing rule from JSON */
    void updateRule(String ruleJson);

    /** Remove a rule by ID */
    void removeRule(String ruleId);

    /** Enable or disable a rule */
    void setRuleEnabled(String ruleId, boolean enabled);
```

- [ ] **Step 2: Implement in DollOSServiceImpl.kt**

```kotlin
import org.dollos.service.rule.Rule
import org.json.JSONArray

override fun getRules(): String {
    val rules = DollOSApp.ruleDao.getAll()
    val array = JSONArray()
    rules.forEach { array.put(it.toJson()) }
    return array.toString()
}

override fun addRule(ruleJson: String) {
    val rule = Rule.fromJson(JSONObject(ruleJson))
    DollOSApp.ruleDao.insert(rule)
    DollOSApp.ruleEngine.reloadRules()
    Log.i(TAG, "Rule added: ${rule.name}")
}

override fun updateRule(ruleJson: String) {
    val rule = Rule.fromJson(JSONObject(ruleJson))
    DollOSApp.ruleDao.update(rule)
    DollOSApp.ruleEngine.reloadRules()
    Log.i(TAG, "Rule updated: ${rule.name}")
}

override fun removeRule(ruleId: String) {
    DollOSApp.ruleDao.delete(ruleId)
    DollOSApp.ruleEngine.reloadRules()
    Log.i(TAG, "Rule removed: $ruleId")
}

override fun setRuleEnabled(ruleId: String, enabled: Boolean) {
    DollOSApp.ruleDao.setEnabled(ruleId, enabled)
    DollOSApp.ruleEngine.reloadRules()
    Log.i(TAG, "Rule $ruleId enabled=$enabled")
}
```

- [ ] **Step 3: Commit**

```bash
git add aosp/packages/apps/DollOSService/aidl/org/dollos/service/IDollOSService.aidl \
        aosp/packages/apps/DollOSService/src/org/dollos/service/DollOSServiceImpl.kt
git commit -m "feat: add rule management AIDL methods"
```

---

## Task 15: Update AI Core Design Spec Status

**Goal:** Update the implementation status table in the AI Core design spec.

**Files:**
- Modify: `docs/superpowers/specs/2026-03-19-ai-core-design.md`

- [ ] **Step 1: Update status table**

Change:

```
| Plan D v2 | Designed | UI operation (VirtualDisplay), smart notification, programmable events |
```

To:

```
| Plan D v2 | Complete | UI operation (AccessibilityService + VirtualDisplay + takeover), smart notification, programmable events |
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-03-19-ai-core-design.md
git commit -m "docs: update AI Core spec — Plan D v2 complete"
```
