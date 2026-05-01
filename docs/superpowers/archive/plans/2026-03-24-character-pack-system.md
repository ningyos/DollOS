# Character Pack System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Character Pack system that allows importing, exporting, and switching AI character bundles (.doll files) containing 3D avatar models, personality, voice settings, scene config, and animations.

**Architecture:** CharacterManager lives in DollOSAIService as an independent module. Character packs are zip files with `.doll` extension. Each character is extracted to app internal storage. Switching characters applies personality settings, switches private notes, and broadcasts to Launcher via AIDL callback. Assets are served to other apps via ParcelFileDescriptor.

**Tech Stack:** Kotlin, AIDL (Binder IPC), java.util.zip, JSON (org.json), ParcelFileDescriptor

---

## File Structure

### DollOSAIService (all new files under character/)

```
app/src/main/java/org/dollos/ai/character/
  CharacterPack.kt              — data classes: Manifest, PersonalityConfig, VoiceConfig, SceneConfig
  CharacterValidator.kt         — validate .doll zip structure + security checks
  CharacterManager.kt           — import, export, switch, list, delete, asset access
```

### DollOSAIService (modify existing)

```
  DollOSAIServiceImpl.kt        — add character AIDL method implementations
  aidl/IDollOSAIService.aidl    — add character management methods
  aidl/IDollOSAICallback.aidl   — add onCharacterChanged, onCharacterImportFailed
  AndroidManifest.xml            — add .doll file association intent filter
```

---

## Task 1: CharacterPack Data Classes

**Goal:** Define the data model for all character pack JSON configs.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/character/CharacterPack.kt`

- [ ] **Step 1: Create CharacterPack.kt**

```kotlin
package org.dollos.ai.character

import org.json.JSONObject
import org.json.JSONArray

data class CharacterManifest(
    val formatVersion: Int,
    val name: String,
    val version: String,
    val author: String,
    val description: String,
    val wakeWord: String?,
    val avatarType: String,  // "3d" or "live2d"
    val created: String
) {
    companion object {
        fun fromJson(json: JSONObject) = CharacterManifest(
            formatVersion = json.getInt("formatVersion"),
            name = json.getString("name"),
            version = json.getString("version"),
            author = json.getString("author"),
            description = json.optString("description", ""),
            wakeWord = json.optString("wakeWord", null),
            avatarType = json.optString("avatarType", "3d"),
            created = json.optString("created", "")
        )
    }

    fun toJson() = JSONObject().apply {
        put("formatVersion", formatVersion)
        put("name", name)
        put("version", version)
        put("author", author)
        put("description", description)
        put("wakeWord", wakeWord)
        put("avatarType", avatarType)
        put("created", created)
    }
}

data class PersonalityConfig(
    val backstory: String,
    val responseDirective: String,
    val dynamism: Float,
    val address: String,
    val languagePreference: String
) {
    companion object {
        fun fromJson(json: JSONObject) = PersonalityConfig(
            backstory = json.optString("backstory", ""),
            responseDirective = json.optString("responseDirective", ""),
            dynamism = json.optDouble("dynamism", 0.5).toFloat(),
            address = json.optString("address", ""),
            languagePreference = json.optString("languagePreference", "")
        )
    }

    fun toJson() = JSONObject().apply {
        put("backstory", backstory)
        put("responseDirective", responseDirective)
        put("dynamism", dynamism.toDouble())
        put("address", address)
        put("languagePreference", languagePreference)
    }
}

data class VoiceConfig(
    val speed: Float,
    val pitch: Float,
    val ttsModel: String,
    val language: String
) {
    companion object {
        fun fromJson(json: JSONObject) = VoiceConfig(
            speed = json.optDouble("speed", 1.0).toFloat(),
            pitch = json.optDouble("pitch", 1.0).toFloat(),
            ttsModel = json.optString("ttsModel", "default"),
            language = json.optString("language", "")
        )

        fun default() = VoiceConfig(1.0f, 1.0f, "default", "")
    }

    fun toJson() = JSONObject().apply {
        put("speed", speed.toDouble())
        put("pitch", pitch.toDouble())
        put("ttsModel", ttsModel)
        put("language", language)
    }
}

