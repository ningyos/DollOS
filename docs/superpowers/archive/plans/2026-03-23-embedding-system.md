# Embedding System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the embedding system to support configurable cloud endpoints and on-device ONNX inference, with dynamic dimensions, per-model vector storage, and a restructured Settings UI.

**Architecture:** Cloud embedding uses any OpenAI-compatible `/v1/embeddings` endpoint (configurable URL/key/model). Local embedding uses ONNX Runtime with WordPiece tokenizer. Each model's embeddings are stored independently (modelId field on MemoryChunk) so switching is instant. Vector search uses brute-force cosine similarity (no HNSW). Settings UI is restructured into sub-pages with a new Memory Settings page.

**Tech Stack:** Kotlin, ONNX Runtime Android, OkHttp, ObjectBox, Room FTS4, Android Settings (DashboardFragment), AIDL

---

## File Structure

### DollOSAIService — embedding backend

```
app/src/main/java/org/dollos/ai/memory/
  EmbeddingProvider.kt              -- unchanged (interface)
  CloudEmbeddingProvider.kt         -- modify: configurable baseUrl, model, dimensions
  LocalEmbeddingProvider.kt         -- rewrite: ONNX Runtime inference
  WordPieceTokenizer.kt             -- new: tokenizer for ONNX models
  MemoryChunk.kt                    -- modify: remove @HnswIndex, add modelId
  MemorySearchEngine.kt             -- modify: filter by modelId, retrieval mode, remove HNSW code
  MemoryManager.kt                  -- modify: modelId tracking, rebuild with notification, retrieval mode

app/src/main/java/org/dollos/ai/
  DollOSAIServiceImpl.kt            -- modify: new AIDL methods

aidl/org/dollos/ai/
  IDollOSAIService.aidl             -- modify: add embedding/retrieval AIDL methods

app/build.gradle.kts                -- modify: add onnxruntime-android
```

### Settings app — UI restructure

```
packages/apps/Settings/
  src/com/android/settings/dollos/
    DollOSAISettingsFragment.java       -- modify: keep stats + personality, add sub-page links
    DollOSLLMSettingsFragment.java      -- new: LLM config sub-page
    DollOSMemorySettingsFragment.java   -- new: memory/embedding/search sub-page
    DollOSBudgetSettingsFragment.java   -- new: budget sub-page
  res/xml/
    dollos_ai_settings.xml              -- modify: slim down
    dollos_llm_settings.xml             -- new
    dollos_memory_settings.xml          -- new
    dollos_budget_settings.xml          -- new
```

---

## Task 1: CloudEmbeddingProvider — configurable endpoint

**Goal:** Make baseUrl, model, dimensions configurable instead of hardcoded.

