# Long-Term Conversational Memory in Production AI Systems
*Research report — 2026-05-06 — DollOS step 8 (auto-write memory) design input*

---

## 1. TL;DR Table

| System | Write policy | Format | Role-tagged | Working / LT separation | Forgetting / consolidation | Recall mechanism |
|---|---|---|---|---|---|---|
| **ChatGPT** | Selective: LLM classifier extracts "important" facts; optional explicit user request | Extracted facts (key-value-ish prose) + lightweight session summaries (~15 chats) | User-only (summaries capture what *user* said, not model's replies) | Separated: permanent facts layer vs. session summaries vs. current session | Auto-manage: recency + frequency scoring; less-important facts deprioritized | Injected into context window as flat list; no vector search |
| **Claude Projects / Memory** | Selective: Claude decides what is "worth remembering" (corrections, preferences, architecture notes); file-based CLAUDE.md + API memory tool | Markdown files (human-readable prose notes) | Not explicitly tagged — content is declarative facts, not turn-by-turn | Separated: CLAUDE.md loaded at session start; active session stays separate | No automatic forgetting; user manually edits; risk of "fading" as file grows large | Loaded wholesale at session start; no semantic retrieval (file-based) |
| **LangChain (patterns)** | Buffer: everything; Summary: everything (compressed); VectorStore: everything (embedded); Window: recent N | Raw turns, progressive LLM summaries, dense embeddings, or hybrid summary+raw | Human/AI labels preserved in all modes | No hard separation; all variants inject into prompt | Window: hard eviction; Summary: older turns consolidated into prose; no dedicated LT store in base patterns | Recency (buffer/window), LLM summary injection, or top-k semantic similarity |
| **Letta (MemGPT)** | Agent-self-managed: LLM decides what to write to core memory and archival via tool calls | Core memory: short prose facts; Recall: raw message log; Archival: arbitrary text in vector DB | Raw recall log preserves all roles; core memory is declarative (no role) | Hard three-tier separation: main context (RAM) / recall storage / archival storage | Agent-triggered summarization on FIFO overflow; "sleep-time" agents for background consolidation | Core: always in context; Recall: tool call (text/date search); Archival: tool call (semantic search) |
| **mem0** | Default (`infer=True`): selective LLM extraction of facts/preferences; optional raw (`infer=False`) | Extracted facts with metadata tags; optional graph storage | Messages keyed by `user_id`/`run_id`; role field (`user`/`assistant`) preserved in payload | Four tiers: conversation / session / user / org; each scoped separately | Conflict resolution: new facts overwrite contradicting old facts; no explicit forgetting | Vector search + optional graph; metadata-filtered retrieval |
| **Pinecone / Weaviate patterns** | Selective: importance-scored reflection step before write; raw dump explicitly discouraged | Episodic (timestamped events) + semantic (facts/profiles) + procedural (skills/workflows) | Varies; episodic entries usually include speaker metadata | STM = context window; LTM = external vector DB; explicitly separate | Periodic maintenance: prune stale facts, merge duplicates, replace transcripts with summaries | Dense vector retrieval with recency/frequency re-ranking |
| **Replika** | Everything remembered passively; explicit Memory Bank for user facts; user feedback loop (upvote/downvote) | Key-value facts ("User has dog named Buster") + AI-generated diary entries (narrative reflections) | Implicit: facts are about the user; diary entries are the AI's "voice" | Short-term session context clears; long-term Memory Bank persists; diary separate | No explicit forgetting; platform compresses old context; pinned memories are protected | Facts injected into prompt context; diary entries available for reference |
| **Character.ai** | Minimal persistent memory; primarily ephemeral per-session | Effectively none across sessions | N/A | No meaningful LT separation | N/A | Stateless; context window only |

---

## 2. Per-System Detail

### 2.1 ChatGPT Memory

ChatGPT's memory feature (rolled out broadly September 2024, expanded April 2025) operates through four context-window layers: session metadata (ephemeral), permanent user facts, recent-conversation summaries, and the current session transcript.

**Write policy — selective.** OpenAI uses classification models to identify inputs that "look important or useful." Memory storage is described as deliberate — the user either explicitly asks for a fact to be saved, or the system prompts for confirmation. Memory is not saved by simply observing everything said. The guidance is explicit: "Memory is intended for high-level preferences and details, and should not be relied on to store exact templates or large blocks of verbatim text."

**Format.** Permanent facts are stored as discrete prose snippets (e.g., "User is a vegetarian"). Recent-conversation summaries are brief notes of ~15 past chats. Critically, summaries capture only what *the user* said — not model responses. The system therefore is asymmetrically role-tagged: user contributions are considered "memorable," model contributions are ephemeral.

**Working / LT separation.** Clean separation: permanent facts and session summaries reside outside the active transcript; they are injected as distinct sections at session start.

**Forgetting.** Auto-management (Plus/Pro) considers recency and frequency: highly referenced facts stay top-of-mind; less-referenced facts are deprioritized. Deleting a conversation does *not* delete associated memories — memories must be deleted independently.

**Recall.** No vector search. Relevant memories are injected flat into the context window at session start alongside the system prompt.

Sources: [OpenAI Memory FAQ](https://help.openai.com/en/articles/8590148-memory-faq), [reverse-engineering analysis](https://llmrefs.com/blog/reverse-engineering-chatgpt-memory), [OpenAI announcement](https://openai.com/index/memory-and-new-controls-for-chatgpt/).

---

### 2.2 Claude Projects / Claude Memory

Anthropic introduced project-based memory for Team/Enterprise in September 2025, expanded to Pro/Max October 2025. The architecture is deliberately transparent and file-based rather than vector-database-backed.

**Write policy — agent-selective.** Claude's "auto memory" lets the model write notes to `CLAUDE.md` files when it judges information would be useful in a future session: "Claude doesn't save something every session; it decides what's worth remembering." Explicitly saved items include build commands, debugging insights, architecture notes, code-style preferences, and workflow habits. The API memory tool exposes CRUD operations on a developer-managed file directory.

**Format.** Markdown prose files. Memory is declarative facts and notes, not turn-by-turn transcripts. The advice is to keep files minimal — overly large CLAUDE.md files cause "fading memory" where the model loses ability to pinpoint relevant items within a large flat context.

**Working / LT separation.** Files loaded wholesale at session start form the persistent context; the active conversation is separate. No semantic retrieval — everything in the memory file is always visible.

**Forgetting.** No automatic forgetting; users edit directly. Risk: files accumulate and become noisy. Anthropic's own internal data shows the memory tool + context editing improves agentic task performance by 39% over baseline, and context editing alone reduces token consumption by 84% in long workflows.

**Recall.** Flat injection at session start. No retrieval step.

Sources: [Anthropic context management announcement](https://www.anthropic.com/news/context-management), [Claude Code memory docs](https://code.claude.com/docs/en/memory), [Claude Memory API docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool).

---

### 2.3 LangChain Memory Patterns

LangChain provides a toolkit of memory classes rather than a single opinionated system. The major variants:

**ConversationBufferMemory.** Stores complete raw turn-by-turn history with Human/AI role labels. Grows unboundedly; fails at token limits. Good for short, bounded sessions; impractical for long-running companions.

**ConversationBufferWindowMemory.** Variant of buffer with hard eviction: only last *k* turns retained. Simple and predictable, but loses all older context without consolidation.

**ConversationSummaryMemory.** Uses an LLM to progressively summarize the conversation after each exchange. The prompt takes `{summary}` + `{new_lines}` and returns a new summary. Role information is implicit in the summary text (not formally tagged). Lossy by design — details dropped in summarization may become relevant later.

**ConversationSummaryBufferMemory.** Hybrid: recent turns kept raw (fully role-tagged); older content consolidated into LLM summary. The token budget controls the boundary between raw and summarized regions. This is arguably the most production-practical pattern for single-session memory.

**VectorStoreRetrieverMemory.** Each turn (input + output) is embedded and stored in a vector DB (FAISS, Pinecone, Chroma, etc.). Recall is top-k semantic similarity to the current query. Retrieves semantically relevant past exchanges regardless of recency. Requires an embedding model and a vector store. Does not inherently consolidate or forget — the index grows indefinitely.

**ConversationKnowledgeGraphMemory.** Extracts triples (entity-relation-entity) from conversation turns and maintains a knowledge graph. Enables structured reasoning over facts. Less commonly deployed in production due to extraction quality variance.

LangChain's design philosophy is pluggable primitives, not an end-to-end system. The developer assembles the appropriate combination for their use case.

Sources: [Pinecone LangChain conversational memory guide](https://www.pinecone.io/learn/series/langchain/langchain-conversational-memory/), [LangChain memory overview on Medium](https://medium.com/@khadkaujjwal47/conversational-memory-in-langchain-532fe4460add).

---

### 2.4 Letta (MemGPT)

The MemGPT paper (arxiv 2310.08560, Packer et al., 2023) introduced virtual context management — an OS-inspired memory hierarchy for LLMs — now productized as the Letta platform.

**Three-tier architecture.** The system distinguishes:
- **Main context (RAM):** The fixed-size prompt window. Subdivided into: static system prompt + function schemas; *core memory* (writeable scratchpad for key user/persona facts, fixed size); FIFO message buffer (recent conversation turns).
- **Recall storage (disk-log):** Full conversation history preserved indefinitely, searchable by text or date via tool calls.
- **Archival storage (disk-vector):** Arbitrary-length text objects in a vector DB, retrieved via semantic search tool calls.

**Write policy — agent self-managed.** The LLM itself decides when to call `memory_replace`, `memory_insert`, `archival_memory_insert` to update its own memory stores. This is the defining architectural choice: the agent has explicit read/write tools for its own memory.

**Paging / overflow.** When the FIFO message buffer approaches the context limit (warning at ~70% fill, flush at ~100%), old messages are evicted and a recursive summary is generated from the evicted batch. Evicted messages remain searchable in recall storage.

**Consolidation.** "Sleep-time agents" can run background consolidation passes — reading archival/recall content and rewriting core memory to be more accurate or concise. This is optional and configurable.

**Recall.** Core memory is always in-context (passive). Recall and archival require explicit agent tool calls, which can be chained (`request_heartbeat=true` triggers immediate follow-up inference). The agent decides when to retrieve.

Sources: [Letta docs on MemGPT](https://docs.letta.com/concepts/memgpt/), [Letta memory management](https://docs.letta.com/advanced/memory-management/), [Letta blog on agent memory](https://www.letta.com/blog/agent-memory), [arXiv 2310.08560](https://arxiv.org/abs/2310.08560).

---

### 2.5 mem0

mem0 is a dedicated memory-as-a-service layer designed to sit between an LLM application and persistent storage. It abstracts storage backend (vector DB + optional graph DB) behind a simple add/search API.

**Four memory scopes.** Conversation (single turn, ephemeral), session (minutes to hours), user (weeks to permanent), and organizational (global). Each scope is keyed separately by `run_id`, `user_id`, or org identifiers.

**Write policy — LLM-extracted by default.** With `infer=True` (default), submitted messages are passed through an LLM that extracts key facts, decisions, and preferences. Role metadata (`{"role": "user"/"assistant", "content": "..."}`) is preserved in the payload. With `infer=False`, content is stored verbatim, bypassing extraction — but this disables the deduplication/conflict-resolution step.

**Format.** Extracted facts stored as semantically discrete units with metadata tags (e.g., `{"category": "movie_recommendations"}`). Tags improve later retrieval precision.

**Conflict resolution.** When `infer=True`, new memories are checked against existing ones for duplicates or contradictions; the latest truth wins. This is mem0's primary "forgetting" mechanism — stale facts are overwritten rather than accumulated.

**Recall.** Managed vector storage with optional graph traversal. Metadata-filtered search enables category-scoped queries. Results are ranked by semantic similarity.

Sources: [mem0 overview docs](https://docs.mem0.ai/overview), [mem0 memory operations docs](https://docs.mem0.ai/core-concepts/memory-operations), [mem0 memory types](https://docs.mem0.ai/core-concepts/memory-types).

---

### 2.6 Pinecone / Weaviate Agentic Memory Patterns

Both Pinecone and Weaviate have published practitioner guidance on memory architecture for production agents.

**The cardinal rule (Weaviate).** "The worst memory system is the one that faithfully stores everything." Production guidance explicitly recommends selective write with an importance-scoring reflection step before committing to long-term storage. The failure modes of storing everything are well-documented: context poisoning (hallucinated/incorrect facts re-entering the loop), context distraction (agent over-anchors to stale history), and context clash (contradictions degrade reasoning).

**Recommended LTM storage types.** Episodic memory: timestamped events tied to specific interactions. Semantic memory: generalized facts, user profiles, domain rules. Procedural memory: reusable workflows, skills, optimized action sequences. Raw transcripts are explicitly discouraged for LTM — replaced with compact summaries.

**Two-tier STM/LTM.** Short-term memory (STM) = context window with recency-managed sliding window. Long-term memory (LTM) = external vector DB. The boundary is explicit and enforced architecturally, not blurred.

**Consolidation.** Periodic maintenance passes: prune stale facts, merge duplicates, delete outdated entries, replace long transcripts with summaries. Recency and retrieval frequency are the primary signals for what to keep vs. retire.

**Recall.** Dense vector retrieval with recency and frequency re-ranking. "Context engineering" framing: the context window is a scarce resource — every token consumed by memory is a token unavailable for reasoning. Production agents need deliberate memory architecture, not just larger windows.

Sources: [Weaviate context engineering blog](https://weaviate.io/blog/context-engineering), [Pinecone conversational memory guide](https://www.pinecone.io/learn/series/langchain/langchain-conversational-memory/), [Weaviate agentic workflows](https://weaviate.io/blog/what-are-agentic-workflows).

---

### 2.7 Replika and Character.ai (Companion Systems)

**Replika.** Operates a three-layer memory system: (1) short-term session context that clears after inactivity, (2) a persistent long-term Memory Bank of user facts (name, hobbies, relationships, pets — stored as discrete key-value-ish facts), and (3) AI-generated diary entries where the companion writes reflective narrative about past conversations from its "own perspective." User feedback (upvote/downvote on responses) feeds a learning loop that adjusts response style. Long-term testing shows retention of facts from 8+ months ago. A "pinned memory" system protects key facts from platform-side context compression. Key design insight: the companion writes its own diary — it is a role-tagged narrative from the AI's viewpoint, not a user-facts list.

**Write policy.** Passive observation of everything said, with the Memory Bank as the curated structured layer. Users can also explicitly state facts ("remember that..."). Frequency weighting: repeating a fact twice in a week improves retention.

**Character.ai.** Widely criticized for having no meaningful persistent memory. Each character session is stateless. Memory is context-window only. The philosophical tradeoff is deliberate: Character.ai optimizes for breadth (millions of different characters) rather than depth (one evolving companion). The consequence is that no long-term relationship forms.

Sources: [Replika help docs on memory](https://help.replika.com/hc/en-us/articles/360000874712-What-does-my-Replika-remember-about-me), [AI companion memory comparison 2026](https://aicompanionguides.com/blog/ai-companion-memory-systems-ranked-2026/), [Replika vs Character.ai comparison](https://aicompanionguides.com/blog/replika-vs-character-ai/).

---

## 3. Cross-Cutting Patterns

### Pattern A — Selective write dominates production systems

Every production system that has been scaled (ChatGPT, mem0, Weaviate-pattern agents, Replika) converges on *not* writing raw transcripts to long-term storage. The consensus is explicit and strong: verbatim transcripts are expensive, noisy, and actively harmful (context poisoning, distraction). The decision of *what* to write is resolved by one of:
- **LLM extraction classifier** (ChatGPT, mem0 `infer=True`): automated, runs after each turn or batch of turns.
- **Agent self-managed tool calls** (Letta/MemGPT): agent decides when to write, using the same LLM that does everything else.
- **Heuristic + user-explicit** (Replika): passive observation plus explicit user statements.

The simplest viable filter is: *did this turn contain a new fact, preference, or event that could affect future responses?* An LLM judge on a single turn costs roughly one small-model inference.

### Pattern B — Three-tier hierarchy is the convergent architecture

Letta made this explicit, but it appears across all systems in some form:
1. **Active context (RAM):** current session, always visible.
2. **Working memory / core memory:** curated facts about user and self, injected at session start, updated selectively.
3. **Archival / episodic storage:** full history, never in-context by default, retrieved on demand.

Systems differ primarily in how they *manage the boundary* between tier 2 and tier 3 — either by LLM self-management (Letta), by LLM extraction at write time (mem0, ChatGPT), or by a flat file that the user curates (Claude CLAUDE.md).

### Pattern C — Role-tagging is asymmetric in practice

ChatGPT summaries capture only what the *user* said. Replika's Memory Bank stores facts about the *user*. The AI's own outputs are rarely preserved as "memories" — they are treated as ephemeral responses. The exception is Replika's diary, where the AI writes its own narrative — a form of AI self-memory that supports the companion's sense of continuity and personality. This asymmetry is intentional: user facts have higher persistence value than model-generated text.

### Pattern D — Forgetting is consolidation, not deletion

No production system implements time-based expiry or random deletion. Forgetting is implemented as:
- **Conflict resolution / overwrite** (mem0): newer fact replaces contradicting older fact.
- **Summarization of evicted turns** (Letta, LangChain SummaryBuffer): information is lossy-compressed, not deleted.
- **Deprioritization by recency + frequency** (ChatGPT auto-manage): facts fade from active injection without being deleted.
- **User-explicit edit** (Claude CLAUDE.md, Replika pinning): human judgment is the ultimate arbiter.

### Pattern E — Recall is shifting from recency-only to hybrid

Early systems used recency (most recent N turns). Production systems now use hybrid: dense vector retrieval for semantic relevance + recency signal for temporal grounding + frequency as a signal of importance. Pure recency misses facts from months ago; pure semantic search misses recent but not-yet-reinforced events. The hybrid balances both.

### Outlier — Claude CLAUDE.md flat-file approach

Anthropic's chosen approach is deliberately *not* a vector database. The bet is transparency and controllability over retrieval sophistication. The downside (acknowledged: "fading memory" as files grow) suggests this works well for short-to-medium memory footprints but degrades for very long-running companions. For DollOS, which has memsearch (hybrid vector + BM25) already deployed, this limitation does not apply.

---

## 4. Recommendations for DollOS Step 8

### Existing DollOS memory assets (context for recommendations)

| What exists | Description |
|---|---|
| `SmallModelInstinct._last_summary` | Rolling 1-3 sentence prose summary, in-memory, restart-clears. Maps to STM / working memory. |
| `data/memory/shared/{date}.md` | Markdown daily files. The LT store. Role of "archival storage" in the hierarchy. |
| `memsearch` | Milvus Lite + ONNX bge-m3 + FTS5 hybrid retrieval. Production-grade recall mechanism already in place. |
| `NoteMemory` tool | Big model (Doll) writes facts explicitly via tool call. Maps to Letta's `archival_memory_insert`. |
| VoM RECALL block | Per-event memsearch hits synthesized by Instinct and prefilled into big model's `<think>`. Production recall already working. |

### Does auto-write add value beyond NoteMemory?

**Yes, for two distinct reasons:**

1. **Coverage gap.** NoteMemory is Doll-triggered: it only fires when Doll explicitly judges a fact worth saving. Doll won't write a note for every turn of a long emotional conversation, even though those turns contain signals (user mood, relationship events, preferences stated in passing) that have long-term value. Auto-write fills the coverage gap for turns Doll doesn't consciously process as "memorable."

2. **Instinct-tier capture.** Many events are handled by Instinct alone (reflex path, no Doll wakeup). These events have no opportunity for Doll to call NoteMemory. Auto-write by Instinct can capture lightweight summaries of these events without waking the big model.

**But raw auto-write is harmful.** Storing every user message and every Doll utterance verbatim creates exactly the "context poisoning / distraction" failure modes documented across all production systems. The existing daily markdown files would balloon, retrieval quality would degrade, and memsearch would return noisy candidates.

### Recommended architectures (choose one for step 8)

---

#### Architecture A — Instinct-filtered episodic auto-write (recommended)

**How it works.** After each event cycle (Instinct + optional Doll turn), Instinct runs a lightweight filter pass on the completed turn. If the turn passes the filter, Instinct writes a compact episodic summary to the daily markdown file. If not, nothing is written.

**Filter criteria (heuristic, no extra LLM call needed):**
- User message is longer than ~20 tokens (not a one-word acknowledgment).
- OR: Doll's response invoked a tool call (NoteMemory, Say with emotional content).
- OR: Instinct's existing `_last_summary` changed significantly from the previous one (delta-based: if the new summary differs from the old by more than a cosine threshold, the turn was "interesting").
- NOT a pure system event with no user/Doll content.

**Format.** One-paragraph episodic entry per qualifying turn:
```
[2026-05-06T14:23] User expressed frustration about work deadline. Doll acknowledged and offered encouragement. User mentioned project is due Friday.
```
Role-tagged implicitly in the prose. Keep under 100 tokens per entry.

**Why this fits DollOS.** Instinct already synthesizes a rolling summary — the delta-check is near-zero extra cost. No extra LLM inference needed for the filter. The daily markdown file stays compact and memsearch retrieval quality remains high. NoteMemory continues to serve explicit Doll-decided facts; auto-write serves implicit episodic capture.

---

#### Architecture B — Async background consolidation pass (complementary to A)

**How it works.** A background task (e.g., triggered when Doll has been idle for 30 minutes, or once per day) reads the last N raw-or-episodic entries from the daily file and runs a consolidation pass:
- Merge duplicate facts (similar to mem0 conflict resolution).
- Promote recurring themes into stable semantic facts in a separate `facts/{character_id}.md` file.
- Entries older than 30 days that have not been recalled recently are summarized and archived to a `archive/{year-month}.md` file, removing them from the active daily index.

This maps to Letta's "sleep-time agent" pattern and Weaviate's "periodic maintenance" recommendation.

**Why this fits DollOS.** The event loop is already asyncio-based. A sleep-triggered consolidation coroutine is a natural fit. The memsearch index over daily markdown files naturally benefits from cleaner, deduplicated content.

---

#### Architecture C — Small-model turn judge (higher quality, higher cost)

**How it works.** After each completed turn, Instinct makes a single additional small-model inference call: *"Does this turn contain a new fact, preference, relationship event, or mood signal worth remembering? Answer yes/no and if yes, write a one-sentence summary."* The output is written to the daily file only on "yes."

This is the mem0 `infer=True` pattern, but running on the locally-hosted Inner Voice model rather than a cloud API.

**Tradeoff.** Highest quality filter — catches subtle facts that heuristics miss. Cost: one extra small-model inference per event cycle. On a 0.6B–1.7B model running locally, this is likely 50–200ms per cycle. Acceptable for most event rates, borderline for high-frequency rapid-fire events.

**Hybrid.** Use architecture A's heuristics as a gate: only invoke the small-model judge if heuristics return "maybe interesting" (i.e., turn passes basic length/tool thresholds). Discard outright short/trivial turns without spending the inference.

---

### Simplest starting point for step 8

Start with **Architecture A** (heuristic filter + episodic prose entry) because:
- Zero extra LLM calls.
- Zero new infrastructure (writes to the same daily markdown files already indexed by memsearch).
- Directly addresses the coverage gap (turns Doll doesn't explicitly NoteMemory).
- The delta-summary heuristic reuses `_last_summary` already computed by Instinct.
- Adds Architecture B as a follow-up step in step 9 or 10 when memory accumulation is measurably observed.

**What step 8 does NOT need to do:**
- Store raw turn-by-turn transcripts (harmful, as documented).
- Replace or compete with NoteMemory (complementary, not substitute).
- Implement forgetting immediately (daily files are small; consolidation can wait for step 9).

---

*End of report.*