data class SceneConfig(
    val backgroundType: String,  // "color", "gradient", "image"
    val backgroundColor: String,
    val ambientLight: Float,
    val directionalIntensity: Float,
    val directionalDirection: FloatArray,
    val directionalColor: String,
    val cameraPosition: FloatArray,
    val cameraTarget: FloatArray,
    val cameraFov: Float
) {
    companion object {
        fun fromJson(json: JSONObject): SceneConfig {
            val bg = json.optJSONObject("background") ?: JSONObject()
            val lighting = json.optJSONObject("lighting") ?: JSONObject()
            val dir = lighting.optJSONObject("directional") ?: JSONObject()
            val camera = json.optJSONObject("camera") ?: JSONObject()

            fun jsonArrayToFloatArray(arr: org.json.JSONArray?, default: FloatArray): FloatArray {
                if (arr == null) return default
                return FloatArray(arr.length()) { arr.getDouble(it).toFloat() }
            }

            return SceneConfig(
                backgroundType = bg.optString("type", "color"),
                backgroundColor = bg.optString("value", "#0a0a1a"),
                ambientLight = lighting.optDouble("ambient", 0.3).toFloat(),
                directionalIntensity = dir.optDouble("intensity", 1.0).toFloat(),
                directionalDirection = jsonArrayToFloatArray(dir.optJSONArray("direction"), floatArrayOf(0f, -1f, -0.5f)),
                directionalColor = dir.optString("color", "#ffffff"),
                cameraPosition = jsonArrayToFloatArray(camera.optJSONArray("position"), floatArrayOf(0f, 1.2f, 3f)),
                cameraTarget = jsonArrayToFloatArray(camera.optJSONArray("target"), floatArrayOf(0f, 1f, 0f)),
                cameraFov = camera.optDouble("fov", 45.0).toFloat()
            )
        }

        fun default() = SceneConfig(
            "color", "#0a0a1a", 0.3f, 1.0f,
            floatArrayOf(0f, -1f, -0.5f), "#ffffff",
            floatArrayOf(0f, 1.2f, 3f), floatArrayOf(0f, 1f, 0f), 45f
        )
    }
}

data class InstalledCharacter(
    val id: String,
    val manifest: CharacterManifest,
    val installedAt: Long,
    val directoryPath: String
) {
    fun toListJson() = JSONObject().apply {
        put("id", id)
        put("name", manifest.name)
        put("author", manifest.author)
        put("avatarType", manifest.avatarType)
        put("description", manifest.description)
        put("installedAt", installedAt)
    }

    fun toDetailJson() = JSONObject().apply {
        put("id", id)
        put("manifest", manifest.toJson())
        put("installedAt", installedAt)
    }
}
```

- [ ] **Step 2: Commit**

```bash
cd ~/Projects/DollOSAIService
git add app/src/main/java/org/dollos/ai/character/
git commit -m "feat: add CharacterPack data classes (manifest, personality, voice, scene)"
```

---

## Task 2: CharacterValidator

**Goal:** Validate .doll zip structure with security checks (zip-slip, size limits).

**Files:**
- Create: `app/src/main/java/org/dollos/ai/character/CharacterValidator.kt`

- [ ] **Step 1: Create CharacterValidator.kt**

```kotlin
package org.dollos.ai.character

import android.util.Log
import org.json.JSONObject
import java.io.File
import java.io.InputStream
import java.util.zip.ZipInputStream

class CharacterValidator {

    companion object {
        private const val TAG = "CharacterValidator"
        private const val MAX_DOLL_SIZE = 200L * 1024 * 1024        // 200MB
        private const val MAX_ENTRY_SIZE = 100L * 1024 * 1024       // 100MB per entry
        private const val MAX_TOTAL_EXTRACTED = 300L * 1024 * 1024   // 300MB total
        private const val MAX_ENTRY_COUNT = 100
        private const val BACKSTORY_MAX = 2500
        private const val DIRECTIVE_MAX = 150
    }

    data class ValidationResult(
        val valid: Boolean,
        val errorCode: String? = null,
        val errorMessage: String? = null,
        val manifest: CharacterManifest? = null
    )