**Files:**
- Modify: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/memory/CloudEmbeddingProvider.kt`

- [ ] **Step 1: Update constructor and fields**

Change from hardcoded constants to constructor parameters:

```kotlin
class CloudEmbeddingProvider(
    private var baseUrl: String = "https://api.openai.com/v1/embeddings",
    private var model: String = "text-embedding-3-small",
    private var dims: Int = 384
) : EmbeddingProvider {

    companion object {
        private const val TAG = "CloudEmbeddingProvider"
        private const val MAX_BATCH = 64
    }

    override val dimension: Int get() = dims
    override val name: String get() = "cloud:$model"
    override val requiresNetwork = true

    @Volatile
    private var apiKey: String? = null

    private val client = OkHttpClient.Builder()
        .connectTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
        .readTimeout(60, java.util.concurrent.TimeUnit.SECONDS)
        .build()

    fun configure(baseUrl: String, apiKey: String, model: String, dimensions: Int) {
        this.baseUrl = baseUrl
        this.apiKey = apiKey
        this.model = model
        this.dims = dimensions
    }

    fun setApiKey(key: String) { apiKey = key }
```

Update `requestEmbeddings()` to use `this.baseUrl`, `this.model`, `this.dims` instead of constants.

- [ ] **Step 2: Commit**

```bash
cd ~/Projects/DollOSAIService
git add app/src/main/java/org/dollos/ai/memory/CloudEmbeddingProvider.kt
git commit -m "feat: make CloudEmbeddingProvider configurable (baseUrl, model, dimensions)"
```

---

## Task 2: MemoryChunk — remove HNSW, add modelId

**Goal:** Remove the `@HnswIndex` annotation for dynamic dimensions. Add `modelId` field for per-model storage.

**Files:**
- Modify: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/memory/MemoryChunk.kt`

- [ ] **Step 1: Update MemoryChunk**

```kotlin
package org.dollos.ai.memory

import io.objectbox.annotation.Entity
import io.objectbox.annotation.Id
import io.objectbox.annotation.Index

@Entity
data class MemoryChunk(
    @Id var id: Long = 0,
    @Index var filePath: String = "",
    var chunkText: String = "",
    @Index var contentHash: String = "",
    var heading: String = "",
    var startLine: Int = 0,
    var endLine: Int = 0,
    var fileModifiedAt: Long = 0,
    @Index var modelId: String = "",   // e.g. "cloud:text-embedding-3-small" or "local:all-MiniLM-L6-v2"
    var embedding: FloatArray = FloatArray(0)
)
```

Changes: removed `@HnswIndex(dimensions = 384)`, added `@Index var modelId`.

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/org/dollos/ai/memory/MemoryChunk.kt
git commit -m "feat: remove HNSW annotation, add modelId for per-model vector storage"
```

---

## Task 3: MemorySearchEngine — brute-force only, filter by modelId, retrieval mode

**Goal:** Remove HNSW references, filter vector search by modelId, add retrieval mode support.

**Files:**
- Modify: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/memory/MemorySearchEngine.kt`

- [ ] **Step 1: Add retrieval mode and modelId filtering**

Key changes to the class:

```kotlin
class MemorySearchEngine(
    private val chunkBox: Box<MemoryChunk>,
    private val ftsDao: MemoryFtsDao,
    private val embeddingProvider: EmbeddingProvider
) {
    companion object {
        private const val TAG = "MemorySearchEngine"
        private const val RRF_K = 60
        private const val DEFAULT_VECTOR_WEIGHT = 0.7f
        private const val DEFAULT_BM25_WEIGHT = 0.3f
    }

    var vectorWeight = DEFAULT_VECTOR_WEIGHT
    var bm25Weight = DEFAULT_BM25_WEIGHT
    var retrievalMode: String = "hybrid"  // "hybrid" or "fts4"
    var currentModelId: String = ""       // set by MemoryManager

    suspend fun search(query: String, maxResults: Int = 10): List<SearchResult> {
        if (query.length < 2) return emptyList()

        // FTS4 always runs
        val ftsResults = ftsSearch(query, maxResults * 2)

        // Vector search only in hybrid mode and if modelId chunks exist
        val vectorResults = if (retrievalMode == "hybrid" && currentModelId.isNotEmpty()) {
            vectorSearch(query, maxResults * 2)
        } else {
            emptyList()
        }

        if (vectorResults.isEmpty() && ftsResults.isEmpty()) {
            return substringSearch(query, maxResults)
        }

        if (vectorResults.isEmpty()) {
            // FTS4-only results
            return ftsResults.take(maxResults).mapIndexed { i, fr ->
                SearchResult(fr.filePath, fr.chunkText, 1f / (1f + i), 0f, 1f / (1f + i))
            }
        }

        return mergeWithRRF(vectorResults, ftsResults, maxResults)
    }
```

Update `vectorSearch()` to filter by modelId:

```kotlin
    private suspend fun vectorSearch(query: String, limit: Int): List<VectorResult> {
        try {
            val queryEmbedding = embeddingProvider.embed(ChunkUtils.cleanForEmbedding(query))
            if (queryEmbedding.all { it == 0f }) return emptyList()

            val allChunks = chunkBox.all
                .filter { it.modelId == currentModelId && it.embedding.isNotEmpty() && !it.embedding.all { v -> v == 0f } }

            if (allChunks.isEmpty()) return emptyList()

            return allChunks
                .map { chunk ->
                    val dist = cosineDistance(queryEmbedding, chunk.embedding)
                    VectorResult(chunk.id, chunk.filePath, chunk.chunkText, dist)
                }
                .sortedBy { it.distance }
                .take(limit)
        } catch (e: Exception) {
            Log.e(TAG, "Vector search failed", e)
            return emptyList()
        }
    }
```

Remove all HNSW comments/TODOs.

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/org/dollos/ai/memory/MemorySearchEngine.kt
git commit -m "feat: brute-force vector search with modelId filter and retrieval mode"
```

---

## Task 4: MemoryManager — modelId tracking, rebuild with notification

**Goal:** Track current modelId, detect model changes, rebuild vector store with progress notification.

**Files:**
- Modify: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/memory/MemoryManager.kt`

- [ ] **Step 1: Add modelId tracking and retrieval mode**

Add fields:
```kotlin
private var currentModelId: String = ""
private var retrievalMode: String = "hybrid"
private var isRebuilding = AtomicBoolean(false)
```

Update `resolveEmbeddingProvider()` to also set `currentModelId`:
```kotlin
private fun resolveEmbeddingProvider() {
    val source = prefs.getString("embedding_source", "local") ?: "local"
    embeddingProvider = if (source == "cloud") {
        val baseUrl = prefs.getString("embedding_base_url", "https://api.openai.com/v1/embeddings") ?: "https://api.openai.com/v1/embeddings"
        val apiKey = prefs.getString("embedding_api_key", "") ?: ""
        val model = prefs.getString("embedding_model", "text-embedding-3-small") ?: "text-embedding-3-small"
        val dims = prefs.getInt("embedding_dimensions", 384)
        CloudEmbeddingProvider(baseUrl, model, dims).also { it.setApiKey(apiKey) }
    } else {
        LocalEmbeddingProvider(context)
    }
    currentModelId = embeddingProvider.name
    searchEngine?.currentModelId = currentModelId
}
```

- [ ] **Step 2: Add setCloudEmbeddingConfig()**

```kotlin
fun setCloudEmbeddingConfig(baseUrl: String, apiKey: String, model: String, dimensions: Int) {
    prefs.edit()
        .putString("embedding_base_url", baseUrl)
        .putString("embedding_api_key", apiKey)
        .putString("embedding_model", model)
        .putInt("embedding_dimensions", dimensions)
        .apply()
    resolveEmbeddingProvider()
    searchEngine = MemorySearchEngine(chunkBox, ftsDao, embeddingProvider).also {
        it.currentModelId = currentModelId
        it.retrievalMode = retrievalMode
    }
}
```

- [ ] **Step 3: Add setRetrievalMode() / getRetrievalMode()**

```kotlin
fun setRetrievalMode(mode: String) {
    retrievalMode = mode
    prefs.edit().putString("retrieval_mode", mode).apply()
    searchEngine?.retrievalMode = mode
    Log.i(TAG, "Retrieval mode set: $mode")
}

fun getRetrievalMode(): String = retrievalMode
```

- [ ] **Step 4: Add rebuildVectorStore() with progress notification**

```kotlin
fun rebuildVectorStore() {
    if (isRebuilding.getAndSet(true)) {
        Log.w(TAG, "Rebuild already in progress")
        return
    }

    scope.launch {
        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val notifId = 9001

        try {
            // Get all markdown files
            val files = store.listAllFiles()
            val total = files.size
            Log.i(TAG, "Rebuilding vector store for modelId=$currentModelId: $total files")

            // Delete existing chunks for this modelId
            val existing = chunkBox.all.filter { it.modelId == currentModelId }
            chunkBox.remove(existing)

            files.forEachIndexed { index, file ->
                // Update notification
                val notification = NotificationCompat.Builder(context, DollOSAIApp.NOTIFICATION_CHANNEL_ID)
                    .setSmallIcon(android.R.drawable.ic_popup_sync)
                    .setContentTitle("Rebuilding vector store")
                    .setContentText("${index + 1}/$total files")
                    .setProgress(total, index + 1, false)
                    .setOngoing(true)
                    .build()
                nm.notify(notifId, notification)

                indexFile(file)
            }

            // Done
            nm.cancel(notifId)
            val doneNotif = NotificationCompat.Builder(context, DollOSAIApp.NOTIFICATION_CHANNEL_ID)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle("Vector store rebuilt")
                .setContentText("$total files indexed for ${embeddingProvider.name}")
                .setAutoCancel(true)
                .build()
            nm.notify(notifId, doneNotif)

            Log.i(TAG, "Vector store rebuild complete: $total files")
        } catch (e: Exception) {
            Log.e(TAG, "Rebuild failed", e)
            nm.cancel(notifId)
        } finally {
            isRebuilding.set(false)
        }
    }
}
```

- [ ] **Step 5: Update indexFile() to use currentModelId**

In the existing `indexFile()` method, when creating new MemoryChunk objects, set `modelId = currentModelId`.

- [ ] **Step 6: Add needsRebuild() check**

```kotlin
fun needsRebuild(): Boolean {
    val count = chunkBox.all.count { it.modelId == currentModelId }
    return count == 0 && store.listAllFiles().isNotEmpty()
}

fun getVectorStoreInfo(): String {
    val count = chunkBox.all.count { it.modelId == currentModelId }
    val totalFiles = store.listAllFiles().size
    return """{"modelId":"$currentModelId","chunks":$count,"files":$totalFiles,"dimension":${embeddingProvider.dimension},"rebuilding":${isRebuilding.get()}}"""
}
```

- [ ] **Step 7: Commit**

```bash
git add app/src/main/java/org/dollos/ai/memory/MemoryManager.kt
git commit -m "feat: modelId tracking, rebuild with notification, retrieval mode"
```

---

## Task 5: WordPieceTokenizer

**Goal:** Implement a WordPiece tokenizer for ONNX models (no external library dependency).

**Files:**
- Create: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/memory/WordPieceTokenizer.kt`

- [ ] **Step 1: Create WordPieceTokenizer**

```kotlin
package org.dollos.ai.memory

import java.io.InputStream

class WordPieceTokenizer(vocabStream: InputStream) {

    private val vocab: Map<String, Int>
    private val idToToken: Map<Int, String>
    val clsTokenId: Int
    val sepTokenId: Int
    val padTokenId: Int

    companion object {
        private const val UNK_TOKEN = "[UNK]"
        private const val CLS_TOKEN = "[CLS]"
        private const val SEP_TOKEN = "[SEP]"
        private const val PAD_TOKEN = "[PAD]"
        private const val MAX_WORD_LEN = 200
    }

    init {
        val lines = vocabStream.bufferedReader().readLines()
        vocab = lines.withIndex().associate { (i, token) -> token to i }
        idToToken = vocab.entries.associate { it.value to it.key }
        clsTokenId = vocab[CLS_TOKEN] ?: 0
        sepTokenId = vocab[SEP_TOKEN] ?: 0
        padTokenId = vocab[PAD_TOKEN] ?: 0
    }

    fun tokenize(text: String, maxLength: Int = 512): IntArray {
        val tokens = mutableListOf(clsTokenId)

        val words = text.lowercase().split(Regex("\\s+")).filter { it.isNotEmpty() }
        for (word in words) {
            if (tokens.size >= maxLength - 1) break
            tokens.addAll(wordPiece(word))
        }

        tokens.add(sepTokenId)

        // Pad to maxLength
        while (tokens.size < maxLength) {
            tokens.add(padTokenId)
        }

        return tokens.take(maxLength).toIntArray()
    }

    fun createAttentionMask(tokenIds: IntArray): IntArray {
        return IntArray(tokenIds.size) { if (tokenIds[it] != padTokenId) 1 else 0 }
    }

    fun createTokenTypeIds(tokenIds: IntArray): IntArray {
        return IntArray(tokenIds.size) { 0 }
    }

    private fun wordPiece(word: String): List<Int> {
        if (word.length > MAX_WORD_LEN) {
            return listOf(vocab[UNK_TOKEN] ?: 0)
        }

        val tokens = mutableListOf<Int>()
        var start = 0

        while (start < word.length) {
            var end = word.length
            var found = false

            while (start < end) {
                val substr = if (start == 0) {
                    word.substring(start, end)
                } else {
                    "##${word.substring(start, end)}"
                }

                val id = vocab[substr]
                if (id != null) {
                    tokens.add(id)
                    found = true
                    start = end
                    break
                }
                end--
            }

            if (!found) {
                tokens.add(vocab[UNK_TOKEN] ?: 0)
                start++
            }
        }

        return tokens
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/org/dollos/ai/memory/WordPieceTokenizer.kt
git commit -m "feat: add WordPiece tokenizer for ONNX embedding models"
```

---

## Task 6: LocalEmbeddingProvider — ONNX Runtime

**Goal:** Replace placeholder with actual ONNX Runtime inference.

**Files:**
- Modify: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/memory/LocalEmbeddingProvider.kt`
- Modify: `~/Projects/DollOSAIService/app/build.gradle.kts`

- [ ] **Step 1: Add ONNX Runtime dependency**

Add to `app/build.gradle.kts` dependencies:
```kotlin
implementation("com.microsoft.onnxruntime:onnxruntime-android:1.17.0")
```

- [ ] **Step 2: Rewrite LocalEmbeddingProvider**

```kotlin
package org.dollos.ai.memory

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import android.content.Context
import android.util.Log
import java.io.File
import java.nio.LongBuffer
import kotlin.math.sqrt

class LocalEmbeddingProvider(private val context: Context) : EmbeddingProvider {

    companion object {
        private const val TAG = "LocalEmbeddingProvider"
        private const val BUNDLED_MODEL_PATH = "/system_ext/dollos/models/embedding.onnx"
        private const val BUNDLED_VOCAB_PATH = "/system_ext/dollos/models/vocab.txt"
        private const val LOCAL_MODEL_DIR = "models"
        private const val MAX_SEQ_LENGTH = 128
    }

    private var ortEnv: OrtEnvironment? = null
    private var ortSession: OrtSession? = null
    private var tokenizer: WordPieceTokenizer? = null
    private var _dimension: Int = 0

    override val dimension: Int get() = _dimension
    override val name: String get() = "local:${modelName()}"
    override val requiresNetwork = false

    init {
        try {
            loadModel()
        } catch (e: Exception) {
            Log.e(TAG, "Failed to load ONNX model", e)
        }
    }

    fun isReady(): Boolean = ortSession != null && tokenizer != null

    override suspend fun embed(text: String): FloatArray {
        val session = ortSession ?: return FloatArray(0)
        val tok = tokenizer ?: return FloatArray(0)

        val tokenIds = tok.tokenize(text, MAX_SEQ_LENGTH)
        val attentionMask = tok.createAttentionMask(tokenIds)
        val tokenTypeIds = tok.createTokenTypeIds(tokenIds)

        val env = ortEnv ?: return FloatArray(0)

        val inputIds = OnnxTensor.createTensor(
            env, LongBuffer.wrap(tokenIds.map { it.toLong() }.toLongArray()), longArrayOf(1, tokenIds.size.toLong())
        )
        val maskTensor = OnnxTensor.createTensor(
            env, LongBuffer.wrap(attentionMask.map { it.toLong() }.toLongArray()), longArrayOf(1, attentionMask.size.toLong())
        )
        val typeTensor = OnnxTensor.createTensor(
            env, LongBuffer.wrap(tokenTypeIds.map { it.toLong() }.toLongArray()), longArrayOf(1, tokenTypeIds.size.toLong())
        )

        val inputs = mapOf(
            "input_ids" to inputIds,
            "attention_mask" to maskTensor,
            "token_type_ids" to typeTensor
        )

        try {
            val result = session.run(inputs)
            // Output shape: [1, seq_len, hidden_size] — need mean pooling
            val output = result[0].value as Array<Array<FloatArray>>
            val tokenEmbeddings = output[0]  // [seq_len, hidden_size]

            // Mean pooling (only non-padding tokens)
            val pooled = meanPool(tokenEmbeddings, attentionMask)
            return normalize(pooled)
        } finally {
            inputIds.close()
            maskTensor.close()
            typeTensor.close()
        }
    }

    private fun meanPool(tokenEmbeddings: Array<FloatArray>, attentionMask: IntArray): FloatArray {
        val hiddenSize = tokenEmbeddings[0].size
        val sum = FloatArray(hiddenSize)
        var count = 0f

        for (i in tokenEmbeddings.indices) {
            if (attentionMask[i] == 1) {
                for (j in 0 until hiddenSize) {
                    sum[j] += tokenEmbeddings[i][j]
                }
                count += 1f
            }
        }

        if (count == 0f) return sum
        for (j in sum.indices) sum[j] /= count
        return sum
    }

    private fun normalize(vec: FloatArray): FloatArray {
        var norm = 0f
        for (v in vec) norm += v * v
        norm = sqrt(norm)
        if (norm == 0f) return vec
        return FloatArray(vec.size) { vec[it] / norm }
    }

    private fun loadModel() {
        // Check for user-provided model in app storage first, then bundled
        val localModelDir = File(context.filesDir, LOCAL_MODEL_DIR)
        val localModel = File(localModelDir, "embedding.onnx")
        val localVocab = File(localModelDir, "vocab.txt")

        val modelFile = when {
            localModel.exists() -> localModel
            File(BUNDLED_MODEL_PATH).exists() -> File(BUNDLED_MODEL_PATH)
            else -> {
                Log.w(TAG, "No embedding model found")
                return
            }
        }

        val vocabFile = when {
            localVocab.exists() -> localVocab
            File(BUNDLED_VOCAB_PATH).exists() -> File(BUNDLED_VOCAB_PATH)
            else -> {
                Log.w(TAG, "No vocab file found")
                return
            }
        }

        Log.i(TAG, "Loading model from: ${modelFile.absolutePath}")
        ortEnv = OrtEnvironment.getEnvironment()
        val opts = OrtSession.SessionOptions().apply {
            setIntraOpNumThreads(2)
        }
        ortSession = ortEnv!!.createSession(modelFile.absolutePath, opts)

        tokenizer = WordPieceTokenizer(vocabFile.inputStream())

        // Detect dimension from a dummy inference
        val dummy = embed_sync("test")
        _dimension = dummy.size
        Log.i(TAG, "Model loaded: ${modelFile.name}, dimension=$_dimension")
    }

    private fun embed_sync(text: String): FloatArray {
        return kotlinx.coroutines.runBlocking { embed(text) }
    }

    private fun modelName(): String {
        val localModel = File(context.filesDir, "$LOCAL_MODEL_DIR/embedding.onnx")
        return when {
            localModel.exists() -> localModel.nameWithoutExtension
            File(BUNDLED_MODEL_PATH).exists() -> "bundled"
            else -> "none"
        }
    }
}
```

- [ ] **Step 3: Commit**

```bash
git add app/src/main/java/org/dollos/ai/memory/LocalEmbeddingProvider.kt app/build.gradle.kts
git commit -m "feat: ONNX Runtime local embedding with WordPiece tokenizer"
```

---

## Task 7: AIDL Updates

**Goal:** Add embedding and retrieval AIDL methods.

**Files:**
- Modify: `~/Projects/DollOSAIService/aidl/org/dollos/ai/IDollOSAIService.aidl`
- Modify: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/DollOSAIServiceImpl.kt`

- [ ] **Step 1: Update IDollOSAIService.aidl**

Add after existing embedding methods:

```aidl
    // Embedding configuration
    void setCloudEmbeddingConfig(String baseUrl, String apiKey, String model, int dimensions);

    // Retrieval mode
    void setRetrievalMode(String mode);
    String getRetrievalMode();

    // Vector store management
    void rebuildVectorStore();
    String getVectorStoreInfo();
    boolean needsRebuild();
```

- [ ] **Step 2: Update DollOSAIServiceImpl.kt**

Add implementations:

```kotlin
override fun setCloudEmbeddingConfig(baseUrl: String?, apiKey: String?, model: String?, dimensions: Int) {
    if (baseUrl == null || apiKey == null || model == null) return
    memoryManager.setCloudEmbeddingConfig(baseUrl, apiKey, model, dimensions)
    Log.i(TAG, "Cloud embedding configured: $model @ $baseUrl")
}

override fun setRetrievalMode(mode: String?) {
    memoryManager.setRetrievalMode(mode ?: "hybrid")
}

override fun getRetrievalMode(): String = memoryManager.getRetrievalMode()

override fun rebuildVectorStore() {
    memoryManager.rebuildVectorStore()
}

override fun getVectorStoreInfo(): String = memoryManager.getVectorStoreInfo()

override fun needsRebuild(): Boolean = memoryManager.needsRebuild()
```

- [ ] **Step 3: Commit**

```bash
git add aidl/ app/src/main/java/org/dollos/ai/DollOSAIServiceImpl.kt
git commit -m "feat: add embedding and retrieval AIDL methods"
```

---

## Task 8: Settings UI — restructure main page

**Goal:** Slim down the main AI Settings page to Stats + Personality, add sub-page links for LLM, Memory, Budget.

**Files:**
- Modify: `~/Projects/DollOS-build/packages/apps/Settings/res/xml/dollos_ai_settings.xml`
- Modify: `~/Projects/DollOS-build/packages/apps/Settings/src/com/android/settings/dollos/DollOSAISettingsFragment.java`

- [ ] **Step 1: Update dollos_ai_settings.xml**

Keep Stats Card and Personality category. Replace API & Model and Budget categories with sub-page links:

```xml
    <!-- Sub-pages -->
    <Preference
        android:key="llm_settings"
        android:title="LLM Settings"
        android:summary="Provider, API key, model configuration"
        android:fragment="com.android.settings.dollos.DollOSLLMSettingsFragment" />

    <Preference
        android:key="memory_settings"
        android:title="Memory Settings"
        android:summary="Embedding, search mode, vector store, export/import"
        android:fragment="com.android.settings.dollos.DollOSMemorySettingsFragment" />

    <Preference
        android:key="budget_settings"
        android:title="Budget Settings"
        android:summary="Usage warnings and limits"
        android:fragment="com.android.settings.dollos.DollOSBudgetSettingsFragment" />
```

- [ ] **Step 2: Update DollOSAISettingsFragment.java**

Remove LLM and Budget preference change listeners and loading code (moved to sub-fragments). Keep Stats Card and Personality listeners.

- [ ] **Step 3: Commit**

```bash
cd ~/Projects/DollOS-build/packages/apps/Settings
git add res/xml/dollos_ai_settings.xml src/com/android/settings/dollos/DollOSAISettingsFragment.java
git commit -m "feat: restructure AI Settings — stats + personality on main page, sub-page links"
```

---

## Task 9: Settings UI — LLM Settings sub-page

**Goal:** Create the LLM Settings sub-page with foreground/background model config.

**Files:**
- Create: `~/Projects/DollOS-build/packages/apps/Settings/res/xml/dollos_llm_settings.xml`
- Create: `~/Projects/DollOS-build/packages/apps/Settings/src/com/android/settings/dollos/DollOSLLMSettingsFragment.java`

- [ ] **Step 1: Create dollos_llm_settings.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<PreferenceScreen xmlns:android="http://schemas.android.com/apk/res/android"
    android:title="LLM Settings">

    <PreferenceCategory android:title="Foreground Model">
        <ListPreference
            android:key="fg_provider"
            android:title="Provider"
            android:entries="@array/ai_provider_entries"
            android:entryValues="@array/ai_provider_values" />
        <EditTextPreference
            android:key="fg_api_key"
            android:title="API Key"
            android:inputType="textPassword" />
        <EditTextPreference
            android:key="fg_model"
            android:title="Model" />
    </PreferenceCategory>

    <PreferenceCategory android:title="Background Model">
        <ListPreference
            android:key="bg_provider"
            android:title="Provider"
            android:entries="@array/ai_provider_entries"
            android:entryValues="@array/ai_provider_values" />
        <EditTextPreference
            android:key="bg_api_key"
            android:title="API Key"
            android:inputType="textPassword" />
        <EditTextPreference
            android:key="bg_model"
            android:title="Model" />
    </PreferenceCategory>
</PreferenceScreen>
```

- [ ] **Step 2: Create DollOSLLMSettingsFragment.java**

DashboardFragment that binds to IDollOSAIService, loads/saves foreground+background model preferences via AIDL. Follow the same pattern as the existing DollOSAISettingsFragment (ServiceConnection, preference change listeners).

- [ ] **Step 3: Commit**

```bash
git add res/xml/dollos_llm_settings.xml src/com/android/settings/dollos/DollOSLLMSettingsFragment.java
git commit -m "feat: add LLM Settings sub-page"
```

---

## Task 10: Settings UI — Memory Settings sub-page

**Goal:** Create the Memory Settings sub-page with embedding, search mode, vector store management, export/import.

**Files:**
- Create: `~/Projects/DollOS-build/packages/apps/Settings/res/xml/dollos_memory_settings.xml`
- Create: `~/Projects/DollOS-build/packages/apps/Settings/src/com/android/settings/dollos/DollOSMemorySettingsFragment.java`

- [ ] **Step 1: Create dollos_memory_settings.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<PreferenceScreen xmlns:android="http://schemas.android.com/apk/res/android"
    android:title="Memory Settings">

    <PreferenceCategory android:title="Embedding">
        <ListPreference
            android:key="embedding_source"
            android:title="Embedding Source"
            android:entries="@array/embedding_source_entries"
            android:entryValues="@array/embedding_source_values"
            android:defaultValue="local" />

        <!-- Cloud config (visible when source=cloud) -->
        <EditTextPreference
            android:key="cloud_embedding_url"
            android:title="API Endpoint"
            android:defaultValue="https://api.openai.com/v1/embeddings"
            android:dependency="embedding_source" />
        <EditTextPreference
            android:key="cloud_embedding_api_key"
            android:title="API Key"
            android:inputType="textPassword"
            android:dependency="embedding_source" />
        <EditTextPreference
            android:key="cloud_embedding_model"
            android:title="Model"
            android:defaultValue="text-embedding-3-small"
            android:dependency="embedding_source" />
        <EditTextPreference
            android:key="cloud_embedding_dimensions"
            android:title="Dimensions"
            android:inputType="number"
            android:defaultValue="384"
            android:dependency="embedding_source" />
    </PreferenceCategory>

    <PreferenceCategory android:title="Search">
        <ListPreference
            android:key="retrieval_mode"
            android:title="Search Mode"
            android:entries="@array/retrieval_mode_entries"
            android:entryValues="@array/retrieval_mode_values"
            android:defaultValue="hybrid" />
    </PreferenceCategory>

    <PreferenceCategory android:title="Vector Store">
        <Preference
            android:key="vector_store_info"
            android:title="Status"
            android:selectable="false" />
        <Preference
            android:key="rebuild_vector_store"
            android:title="Rebuild Vector Store"
            android:summary="Re-embed all memory chunks with current model" />
    </PreferenceCategory>

    <PreferenceCategory android:title="Backup">
        <Preference
            android:key="export_memory"
            android:title="Export Memory"
            android:summary="Export all memory as ZIP" />
        <Preference
            android:key="import_memory"
            android:title="Import Memory"
            android:summary="Import memory from ZIP" />
    </PreferenceCategory>
</PreferenceScreen>
```

- [ ] **Step 2: Add array resources**

Add to `res/values/arrays.xml`:
```xml
<string-array name="embedding_source_entries">
    <item>Local (on-device)</item>
    <item>Cloud (API)</item>
</string-array>
<string-array name="embedding_source_values">
    <item>local</item>
    <item>cloud</item>
</string-array>
<string-array name="retrieval_mode_entries">
    <item>Hybrid (semantic + keyword)</item>
    <item>Keyword only (FTS4)</item>
</string-array>
<string-array name="retrieval_mode_values">
    <item>hybrid</item>
    <item>fts4</item>
</string-array>
```

- [ ] **Step 3: Create DollOSMemorySettingsFragment.java**

DashboardFragment that:
- Binds to IDollOSAIService
- Loads embedding source, cloud config, retrieval mode, vector store info
- Shows/hides cloud config fields based on embedding source selection
- "Rebuild Vector Store" button calls `rebuildVectorStore()`
- "Export/Import Memory" uses SAF file picker + `exportMemory()`/`importMemory()`
- Updates vector store info on resume

- [ ] **Step 4: Commit**

```bash
git add res/xml/dollos_memory_settings.xml res/values/arrays.xml src/com/android/settings/dollos/DollOSMemorySettingsFragment.java
git commit -m "feat: add Memory Settings sub-page (embedding, search, vector store, export/import)"
```

---

## Task 11: Settings UI — Budget Settings sub-page

**Goal:** Move budget preferences to sub-page.

**Files:**
- Create: `~/Projects/DollOS-build/packages/apps/Settings/res/xml/dollos_budget_settings.xml`
- Create: `~/Projects/DollOS-build/packages/apps/Settings/src/com/android/settings/dollos/DollOSBudgetSettingsFragment.java`

- [ ] **Step 1: Create dollos_budget_settings.xml**

Move the existing budget preferences from dollos_ai_settings.xml:

```xml
<?xml version="1.0" encoding="utf-8"?>
<PreferenceScreen xmlns:android="http://schemas.android.com/apk/res/android"
    android:title="Budget Settings">

    <PreferenceCategory android:title="Warning">
        <EditTextPreference
            android:key="warning_tokens"
            android:title="Warning Threshold (tokens)"
            android:inputType="number" />
        <ListPreference
            android:key="warning_period"
            android:title="Period"
            android:entries="@array/budget_period_entries"
            android:entryValues="@array/budget_period_values"
            android:defaultValue="daily" />
    </PreferenceCategory>

    <PreferenceCategory android:title="Hard Limit">
        <EditTextPreference
            android:key="limit_tokens"
            android:title="Hard Limit (tokens)"
            android:inputType="number" />
        <ListPreference
            android:key="limit_period"
            android:title="Period"
            android:entries="@array/budget_period_entries"
            android:entryValues="@array/budget_period_values"
            android:defaultValue="daily" />
    </PreferenceCategory>
</PreferenceScreen>
```

- [ ] **Step 2: Create DollOSBudgetSettingsFragment.java**

DashboardFragment binding to IDollOSAIService, loads/saves budget preferences. Move existing budget code from DollOSAISettingsFragment.

- [ ] **Step 3: Commit**

```bash
git add res/xml/dollos_budget_settings.xml src/com/android/settings/dollos/DollOSBudgetSettingsFragment.java
git commit -m "feat: add Budget Settings sub-page"
```

---

## Task 12: Build, Deploy, and Verify

**Goal:** Build everything and verify on device.

- [ ] **Step 1: Build DollOSAIService**

```bash
cd ~/Projects/DollOSAIService
./gradlew assembleRelease
```

- [ ] **Step 2: Build Settings + deploy**

```bash
cp app/build/outputs/apk/release/app-release-unsigned.apk prebuilt/DollOSAIService.apk
rsync -av --delete . ~/Projects/DollOS-build/external/DollOSAIService/
cd ~/Projects/DollOS-build
source build/envsetup.sh && lunch dollos_bluejay-bp2a-userdebug
m DollOSAIService Settings -j$(nproc)
```

Push to device and reboot.

- [ ] **Step 3: Verify Settings UI**

Open Settings → AI. Verify:
- Stats Card and Personality on main page
- LLM Settings, Memory Settings, Budget Settings sub-page links
- Memory Settings page shows embedding source, search mode, vector store info
- Cloud config fields visible only when source=cloud

- [ ] **Step 4: Verify embedding backend**

```bash
adb logcat -d | grep -iE "CloudEmbedding|LocalEmbedding|MemorySearch|MemoryManager" | head -20
```

Expected: provider initialized with correct modelId.

- [ ] **Step 5: Test retrieval mode switch**

In Memory Settings, switch between Hybrid and Keyword only. Send a message and verify logcat shows correct search behavior.

---

## Notes

### ONNX model bundling

The default ONNX model is placed at `/system_ext/dollos/models/embedding.onnx` with `vocab.txt` alongside it. This is added to the system image via `dollos_bluejay.mk`. The specific model choice is deferred — the architecture works with any ONNX sentence-transformer.

### ObjectBox schema migration

Removing `@HnswIndex` and adding `modelId` changes the ObjectBox schema. ObjectBox handles this automatically (adds new properties, removes index). No manual migration needed.

### Existing embedding data

After this change, existing MemoryChunks will have `modelId = ""`. The system will detect this as "no chunks for current model" and prompt rebuild. Old chunks are not deleted — they just won't be searched.
