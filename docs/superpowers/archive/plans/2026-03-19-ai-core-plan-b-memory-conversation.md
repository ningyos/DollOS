# AI Core Plan B: Memory System + Conversation Engine

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the persistent memory system and conversation engine for DollOS AI. This includes a three-tier Markdown memory architecture (inspired by OpenClaw), a sqlite-vec + FTS5 hybrid search index (vector + BM25), a serialized memory write queue with retry logic, date-segmented conversation management, proactive context compression, and memory export/import via ParcelFileDescriptor.

**Architecture:** Markdown files are the single source of truth, stored in DollOSAIService's internal storage (`/data/user/0/org.dollos.ai/files/memory/`). sqlite-vec provides vector search via a SQLite extension, and FTS5 provides BM25 keyword search. Both are backed by a single SQLite database. All memory writes flow through a single serialized queue to prevent race conditions. The conversation engine segments by date and proactively compresses context at 70-80% capacity using the foreground model asynchronously.

**Tech Stack:** Kotlin, sqlite-vec (SQLite extension for vector search), FTS5 (built into Android SQLite, BM25 keyword search), OkHttp (cloud embedding API calls), coroutines (async compression, write queue), AIDL (memory export/import), ParcelFileDescriptor (scoped storage), JSON (pending writes, config)

**Depends on:** Plan A (DollOSAIService Gradle project skeleton must exist). Plan B adds modules to the existing Gradle project.

**Key difference from previous revision:** DollOSAIService is now a Gradle Android project (not AOSP Soong). ObjectBox is replaced by sqlite-vec for vector search. No annotation processing is needed. The sqlite-vec native library (.so) is bundled in jniLibs and loaded at runtime.

---

## File Structure

### DollOSAIService (additions to existing Gradle project from Plan A)

```
app/src/main/
  jniLibs/arm64-v8a/
    vec0.so                                -- sqlite-vec native library (pre-compiled ARM64)
  java/org/dollos/ai/
    memory/
      MemoryManager.kt                     -- main memory orchestrator
      MemoryTier.kt                        -- tier enum and config
      MarkdownStore.kt                     -- read/write Markdown files (source of truth)
      MemoryDatabase.kt                    -- SQLite database (FTS5 + vec0 index)
      MemorySearchEngine.kt                -- hybrid search (vector + BM25)
      MemoryWriteQueue.kt                  -- serialized write queue with retry
      EmbeddingProvider.kt                 -- interface for embedding generation
      CloudEmbeddingProvider.kt            -- OpenAI text-embedding-3-small via OkHttp
      LocalEmbeddingProvider.kt            -- placeholder (zero vectors)
      MemoryExporter.kt                    -- export/import via ParcelFileDescriptor
    conversation/
      ConversationManager.kt               -- conversation state + history
      ConversationSegment.kt               -- date-based conversation segment
      ContextCompressor.kt                 -- proactive context compression
      MessageStore.kt                      -- message persistence (SQLite)
```

### Gradle dependency additions (in app/build.gradle.kts)

```kotlin
dependencies {
    // OkHttp for cloud embedding API calls
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
```