    fun validate(inputStream: InputStream): ValidationResult {
        val entries = mutableMapOf<String, Long>()
        var manifestJson: String? = null
        var totalSize = 0L
        var entryCount = 0

        try {
            ZipInputStream(inputStream).use { zis ->
                var entry = zis.nextEntry
                while (entry != null) {
                    entryCount++

                    // Max entry count
                    if (entryCount > MAX_ENTRY_COUNT) {
                        return ValidationResult(false, "TOO_MANY_ENTRIES", "Archive has more than $MAX_ENTRY_COUNT entries")
                    }

                    // Path traversal check
                    val name = entry.name
                    if (name.contains("..") || name.startsWith("/")) {
                        return ValidationResult(false, "PATH_TRAVERSAL", "Dangerous path: $name")
                    }

                    // Read entry to check size
                    val buffer = ByteArray(8192)
                    var entrySize = 0L
                    val content = if (name == "manifest.json") StringBuilder() else null

                    while (true) {
                        val len = zis.read(buffer)
                        if (len <= 0) break
                        entrySize += len
                        totalSize += len

                        if (entrySize > MAX_ENTRY_SIZE) {
                            return ValidationResult(false, "ENTRY_TOO_LARGE", "$name exceeds ${MAX_ENTRY_SIZE / 1024 / 1024}MB")
                        }
                        if (totalSize > MAX_TOTAL_EXTRACTED) {
                            return ValidationResult(false, "ARCHIVE_TOO_LARGE", "Total extracted size exceeds ${MAX_TOTAL_EXTRACTED / 1024 / 1024}MB")
                        }

                        content?.append(String(buffer, 0, len))
                    }

                    if (name == "manifest.json") {
                        manifestJson = content?.toString()
                    }

                    entries[name] = entrySize
                    zis.closeEntry()
                    entry = zis.nextEntry
                }
            }
        } catch (e: Exception) {
            return ValidationResult(false, "CORRUPT_ARCHIVE", "Failed to read archive: ${e.message}")
        }

        // Must have manifest.json
        if (manifestJson == null) {
            return ValidationResult(false, "NO_MANIFEST", "manifest.json not found in archive")
        }

        // Parse manifest
        val manifest = try {
            val json = JSONObject(manifestJson)
            CharacterManifest.fromJson(json)
        } catch (e: Exception) {
            return ValidationResult(false, "INVALID_MANIFEST", "Failed to parse manifest.json: ${e.message}")
        }

        // Format version check
        if (manifest.formatVersion != 1) {
            return ValidationResult(false, "UNKNOWN_FORMAT", "Unknown format version: ${manifest.formatVersion}")
        }

        // Must have model.glb for 3d type
        if (manifest.avatarType == "3d" && "model.glb" !in entries) {
            return ValidationResult(false, "MISSING_MODEL", "model.glb required for 3d avatar type")
        }

        // Check personality field lengths if personality.json exists
        if ("personality.json" in entries) {
            // Already read during zip scan — will be validated again on import
        }

        Log.i(TAG, "Validation passed: ${manifest.name} (${entries.size} entries, ${totalSize / 1024}KB)")
        return ValidationResult(true, manifest = manifest)
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/org/dollos/ai/character/CharacterValidator.kt
git commit -m "feat: add CharacterValidator with zip-slip protection and size limits"
```

---

## Task 3: CharacterManager

**Goal:** Core manager for importing, exporting, switching, listing, deleting characters and serving assets.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/character/CharacterManager.kt`

- [ ] **Step 1: Create CharacterManager.kt**

```kotlin
package org.dollos.ai.character

import android.content.Context
import android.os.ParcelFileDescriptor
import android.util.Log
import org.dollos.ai.DollOSAIApp
import org.dollos.ai.personality.PersonalityManager
import org.json.JSONArray
import org.json.JSONObject
import java.io.*
import java.util.UUID
import java.util.zip.ZipEntry
import java.util.zip.ZipInputStream
import java.util.zip.ZipOutputStream

class CharacterManager(
    private val context: Context,
    private val personalityManager: PersonalityManager
) {

    companion object {
        private const val TAG = "CharacterManager"
        private const val CHARACTERS_DIR = "characters"
        private const val PREF_ACTIVE_CHARACTER = "active_character_id"
    }

    private val prefs = DollOSAIApp.prefs
    private val charactersDir = File(context.filesDir, CHARACTERS_DIR)
    private val validator = CharacterValidator()

    var onCharacterChanged: ((String, String) -> Unit)? = null
    var onImportFailed: ((String, String) -> Unit)? = null

    init {
        charactersDir.mkdirs()
    }

    fun importCharacter(fd: ParcelFileDescriptor): String? {
        val inputStream = ParcelFileDescriptor.AutoCloseInputStream(fd)

        // First pass: validate
        val tempFile = File(context.cacheDir, "import_${System.currentTimeMillis()}.doll")
        try {
            tempFile.outputStream().use { out -> inputStream.copyTo(out) }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to copy import file", e)
            onImportFailed?.invoke("COPY_FAILED", e.message ?: "Failed to read file")
            tempFile.delete()
            return null
        }

        val validationResult = tempFile.inputStream().use { validator.validate(it) }
        if (!validationResult.valid) {
            Log.e(TAG, "Validation failed: ${validationResult.errorCode} — ${validationResult.errorMessage}")
            onImportFailed?.invoke(validationResult.errorCode ?: "UNKNOWN", validationResult.errorMessage ?: "Validation failed")
            tempFile.delete()
            return null
        }

        // Extract
        val characterId = UUID.randomUUID().toString()
        val characterDir = File(charactersDir, characterId)
        characterDir.mkdirs()

        try {
            ZipInputStream(tempFile.inputStream()).use { zis ->
                var entry = zis.nextEntry
                while (entry != null) {
                    if (entry.isDirectory) {
                        File(characterDir, entry.name).mkdirs()
                    } else {
                        val outFile = File(characterDir, entry.name)
                        // Zip-slip protection
                        if (!outFile.canonicalPath.startsWith(characterDir.canonicalPath)) {
                            Log.e(TAG, "Zip-slip detected: ${entry.name}")
                            characterDir.deleteRecursively()
                            onImportFailed?.invoke("PATH_TRAVERSAL", "Dangerous path in archive")
                            tempFile.delete()
                            return null
                        }
                        outFile.parentFile?.mkdirs()
                        outFile.outputStream().use { out -> zis.copyTo(out) }
                    }
                    zis.closeEntry()
                    entry = zis.nextEntry
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Extraction failed", e)
            characterDir.deleteRecursively()
            onImportFailed?.invoke("EXTRACT_FAILED", e.message ?: "Extraction failed")
            tempFile.delete()
            return null
        }

        tempFile.delete()

        // Save metadata
        val metaFile = File(characterDir, ".installed_meta.json")
        metaFile.writeText(JSONObject().apply {
            put("id", characterId)
            put("installedAt", System.currentTimeMillis())
        }.toString())

        // Create private notes file
        File(characterDir, "notes.md").writeText("# ${validationResult.manifest!!.name} — Private Notes\n\n")

        Log.i(TAG, "Character imported: $characterId (${validationResult.manifest.name})")
        return characterId
    }

    fun exportCharacter(characterId: String, fd: ParcelFileDescriptor) {
        val characterDir = File(charactersDir, characterId)
        if (!characterDir.exists()) return

        val outputStream = ParcelFileDescriptor.AutoCloseOutputStream(fd)
        ZipOutputStream(BufferedOutputStream(outputStream)).use { zos ->
            characterDir.walkTopDown()
                .filter { it.isFile && it.name != ".installed_meta.json" }
                .forEach { file ->
                    val entryName = file.relativeTo(characterDir).path
                    zos.putNextEntry(ZipEntry(entryName))
                    file.inputStream().use { it.copyTo(zos) }
                    zos.closeEntry()
                }
        }
        Log.i(TAG, "Character exported: $characterId")
    }

    fun setActiveCharacter(characterId: String) {
        val characterDir = File(charactersDir, characterId)
        if (!characterDir.exists()) {
            Log.e(TAG, "Character not found: $characterId")
            return
        }

        // Load personality
        val personalityFile = File(characterDir, "personality.json")
        if (personalityFile.exists()) {
            val config = PersonalityConfig.fromJson(JSONObject(personalityFile.readText()))
            personalityManager.backstory = config.backstory
            personalityManager.responseDirective = config.responseDirective
            personalityManager.dynamism = config.dynamism
            personalityManager.address = config.address
            personalityManager.languagePreference = config.languagePreference
        }

        // Save active ID
        prefs.edit().putString(PREF_ACTIVE_CHARACTER, characterId).apply()

        // Load manifest for name
        val manifest = loadManifest(characterId)
        val name = manifest?.name ?: "Unknown"

        Log.i(TAG, "Active character set: $characterId ($name)")
        onCharacterChanged?.invoke(characterId, name)
    }

    fun getActiveCharacter(): String? {
        return prefs.getString(PREF_ACTIVE_CHARACTER, null)
    }

    fun listCharacters(): String {
        val characters = JSONArray()
        val activeId = getActiveCharacter()

        charactersDir.listFiles()?.filter { it.isDirectory }?.forEach { dir ->
            val manifest = loadManifest(dir.name) ?: return@forEach
            val meta = loadMeta(dir.name)
            val entry = JSONObject().apply {
                put("id", dir.name)
                put("name", manifest.name)
                put("author", manifest.author)
                put("avatarType", manifest.avatarType)
                put("description", manifest.description)
                put("isActive", dir.name == activeId)
                put("installedAt", meta?.optLong("installedAt", 0) ?: 0)
                put("hasThumbnail", File(dir, "thumbnail.png").exists())
            }
            characters.put(entry)
        }

        return characters.toString()
    }

    fun deleteCharacter(characterId: String) {
        val characterDir = File(charactersDir, characterId)
        if (!characterDir.exists()) return

        // If deleting active character, clear active
        if (getActiveCharacter() == characterId) {
            prefs.edit().remove(PREF_ACTIVE_CHARACTER).apply()
        }

        characterDir.deleteRecursively()
        Log.i(TAG, "Character deleted: $characterId")
    }

    fun getCharacterInfo(characterId: String): String {
        val manifest = loadManifest(characterId) ?: return "{}"
        val meta = loadMeta(characterId)
        val dir = File(charactersDir, characterId)

        return JSONObject().apply {
            put("id", characterId)
            put("manifest", manifest.toJson())
            put("installedAt", meta?.optLong("installedAt", 0) ?: 0)
            put("hasModel", File(dir, "model.glb").exists())
            put("hasAnimations", File(dir, "animations").isDirectory)
            put("hasThumbnail", File(dir, "thumbnail.png").exists())
            put("hasWakeWord", File(dir, "wake_word.bin").exists())
        }.toString()
    }

    fun getCharacterAsset(characterId: String, path: String): ParcelFileDescriptor? {
        val file = File(File(charactersDir, characterId), path)
        // Security: ensure path doesn't escape character directory
        if (!file.canonicalPath.startsWith(File(charactersDir, characterId).canonicalPath)) {
            Log.e(TAG, "Path traversal attempt: $path")
            return null
        }
        if (!file.exists()) return null
        return ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY)
    }

    fun getPrivateNotesPath(characterId: String): String? {
        val file = File(File(charactersDir, characterId), "notes.md")
        return if (file.exists()) file.absolutePath else null
    }

    private fun loadManifest(characterId: String): CharacterManifest? {
        val file = File(File(charactersDir, characterId), "manifest.json")
        if (!file.exists()) return null
        return try {
            CharacterManifest.fromJson(JSONObject(file.readText()))
        } catch (e: Exception) {
            Log.e(TAG, "Failed to load manifest for $characterId", e)
            null
        }
    }

    private fun loadMeta(characterId: String): JSONObject? {
        val file = File(File(charactersDir, characterId), ".installed_meta.json")
        if (!file.exists()) return null
        return try {
            JSONObject(file.readText())
        } catch (e: Exception) { null }
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/org/dollos/ai/character/CharacterManager.kt
git commit -m "feat: add CharacterManager — import, export, switch, list, delete, asset access"
```

---

## Task 4: AIDL Updates

**Goal:** Add character management methods to AIDL interfaces.

**Files:**
- Modify: `aidl/org/dollos/ai/IDollOSAIService.aidl`
- Modify: `aidl/org/dollos/ai/IDollOSAICallback.aidl`

- [ ] **Step 1: Update IDollOSAIService.aidl**

Add before closing `}`:

```aidl
    // Character Pack management
    String importCharacter(in ParcelFileDescriptor fd);
    void exportCharacter(String characterId, in ParcelFileDescriptor fd);
    void setActiveCharacter(String characterId);
    String getActiveCharacter();
    String listCharacters();
    void deleteCharacter(String characterId);
    String getCharacterInfo(String characterId);
    ParcelFileDescriptor getCharacterAsset(String characterId, String path);
```

- [ ] **Step 2: Update IDollOSAICallback.aidl**

Add:

```aidl
    void onCharacterChanged(String characterId, String characterName);
    void onCharacterImportFailed(String errorCode, String message);
```

- [ ] **Step 3: Commit**

```bash
git add aidl/
git commit -m "feat: add character pack AIDL methods and callbacks"
```

---

## Task 5: DollOSAIServiceImpl Integration

**Goal:** Wire CharacterManager into the service, implement AIDL methods, handle character switch during active conversation.

**Files:**
- Modify: `app/src/main/java/org/dollos/ai/DollOSAIServiceImpl.kt`

- [ ] **Step 1: Add CharacterManager field and init**

Add field:
```kotlin
private val characterManager = CharacterManager(DollOSAIApp.instance, personalityManager)
```

Add to init block:
```kotlin
// Wire character callbacks
characterManager.onCharacterChanged = { id, name ->
    broadcastCharacterChanged(id, name)
}
characterManager.onImportFailed = { code, message ->
    broadcastCharacterImportFailed(code, message)
}
```

- [ ] **Step 2: Add AIDL method implementations**

```kotlin
// --- Character Pack ---

override fun importCharacter(fd: ParcelFileDescriptor?): String? {
    if (fd == null) return null
    return characterManager.importCharacter(fd)
}

override fun exportCharacter(characterId: String?, fd: ParcelFileDescriptor?) {
    if (characterId == null || fd == null) return
    characterManager.exportCharacter(characterId, fd)
}

override fun setActiveCharacter(characterId: String?) {
    if (characterId == null) return
    // Stop active conversation before switching
    stopGeneration()
    // Clear pending confirmations
    pendingConfirmations.clear()
    // Cancel background workers
    workerManager.cancelAll()
    // Switch
    characterManager.setActiveCharacter(characterId)
}

override fun getActiveCharacter(): String? = characterManager.getActiveCharacter()

override fun listCharacters(): String = characterManager.listCharacters()

override fun deleteCharacter(characterId: String?) {
    if (characterId == null) return
    characterManager.deleteCharacter(characterId)
}

override fun getCharacterInfo(characterId: String?): String {
    if (characterId == null) return "{}"
    return characterManager.getCharacterInfo(characterId)
}

override fun getCharacterAsset(characterId: String?, path: String?): ParcelFileDescriptor? {
    if (characterId == null || path == null) return null
    return characterManager.getCharacterAsset(characterId, path)
}
```

- [ ] **Step 3: Add broadcast helpers**

```kotlin
private fun broadcastCharacterChanged(characterId: String, characterName: String) {
    val n = callbacks.beginBroadcast()
    try {
        for (i in 0 until n) {
            try { callbacks.getBroadcastItem(i).onCharacterChanged(characterId, characterName) } catch (_: Exception) {}
        }
    } finally { callbacks.finishBroadcast() }
}

private fun broadcastCharacterImportFailed(errorCode: String, message: String) {
    val n = callbacks.beginBroadcast()
    try {
        for (i in 0 until n) {
            try { callbacks.getBroadcastItem(i).onCharacterImportFailed(errorCode, message) } catch (_: Exception) {}
        }
    } finally { callbacks.finishBroadcast() }
}
```

- [ ] **Step 4: Commit**

```bash
git add app/src/main/java/org/dollos/ai/DollOSAIServiceImpl.kt
git commit -m "feat: integrate CharacterManager into DollOSAIServiceImpl"
```

---

## Task 6: AndroidManifest + TestActivity Update

**Goal:** Register .doll file association and update TestActivity callback.

**Files:**
- Modify: `app/src/main/AndroidManifest.xml`
- Modify: `app/src/main/java/org/dollos/ai/TestActivity.kt`

- [ ] **Step 1: Add .doll file association to AndroidManifest.xml**

Add a new activity inside `<application>` for handling .doll file imports:

```xml
<activity
    android:name=".character.CharacterImportActivity"
    android:exported="true"
    android:theme="@android:style/Theme.Translucent.NoTitleBar">
    <intent-filter>
        <action android:name="android.intent.action.VIEW" />
        <category android:name="android.intent.category.DEFAULT" />
        <data android:scheme="content" />
        <data android:mimeType="*/*" />
    </intent-filter>
</activity>
```

- [ ] **Step 2: Create CharacterImportActivity.kt**

Simple activity that checks URI, binds to service, calls importCharacter:

```kotlin
package org.dollos.ai.character

import android.app.Activity
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.Bundle
import android.os.IBinder
import android.util.Log
import android.widget.Toast
import org.dollos.ai.IDollOSAIService

class CharacterImportActivity : Activity() {

    companion object {
        private const val TAG = "CharacterImport"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val uri = intent?.data
        if (uri == null) {
            Toast.makeText(this, "No file provided", Toast.LENGTH_SHORT).show()
            finish()
            return
        }

        // Check file extension
        val displayName = uri.lastPathSegment ?: ""
        if (!displayName.endsWith(".doll", ignoreCase = true)) {
            Toast.makeText(this, "Not a .doll file", Toast.LENGTH_SHORT).show()
            finish()
            return
        }

        // Bind to service and import
        val serviceIntent = Intent("org.dollos.ai.IDollOSAIService").apply {
            setPackage("org.dollos.ai")
        }

        bindService(serviceIntent, object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName?, service: IBinder?) {
                val aiService = IDollOSAIService.Stub.asInterface(service)
                try {
                    val fd = contentResolver.openFileDescriptor(uri, "r")
                    if (fd != null) {
                        val characterId = aiService.importCharacter(fd)
                        if (characterId != null) {
                            Toast.makeText(this@CharacterImportActivity, "Character imported!", Toast.LENGTH_SHORT).show()
                        } else {
                            Toast.makeText(this@CharacterImportActivity, "Import failed", Toast.LENGTH_SHORT).show()
                        }
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Import failed", e)
                    Toast.makeText(this@CharacterImportActivity, "Import failed: ${e.message}", Toast.LENGTH_SHORT).show()
                }
                unbindService(this)
                finish()
            }

            override fun onServiceDisconnected(name: ComponentName?) {
                finish()
            }
        }, Context.BIND_AUTO_CREATE)
    }
}
```

- [ ] **Step 3: Update TestActivity callback**

Add the two new callback methods to TestActivity's callback object:

```kotlin
override fun onCharacterChanged(characterId: String?, characterName: String?) {
    Log.i("TestActivity", "Character changed: $characterName ($characterId)")
}

override fun onCharacterImportFailed(errorCode: String?, message: String?) {
    Log.e("TestActivity", "Character import failed: $errorCode — $message")
}
```

- [ ] **Step 4: Commit**

```bash
git add app/src/main/AndroidManifest.xml app/src/main/java/org/dollos/ai/character/CharacterImportActivity.kt app/src/main/java/org/dollos/ai/TestActivity.kt
git commit -m "feat: add .doll file association and CharacterImportActivity"
```

---

## Task 7: Build, Deploy, and Verify

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

- [ ] **Step 3: Verify service startup**

```bash
adb logcat -d | grep -iE "CharacterManager|CharacterValidator" | head -10
```

- [ ] **Step 4: Test import via adb**

Create a test .doll file and push to device:

```bash
# On host: create a minimal test .doll file
mkdir -p /tmp/test_character/animations
echo '{"formatVersion":1,"name":"Test","version":"1.0","author":"test","description":"Test character","wakeWord":null,"avatarType":"3d","created":"2026-03-24"}' > /tmp/test_character/manifest.json
echo '{"backstory":"A test character","responseDirective":"Be brief","dynamism":0.5,"address":"user","languagePreference":"en"}' > /tmp/test_character/personality.json
echo '{"speed":1.0,"pitch":1.0,"ttsModel":"default","language":"en"}' > /tmp/test_character/voice.json
echo '{"background":{"type":"color","value":"#0a0a1a"},"lighting":{"ambient":0.3},"camera":{"position":[0,1.2,3],"target":[0,1,0],"fov":45}}' > /tmp/test_character/scene.json
# Create a tiny placeholder model.glb (just needs to exist for validation)
echo "placeholder" > /tmp/test_character/model.glb
cd /tmp/test_character && zip -r /tmp/test.doll . && cd -
adb push /tmp/test.doll /sdcard/Download/test.doll
```

Then open it on device or test via service call.

- [ ] **Step 5: Verify import in logcat**

```bash
adb logcat -d | grep -iE "CharacterManager|CharacterValidator|Character imported" | head -20
```

---

## Notes

### Memory Integration

The shared memory system continues to work as before. Character private notes (`notes.md`) are stored per-character but are not currently injected into LLM context. This will be wired up when the Launcher's character system is actively used (future task: inject active character's notes.md into system prompt builder).

### Export Includes User Modifications

When exporting a character, the personality.json reflects the current PersonalityManager values (which may have been modified by the user via Settings), not the original imported values. This means export captures the user's customizations.

### Default Character

DollOS can optionally ship with a default character pack in the system image. The OOBE or first-boot script can call `importCharacter()` with a bundled .doll file. This is handled outside this plan — the system just needs a .doll file placed at a known path.