No additional Gradle dependencies are needed for sqlite-vec (it is a native .so loaded at runtime) or FTS5 (built into Android's SQLite).

### sqlite-vec setup

Pre-compiled ARM64 binary from GitHub releases: https://github.com/asg017/sqlite-vec

Download the `sqlite-vec-<version>-android-aarch64.tar.gz` release artifact, extract `vec0.so`, and place it at:

```
app/src/main/jniLibs/arm64-v8a/vec0.so
```

At runtime, load the extension before use:

```kotlin
val db = SQLiteDatabase.openOrCreateDatabase(dbPath, null)
db.loadExtension("vec0")
```

---

## Task 1: Memory Tier Definitions and Markdown Store

**Goal:** Define the three-tier memory structure and implement basic Markdown file I/O for reading and writing memory files.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/memory/MemoryTier.kt`
- Create: `app/src/main/java/org/dollos/ai/memory/MarkdownStore.kt`

- [ ] **Step 1: Create MemoryTier.kt**

Create `app/src/main/java/org/dollos/ai/memory/MemoryTier.kt`:

```kotlin
package org.dollos.ai.memory

/**
 * Three-tier memory structure inspired by OpenClaw/memsearch.
 *
 * Tier 1: MEMORY.md -- always loaded into LLM context.
 * Tier 2: memory/YYYY-MM-DD.md -- daily notes, today + yesterday auto-loaded.
 * Tier 3: memory/people/, topics/, decisions/ -- deep knowledge, searched on demand.
 */
enum class MemoryTier(val description: String) {
    /** Always in context. Core facts, preferences, identity. */
    CORE("Always-loaded core memory (MEMORY.md)"),

    /** Auto-loaded for today and yesterday. Ephemeral daily notes. */
    DAILY("Date-segmented daily memory"),

    /** Deep knowledge loaded via search. People, topics, decisions. */
    DEEP("Searchable deep knowledge");
}

/**
 * Subdirectories under the deep knowledge tier.
 */
enum class DeepMemoryCategory(val dirName: String) {
    PEOPLE("people"),
    TOPICS("topics"),
    DECISIONS("decisions");
}
```

- [ ] **Step 2: Create MarkdownStore.kt**

Create `app/src/main/java/org/dollos/ai/memory/MarkdownStore.kt`:

```kotlin
package org.dollos.ai.memory

import android.util.Log
import java.io.File
import java.io.IOException
import java.time.LocalDate
import java.time.format.DateTimeFormatter

/**
 * Reads and writes Markdown memory files.
 * Markdown is the single source of truth -- no special parser needed, just file I/O.
 *
 * Storage root: /data/user/0/org.dollos.ai/files/memory/
 *
 * Layout:
 *   MEMORY.md                    -- Tier 1 (core)
 *   memory/2026-03-19.md         -- Tier 2 (daily)
 *   memory/people/alice.md       -- Tier 3 (deep)
 *   memory/topics/cooking.md     -- Tier 3 (deep)
 *   memory/decisions/phone.md    -- Tier 3 (deep)
 */
class MarkdownStore(private val rootDir: File) {

    companion object {
        private const val TAG = "MarkdownStore"
        private const val CORE_FILENAME = "MEMORY.md"
        private val DATE_FORMAT = DateTimeFormatter.ofPattern("yyyy-MM-dd")
    }

    init {
        rootDir.mkdirs()
        File(rootDir, "memory").mkdirs()
        DeepMemoryCategory.entries.forEach { category ->
            File(rootDir, "memory/${category.dirName}").mkdirs()
        }
    }

    // -- Tier 1: Core Memory --

    fun readCoreMemory(): String {
        val file = File(rootDir, CORE_FILENAME)
        if (!file.exists()) {
            return ""
        }
        return try {
            file.readText()
        } catch (e: IOException) {
            Log.e(TAG, "Failed to read core memory", e)
            ""
        }
    }

    fun writeCoreMemory(content: String) {
        val file = File(rootDir, CORE_FILENAME)
        try {
            file.writeText(content)
            Log.i(TAG, "Wrote core memory (${content.length} chars)")
        } catch (e: IOException) {
            Log.e(TAG, "Failed to write core memory", e)
            throw e
        }
    }

    fun appendToCoreMemory(section: String) {
        val existing = readCoreMemory()
        val updated = if (existing.isBlank()) {
            section
        } else {
            "$existing\n\n$section"
        }
        writeCoreMemory(updated)
    }

    // -- Tier 2: Daily Memory --

    fun readDailyMemory(date: LocalDate): String {
        val file = dailyFile(date)
        if (!file.exists()) {
            return ""
        }
        return try {
            file.readText()
        } catch (e: IOException) {
            Log.e(TAG, "Failed to read daily memory for $date", e)
            ""
        }
    }

    fun writeDailyMemory(date: LocalDate, content: String) {
        val file = dailyFile(date)
        try {
            file.writeText(content)
            Log.i(TAG, "Wrote daily memory for $date (${content.length} chars)")
        } catch (e: IOException) {
            Log.e(TAG, "Failed to write daily memory for $date", e)
            throw e
        }
    }

    fun appendToDailyMemory(date: LocalDate, section: String) {
        val existing = readDailyMemory(date)
        val updated = if (existing.isBlank()) {
            "# ${date.format(DATE_FORMAT)}\n\n$section"
        } else {
            "$existing\n\n$section"
        }
        writeDailyMemory(date, updated)
    }

    /**
     * Load today and yesterday daily memory (auto-loaded into context).
     */
    fun loadRecentDailyMemories(): String {
        val today = LocalDate.now()
        val yesterday = today.minusDays(1)

        val todayContent = readDailyMemory(today)
        val yesterdayContent = readDailyMemory(yesterday)

        val parts = mutableListOf<String>()
        if (yesterdayContent.isNotBlank()) {
            parts.add("## Yesterday (${yesterday.format(DATE_FORMAT)})\n\n$yesterdayContent")
        }
        if (todayContent.isNotBlank()) {
            parts.add("## Today (${today.format(DATE_FORMAT)})\n\n$todayContent")
        }

        return parts.joinToString("\n\n---\n\n")
    }

    // -- Tier 3: Deep Memory --

    fun readDeepMemory(category: DeepMemoryCategory, name: String): String {
        val file = deepFile(category, name)
        if (!file.exists()) {
            return ""
        }
        return try {
            file.readText()
        } catch (e: IOException) {
            Log.e(TAG, "Failed to read deep memory ${category.dirName}/$name", e)
            ""
        }
    }

    fun writeDeepMemory(category: DeepMemoryCategory, name: String, content: String) {
        val file = deepFile(category, name)
        try {
            file.writeText(content)
            Log.i(TAG, "Wrote deep memory ${category.dirName}/$name (${content.length} chars)")
        } catch (e: IOException) {
            Log.e(TAG, "Failed to write deep memory ${category.dirName}/$name", e)
            throw e
        }
    }

    fun appendToDeepMemory(category: DeepMemoryCategory, name: String, section: String) {
        val existing = readDeepMemory(category, name)
        val updated = if (existing.isBlank()) {
            "# ${name.removeSuffix(".md").replaceFirstChar { it.uppercase() }}\n\n$section"
        } else {
            "$existing\n\n$section"
        }
        writeDeepMemory(category, name, updated)
    }

    // -- Listing and search support --

    /**
     * List all memory files across all tiers.
     * Returns pairs of (relative path, File).
     */
    fun listAllMemoryFiles(): List<Pair<String, File>> {
        val result = mutableListOf<Pair<String, File>>()

        // Tier 1
        val coreFile = File(rootDir, CORE_FILENAME)
        if (coreFile.exists()) {
            result.add(CORE_FILENAME to coreFile)
        }

        // Tier 2 -- daily files
        val memoryDir = File(rootDir, "memory")
        memoryDir.listFiles()?.filter { it.isFile && it.name.endsWith(".md") }?.forEach { file ->
            result.add("memory/${file.name}" to file)
        }

        // Tier 3 -- deep knowledge
        DeepMemoryCategory.entries.forEach { category ->
            val catDir = File(rootDir, "memory/${category.dirName}")
            catDir.listFiles()?.filter { it.isFile && it.name.endsWith(".md") }?.forEach { file ->
                result.add("memory/${category.dirName}/${file.name}" to file)
            }
        }

        return result
    }

    /**
     * Delete a memory file by its relative path.
     */
    fun deleteMemoryFile(relativePath: String): Boolean {
        val file = File(rootDir, relativePath)
        if (!file.exists()) return false
        return file.delete()
    }

    // -- Private helpers --

    private fun dailyFile(date: LocalDate): File {
        return File(rootDir, "memory/${date.format(DATE_FORMAT)}.md")
    }

    private fun deepFile(category: DeepMemoryCategory, name: String): File {
        val safeName = if (name.endsWith(".md")) name else "$name.md"
        return File(rootDir, "memory/${category.dirName}/$safeName")
    }
}
```

- [ ] **Step 3: Commit**

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService
git add app/src/main/java/org/dollos/ai/memory/MemoryTier.kt
git add app/src/main/java/org/dollos/ai/memory/MarkdownStore.kt
git commit -m "feat: add three-tier memory definitions and Markdown file store"
```

---

## Task 2: Memory Write Queue with Retry and Pending Writes

**Goal:** Implement a single serialized write queue that all memory writes flow through, with retry logic and pending_writes.json persistence for failure recovery.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/memory/MemoryWriteQueue.kt`

- [ ] **Step 1: Create MemoryWriteQueue.kt**

Create `app/src/main/java/org/dollos/ai/memory/MemoryWriteQueue.kt`:

```kotlin
package org.dollos.ai.memory

import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.time.Instant
import java.time.LocalDate

/**
 * Single serialized write queue for all memory operations.
 * Prevents race conditions by processing writes sequentially.
 *
 * Write failure handling:
 *   1. Retry up to 3 times
 *   2. On final failure, persist to pending_writes.json
 *   3. On next startup, replay pending writes before normal operation
 */
class MemoryWriteQueue(
    private val store: MarkdownStore,
    private val rootDir: File,
    private val onWriteComplete: ((MemoryWriteOp) -> Unit)? = null
) {
    companion object {
        private const val TAG = "MemoryWriteQueue"
        private const val MAX_RETRIES = 3
        private const val PENDING_WRITES_FILE = "pending_writes.json"
    }

    /**
     * Represents a single memory write operation.
     */
    data class MemoryWriteOp(
        val id: String,
        val type: WriteType,
        val tier: MemoryTier,
        val target: String,         // filename or category/name
        val content: String,
        val append: Boolean,        // true = append, false = overwrite
        val timestamp: Long = Instant.now().toEpochMilli(),
        val retryCount: Int = 0
    ) {
        enum class WriteType {
            CORE,           // Write to MEMORY.md
            DAILY,          // Write to memory/YYYY-MM-DD.md
            DEEP            // Write to memory/{category}/{name}.md
        }

        fun toJson(): JSONObject {
            return JSONObject().apply {
                put("id", id)
                put("type", type.name)
                put("tier", tier.name)
                put("target", target)
                put("content", content)
                put("append", append)
                put("timestamp", timestamp)
                put("retryCount", retryCount)
            }
        }

        companion object {
            fun fromJson(json: JSONObject): MemoryWriteOp {
                return MemoryWriteOp(
                    id = json.getString("id"),
                    type = WriteType.valueOf(json.getString("type")),
                    tier = MemoryTier.valueOf(json.getString("tier")),
                    target = json.getString("target"),
                    content = json.getString("content"),
                    append = json.getBoolean("append"),
                    timestamp = json.getLong("timestamp"),
                    retryCount = json.optInt("retryCount", 0)
                )
            }
        }
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val writeChannel = Channel<MemoryWriteOp>(capacity = Channel.UNLIMITED)
    private var running = false

    /**
     * Start processing the write queue.
     * Call this on service startup after replaying pending writes.
     */
    fun start() {
        if (running) return
        running = true
        scope.launch {
            for (op in writeChannel) {
                processWrite(op)
            }
        }
        Log.i(TAG, "Write queue started")
    }

    /**
     * Stop processing the write queue.
     */
    fun stop() {
        running = false
        writeChannel.close()
        Log.i(TAG, "Write queue stopped")
    }

    /**
     * Enqueue a memory write operation.
     */
    fun enqueue(op: MemoryWriteOp) {
        val result = writeChannel.trySend(op)
        if (result.isFailure) {
            Log.e(TAG, "Failed to enqueue write op ${op.id}, persisting to pending")
            persistPendingWrite(op)
        } else {
            Log.d(TAG, "Enqueued write op ${op.id} (${op.type})")
        }
    }

    // -- Convenience enqueue methods --

    fun enqueueCoreWrite(content: String, append: Boolean = true) {
        enqueue(MemoryWriteOp(
            id = generateId(),
            type = MemoryWriteOp.WriteType.CORE,
            tier = MemoryTier.CORE,
            target = "MEMORY.md",
            content = content,
            append = append
        ))
    }

    fun enqueueDailyWrite(date: LocalDate, content: String, append: Boolean = true) {
        enqueue(MemoryWriteOp(
            id = generateId(),
            type = MemoryWriteOp.WriteType.DAILY,
            tier = MemoryTier.DAILY,
            target = date.toString(),
            content = content,
            append = append
        ))
    }

    fun enqueueDeepWrite(
        category: DeepMemoryCategory,
        name: String,
        content: String,
        append: Boolean = true
    ) {
        enqueue(MemoryWriteOp(
            id = generateId(),
            type = MemoryWriteOp.WriteType.DEEP,
            tier = MemoryTier.DEEP,
            target = "${category.dirName}/$name",
            content = content,
            append = append
        ))
    }

    // -- Pending writes replay --

    /**
     * Replay pending writes from pending_writes.json.
     * Call this on service startup BEFORE calling start().
     */
    fun replayPendingWrites() {
        val pendingFile = File(rootDir, PENDING_WRITES_FILE)
        if (!pendingFile.exists()) {
            Log.i(TAG, "No pending writes to replay")
            return
        }

        try {
            val jsonStr = pendingFile.readText()
            val arr = JSONArray(jsonStr)
            val count = arr.length()
            Log.i(TAG, "Replaying $count pending writes")

            for (i in 0 until count) {
                val op = MemoryWriteOp.fromJson(arr.getJSONObject(i))
                processWrite(op)
            }

            // Clear pending writes after successful replay
            pendingFile.delete()
            Log.i(TAG, "Pending writes replayed and cleared")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to replay pending writes", e)
        }
    }

    // -- Internal --

    private fun processWrite(op: MemoryWriteOp) {
        var currentOp = op
        for (attempt in 1..MAX_RETRIES) {
            try {
                executeWrite(currentOp)
                Log.i(TAG, "Write op ${currentOp.id} succeeded on attempt $attempt")
                onWriteComplete?.invoke(currentOp)
                return
            } catch (e: Exception) {
                Log.w(TAG, "Write op ${currentOp.id} failed on attempt $attempt: ${e.message}")
                currentOp = currentOp.copy(retryCount = attempt)
            }
        }

        // All retries exhausted -- persist to pending_writes.json
        Log.e(TAG, "Write op ${currentOp.id} failed after $MAX_RETRIES retries, persisting")
        persistPendingWrite(currentOp)
    }

    private fun executeWrite(op: MemoryWriteOp) {
        when (op.type) {
            MemoryWriteOp.WriteType.CORE -> {
                if (op.append) {
                    store.appendToCoreMemory(op.content)
                } else {
                    store.writeCoreMemory(op.content)
                }
            }
            MemoryWriteOp.WriteType.DAILY -> {
                val date = LocalDate.parse(op.target)
                if (op.append) {
                    store.appendToDailyMemory(date, op.content)
                } else {
                    store.writeDailyMemory(date, op.content)
                }
            }
            MemoryWriteOp.WriteType.DEEP -> {
                val parts = op.target.split("/", limit = 2)
                val category = DeepMemoryCategory.entries.first { it.dirName == parts[0] }
                val name = parts[1]
                if (op.append) {
                    store.appendToDeepMemory(category, name, op.content)
                } else {
                    store.writeDeepMemory(category, name, op.content)
                }
            }
        }
    }

    private fun persistPendingWrite(op: MemoryWriteOp) {
        val pendingFile = File(rootDir, PENDING_WRITES_FILE)
        try {
            val existing = if (pendingFile.exists()) {
                JSONArray(pendingFile.readText())
            } else {
                JSONArray()
            }
            existing.put(op.toJson())
            pendingFile.writeText(existing.toString(2))
            Log.i(TAG, "Persisted pending write ${op.id} (total: ${existing.length()})")
        } catch (e: IOException) {
            Log.e(TAG, "CRITICAL: Failed to persist pending write ${op.id}", e)
        }
    }

    private fun generateId(): String {
        return "${Instant.now().toEpochMilli()}-${(Math.random() * 10000).toInt()}"
    }
}
```

- [ ] **Step 2: Commit**

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService
git add app/src/main/java/org/dollos/ai/memory/MemoryWriteQueue.kt
git commit -m "feat: add serialized memory write queue with retry and pending persistence"
```

---

## Task 3: SQLite Database with sqlite-vec and FTS5

**Goal:** Set up the SQLite database that holds both the vec0 virtual table (vector search) and FTS5 table (keyword search). This replaces ObjectBox from the previous plan.

**Files:**
- Download: `app/src/main/jniLibs/arm64-v8a/vec0.so` (from sqlite-vec GitHub releases)
- Create: `app/src/main/java/org/dollos/ai/memory/MemoryDatabase.kt`

- [ ] **Step 1: Download vec0.so**

Download the pre-compiled ARM64 sqlite-vec binary from GitHub releases and place it in the jniLibs directory:

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService

mkdir -p app/src/main/jniLibs/arm64-v8a

# Download from: https://github.com/asg017/sqlite-vec/releases
# Pick the latest stable release, download sqlite-vec-<version>-android-aarch64.tar.gz
# Extract vec0.so and place it:
# cp vec0.so app/src/main/jniLibs/arm64-v8a/vec0.so
```

Verify the file exists:

```bash
ls -la app/src/main/jniLibs/arm64-v8a/vec0.so
```

- [ ] **Step 2: Create MemoryDatabase.kt**

Create `app/src/main/java/org/dollos/ai/memory/MemoryDatabase.kt`:

```kotlin
package org.dollos.ai.memory

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.util.Log
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * SQLite database for memory search indexing.
 *
 * Uses two search mechanisms:
 *   1. sqlite-vec (vec0 virtual table) for vector similarity search
 *   2. FTS5 for BM25 keyword search
 *
 * This database is purely a search index -- Markdown files are the source of truth.
 * If the database is corrupted or missing, it can be fully rebuilt from Markdown files.
 *
 * Schema:
 *   - memory_chunks: metadata table (rowid, file_path, chunk_text, file_modified_at)
 *   - memory_chunks_fts: FTS5 virtual table for BM25 keyword search
 *   - memory_chunks_vec: vec0 virtual table for vector similarity search
 */
class MemoryDatabase(
    context: Context,
    private val embeddingDimension: Int = 384
) : SQLiteOpenHelper(context, DB_NAME, null, DB_VERSION) {

    companion object {
        private const val TAG = "MemoryDatabase"
        private const val DB_NAME = "memory_index.db"
        private const val DB_VERSION = 1
    }

    private var vec0Available = false

    /**
     * Initialize the database and attempt to load the sqlite-vec extension.
     * Call this once after construction.
     */
    fun initialize() {
        // Force database creation
        val db = writableDatabase

        // Attempt to load sqlite-vec extension
        try {
            db.execSQL("SELECT load_extension('vec0')")
            vec0Available = true
            Log.i(TAG, "sqlite-vec extension loaded successfully")
        } catch (e: Exception) {
            Log.w(TAG, "sqlite-vec extension not available: ${e.message}")
            Log.w(TAG, "Vector search will be disabled, FTS5 keyword search only")
            vec0Available = false
        }

        // Create vec0 virtual table if extension is available
        if (vec0Available) {
            try {
                db.execSQL("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_vec
                    USING vec0(embedding float[$embeddingDimension])
                """.trimIndent())
                Log.i(TAG, "vec0 virtual table created (dimension=$embeddingDimension)")
            } catch (e: Exception) {
                Log.e(TAG, "Failed to create vec0 virtual table", e)
                vec0Available = false
            }
        }
    }

    /**
     * Whether the sqlite-vec extension is available for vector search.
     */
    fun isVec0Available(): Boolean = vec0Available

    override fun onCreate(db: SQLiteDatabase) {
        // Metadata table for memory chunks
        db.execSQL("""
            CREATE TABLE IF NOT EXISTS memory_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                chunk_text TEXT NOT NULL,
                file_modified_at INTEGER NOT NULL
            )
        """.trimIndent())

        // FTS5 virtual table for BM25 keyword search
        // content= links to memory_chunks table for content sync
        db.execSQL("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_fts
            USING fts5(chunk_text, content=memory_chunks, content_rowid=id)
        """.trimIndent())

        // Triggers to keep FTS5 in sync with memory_chunks
        db.execSQL("""
            CREATE TRIGGER IF NOT EXISTS memory_chunks_ai AFTER INSERT ON memory_chunks BEGIN
                INSERT INTO memory_chunks_fts(rowid, chunk_text)
                VALUES (new.id, new.chunk_text);
            END
        """.trimIndent())

        db.execSQL("""
            CREATE TRIGGER IF NOT EXISTS memory_chunks_ad AFTER DELETE ON memory_chunks BEGIN
                INSERT INTO memory_chunks_fts(memory_chunks_fts, rowid, chunk_text)
                VALUES ('delete', old.id, old.chunk_text);
            END
        """.trimIndent())

        db.execSQL("""
            CREATE TRIGGER IF NOT EXISTS memory_chunks_au AFTER UPDATE ON memory_chunks BEGIN
                INSERT INTO memory_chunks_fts(memory_chunks_fts, rowid, chunk_text)
                VALUES ('delete', old.id, old.chunk_text);
                INSERT INTO memory_chunks_fts(rowid, chunk_text)
                VALUES (new.id, new.chunk_text);
            END
        """.trimIndent())

        // Index on file_path for fast lookups by file
        db.execSQL("""
            CREATE INDEX IF NOT EXISTS idx_chunks_file_path ON memory_chunks(file_path)
        """.trimIndent())

        Log.i(TAG, "Database created with FTS5 tables")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        // For v1, drop and recreate. The database is a search index that can be rebuilt.
        db.execSQL("DROP TABLE IF EXISTS memory_chunks")
        db.execSQL("DROP TABLE IF EXISTS memory_chunks_fts")
        if (vec0Available) {
            db.execSQL("DROP TABLE IF EXISTS memory_chunks_vec")
        }
        onCreate(db)
        Log.i(TAG, "Database upgraded from $oldVersion to $newVersion (full rebuild needed)")
    }

    // -- Indexing operations --

    /**
     * Index a memory file. Splits content into chunks and stores with embeddings.
     * Replaces any existing chunks for the same file path.
     *
     * @param filePath Relative path of the source Markdown file.
     * @param chunks Pre-split text chunks from the file.
     * @param embeddings Embedding vectors for each chunk (same order as chunks).
     * @param fileModifiedAt Last modified timestamp of the source file.
     */
    fun indexFile(
        filePath: String,
        chunks: List<String>,
        embeddings: List<FloatArray>,
        fileModifiedAt: Long
    ) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            // Remove old chunks for this file
            removeFileChunks(db, filePath)

            // Insert new chunks
            for (i in chunks.indices) {
                val values = ContentValues().apply {
                    put("file_path", filePath)
                    put("chunk_text", chunks[i])
                    put("file_modified_at", fileModifiedAt)
                }
                val rowId = db.insert("memory_chunks", null, values)

                // Insert embedding into vec0 if available
                if (vec0Available && i < embeddings.size) {
                    insertVec0Embedding(db, rowId, embeddings[i])
                }
            }

            db.setTransactionSuccessful()
            Log.i(TAG, "Indexed $filePath: ${chunks.size} chunks")
        } finally {
            db.endTransaction()
        }
    }

    /**
     * Remove all chunks for a file path.
     */
    fun removeFile(filePath: String) {
        val db = writableDatabase
        removeFileChunks(db, filePath)
    }

    /**
     * Remove all data from the database.
     */
    fun removeAll() {
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.execSQL("DELETE FROM memory_chunks")
            if (vec0Available) {
                db.execSQL("DELETE FROM memory_chunks_vec")
            }
            db.setTransactionSuccessful()
            Log.i(TAG, "Cleared all indexed data")
        } finally {
            db.endTransaction()
        }
    }

    // -- Search operations --

    /**
     * Vector similarity search using sqlite-vec (vec0).
     * Returns a list of (rowId, distance) pairs sorted by distance ascending.
     *
     * @param queryEmbedding The query embedding vector.
     * @param limit Maximum number of results.
     * @return List of (rowId, distance) pairs. Empty if vec0 is not available.
     */
    fun vectorSearch(queryEmbedding: FloatArray, limit: Int): List<Pair<Long, Float>> {
        if (!vec0Available) return emptyList()

        val db = readableDatabase
        val results = mutableListOf<Pair<Long, Float>>()

        val embeddingBlob = floatArrayToBlob(queryEmbedding)

        try {
            val cursor = db.rawQuery(
                "SELECT rowid, distance FROM memory_chunks_vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                arrayOf(embeddingBlob.toString(), limit.toString())
            )
            // Note: sqlite-vec expects the embedding as a blob parameter.
            // Use the blob binding approach via a compiled statement.
            cursor.close()

            // Use compiled statement for proper blob binding
            val stmt = db.compileStatement(
                "SELECT rowid, distance FROM memory_chunks_vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?"
            )
            // Unfortunately, Android's SQLiteStatement doesn't support SELECT.
            // Use rawQuery with hex-encoded blob instead.

            val hexBlob = bytesToHex(embeddingBlob)
            val queryCursor = db.rawQuery(
                "SELECT rowid, distance FROM memory_chunks_vec WHERE embedding MATCH x'$hexBlob' ORDER BY distance LIMIT $limit",
                null
            )

            queryCursor.use { c ->
                while (c.moveToNext()) {
                    val rowId = c.getLong(0)
                    val distance = c.getFloat(1)
                    results.add(rowId to distance)
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Vector search failed", e)
        }

        return results
    }

    /**
     * BM25 keyword search using FTS5.
     * Returns a list of (rowId, bm25Score) pairs sorted by relevance.
     *
     * @param query The search query string.
     * @param limit Maximum number of results.
     * @return List of (rowId, bm25Score) pairs.
     */
    fun fts5Search(query: String, limit: Int): List<Pair<Long, Float>> {
        val db = readableDatabase
        val results = mutableListOf<Pair<Long, Float>>()

        // Escape FTS5 special characters
        val safeQuery = query.replace("\"", "\"\"")

        try {
            val cursor = db.rawQuery(
                """
                SELECT rowid, bm25(memory_chunks_fts)
                FROM memory_chunks_fts
                WHERE memory_chunks_fts MATCH ?
                ORDER BY bm25(memory_chunks_fts)
                LIMIT ?
                """.trimIndent(),
                arrayOf(safeQuery, limit.toString())
            )

            cursor.use { c ->
                while (c.moveToNext()) {
                    val rowId = c.getLong(0)
                    // FTS5 bm25() returns negative values (lower = better match)
                    // Negate it so higher = better
                    val bm25Score = -c.getFloat(1)
                    results.add(rowId to bm25Score)
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "FTS5 search failed for query: $query", e)
        }

        return results
    }

    /**
     * Get chunk metadata by rowId.
     */
    fun getChunk(rowId: Long): ChunkRecord? {
        val db = readableDatabase
        val cursor = db.rawQuery(
            "SELECT id, file_path, chunk_text, file_modified_at FROM memory_chunks WHERE id = ?",
            arrayOf(rowId.toString())
        )

        return cursor.use { c ->
            if (c.moveToFirst()) {
                ChunkRecord(
                    id = c.getLong(0),
                    filePath = c.getString(1),
                    chunkText = c.getString(2),
                    fileModifiedAt = c.getLong(3)
                )
            } else {
                null
            }
        }
    }

    /**
     * Get multiple chunks by rowIds.
     */
    fun getChunks(rowIds: List<Long>): Map<Long, ChunkRecord> {
        if (rowIds.isEmpty()) return emptyMap()

        val db = readableDatabase
        val placeholders = rowIds.joinToString(",") { "?" }
        val args = rowIds.map { it.toString() }.toTypedArray()

        val result = mutableMapOf<Long, ChunkRecord>()
        val cursor = db.rawQuery(
            "SELECT id, file_path, chunk_text, file_modified_at FROM memory_chunks WHERE id IN ($placeholders)",
            args
        )

        cursor.use { c ->
            while (c.moveToNext()) {
                val id = c.getLong(0)
                result[id] = ChunkRecord(
                    id = id,
                    filePath = c.getString(1),
                    chunkText = c.getString(2),
                    fileModifiedAt = c.getLong(3)
                )
            }
        }

        return result
    }

    /**
     * Check if a file is indexed and up to date.
     */
    fun isFileIndexed(filePath: String, currentModifiedAt: Long): Boolean {
        val db = readableDatabase
        val cursor = db.rawQuery(
            "SELECT file_modified_at FROM memory_chunks WHERE file_path = ? LIMIT 1",
            arrayOf(filePath)
        )

        return cursor.use { c ->
            if (c.moveToFirst()) {
                val indexedAt = c.getLong(0)
                indexedAt >= currentModifiedAt
            } else {
                false
            }
        }
    }

    /**
     * Get the count of indexed chunks.
     */
    fun chunkCount(): Int {
        val db = readableDatabase
        val cursor = db.rawQuery("SELECT COUNT(*) FROM memory_chunks", null)
        return cursor.use { c ->
            if (c.moveToFirst()) c.getInt(0) else 0
        }
    }

    // -- Private helpers --

    private fun removeFileChunks(db: SQLiteDatabase, filePath: String) {
        // Get rowIds for vec0 cleanup
        if (vec0Available) {
            val cursor = db.rawQuery(
                "SELECT id FROM memory_chunks WHERE file_path = ?",
                arrayOf(filePath)
            )
            cursor.use { c ->
                while (c.moveToNext()) {
                    val rowId = c.getLong(0)
                    try {
                        db.execSQL("DELETE FROM memory_chunks_vec WHERE rowid = ?", arrayOf(rowId))
                    } catch (e: Exception) {
                        Log.w(TAG, "Failed to delete vec0 row $rowId", e)
                    }
                }
            }
        }

        // Delete from metadata table (triggers handle FTS5 sync)
        db.delete("memory_chunks", "file_path = ?", arrayOf(filePath))
    }

    private fun insertVec0Embedding(db: SQLiteDatabase, rowId: Long, embedding: FloatArray) {
        try {
            val blob = floatArrayToBlob(embedding)
            val hexBlob = bytesToHex(blob)
            db.execSQL(
                "INSERT INTO memory_chunks_vec(rowid, embedding) VALUES (?, x'$hexBlob')",
                arrayOf(rowId)
            )
        } catch (e: Exception) {
            Log.w(TAG, "Failed to insert vec0 embedding for rowid=$rowId", e)
        }
    }

    /**
     * Convert a FloatArray to a little-endian byte array (blob format for sqlite-vec).
     */
    private fun floatArrayToBlob(arr: FloatArray): ByteArray {
        val buffer = ByteBuffer.allocate(arr.size * 4).order(ByteOrder.LITTLE_ENDIAN)
        for (f in arr) {
            buffer.putFloat(f)
        }
        return buffer.array()
    }

    /**
     * Convert a byte array to a hex string for SQL embedding.
     */
    private fun bytesToHex(bytes: ByteArray): String {
        val sb = StringBuilder(bytes.size * 2)
        for (b in bytes) {
            sb.append(String.format("%02x", b))
        }
        return sb.toString()
    }
}

/**
 * A record from the memory_chunks table.
 */
data class ChunkRecord(
    val id: Long,
    val filePath: String,
    val chunkText: String,
    val fileModifiedAt: Long
)
```

- [ ] **Step 3: Commit**

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService
git add app/src/main/jniLibs/arm64-v8a/vec0.so
git add app/src/main/java/org/dollos/ai/memory/MemoryDatabase.kt
git commit -m "feat: add SQLite database with sqlite-vec and FTS5 for memory search index"
```

---

## Task 4: Embedding Provider Interface and Implementations

**Goal:** Define the embedding provider interface, implement cloud embedding (OpenAI text-embedding-3-small via OkHttp), and a local placeholder that returns zero vectors.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/memory/EmbeddingProvider.kt`
- Create: `app/src/main/java/org/dollos/ai/memory/CloudEmbeddingProvider.kt`
- Create: `app/src/main/java/org/dollos/ai/memory/LocalEmbeddingProvider.kt`

- [ ] **Step 1: Create EmbeddingProvider.kt**

Create `app/src/main/java/org/dollos/ai/memory/EmbeddingProvider.kt`:

```kotlin
package org.dollos.ai.memory

/**
 * Interface for generating text embeddings.
 * Used by the memory search engine for vector similarity search.
 *
 * Two implementations:
 *   - CloudEmbeddingProvider: OpenAI text-embedding-3-small (384 dims via shortening)
 *   - LocalEmbeddingProvider: placeholder returning zero vectors (384 dims)
 *
 * Dimension is fixed at 384 for sqlite-vec table compatibility.
 * OpenAI text-embedding-3-small supports dimension shortening via the "dimensions" parameter.
 */
interface EmbeddingProvider {

    /** Dimension of the embedding vectors produced by this provider. */
    val dimension: Int

    /** Human-readable name for logging. */
    val name: String

    /** Whether this provider requires network access. */
    val requiresNetwork: Boolean

    /**
     * Generate an embedding vector for the given text.
     * Returns a float array of size [dimension].
     * Throws on failure (network error, invalid API key, etc.).
     */
    suspend fun embed(text: String): FloatArray

    /**
     * Generate embeddings for multiple texts in a batch.
     * Default implementation calls [embed] for each text sequentially.
     */
    suspend fun embedBatch(texts: List<String>): List<FloatArray> {
        return texts.map { embed(it) }
    }
}
```

- [ ] **Step 2: Create CloudEmbeddingProvider.kt**

Create `app/src/main/java/org/dollos/ai/memory/CloudEmbeddingProvider.kt`:

```kotlin
package org.dollos.ai.memory

import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Cloud embedding via OpenAI text-embedding-3-small API.
 * Dimension: 384 (via OpenAI's dimension shortening parameter).
 *
 * Uses OkHttp for HTTP requests (from Plan A's existing dependency).
 * Requires a valid OpenAI API key set via [setApiKey].
 */
class CloudEmbeddingProvider : EmbeddingProvider {

    companion object {
        private const val TAG = "CloudEmbeddingProvider"
        private const val OPENAI_EMBEDDING_URL = "https://api.openai.com/v1/embeddings"
        private const val MODEL = "text-embedding-3-small"
        private const val EMBEDDING_DIMENSION = 384
    }

    override val dimension: Int = EMBEDDING_DIMENSION
    override val name: String = "OpenAI text-embedding-3-small"
    override val requiresNetwork: Boolean = true

    private var apiKey: String = ""

    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    fun setApiKey(key: String) {
        apiKey = key
    }

    override suspend fun embed(text: String): FloatArray = withContext(Dispatchers.IO) {
        if (apiKey.isBlank()) {
            throw IllegalStateException("OpenAI API key not set for embedding")
        }

        val requestBody = JSONObject().apply {
            put("input", text)
            put("model", MODEL)
            put("dimensions", EMBEDDING_DIMENSION)
        }

        val request = Request.Builder()
            .url(OPENAI_EMBEDDING_URL)
            .addHeader("Authorization", "Bearer $apiKey")
            .addHeader("Content-Type", "application/json")
            .post(requestBody.toString().toRequestBody("application/json".toMediaType()))
            .build()

        val response = client.newCall(request).execute()
        response.use { resp ->
            if (!resp.isSuccessful) {
                val errorBody = resp.body?.string() ?: ""
                throw RuntimeException(
                    "OpenAI embedding API returned ${resp.code}: $errorBody"
                )
            }

            val json = JSONObject(resp.body!!.string())
            val embeddingArray = json
                .getJSONArray("data")
                .getJSONObject(0)
                .getJSONArray("embedding")

            jsonArrayToFloatArray(embeddingArray)
        }
    }

    override suspend fun embedBatch(texts: List<String>): List<FloatArray> =
        withContext(Dispatchers.IO) {
            if (apiKey.isBlank()) {
                throw IllegalStateException("OpenAI API key not set for embedding")
            }
            if (texts.isEmpty()) return@withContext emptyList()

            val inputArray = JSONArray()
            texts.forEach { inputArray.put(it) }

            val requestBody = JSONObject().apply {
                put("input", inputArray)
                put("model", MODEL)
                put("dimensions", EMBEDDING_DIMENSION)
            }

            val request = Request.Builder()
                .url(OPENAI_EMBEDDING_URL)
                .addHeader("Authorization", "Bearer $apiKey")
                .addHeader("Content-Type", "application/json")
                .post(requestBody.toString().toRequestBody("application/json".toMediaType()))
                .build()

            val response = client.newCall(request).execute()
            response.use { resp ->
                if (!resp.isSuccessful) {
                    val errorBody = resp.body?.string() ?: ""
                    throw RuntimeException(
                        "OpenAI embedding batch API returned ${resp.code}: $errorBody"
                    )
                }

                val json = JSONObject(resp.body!!.string())
                val dataArray = json.getJSONArray("data")

                val results = mutableListOf<FloatArray>()
                for (i in 0 until dataArray.length()) {
                    val embeddingArray = dataArray.getJSONObject(i).getJSONArray("embedding")
                    results.add(jsonArrayToFloatArray(embeddingArray))
                }

                Log.i(TAG, "Batch embedded ${texts.size} texts")
                results
            }
        }

    private fun jsonArrayToFloatArray(arr: JSONArray): FloatArray {
        val result = FloatArray(arr.length())
        for (i in 0 until arr.length()) {
            result[i] = arr.getDouble(i).toFloat()
        }
        return result
    }
}
```

- [ ] **Step 3: Create LocalEmbeddingProvider.kt**

Create `app/src/main/java/org/dollos/ai/memory/LocalEmbeddingProvider.kt`:

```kotlin
package org.dollos.ai.memory

import android.util.Log

/**
 * Local ONNX embedding provider placeholder.
 *
 * Target model: all-MiniLM-L6-v2 (~23MB, 384 dimensions).
 * For v1, this returns zero vectors. Actual ONNX Runtime integration
 * will be implemented in a future plan.
 *
 * When fully implemented, this will:
 *   1. Load all-MiniLM-L6-v2.onnx from assets
 *   2. Tokenize input text (WordPiece tokenizer)
 *   3. Run inference via ONNX Runtime Android
 *   4. Mean-pool the token embeddings
 *   5. L2-normalize the result
 */
class LocalEmbeddingProvider : EmbeddingProvider {

    companion object {
        private const val TAG = "LocalEmbeddingProvider"
        private const val EMBEDDING_DIMENSION = 384
    }

    override val dimension: Int = EMBEDDING_DIMENSION
    override val name: String = "Local ONNX all-MiniLM-L6-v2 (placeholder)"
    override val requiresNetwork: Boolean = false

    override suspend fun embed(text: String): FloatArray {
        Log.w(TAG, "Using placeholder zero vector -- local ONNX not yet implemented")
        return FloatArray(EMBEDDING_DIMENSION)
    }

    override suspend fun embedBatch(texts: List<String>): List<FloatArray> {
        Log.w(TAG, "Using placeholder zero vectors -- local ONNX not yet implemented")
        return texts.map { FloatArray(EMBEDDING_DIMENSION) }
    }
}
```

- [ ] **Step 4: Commit**

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService
git add app/src/main/java/org/dollos/ai/memory/EmbeddingProvider.kt
git add app/src/main/java/org/dollos/ai/memory/CloudEmbeddingProvider.kt
git add app/src/main/java/org/dollos/ai/memory/LocalEmbeddingProvider.kt
git commit -m "feat: add embedding provider interface with cloud (OkHttp) and local implementations"
```

---

## Task 5: Memory Search Engine (Hybrid Search with Fallback)

**Goal:** Implement the top-level search engine that uses sqlite-vec for vector search and FTS5 for BM25, with configurable weights and a fallback to FTS5-only when vec0 is unavailable.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/memory/MemorySearchEngine.kt`

- [ ] **Step 1: Create MemorySearchEngine.kt**

Create `app/src/main/java/org/dollos/ai/memory/MemorySearchEngine.kt`:

```kotlin
package org.dollos.ai.memory

import android.util.Log

/**
 * Hybrid memory search engine.
 *
 * Combines two search strategies:
 *   1. Vector search via sqlite-vec (cosine similarity via vec0)
 *   2. Keyword search via FTS5 (BM25 scoring)
 *
 * Results are merged using reciprocal rank fusion (RRF) with configurable weights.
 * Falls back to FTS5-only if vec0 is not available.
 * Falls back to simple substring matching if both are unavailable.
 *
 * Markdown files are always the source of truth.
 */
class MemorySearchEngine(
    private val store: MarkdownStore,
    private val database: MemoryDatabase,
    private val embeddingProvider: EmbeddingProvider
) {
    companion object {
        private const val TAG = "MemorySearchEngine"
        private const val MAX_RESULTS = 10
        private const val MIN_QUERY_LENGTH = 2
        private const val RRF_K = 60 // Reciprocal rank fusion constant
    }

    var vectorWeight: Float = 0.7f
    var bm25Weight: Float = 0.3f

    /**
     * Search memory for content relevant to the query.
     * Returns a list of SearchResult sorted by relevance.
     */
    suspend fun search(query: String, maxResults: Int = MAX_RESULTS): List<SearchResult> {
        if (query.length < MIN_QUERY_LENGTH) {
            Log.d(TAG, "Query too short, skipping search")
            return emptyList()
        }

        // BM25 keyword search via FTS5 (always available)
        val bm25Results = try {
            database.fts5Search(query, maxResults * 2)
        } catch (e: Exception) {
            Log.e(TAG, "FTS5 search failed", e)
            emptyList()
        }

        // Vector search via sqlite-vec (only if vec0 is available)
        val vectorResults = if (database.isVec0Available()) {
            try {
                val queryEmbedding = embeddingProvider.embed(query)
                // Skip vector search if embedding is all zeros (placeholder provider)
                val isZeroVector = queryEmbedding.all { it == 0f }
                if (isZeroVector) {
                    Log.d(TAG, "Skipping vector search -- zero embedding (placeholder provider)")
                    emptyList()
                } else {
                    database.vectorSearch(queryEmbedding, maxResults * 2)
                }
            } catch (e: Exception) {
                Log.e(TAG, "Vector search failed", e)
                emptyList()
            }
        } else {
            emptyList()
        }

        // If both searches returned nothing, fall back to substring search
        if (vectorResults.isEmpty() && bm25Results.isEmpty()) {
            Log.d(TAG, "No results from vec0 or FTS5, falling back to substring search")
            return substringSearch(query, maxResults)
        }

        // Merge results using reciprocal rank fusion
        val merged = mergeResults(vectorResults, bm25Results, maxResults)
        Log.i(TAG, "Hybrid search returned ${merged.size} results for: $query " +
            "(vec0=${vectorResults.size}, fts5=${bm25Results.size})")
        return merged
    }

    /**
     * Merge vector and BM25 results using reciprocal rank fusion.
     */
    private fun mergeResults(
        vectorResults: List<Pair<Long, Float>>,
        bm25Results: List<Pair<Long, Float>>,
        maxResults: Int
    ): List<SearchResult> {
        val scoreMap = mutableMapOf<Long, MergedScore>()

        // Add vector scores
        vectorResults.forEachIndexed { rank, (rowId, distance) ->
            val existing = scoreMap.getOrPut(rowId) { MergedScore(rowId) }
            // Convert distance to similarity: 1 / (1 + distance)
            existing.vectorScore = 1.0f / (1.0f + distance)
            existing.vectorRank = rank
        }

        // Add BM25 scores
        bm25Results.forEachIndexed { rank, (rowId, bm25Score) ->
            val existing = scoreMap.getOrPut(rowId) { MergedScore(rowId) }
            existing.bm25Score = bm25Score
            existing.bm25Rank = rank
        }

        // Compute combined RRF score and resolve chunk metadata
        val allRowIds = scoreMap.keys.toList()
        val chunkMap = database.getChunks(allRowIds)

        return scoreMap.values
            .mapNotNull { merged ->
                val chunk = chunkMap[merged.rowId] ?: return@mapNotNull null

                val vectorRRF = if (merged.vectorRank >= 0) {
                    vectorWeight / (RRF_K + merged.vectorRank)
                } else 0f
                val bm25RRF = if (merged.bm25Rank >= 0) {
                    bm25Weight / (RRF_K + merged.bm25Rank)
                } else 0f

                SearchResult(
                    filePath = chunk.filePath,
                    content = chunk.chunkText,
                    score = vectorRRF + bm25RRF,
                    vectorScore = merged.vectorScore,
                    bm25Score = merged.bm25Score
                )
            }
            .sortedByDescending { it.score }
            .take(maxResults)
    }

    /**
     * Simple substring search across all Markdown memory files.
     * Used as a last-resort fallback when both vec0 and FTS5 return no results.
     */
    private fun substringSearch(query: String, maxResults: Int): List<SearchResult> {
        val queryTerms = query.lowercase().split(Regex("\\s+")).filter { it.length > MIN_QUERY_LENGTH }
        if (queryTerms.isEmpty()) return emptyList()

        val results = mutableListOf<SearchResult>()
        val files = store.listAllMemoryFiles()

        for ((relativePath, file) in files) {
            try {
                val content = file.readText()
                if (content.isBlank()) continue

                // Split into sections for granular results
                val sections = splitIntoSections(content)
                for (section in sections) {
                    val sectionLower = section.lowercase()
                    val matchCount = queryTerms.count { term -> sectionLower.contains(term) }
                    if (matchCount > 0) {
                        val score = matchCount.toFloat() / queryTerms.size.toFloat()
                        results.add(SearchResult(
                            filePath = relativePath,
                            content = section,
                            score = score,
                            bm25Score = score
                        ))
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error reading $relativePath during search", e)
            }
        }

        Log.i(TAG, "Substring search returned ${results.size} results for: $query")
        return results
            .sortedByDescending { it.score }
            .take(maxResults)
    }

    /**
     * Split Markdown content into sections for search granularity.
     */
    private fun splitIntoSections(content: String): List<String> {
        val sections = content.split(Regex("(?=^## )", RegexOption.MULTILINE))
            .map { it.trim() }
            .filter { it.isNotBlank() }

        if (sections.size <= 1) {
            // No headers -- split by double newlines
            val paragraphs = content.split(Regex("\n\n+"))
                .map { it.trim() }
                .filter { it.isNotBlank() && it.length > 20 }
            if (paragraphs.isNotEmpty()) return paragraphs
            // Return whole content as single section
            return listOf(content.trim())
        }

        return sections
    }

    // -- Internal data classes --

    private data class MergedScore(
        val rowId: Long,
        var vectorScore: Float = 0f,
        var bm25Score: Float = 0f,
        var vectorRank: Int = -1,
        var bm25Rank: Int = -1
    )
}

/**
 * A search result from hybrid search.
 */
data class SearchResult(
    val filePath: String,
    val content: String,
    val score: Float,
    val vectorScore: Float = 0f,
    val bm25Score: Float = 0f
)
```

- [ ] **Step 2: Commit**

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService
git add app/src/main/java/org/dollos/ai/memory/MemorySearchEngine.kt
git commit -m "feat: add hybrid memory search engine with vec0 + FTS5 and substring fallback"
```

---

## Task 6: Memory Manager (Orchestrator with Write Triggers)

**Goal:** Implement the main memory orchestrator that ties together all memory components, handles write triggers (context threshold, idle, event-based), manages index sync, and manages the "remember this" flow.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/memory/MemoryManager.kt`

- [ ] **Step 1: Create MemoryManager.kt**

Create `app/src/main/java/org/dollos/ai/memory/MemoryManager.kt`:

```kotlin
package org.dollos.ai.memory

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.io.File
import java.time.LocalDate

/**
 * Main memory orchestrator.
 *
 * Manages three-tier memory, coordinates write triggers, handles "remember this" flow,
 * and keeps the SQLite index (vec0 + FTS5) in sync with Markdown files.
 *
 * Write triggers (all feed into a single serialized write queue):
 *   1. Context threshold -- extract facts when context reaches 70-80% capacity
 *   2. Idle background -- review recent conversation after N minutes idle
 *   3. Event-based -- screen lock, app switch, "remember this"
 */
class MemoryManager(
    private val context: Context
) {
    companion object {
        private const val TAG = "MemoryManager"
        private const val IDLE_TRIGGER_DELAY_MS = 5L * 60 * 1000 // 5 minutes
        private const val PREFS_NAME = "memory_config"
        private const val KEY_EMBEDDING_SOURCE = "embedding_source"
    }

    private val prefs: SharedPreferences =
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    private val rootDir = File(context.filesDir, "memory")
    val store = MarkdownStore(rootDir)
    private val embeddingProvider: EmbeddingProvider = createEmbeddingProvider()
    val database = MemoryDatabase(context, embeddingProvider.dimension)
    val searchEngine = MemorySearchEngine(store, database, embeddingProvider)
    val writeQueue = MemoryWriteQueue(store, rootDir) { op ->
        // After each successful write, update the SQLite index
        onWriteComplete(op)
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var idleJob: Job? = null
    private var pendingRememberContent: String? = null

    /**
     * Callback for when the AI presents a "remember this" confirmation to the user.
     * Set this to route the confirmation through the AIDL callback.
     */
    var onMemoryConfirmRequired: ((String) -> Unit)? = null

    /**
     * Initialize the memory system. Call on service startup.
     */
    fun init() {
        Log.i(TAG, "Initializing memory system...")

        // Replay any pending writes from previous crash/failure
        writeQueue.replayPendingWrites()

        // Start the write queue
        writeQueue.start()

        // Initialize SQLite database and load sqlite-vec extension
        database.initialize()

        // Check index consistency and rebuild if needed
        scope.launch {
            if (!isIndexConsistent()) {
                Log.i(TAG, "SQLite index inconsistent, rebuilding...")
                rebuildIndex()
            }
        }

        // Ensure core memory file exists
        if (store.readCoreMemory().isBlank()) {
            store.writeCoreMemory("# Memory\n\nCore facts and preferences.\n")
            Log.i(TAG, "Created initial MEMORY.md")
        }

        Log.i(TAG, "Memory system initialized")
    }

    /**
     * Shut down the memory system. Call on service shutdown.
     */
    fun shutdown() {
        idleJob?.cancel()
        writeQueue.stop()
        database.close()
        Log.i(TAG, "Memory system shut down")
    }

    // -- Context loading for LLM --

    /**
     * Build the memory context to inject into the LLM system prompt.
     * Includes Tier 1 (always) + Tier 2 (today + yesterday).
     */
    fun buildMemoryContext(): String {
        val parts = mutableListOf<String>()

        // Tier 1: Core memory
        val coreMemory = store.readCoreMemory()
        if (coreMemory.isNotBlank()) {
            parts.add("# Core Memory\n\n$coreMemory")
        }

        // Tier 2: Recent daily memory
        val dailyMemory = store.loadRecentDailyMemories()
        if (dailyMemory.isNotBlank()) {
            parts.add("# Recent Notes\n\n$dailyMemory")
        }

        return parts.joinToString("\n\n---\n\n")
    }

    /**
     * Search Tier 3 memory for relevant context based on user message.
     * Returns formatted search results to include in the LLM context.
     */
    suspend fun searchRelevantMemory(query: String): String {
        val results = searchEngine.search(query, maxResults = 5)
        if (results.isEmpty()) return ""

        val formatted = results.joinToString("\n\n") { result ->
            "### ${result.filePath}\n${result.content}"
        }

        return "# Relevant Memory (from search)\n\n$formatted"
    }

    // -- Write trigger 1: Context threshold --

    /**
     * Called when context usage reaches the compression threshold (70-80%).
     * Extracts important facts from the conversation before compression.
     *
     * @param extractedFacts Facts extracted by the LLM from the conversation being compressed.
     */
    fun onContextThresholdReached(extractedFacts: List<String>) {
        if (extractedFacts.isEmpty()) return

        val today = LocalDate.now()
        val content = extractedFacts.joinToString("\n") { "- $it" }

        writeQueue.enqueueDailyWrite(
            date = today,
            content = "### From conversation (compressed)\n\n$content"
        )

        Log.i(TAG, "Context threshold triggered: ${extractedFacts.size} facts queued for write")
    }

    // -- Write trigger 2: Idle background --

    /**
     * Reset the idle timer. Call after each user message or AI response.
     */
    fun resetIdleTimer() {
        idleJob?.cancel()
        idleJob = scope.launch {
            delay(IDLE_TRIGGER_DELAY_MS)
            onIdleTriggered()
        }
    }

    /**
     * Cancel the idle timer. Call when the conversation becomes active.
     */
    fun cancelIdleTimer() {
        idleJob?.cancel()
        idleJob = null
    }

    /**
     * Called when the idle timer fires.
     * The actual memory extraction is done by the LLM (background model) --
     * this method is the trigger point that the ConversationManager calls.
     */
    private fun onIdleTriggered() {
        Log.i(TAG, "Idle trigger fired after ${IDLE_TRIGGER_DELAY_MS / 1000}s")
        onIdleCallback?.invoke()
    }

    /**
     * Callback invoked when idle trigger fires.
     * Set this so ConversationManager can send recent conversation to background model
     * for memory extraction.
     */
    var onIdleCallback: (() -> Unit)? = null

    /**
     * Write memories extracted by the background model during idle processing.
     *
     * @param memories List of memory items to write.
     */
    fun writeIdleExtractedMemories(memories: List<ExtractedMemory>) {
        val today = LocalDate.now()
        for (memory in memories) {
            when (memory.tier) {
                MemoryTier.CORE -> {
                    writeQueue.enqueueCoreWrite(memory.content)
                }
                MemoryTier.DAILY -> {
                    writeQueue.enqueueDailyWrite(today, memory.content)
                }
                MemoryTier.DEEP -> {
                    val category = memory.category ?: DeepMemoryCategory.TOPICS
                    val name = memory.name ?: "uncategorized"
                    writeQueue.enqueueDeepWrite(category, name, memory.content)
                }
            }
        }
        Log.i(TAG, "Idle extraction: ${memories.size} memories queued for write")
    }

    // -- Write trigger 3: Event-based --

    /**
     * Called on screen lock or app switch.
     * Similar to idle trigger but immediate.
     */
    fun onScreenLockOrAppSwitch() {
        Log.i(TAG, "Event trigger: screen lock or app switch")
        onIdleCallback?.invoke()
    }

    /**
     * Handle "remember this" request from user.
     * The AI extracts and formats the memory, then presents to user for confirmation.
     *
     * @param formattedMemory The memory content formatted by the AI.
     */
    fun requestRememberThis(formattedMemory: String) {
        pendingRememberContent = formattedMemory
        onMemoryConfirmRequired?.invoke(formattedMemory)
        Log.i(TAG, "Remember-this: presented to user for confirmation")
    }

    /**
     * User confirms or denies the "remember this" write.
     *
     * @param approved True if user approves, false if denied.
     */
    fun confirmRememberThis(approved: Boolean) {
        val content = pendingRememberContent
        pendingRememberContent = null

        if (!approved || content == null) {
            Log.i(TAG, "Remember-this: user denied or no pending content")
            return
        }

        val today = LocalDate.now()
        writeQueue.enqueueDailyWrite(
            date = today,
            content = "### Remembered\n\n$content"
        )
        Log.i(TAG, "Remember-this: user approved, queued for write")
    }

    // -- Index sync --

    /**
     * Called after a successful memory write to update the SQLite index.
     */
    private fun onWriteComplete(op: MemoryWriteQueue.MemoryWriteOp) {
        scope.launch {
            try {
                val relativePath = when (op.type) {
                    MemoryWriteQueue.MemoryWriteOp.WriteType.CORE -> "MEMORY.md"
                    MemoryWriteQueue.MemoryWriteOp.WriteType.DAILY -> "memory/${op.target}.md"
                    MemoryWriteQueue.MemoryWriteOp.WriteType.DEEP -> "memory/${op.target}.md"
                }

                // Re-read the full file content (may have been appended to)
                val allFiles = store.listAllMemoryFiles()
                val match = allFiles.find { it.first == relativePath }
                if (match != null) {
                    val content = match.second.readText()
                    val chunks = splitIntoChunks(content)
                    val embeddings = try {
                        embeddingProvider.embedBatch(chunks)
                    } catch (e: Exception) {
                        Log.e(TAG, "Failed to generate embeddings for $relativePath", e)
                        chunks.map { FloatArray(embeddingProvider.dimension) }
                    }
                    database.indexFile(relativePath, chunks, embeddings, match.second.lastModified())
                }
            } catch (e: Exception) {
                Log.e(TAG, "Failed to update index after write op ${op.id}", e)
            }
        }
    }

    /**
     * Rebuild the entire index from Markdown files.
     */
    suspend fun rebuildIndex() {
        Log.i(TAG, "Rebuilding index from Markdown files...")
        database.removeAll()

        val files = store.listAllMemoryFiles()
        var totalChunks = 0
        for ((relativePath, file) in files) {
            try {
                val content = file.readText()
                if (content.isNotBlank()) {
                    val chunks = splitIntoChunks(content)
                    val embeddings = try {
                        embeddingProvider.embedBatch(chunks)
                    } catch (e: Exception) {
                        Log.e(TAG, "Failed to generate embeddings for $relativePath", e)
                        chunks.map { FloatArray(embeddingProvider.dimension) }
                    }
                    database.indexFile(relativePath, chunks, embeddings, file.lastModified())
                    totalChunks += chunks.size
                }
            } catch (e: Exception) {
                Log.e(TAG, "Failed to index $relativePath during rebuild", e)
            }
        }
        Log.i(TAG, "Index rebuild complete: ${files.size} files, $totalChunks chunks")
    }

    /**
     * Check if the index is consistent with the Markdown files.
     */
    private fun isIndexConsistent(): Boolean {
        val files = store.listAllMemoryFiles()
        for ((relativePath, file) in files) {
            if (!database.isFileIndexed(relativePath, file.lastModified())) {
                Log.d(TAG, "Index missing or stale for file: $relativePath")
                return false
            }
        }
        return true
    }

    // -- Embedding source management --

    /**
     * Switch the embedding source. Triggers a full index rebuild since
     * the vector dimensions may differ between providers.
     */
    fun setEmbeddingSource(source: String) {
        Log.i(TAG, "Switching embedding source to: $source")
        prefs.edit().putString(KEY_EMBEDDING_SOURCE, source).apply()
        // Changing the embedding provider requires restarting the memory system
        // because the vec0 table dimension is fixed at creation time.
        Log.w(TAG, "Embedding source change requires service restart to take effect")
    }

    fun getEmbeddingSource(): String {
        return prefs.getString(KEY_EMBEDDING_SOURCE, "cloud") ?: "cloud"
    }

    // -- Helpers --

    private fun createEmbeddingProvider(): EmbeddingProvider {
        val source = prefs.getString(KEY_EMBEDDING_SOURCE, "cloud") ?: "cloud"
        return when (source) {
            "cloud" -> CloudEmbeddingProvider()
            "local" -> LocalEmbeddingProvider()
            else -> {
                Log.w(TAG, "Unknown embedding source '$source', defaulting to local")
                LocalEmbeddingProvider()
            }
        }
    }

    /**
     * Split Markdown content into chunks by section headers (##) or double newlines.
     * Each chunk is a meaningful unit for embedding and search.
     */
    private fun splitIntoChunks(content: String): List<String> {
        if (content.isBlank()) return emptyList()

        // Split by ## headers first
        val sections = content.split(Regex("(?=^## )", RegexOption.MULTILINE))
            .map { it.trim() }
            .filter { it.isNotBlank() }

        // If no sections found, split by double newlines
        if (sections.size <= 1) {
            val paragraphs = content.split(Regex("\n\n+"))
                .map { it.trim() }
                .filter { it.isNotBlank() && it.length > 20 }
            if (paragraphs.isNotEmpty()) return paragraphs
        }

        return sections
    }
}

/**
 * Represents a memory item extracted by the LLM during idle processing.
 */
data class ExtractedMemory(
    val tier: MemoryTier,
    val content: String,
    val category: DeepMemoryCategory? = null,  // Only for DEEP tier
    val name: String? = null                    // Only for DEEP tier (filename without .md)
)
```

- [ ] **Step 2: Commit**

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService
git add app/src/main/java/org/dollos/ai/memory/MemoryManager.kt
git commit -m "feat: add MemoryManager orchestrator with three write triggers and index sync"
```

---

## Task 7: Conversation Segment and Message Store

**Goal:** Implement date-segmented conversation storage and the persistent message store using SQLite.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/conversation/ConversationSegment.kt`
- Create: `app/src/main/java/org/dollos/ai/conversation/MessageStore.kt`

- [x] **Step 1: Create ConversationSegment.kt**

Create `app/src/main/java/org/dollos/ai/conversation/ConversationSegment.kt`:

```kotlin
package org.dollos.ai.conversation

import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId

/**
 * A single message in a conversation.
 */
data class Message(
    val id: String,
    val role: Role,
    val content: String,
    val timestamp: Long = Instant.now().toEpochMilli(),
    val tokenCount: Int = 0
) {
    enum class Role {
        USER, ASSISTANT, SYSTEM
    }
}

/**
 * A date-based conversation segment.
 * Each day gets its own segment. Users scroll up to see history.
 * No manual "new conversation" action needed.
 */
data class ConversationSegment(
    val date: LocalDate,
    val messages: MutableList<Message> = mutableListOf(),
    var summary: String? = null   // Set when this segment has been compressed
) {
    /**
     * Total estimated token count for all messages in this segment.
     */
    fun totalTokens(): Int {
        return messages.sumOf { it.tokenCount }
    }

    /**
     * Add a message to this segment.
     */
    fun addMessage(message: Message) {
        messages.add(message)
    }

    /**
     * Get the date of this segment as a string (YYYY-MM-DD).
     */
    fun dateString(): String = date.toString()

    companion object {
        /**
         * Determine the segment date for a given timestamp.
         */
        fun dateForTimestamp(timestampMs: Long): LocalDate {
            return Instant.ofEpochMilli(timestampMs)
                .atZone(ZoneId.systemDefault())
                .toLocalDate()
        }
    }
}
```

- [x] **Step 2: Create MessageStore.kt**

Create `app/src/main/java/org/dollos/ai/conversation/MessageStore.kt`:

```kotlin
package org.dollos.ai.conversation

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.util.Log
import java.time.LocalDate
import java.time.format.DateTimeFormatter

/**
 * Persists conversation messages to SQLite.
 *
 * Schema:
 *   messages (id TEXT PK, date TEXT, role TEXT, content TEXT, timestamp INTEGER, token_count INTEGER)
 *   segment_summaries (date TEXT PK, summary TEXT)
 */
class MessageStore(context: Context) : SQLiteOpenHelper(context, DB_NAME, null, DB_VERSION) {

    companion object {
        private const val TAG = "MessageStore"
        private const val DB_NAME = "conversations.db"
        private const val DB_VERSION = 1
        private val DATE_FORMAT = DateTimeFormatter.ofPattern("yyyy-MM-dd")
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                date TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                token_count INTEGER NOT NULL DEFAULT 0
            )
        """.trimIndent())

        db.execSQL("""
            CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date)
        """.trimIndent())

        db.execSQL("""
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp)
        """.trimIndent())

        db.execSQL("""
            CREATE TABLE IF NOT EXISTS segment_summaries (
                date TEXT PRIMARY KEY,
                summary TEXT NOT NULL
            )
        """.trimIndent())

        Log.i(TAG, "Conversations database created")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        db.execSQL("DROP TABLE IF EXISTS messages")
        db.execSQL("DROP TABLE IF EXISTS segment_summaries")
        onCreate(db)
    }

    /**
     * Append a single message.
     */
    fun appendMessage(message: Message) {
        val date = ConversationSegment.dateForTimestamp(message.timestamp)
        val db = writableDatabase
        val values = ContentValues().apply {
            put("id", message.id)
            put("date", date.toString())
            put("role", message.role.name)
            put("content", message.content)
            put("timestamp", message.timestamp)
            put("token_count", message.tokenCount)
        }
        db.insertWithOnConflict("messages", null, values, SQLiteDatabase.CONFLICT_REPLACE)
        Log.d(TAG, "Appended message ${message.id} (${message.role})")
    }

    /**
     * Load a conversation segment by date.
     * Returns null if no messages exist for that date.
     */
    fun loadSegment(date: LocalDate): ConversationSegment? {
        val db = readableDatabase
        val cursor = db.rawQuery(
            "SELECT id, role, content, timestamp, token_count FROM messages WHERE date = ? ORDER BY timestamp ASC",
            arrayOf(date.toString())
        )

        val messages = mutableListOf<Message>()
        cursor.use { c ->
            while (c.moveToNext()) {
                messages.add(Message(
                    id = c.getString(0),
                    role = Message.Role.valueOf(c.getString(1)),
                    content = c.getString(2),
                    timestamp = c.getLong(3),
                    tokenCount = c.getInt(4)
                ))
            }
        }

        if (messages.isEmpty()) return null

        val summary = loadSummary(date)
        return ConversationSegment(date, messages, summary)
    }

    /**
     * Load today's segment, creating a new one if it doesn't exist.
     */
    fun loadOrCreateToday(): ConversationSegment {
        val today = LocalDate.now()
        return loadSegment(today) ?: ConversationSegment(today)
    }

    /**
     * Save a compression summary for a date segment.
     */
    fun saveSummary(date: LocalDate, summary: String) {
        val db = writableDatabase
        val values = ContentValues().apply {
            put("date", date.toString())
            put("summary", summary)
        }
        db.insertWithOnConflict("segment_summaries", null, values, SQLiteDatabase.CONFLICT_REPLACE)
        Log.d(TAG, "Saved summary for $date")
    }

    /**
     * Load a compression summary for a date segment.
     */
    fun loadSummary(date: LocalDate): String? {
        val db = readableDatabase
        val cursor = db.rawQuery(
            "SELECT summary FROM segment_summaries WHERE date = ?",
            arrayOf(date.toString())
        )
        return cursor.use { c ->
            if (c.moveToFirst()) c.getString(0) else null
        }
    }

    /**
     * List all available segment dates, sorted descending (most recent first).
     */
    fun listSegmentDates(): List<LocalDate> {
        val db = readableDatabase
        val cursor = db.rawQuery(
            "SELECT DISTINCT date FROM messages ORDER BY date DESC",
            null
        )

        val dates = mutableListOf<LocalDate>()
        cursor.use { c ->
            while (c.moveToNext()) {
                try {
                    dates.add(LocalDate.parse(c.getString(0)))
                } catch (e: Exception) {
                    Log.w(TAG, "Invalid date in database: ${c.getString(0)}")
                }
            }
        }

        return dates
    }

    /**
     * Load multiple segments for conversation history display.
     * Returns segments in chronological order (oldest first).
     *
     * @param days Number of days to load from today.
     */
    fun loadRecentSegments(days: Int): List<ConversationSegment> {
        val today = LocalDate.now()
        val segments = mutableListOf<ConversationSegment>()
        for (i in days - 1 downTo 0) {
            val date = today.minusDays(i.toLong())
            loadSegment(date)?.let { segments.add(it) }
        }
        return segments
    }

    /**
     * Delete all messages for a date segment.
     */
    fun deleteSegment(date: LocalDate): Boolean {
        val db = writableDatabase
        val deleted = db.delete("messages", "date = ?", arrayOf(date.toString()))
        db.delete("segment_summaries", "date = ?", arrayOf(date.toString()))
        return deleted > 0
    }

    /**
     * Get the total number of messages stored.
     */
    fun totalMessageCount(): Int {
        val db = readableDatabase
        val cursor = db.rawQuery("SELECT COUNT(*) FROM messages", null)
        return cursor.use { c ->
            if (c.moveToFirst()) c.getInt(0) else 0
        }
    }
}
```

- [x] **Step 3: Commit**

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService
git add app/src/main/java/org/dollos/ai/conversation/ConversationSegment.kt
git add app/src/main/java/org/dollos/ai/conversation/MessageStore.kt
git commit -m "feat: add date-segmented conversation storage with SQLite persistence"
```

---

## Task 8: Context Compressor

**Goal:** Implement proactive context compression that runs asynchronously using the foreground model when context reaches 70-80% capacity.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/conversation/ContextCompressor.kt`

- [x] **Step 1: Create ContextCompressor.kt**

Create `app/src/main/java/org/dollos/ai/conversation/ContextCompressor.kt`:

```kotlin
package org.dollos.ai.conversation

import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/**
 * Proactive context compression.
 *
 * When context usage reaches the threshold (70-80%), this compressor:
 *   1. Snapshots current conversation for background compression
 *   2. Immediately truncates old messages in foreground (keeps recent N)
 *   3. Runs compression in parallel using foreground model (async, non-blocking)
 *   4. When compression completes, merges: summary + new messages accumulated during compression
 *
 * Compression produces:
 *   - A summary of the compressed conversation
 *   - Extracted facts that get written to memory (via MemoryManager)
 */
class ContextCompressor(
    private val contextWindowSize: Int = 128_000,       // model context window in tokens
    private val compressionThreshold: Float = 0.75f,    // trigger at 75% of context window
    private val keepRecentMessages: Int = 10            // messages to keep after truncation
) {
    companion object {
        private const val TAG = "ContextCompressor"
    }

    /**
     * Callback to send compression request to the LLM.
     * Input: list of messages to compress.
     * Output: CompressionResult (summary + extracted facts).
     */
    var compressWithLLM: (suspend (List<Message>) -> CompressionResult)? = null

    /**
     * Callback invoked when compression completes.
     * Called with the summary and extracted facts so the caller can:
     *   - Prepend the summary to the conversation context
     *   - Write extracted facts to memory
     */
    var onCompressionComplete: ((CompressionResult) -> Unit)? = null

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var compressionJob: Job? = null
    private val mutex = Mutex()
    private var isCompressing = false

    /**
     * Check if context usage has reached the compression threshold.
     *
     * @param currentTokenCount Current total token count in the conversation context.
     * @return True if compression should be triggered.
     */
    fun shouldCompress(currentTokenCount: Int): Boolean {
        if (isCompressing) return false
        return currentTokenCount >= (contextWindowSize * compressionThreshold).toInt()
    }

    /**
     * Trigger compression on the given messages.
     *
     * Returns the messages to keep in the foreground (recent messages only).
     * Compression runs in the background asynchronously.
     *
     * @param allMessages All current conversation messages.
     * @return TruncationResult containing the messages to keep immediately.
     */
    fun triggerCompression(allMessages: List<Message>): TruncationResult {
        if (isCompressing) {
            Log.w(TAG, "Compression already in progress, skipping")
            return TruncationResult(allMessages, emptyList())
        }

        if (allMessages.size <= keepRecentMessages) {
            Log.d(TAG, "Not enough messages to compress (${allMessages.size})")
            return TruncationResult(allMessages, emptyList())
        }

        // Split: old messages to compress, recent messages to keep
        val splitPoint = allMessages.size - keepRecentMessages
        val messagesToCompress = allMessages.subList(0, splitPoint).toList()
        val messagesToKeep = allMessages.subList(splitPoint, allMessages.size).toList()

        Log.i(TAG, "Triggering compression: ${messagesToCompress.size} messages to compress, " +
            "${messagesToKeep.size} messages kept")

        // Start async compression
        isCompressing = true
        compressionJob = scope.launch {
            try {
                val result = compressAsync(messagesToCompress)
                mutex.withLock {
                    isCompressing = false
                }
                onCompressionComplete?.invoke(result)
                Log.i(TAG, "Compression complete: summary ${result.summary.length} chars, " +
                    "${result.extractedFacts.size} facts extracted")
            } catch (e: Exception) {
                mutex.withLock {
                    isCompressing = false
                }
                Log.e(TAG, "Compression failed", e)
            }
        }

        return TruncationResult(messagesToKeep, messagesToCompress)
    }

    /**
     * Run compression using the foreground model.
     * This runs asynchronously and does not block the conversation.
     */
    private suspend fun compressAsync(messages: List<Message>): CompressionResult {
        val llmCompress = compressWithLLM
            ?: throw IllegalStateException("compressWithLLM callback not set")

        return llmCompress(messages)
    }

    /**
     * Cancel any in-progress compression.
     */
    fun cancelCompression() {
        compressionJob?.cancel()
        isCompressing = false
        Log.i(TAG, "Compression cancelled")
    }

    /**
     * Whether compression is currently in progress.
     */
    fun isCompressionInProgress(): Boolean = isCompressing

    /**
     * Build the system prompt for the compression LLM call.
     * This is the prompt sent to the foreground model to compress conversation history.
     */
    fun buildCompressionPrompt(messages: List<Message>): String {
        val conversationText = messages.joinToString("\n") { msg ->
            "${msg.role.name}: ${msg.content}"
        }

        return """You are a conversation summarizer. Your task is to:

1. Create a concise summary of the following conversation that preserves all important context, decisions, and ongoing topics. The summary should allow the conversation to continue naturally.

2. Extract any important facts, preferences, or decisions that should be remembered long-term. Output these as a JSON array of strings under the key "facts".

Respond in this exact JSON format:
{
  "summary": "your summary here",
  "facts": ["fact 1", "fact 2", ...]
}

--- CONVERSATION TO COMPRESS ---

$conversationText

--- END ---"""
    }
}

/**
 * Result of immediate truncation (before async compression completes).
 */
data class TruncationResult(
    /** Messages to keep in the foreground context. */
    val keptMessages: List<Message>,
    /** Messages sent for async compression. */
    val compressedMessages: List<Message>
)

/**
 * Result of async compression by the LLM.
 */
data class CompressionResult(
    /** Summary of the compressed conversation. */
    val summary: String,
    /** Facts extracted from the conversation for memory storage. */
    val extractedFacts: List<String>
)
```

- [x] **Step 2: Commit**

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService
git add app/src/main/java/org/dollos/ai/conversation/ContextCompressor.kt
git commit -m "feat: add async context compressor with foreground model compression"
```

---

## Task 9: Conversation Manager

**Goal:** Implement the top-level conversation manager that ties together message storage, context window management, compression, and memory integration.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/conversation/ConversationManager.kt`

- [x] **Step 1: Create ConversationManager.kt**

Create `app/src/main/java/org/dollos/ai/conversation/ConversationManager.kt`:

```kotlin
package org.dollos.ai.conversation

import android.content.Context
import android.util.Log
import org.dollos.ai.memory.MemoryManager
import java.time.Instant
import java.time.LocalDate

/**
 * Top-level conversation manager.
 *
 * Manages conversation state, history, context window, and coordinates
 * with MemoryManager for memory writes and context enrichment.
 *
 * Conversations are date-segmented. Each day is a separate segment.
 * Context compression triggers automatically at 70-80% capacity.
 */
class ConversationManager(
    private val context: Context,
    private val memoryManager: MemoryManager,
    contextWindowSize: Int = 128_000
) {
    companion object {
        private const val TAG = "ConversationManager"
        private const val ROUGH_CHARS_PER_TOKEN = 4
    }

    val messageStore = MessageStore(context)
    val compressor = ContextCompressor(contextWindowSize = contextWindowSize)

    /** Current active messages in the context window. */
    private val activeMessages = mutableListOf<Message>()

    /** Summary from last compression (prepended to context). */
    private var compressionSummary: String? = null

    /** Running token count estimate for the active context. */
    private var estimatedTokenCount: Int = 0

    /**
     * Initialize the conversation manager. Call on service startup.
     */
    fun init() {
        // Load today's segment
        val today = messageStore.loadOrCreateToday()
        activeMessages.clear()
        activeMessages.addAll(today.messages)
        compressionSummary = today.summary
        recalculateTokenCount()

        // Set up compression callbacks
        compressor.onCompressionComplete = { result ->
            onCompressionComplete(result)
        }

        Log.i(TAG, "Initialized with ${activeMessages.size} messages, " +
            "~$estimatedTokenCount tokens estimated")
    }

    /**
     * Add a user message to the conversation.
     * Persists to disk and checks for compression trigger.
     *
     * @return The created Message object.
     */
    fun addUserMessage(content: String): Message {
        val message = Message(
            id = generateMessageId(),
            role = Message.Role.USER,
            content = content,
            tokenCount = estimateTokens(content)
        )

        activeMessages.add(message)
        messageStore.appendMessage(message)
        estimatedTokenCount += message.tokenCount

        // Reset idle timer on user activity
        memoryManager.resetIdleTimer()

        // Check if compression is needed
        checkCompressionThreshold()

        return message
    }

    /**
     * Add an assistant response to the conversation.
     * Persists to disk and checks for compression trigger.
     *
     * @return The created Message object.
     */
    fun addAssistantMessage(content: String, tokenCount: Int = 0): Message {
        val actualTokenCount = if (tokenCount > 0) tokenCount else estimateTokens(content)
        val message = Message(
            id = generateMessageId(),
            role = Message.Role.ASSISTANT,
            content = content,
            tokenCount = actualTokenCount
        )

        activeMessages.add(message)
        messageStore.appendMessage(message)
        estimatedTokenCount += message.tokenCount

        // Reset idle timer on assistant activity
        memoryManager.resetIdleTimer()

        // Check if compression is needed
        checkCompressionThreshold()

        return message
    }

    /**
     * Build the full context to send to the LLM.
     * Includes: compression summary (if any) + active messages.
     *
     * Note: Memory context (Tier 1, 2, 3) is injected separately by the
     * system prompt builder, not here. This only handles conversation messages.
     */
    fun buildConversationContext(): List<Message> {
        val context = mutableListOf<Message>()

        // Prepend compression summary as a system message if available
        if (!compressionSummary.isNullOrBlank()) {
            context.add(Message(
                id = "compression-summary",
                role = Message.Role.SYSTEM,
                content = "Previous conversation summary:\n${compressionSummary}",
                tokenCount = estimateTokens(compressionSummary!!)
            ))
        }

        context.addAll(activeMessages)
        return context
    }

    /**
     * Get the estimated current token usage.
     */
    fun getEstimatedTokenCount(): Int = estimatedTokenCount

    /**
     * Get today's conversation segment.
     */
    fun getTodaySegment(): ConversationSegment {
        return ConversationSegment(
            date = LocalDate.now(),
            messages = activeMessages.toMutableList(),
            summary = compressionSummary
        )
    }

    /**
     * Load historical conversation segments for display.
     *
     * @param days Number of past days to load.
     * @return List of segments in chronological order.
     */
    fun loadHistory(days: Int): List<ConversationSegment> {
        return messageStore.loadRecentSegments(days)
    }

    /**
     * Clear today's conversation. For testing/debugging.
     */
    fun clearToday() {
        activeMessages.clear()
        compressionSummary = null
        estimatedTokenCount = 0
        messageStore.deleteSegment(LocalDate.now())
        Log.i(TAG, "Cleared today's conversation")
    }

    // -- Internal --

    private fun checkCompressionThreshold() {
        if (compressor.shouldCompress(estimatedTokenCount)) {
            Log.i(TAG, "Context threshold reached ($estimatedTokenCount tokens), " +
                "triggering compression")

            val result = compressor.triggerCompression(activeMessages.toList())

            // Immediately update foreground context with truncated messages
            activeMessages.clear()
            activeMessages.addAll(result.keptMessages)
            recalculateTokenCount()

            Log.i(TAG, "Truncated to ${activeMessages.size} messages, " +
                "~$estimatedTokenCount tokens. Compression running async.")
        }
    }

    private fun onCompressionComplete(result: CompressionResult) {
        // Merge: new summary + messages accumulated since truncation
        val previousSummary = compressionSummary
        compressionSummary = if (previousSummary.isNullOrBlank()) {
            result.summary
        } else {
            "$previousSummary\n\n---\n\n${result.summary}"
        }

        // Save the summary to the database
        messageStore.saveSummary(LocalDate.now(), compressionSummary!!)

        // Write extracted facts to memory
        if (result.extractedFacts.isNotEmpty()) {
            memoryManager.onContextThresholdReached(result.extractedFacts)
        }

        recalculateTokenCount()
        Log.i(TAG, "Compression merged. Summary: ${result.summary.length} chars, " +
            "facts: ${result.extractedFacts.size}")
    }

    private fun recalculateTokenCount() {
        var total = 0
        if (!compressionSummary.isNullOrBlank()) {
            total += estimateTokens(compressionSummary!!)
        }
        total += activeMessages.sumOf { it.tokenCount }
        estimatedTokenCount = total
    }

    private fun estimateTokens(text: String): Int {
        // Rough estimate: ~4 characters per token for English text
        return (text.length / ROUGH_CHARS_PER_TOKEN).coerceAtLeast(1)
    }

    private fun generateMessageId(): String {
        return "${Instant.now().toEpochMilli()}-${(Math.random() * 10000).toInt()}"
    }
}
```

- [x] **Step 2: Commit**

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService
git add app/src/main/java/org/dollos/ai/conversation/ConversationManager.kt
git commit -m "feat: add ConversationManager with context window management and compression integration"
```

---

## Task 10: Memory Export/Import

**Goal:** Implement memory export and import via ParcelFileDescriptor for Android scoped storage compliance.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/memory/MemoryExporter.kt`

- [x] **Step 1: Create MemoryExporter.kt**

Create `app/src/main/java/org/dollos/ai/memory/MemoryExporter.kt`:

```kotlin
package org.dollos.ai.memory

import android.os.ParcelFileDescriptor
import android.util.Log
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipInputStream
import java.util.zip.ZipOutputStream

/**
 * Exports and imports memory as a ZIP archive via ParcelFileDescriptor.
 *
 * Uses ParcelFileDescriptor instead of file paths to comply with
 * Android scoped storage. The caller (Settings UI or AIDL client)
 * opens a document via SAF and passes the PFD to these methods.
 *
 * Export format: ZIP containing all Markdown memory files with
 * their relative paths preserved.
 *
 * Import: extracts ZIP into the memory root directory, overwriting
 * existing files. Triggers a full index rebuild after import.
 */
class MemoryExporter(
    private val rootDir: File,
    private val store: MarkdownStore,
    private val memoryManager: MemoryManager
) {
    companion object {
        private const val TAG = "MemoryExporter"
        private const val BUFFER_SIZE = 8192
    }

    /**
     * Export all memory files to a ZIP archive via ParcelFileDescriptor.
     *
     * @param fd ParcelFileDescriptor opened for writing (from SAF / content resolver).
     */
    fun exportMemory(fd: ParcelFileDescriptor) {
        val outputStream = FileOutputStream(fd.fileDescriptor)
        val zipOut = ZipOutputStream(outputStream)

        try {
            val files = store.listAllMemoryFiles()
            Log.i(TAG, "Exporting ${files.size} memory files")

            for ((relativePath, file) in files) {
                val entry = ZipEntry(relativePath)
                zipOut.putNextEntry(entry)

                FileInputStream(file).use { fis ->
                    val buffer = ByteArray(BUFFER_SIZE)
                    var bytesRead: Int
                    while (fis.read(buffer).also { bytesRead = it } != -1) {
                        zipOut.write(buffer, 0, bytesRead)
                    }
                }

                zipOut.closeEntry()
                Log.d(TAG, "Exported: $relativePath")
            }

            Log.i(TAG, "Export complete: ${files.size} files")
        } catch (e: Exception) {
            Log.e(TAG, "Export failed", e)
            throw e
        } finally {
            zipOut.close()
            fd.close()
        }
    }

    /**
     * Import memory files from a ZIP archive via ParcelFileDescriptor.
     * Overwrites existing files. Triggers index rebuild after import.
     *
     * @param fd ParcelFileDescriptor opened for reading (from SAF / content resolver).
     */
    suspend fun importMemory(fd: ParcelFileDescriptor) {
        val inputStream = FileInputStream(fd.fileDescriptor)
        val zipIn = ZipInputStream(inputStream)

        try {
            var entry = zipIn.nextEntry
            var fileCount = 0

            while (entry != null) {
                if (entry.isDirectory) {
                    val dir = File(rootDir, entry.name)
                    dir.mkdirs()
                } else {
                    // Validate path to prevent zip slip attack
                    val targetFile = File(rootDir, entry.name)
                    val canonicalRoot = rootDir.canonicalPath
                    val canonicalTarget = targetFile.canonicalPath
                    if (!canonicalTarget.startsWith(canonicalRoot)) {
                        Log.e(TAG, "Zip slip detected, skipping: ${entry.name}")
                        zipIn.closeEntry()
                        entry = zipIn.nextEntry
                        continue
                    }

                    // Ensure parent directory exists
                    targetFile.parentFile?.mkdirs()

                    FileOutputStream(targetFile).use { fos ->
                        val buffer = ByteArray(BUFFER_SIZE)
                        var bytesRead: Int
                        while (zipIn.read(buffer).also { bytesRead = it } != -1) {
                            fos.write(buffer, 0, bytesRead)
                        }
                    }

                    fileCount++
                    Log.d(TAG, "Imported: ${entry.name}")
                }

                zipIn.closeEntry()
                entry = zipIn.nextEntry
            }

            Log.i(TAG, "Import complete: $fileCount files")

            // Rebuild SQLite index from imported Markdown files
            Log.i(TAG, "Rebuilding index after import...")
            memoryManager.rebuildIndex()
        } catch (e: Exception) {
            Log.e(TAG, "Import failed", e)
            throw e
        } finally {
            zipIn.close()
            fd.close()
        }
    }
}
```

- [x] **Step 2: Commit**

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService
git add app/src/main/java/org/dollos/ai/memory/MemoryExporter.kt
git commit -m "feat: add memory export/import via ParcelFileDescriptor with zip slip protection"
```

---

## Task 11: Gradle Dependencies and Build Integration

**Goal:** Add sqlite-vec native library and OkHttp dependency to the Gradle project. Verify everything compiles.

**Files:**
- Download: `app/src/main/jniLibs/arm64-v8a/vec0.so`
- Modify: `app/build.gradle.kts`

- [x] **Step 1: Download sqlite-vec native library** (jniLibs directory created; vec0.so not yet available)

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService

mkdir -p app/src/main/jniLibs/arm64-v8a

# Download from: https://github.com/asg017/sqlite-vec/releases
# Pick the latest stable release
# Download: sqlite-vec-<version>-android-aarch64.tar.gz
# Extract and copy vec0.so:
# tar xzf sqlite-vec-<version>-android-aarch64.tar.gz
# cp vec0.so app/src/main/jniLibs/arm64-v8a/vec0.so

ls -la app/src/main/jniLibs/arm64-v8a/vec0.so
```

- [x] **Step 2: Update app/build.gradle.kts** (OkHttp already present from Plan A)

Add OkHttp dependency to the existing dependencies block in `app/build.gradle.kts`:

```kotlin
dependencies {
    // ... existing dependencies from Plan A ...

    // OkHttp for cloud embedding API calls (CloudEmbeddingProvider)
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
```

No special Gradle plugin or annotation processor is needed. sqlite-vec is a native .so loaded at runtime, not a Gradle dependency. FTS5 is built into Android's SQLite.

- [x] **Step 3: Verify build** (Plan B code compiles; pre-existing AIDL stub errors from Plan A are unrelated)

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService
./gradlew assembleDebug
```

If the build fails, check:
1. OkHttp dependency resolves (check internet connectivity and Maven Central access)
2. Kotlin files compile without errors (check import paths match the file structure)
3. vec0.so is in the correct jniLibs path

- [x] **Step 4: Commit** (no new changes needed; jniLibs/.gitkeep and OkHttp already committed)

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService
git add app/build.gradle.kts
git add app/src/main/jniLibs/arm64-v8a/vec0.so
git commit -m "feat: add sqlite-vec native library and OkHttp dependency for memory system"
```

---

## Task 12: Build, Flash, and Verify

**Goal:** Build the full APK, install/flash it, and verify the memory system and conversation engine function correctly on device.

- [ ] **Step 1: Build APK**

```bash
cd ~/Desktop/DollOS-build/packages/apps/DollOSAIService
./gradlew assembleDebug
```

- [ ] **Step 2: Install or flash**

For standalone testing:

```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

For full system build (if integrating into AOSP image):

```bash
cd ~/Desktop/DollOS-build
source build/envsetup.sh
lunch dollos_bluejay-cur-userdebug
m -j$(nproc)

adb reboot bootloader
cd out/target/product/bluejay
fastboot flashall -w
```

- [ ] **Step 3: Verify memory file creation**

```bash
# After boot + OOBE + initial conversation
adb shell run-as org.dollos.ai ls -la files/memory/
# Expected: MEMORY.md exists
# Expected: memory/ directory exists with people/, topics/, decisions/ subdirs

adb shell run-as org.dollos.ai cat files/memory/MEMORY.md
# Expected: initial core memory content
```

- [ ] **Step 4: Verify SQLite database creation**

```bash
adb shell run-as org.dollos.ai ls -la databases/
# Expected: memory_index.db, conversations.db

# Check vec0 extension loading
adb shell logcat -d | grep "MemoryDatabase"
# Expected: "sqlite-vec extension loaded successfully" or "sqlite-vec extension not available"
```

- [ ] **Step 5: Verify conversation persistence**

```bash
# After sending a test message via the AI service
adb shell logcat -d | grep "MessageStore"
# Expected: "Appended message" logs

adb shell logcat -d | grep "ConversationManager"
# Expected: "Initialized with N messages" log
```

- [ ] **Step 6: Verify memory write queue**

```bash
adb shell logcat -d | grep "MemoryWriteQueue\|MarkdownStore\|MemoryManager"
# Expected: "Write queue started", write operations logged
```

- [ ] **Step 7: Verify search (FTS5 at minimum)**

```bash
adb shell logcat -d | grep "MemorySearchEngine\|MemoryDatabase"
# If vec0 loaded: "sqlite-vec extension loaded successfully"
# Search calls should show hybrid or FTS5-only results
# If vec0 not available: "Skipping vector search" or "Substring search returned N results"
```

- [ ] **Step 8: Verify memory export via AIDL**

```bash
adb shell logcat -d | grep "MemoryExporter"
```

- [ ] **Step 9: Commit verification results**

Create `docs/verification-plan-b.md` with test results and any issues encountered.

```bash
cd ~/Desktop/DollOS
git add docs/verification-plan-b.md
git commit -m "docs: add Plan B verification results"
```

---

## Notes

### Integration with Plan A

Plan B depends on Plan A (DollOSAIService Gradle project). When integrating:
1. `DollOSAIApp.onCreate()` should create and init `MemoryManager` and `ConversationManager`
2. `IDollOSAIService.sendMessage()` should use `ConversationManager.addUserMessage()` and pass memory context from `MemoryManager.buildMemoryContext()`
3. `IDollOSAIService.searchMemory()` should delegate to `MemorySearchEngine.search()`
4. `IDollOSAIService.exportMemory()` / `importMemory()` should delegate to `MemoryExporter`
5. `IDollOSAIService.confirmMemoryWrite()` should call `MemoryManager.confirmRememberThis()`
6. `IDollOSAIService.setEmbeddingSource()` should call `MemoryManager.setEmbeddingSource()`
7. LLM response handler should use `ConversationManager.addAssistantMessage()`
8. System prompt builder should include `MemoryManager.buildMemoryContext()` and `MemoryManager.searchRelevantMemory(query)`

### Integration with Plan C

After Plan C is complete:
- Background memory operations (idle extraction, index rebuild) will appear as tasks in the AI Task Manager
- `pauseAll()` should pause the memory write queue and any in-progress compression

### sqlite-vec Considerations

- sqlite-vec is a loadable SQLite extension distributed as a native .so file
- It provides the `vec0` virtual table type for approximate nearest neighbor search
- The .so must be in `jniLibs/arm64-v8a/` so Gradle bundles it into the APK's `lib/` directory
- At runtime, call `SQLiteDatabase.loadExtension("vec0")` to activate it
- If the extension fails to load (e.g., wrong architecture, missing file), the system degrades gracefully to FTS5-only search
- No annotation processing, no code generation, no Gradle plugin needed
- The vec0 table dimension is fixed at creation time (384 for our embedding size)

### Embedding Dimension

All embedding providers use 384 dimensions:
- CloudEmbeddingProvider: OpenAI text-embedding-3-small with `"dimensions": 384` parameter (dimension shortening)
- LocalEmbeddingProvider: placeholder returning 384-dimension zero vectors (matching future all-MiniLM-L6-v2)

This ensures the sqlite-vec table can be shared between providers without rebuilding.

### Token Estimation

The `ContextCompressor` and `ConversationManager` use a rough ~4 chars/token estimate. When integrated with Plan A's LLM client, actual token counts from API responses should be used instead via `Message.tokenCount`.

### Future Improvements (Out of Scope)

- Actual ONNX Runtime inference for local embeddings (LocalEmbeddingProvider currently returns zero vectors)
- Smart chunking strategies (semantic boundaries, overlap)
- Memory deduplication and conflict resolution
- Memory aging and archival
- Cross-device memory sync
