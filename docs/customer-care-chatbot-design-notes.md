# Customer Care Chatbot — Design Notes & Conversation Log

This document captures the architecture, design decisions, file plan, and
all the technical Q&A from the planning conversation for extending the
existing RAG pipeline into a conversational customer-care chatbot.

> Status at time of writing: **Phase 0 (planning) complete. Phase 1
> (settings extension) drafted and presented for approval — not yet applied.
> No code has been written or modified yet.**

---

## 1. Project goal

Extend the existing TimescaleDB + OpenAI + Docling RAG document-processing
pipeline into a **conversational customer-care chatbot** that can:

- Hold a multi-turn conversation with a customer.
- Answer questions grounded in documents already ingested into the vector store.
- Use **Pydantic AI** (not LangChain, not bare `instructor`) as the agent framework.
- Apply **guardrails at every phase** (input, retrieval, generation, output,
  conversation, operational).
- Mimic a **production-grade architecture** using local OSS components
  (no managed-service costs).

---

## 2. What the existing project already provides

The existing `app/` directory implements an async-first FastAPI ingestion +
query pipeline:

| Layer | Existing module | Notes |
|---|---|---|
| Ingestion (sync, Celery) | `app/worker.py`, `app/pipeline.py`, `app/services/document_processor.py`, `app/services/chunker.py` | Docling + HybridChunker + tiktoken, then batch-embed and upsert |
| Retrieval / Q&A (async) | `app/api/routes/query.py`, `app/services/synthesizer.py` | Stateless single-shot Q&A via `instructor` |
| Vector store | `app/database/vector_store.py` | TimescaleDB (pgvectorscale) with both sync and async clients |
| Settings | `app/config/settings.py` | Pydantic `BaseModel` per concern |
| Routes | `app/api/routes/{ingest,query,documents,jobs}.py` | FastAPI routers |
| Observability | Langfuse `@observe()` decorators | Already wired across pipeline |
| Infra | `docker/docker-compose.yml` | TimescaleDB + Redis |

The chatbot will be **purely additive** — none of these files are deleted
or rewritten. The Synthesizer-based `/query` endpoint stays untouched.

---

## 3. Locked-in design decisions (for v1)

| Decision | Choice | Reasoning |
|---|---|---|
| Session memory storage | **Redis** | Already in `docker-compose.yml`; fast; native TTL; survives FastAPI restarts; works across multiple workers |
| Guardrail depth | **Lean: regex + curated patterns** | Fast (~0ms), deterministic, testable, covers most real attacks; pluggable so we can flip on `gpt-4o-mini` LLM judge later |
| Streaming | **JSON only for v1** | Simpler output validation, single endpoint; SSE can be added later as `/chat/stream` without refactor |
| Escalation | **Explicit `escalate_to_human` tool** | LLM decides when; structured signal `needs_human=True`; logged via Langfuse |
| Relevance check | **Retrieval-distance gate (Option A)** | Reuses the embedding we'd already make; if best cosine distance > threshold → refuse early; zero extra cost |
| Production-grade-without-cost | Yes — fold in | Structured JSON logs, env-driven settings, `/readyz`, slowapi rate limiting, embedding cache, `gpt-4o-mini` for cheap jobs, Pydantic AI `TestModel` tests |

---

## 4. High-level architecture

```
Customer
   │ POST /chat  { session_id, message }
   ▼
┌──────────────────────────────────────────────────────────┐
│ FastAPI route /chat  (api/routes/chat.py)                │
│  1. Load session memory from Redis (memory.py.load)      │
│  2. INPUT GUARDRAILS                                     │
│     - empty / length / language                          │
│     - PII regex                                          │
│     - jailbreak / prompt-injection patterns              │
│     - profanity                                          │
│  3. EMBED query once  (cached in Redis)                  │
│  4. RELEVANCE GATE  (peek best distance)                 │
│       - if > threshold → return polite "out of scope"    │
│  5. Run Pydantic AI Agent                                │
│       deps = ChatDeps(vector_store, session_id, ...)     │
│       message_history = past_turns                       │
│       Tools available:                                   │
│         - retrieve_knowledge(query)                      │
│             → applies retrieval guardrails               │
│         - escalate_to_human(reason)                      │
│             → side-effects + structured signal           │
│       output_type = ChatAnswer                           │
│       @output_validator: citation + grounding + scrub    │
│  6. OUTPUT GUARDRAILS                                    │
│     - PII scrub                                          │
│     - profanity scrub                                    │
│     - citation integrity                                 │
│     - "no context → don't answer" enforcement            │
│  7. Persist new turn (memory.py.append)                  │
│  8. Return ChatResponse                                  │
└──────────────────────────────────────────────────────────┘
```

---

## 5. Proposed file changes (final list)

### New files

| File | Purpose |
|---|---|
| `app/chatbot/__init__.py` | Package marker |
| `app/chatbot/agent.py` | The Pydantic AI `Agent` (model, system prompt, deps_type, output_type, tools, output validators) |
| `app/chatbot/tools.py` | `retrieve_knowledge` tool + `escalate_to_human` tool |
| `app/chatbot/deps.py` | `ChatDeps` dataclass (vector_store, session_id, user_id, etc.) |
| `app/chatbot/prompts.py` | Hardened system prompt template |
| `app/chatbot/models.py` | Pydantic schemas: `ChatRequest`, `ChatResponse`, `ChatAnswer`, `Citation` |
| `app/chatbot/memory.py` | Redis-backed conversation history store (`load`, `append`, `clear`, `exists`) |
| `app/chatbot/cache.py` | Redis-backed query-embedding cache (used by relevance gate + retrieval tool) |
| `app/chatbot/guardrails/__init__.py` | Package marker |
| `app/chatbot/guardrails/policy.py` | Central `GuardrailPolicy` driven by settings |
| `app/chatbot/guardrails/input_guards.py` | length, PII, jailbreak, profanity, language; **relevance gate function** |
| `app/chatbot/guardrails/retrieval_guards.py` | distance threshold, top-k, context-token budget, metadata allowlist |
| `app/chatbot/guardrails/output_guards.py` | citation enforcement, grounding, PII/profanity scrub |
| `app/api/routes/chat.py` | `POST /chat` endpoint |
| `app/api/routes/sessions.py` | `POST /sessions`, `GET /sessions/{id}`, `DELETE /sessions/{id}` |
| `app/tests/test_guardrails.py` | Unit tests for every guard |
| `app/tests/test_agent.py` | Pydantic AI `TestModel` integration tests (no live OpenAI calls) |

### Modified files (additive only)

| File | Change |
|---|---|
| `app/config/settings.py` | Add `ChatbotSettings`, `RetrievalSettings`, `GuardrailSettings`, `OpsSettings`; wire into `Settings` |
| `app/main.py` | Register `chat.router` and `sessions.router`; add JSON log formatter; slowapi middleware; `/readyz` |
| `app/example.env` | Append documented optional override env vars |
| `requirements.txt` | Add `pydantic-ai-slim[openai]`, `pydantic-settings`, `slowapi`, `python-json-logger`, explicit `redis` |
| `README.md` | Add "Customer Care Chatbot" section |

### Files NOT touched

`pipeline.py`, `worker.py`, `chunker.py`, `document_processor.py`,
`vector_store.py`, `synthesizer.py`, `llm_factory.py`, `ingest.py`,
`query.py`, `documents.py`, `jobs.py`, `docker-compose.yml`.

---

## 6. Guardrails strategy by phase

### A. Input guardrails (`input_guards.py`)
1. Empty / whitespace → 400.
2. Length cap (default 2000 chars).
3. Language allowlist (default `["en"]`).
4. PII regex (email, phone, CC, SSN, IBAN) → block or redact per policy.
5. Jailbreak / prompt-injection patterns → refuse with neutral message.
6. Profanity wordlist → soft handling.
7. Optional `gpt-4o-mini` LLM judge — off in v1, behind `enable_llm_judge`.

### B. Relevance gate (new step, `input_guards.py`)
- Embed the user query (cached).
- Peek best cosine distance from vector store.
- If `> distance_threshold` → return polite "out of scope" message and exit
  without invoking the agent.

### C. Retrieval guardrails (`retrieval_guards.py`)
- Distance threshold cutoff (drop too-far chunks).
- Top-k cap.
- Context-token budget (sum across retrieved chunks).
- Metadata allowlist (filename / file_type).
- Explicit `no_context=True` signal when 0 chunks survive.

### D. Generation guardrails (settings + system prompt)
- Temperature 0; capped `max_output_tokens`.
- Locked system prompt — only retrieved context, must cite, refuse out-of-scope,
  never reveal system prompt, never claim to be human.
- Pydantic AI `UsageLimits` caps tool-call iterations and total tokens per turn.

### E. Output guardrails (`output_guards.py`, `@agent.output_validator`)
- Pydantic schema validation (`ChatAnswer`) — automatic.
- Grounding check: every claim must reference at least one retrieved citation.
- Citation integrity: every cited chunk_id must exist in this turn's retrieval.
- PII scrub on final answer.
- Profanity scrub.
- Surface refusal reasons cleanly when `needs_human=True`.

### F. Conversation guardrails (`memory.py`)
- History truncation by turns and by tokens.
- Session TTL in Redis (default 1h).
- Session-id format validation (UUID).

### G. Operational guardrails (`main.py`, `chat.py`)
- slowapi rate limiting per session_id and per IP.
- Structured JSON logging.
- Langfuse tracing on every turn, every guardrail trigger, every tool call.
- Safe canned response on uncaught exceptions (no stack traces leaked).

---

## 7. Phased implementation plan

| Phase | Scope |
|---|---|
| 0 | Planning + approvals (this conversation) |
| 1 | Settings extension (`settings.py`, `example.env`) — **drafted, awaiting approval** |
| 2 | `requirements.txt` + new dependencies |
| 3 | `chatbot/models.py` (Pydantic schemas) |
| 4 | Guardrail policy + input guards + relevance gate |
| 5 | Retrieval guards |
| 6 | Output guards |
| 7 | `chatbot/deps.py`, `chatbot/prompts.py`, `chatbot/tools.py` |
| 8 | `chatbot/memory.py` + `chatbot/cache.py` |
| 9 | `chatbot/agent.py` (Pydantic AI Agent) |
| 10 | Routes (`chat.py`, `sessions.py`) + `main.py` updates |
| 11 | Tests |
| 12 | README + smoke-test CLI |

### Working rules (strict — must be followed every phase)

1. **Show first, write later.** At the start of every phase, paste the
   proposed diff or full contents of new files into the chat. Do NOT
   write or edit any code file until the user gives explicit approval.
2. **What counts as approval.** Explicit phrases only: "approved",
   "go ahead and write", "apply this", "yes do it", or similar
   unambiguous go-ahead. Phrases like "let's move on", "looks good",
   "next steps", or "continue" are NOT approval — when in doubt, ask.
3. **One reviewable unit at a time.** Smallest possible scope per phase
   (one file, or a small group of tightly-coupled files). Avoid bundling.
4. **Design-doc exception.** The design doc itself
   (`docs/customer-care-chatbot-design-notes.md`) may be updated without
   per-edit approval for housekeeping: marking phases complete,
   appending new Q&A entries, or revising status. Substantive content
   changes (architecture decisions, phase plan rewrites) still require
   approval.
5. **Document Q&A as we go.** Every technical clarification the user
   asks gets appended to section 9 of the design doc as the next
   Q-numbered entry.
6. **Guardrails first-class.** Never defer guardrails to "the end" —
   they must be implemented in their proper phase (4, 5, 6).
7. **Pydantic AI patterns only.** Use `RunContext` + `deps` for tool
   dependencies. No module-level globals for tool-injected state.
8. **Local OSS only.** TimescaleDB, Redis, Langfuse free tier. No new
   managed services without explicit discussion.

---

## 8. Phase 1 — exact proposed changes (drafted, NOT applied)

### 8.1 `app/config/settings.py` (additions only)

Add four new classes after `LangfuseSettings`:

```python
class ChatbotSettings(BaseModel):
    model: str = Field(default="openai:gpt-4o")
    cheap_model: str = Field(default="openai:gpt-4o-mini")
    temperature: float = 0.0
    max_output_tokens: int = 800
    max_history_turns: int = 20
    session_ttl_seconds: int = 3600
    max_tool_iterations: int = 4
    request_timeout_seconds: int = 30


class RetrievalSettings(BaseModel):
    top_k: int = 5
    distance_threshold: float = 0.45
    max_context_tokens: int = 6000
    metadata_filename_allowlist: Optional[list[str]] = None
    metadata_filetype_allowlist: list[str] = Field(default_factory=lambda: [".pdf", ".docx"])
    embedding_cache_ttl_seconds: int = 86400


class GuardrailSettings(BaseModel):
    # Input
    max_input_chars: int = 2000
    min_input_chars: int = 1
    block_pii_in_input: bool = True
    block_jailbreak_attempts: bool = True
    allowed_languages: list[str] = Field(default_factory=lambda: ["en"])
    enable_llm_judge: bool = False
    # Relevance gate
    relevance_gate_enabled: bool = True
    relevance_out_of_scope_message: str = (
        "I can only help with topics covered in my knowledge base. "
        "Could you rephrase your question or ask something more specific?"
    )
    # Output
    require_citations: bool = True
    scrub_pii_in_output: bool = True
    scrub_profanity_in_output: bool = True
    refuse_when_no_context: bool = True
    # Conversation / operational
    max_turns_per_session: int = 50
    rate_limit_per_minute: int = 30


class OpsSettings(BaseModel):
    enable_structured_logs: bool = True
    enable_rate_limiting: bool = True
    enable_readiness_probe: bool = True
    enable_embedding_cache: bool = True
    enable_otel_tracing: bool = False
```

Wire them into the main `Settings` class:

```python
class Settings(BaseModel):
    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    vector_store: VectorStoreSettings = Field(default_factory=VectorStoreSettings)
    chunking: ChunkingSettings = Field(default_factory=ChunkingSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)
    chatbot: ChatbotSettings = Field(default_factory=ChatbotSettings)         # NEW
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)   # NEW
    guardrails: GuardrailSettings = Field(default_factory=GuardrailSettings)  # NEW
    ops: OpsSettings = Field(default_factory=OpsSettings)                     # NEW
```

### 8.2 `app/example.env` (append-only documentation block)

```bash
# ---- Customer Care Chatbot (optional overrides) ----
# CHATBOT_MODEL=openai:gpt-4o
# CHATBOT_CHEAP_MODEL=openai:gpt-4o-mini
# CHATBOT_TEMPERATURE=0.0
# CHATBOT_SESSION_TTL_SECONDS=3600

# ---- Retrieval tuning ----
# RETRIEVAL_TOP_K=5
# RETRIEVAL_DISTANCE_THRESHOLD=0.45
# RETRIEVAL_MAX_CONTEXT_TOKENS=6000
# RETRIEVAL_EMBEDDING_CACHE_TTL_SECONDS=86400

# ---- Guardrails ----
# GUARDRAIL_MAX_INPUT_CHARS=2000
# GUARDRAIL_BLOCK_PII_IN_INPUT=true
# GUARDRAIL_BLOCK_JAILBREAK=true
# GUARDRAIL_ENABLE_LLM_JUDGE=false
# GUARDRAIL_RELEVANCE_GATE_ENABLED=true
# GUARDRAIL_RATE_LIMIT_PER_MINUTE=30

# ---- Operational ----
# OPS_ENABLE_STRUCTURED_LOGS=true
# OPS_ENABLE_RATE_LIMITING=true
# OPS_ENABLE_READINESS_PROBE=true
# OPS_ENABLE_EMBEDDING_CACHE=true
# OPS_ENABLE_OTEL_TRACING=false
```

---

## 9. Technical Q&A captured from the conversation

### Q1. Why is `escalate_to_human(reason)` a tool?

Escalation is a judgment call that the LLM is best placed to make based on
the conversation, and it is also a side-effecting action (creating a Zendesk
ticket, paging Slack, writing to a handoff table). Tools are the natural seam
between LLM judgment and deterministic side-effects. Making it a tool gives:

- The LLM agency to decide *when* to escalate.
- A structured, schema-validated reason for analytics in Langfuse.
- A clean place to do real I/O (tickets, pages, DB rows) without the LLM doing it.
- Smooth integration into the agent loop — the agent can call the tool and
  *then* produce a polite final message using the tool's return value.
- Testability via `agent.override` + `TestModel`.
- Swappability: the tool's implementation can change (Zendesk → PagerDuty)
  without the agent being aware.

### Q2. What do "load session memory from Redis" and "persist new turn to Redis" mean and why?

HTTP is stateless — every `/chat` request is independent and the Python process
has no memory of prior turns. To make conversations feel continuous we use
Redis as the conversation notebook:

- **Load:** at the start of every `/chat` call, read the prior turns for this
  `session_id` from Redis and pass them to the agent as `message_history`.
- **Persist:** after the agent answers, append the latest user message and
  bot reply back to the same Redis key and refresh its TTL.

Redis is preferred over an in-memory dict because it survives restarts, is
shared across worker processes, and has native TTL-based cleanup.

### Q3. What do `api/routes/sessions.py`, `chatbot/deps.py`, and `chatbot/memory.py` do, and how do they relate?

- **`memory.py`** — a Redis-backed storage primitive. Methods like `load`,
  `append`, `clear`, `exists`. Knows nothing about HTTP or agents. Think
  *filing cabinet*.
- **`deps.py`** — defines `ChatDeps`, the per-request dependency bundle that
  Pydantic AI tools receive via `RunContext`. Holds `vector_store`,
  `session_id`, `user_id`, etc. Think *tool belt*.
- **`sessions.py`** — HTTP layer for managing sessions as a resource:
  `POST /sessions` (create), `GET /sessions/{id}` (view), `DELETE /sessions/{id}` (clear).
  Think *receptionist desk*.

How they connect:

- During `POST /chat`: the route uses `deps.py` to build `ChatDeps`, calls
  `memory.load()` to fetch prior turns, runs the agent, then calls
  `memory.append()` to save the new turn.
- During session CRUD endpoints: `sessions.py` is a thin REST layer over
  `memory.py`. It does not touch `deps.py`.

### Q4. Why is `vector_store` in `ChatDeps`? Shouldn't tools own their own dependencies, and shouldn't `message_history` be in deps?

Two halves:

**Why `vector_store` belongs in deps:** Pydantic AI tools are stateless functions.
Anything they need from the outside world should be injected via `ctx.deps`.
Reasons:
- `VectorStore` is expensive to construct (DB pool + OpenAI clients) — build
  once at app startup, share via deps.
- Tools become testable — pass a `FakeVectorStore` in unit tests.
- No hidden globals — the function signature reveals every external dependency.
- Per-request flexibility — multi-tenant or A/B-tested setups become trivial.

**Why `message_history` does NOT belong in deps:** Pydantic AI treats history
as a first-class concept. `Agent.run()` takes `message_history=` as its own
parameter. Putting it in deps would fight the framework. The two enter the
agent through different doors: `deps=` for tools, `message_history=` for
context.

### Q5. Pydantic AI Agent — only the essentials.

The five things to know:

1. **You construct the Agent.** Pydantic AI does not ship a preconfigured one.
2. **Five key parameters:** `model`, `system_prompt`, `deps_type=YourDepsClass`,
   `output_type=YourOutputModel`, plus tools (registered via decorator or list).
3. **`deps_type` is your class.** You design `ChatDeps` yourself — Pydantic AI
   only uses the type to give your tools type-safe access to `ctx.deps`.
4. **`RunContext` is built by the framework, not by you.** When you call
   `agent.run(message, deps=my_deps)`, Pydantic AI wraps `my_deps` in a
   `RunContext` and passes it to every tool. `ctx.deps` is your stuff;
   `ctx.usage` etc. are framework extras.
5. **When to take `RunContext` in a tool:** any tool that needs an external
   resource or request-scoped state takes `ctx: RunContext[ChatDeps]` as its
   first argument. Pure-computation tools can omit it.

### Q6. TimescaleDB connection pool — basics.

- A pool is a small set of pre-opened DB connections kept in memory and
  re-used; saves TCP+auth overhead on every query.
- Defaults are typically min 2–4 idle, max 10–20 per pool.
- Postgres has a server-side ceiling (`max_connections`, often 100); total
  consumed = sum of all process-level pools.
- Across threads (single process): each thread borrows a connection;
  database I/O releases the GIL so threads run queries in parallel.
- Across async tasks (single process): coroutines borrow a connection only
  during the actual `await`, so a small pool can serve many concurrent tasks.
- Across processes: each Python process has its **own** pool. FastAPI process
  + each Celery worker process all keep separate pools. Plan total ≤ Postgres
  ceiling.
- Project specifics: `VectorStore()` is created once at FastAPI startup
  (singleton). Each Celery worker creates its own `VectorStore` per
  ingestion task — that's normal.

### Q7. Celery worker count — defaults and configuration.

- "Worker count" has two layers: number of `celery worker` commands launched,
  and per-command **concurrency** (child processes that actually run tasks).
- **Default if you configure nothing:** Celery uses `os.cpu_count()` for
  per-command concurrency. On a 10-core MacBook that silently spins up 10
  child processes, each with its own `VectorStore` and DB pool.
- Configure in any of three places:
  - CLI: `celery -A worker worker --concurrency=4`
  - Code: `celery_app.conf.update(worker_concurrency=4)`
  - Env var: `CELERY_WORKER_CONCURRENCY=4`
- Sensible numbers: 2 for local dev, 4–8 for real ingestion workloads.
- Connection-pool math at peak ≈
  `(uvicorn workers × FastAPI pool_max) + (celery commands × concurrency × Celery pool_max)`.
  Keep comfortably under Postgres `max_connections`.

### Q8. What does `max_tool_iterations: int = 4` mean?

A hard cap on how many tool-call rounds the agent is allowed within a
single `/chat` turn before it must produce a final answer. Pydantic AI
runs an internal loop: LLM → tool call → tool result → LLM → ... until
the LLM emits a final structured answer. Without a cap, a confused or
adversarial loop could chain `retrieve_knowledge` calls indefinitely.

Realistic patterns per turn for this bot:
- 1 call: retrieve → answer (most common).
- 2 calls: refine retrieval, then answer.
- 2 calls: retrieve, escalate, then answer.
- 3 calls: retrieve, retrieve, escalate.

`4` leaves one call of headroom for unforeseen patterns but firmly rejects
runaway loops. Enforced via Pydantic AI's `UsageLimits` passed to
`agent.run(...)`. Hitting the limit raises `UsageLimitExceeded`, caught by
the route handler and converted to a safe canned response + Langfuse log.
It is one leg of a three-leg per-turn safety tripod alongside
`max_output_tokens` and `request_timeout_seconds`.

### Q9. RetrievalSettings — what each field actually does and why these defaults.

**`max_context_tokens: int = 6000`**

Cap on total tokens of *retrieved chunks* fed into the LLM as grounding
context. Not the model's context window (gpt-4o has 128k) — a deliberate,
much smaller budget. Reasons to keep it tight:
- Cost: every retrieved token is billed on every turn.
- Latency: longer prompts = noticeably slower first-token time.
- Quality: LLMs degrade with too much context ("lost in the middle").
- Attack surface: more retrieved text = more places for prompt-injection
  to hide.

6000 tokens ≈ 4,500 words ≈ 10–15 chunks at 400–600 tokens each.
Worst-case per-turn budget for gpt-4o:

```
1,000  system prompt
5,000  history (20 turns)
  200  user message
6,000  retrieved context  ← the cap
  800  reply (max_output_tokens)
-------
13,000 tokens of 128,000 available  (~10%)
```

Plenty of headroom; the cap exists for cost / latency / quality, not to
fit the window.

**`metadata_filename_allowlist` and `metadata_filetype_allowlist`**

These are retrieval-time filters, not redundant with ingestion-time
filters.

- Ingestion filter: "what enters the corpus?" — gate at the door.
- Retrieval filter: "from the corpus, what may the chatbot draw on?" —
  gate at the answer.

Why both:
- Defense in depth — ingestion rules can be relaxed for an experiment;
  retrieval-side filters keep the chatbot's behavior stable.
- Per-environment scoping — staging may have experimental file types
  ingested while prod stays strict.
- Per-document / per-tenant scoping — `metadata_filename_allowlist` lets
  one chatbot deployment answer only from a named subset of docs without
  re-ingesting.

For v1 it is belt-and-suspenders, costs nothing, and is available when
needed. Default `metadata_filename_allowlist=None` (no scoping);
default `metadata_filetype_allowlist=[".pdf", ".docx"]` mirrors ingestion.

**`embedding_cache_ttl_seconds: int = 86400`**

`86400 = 60 × 60 × 24` = 24 hours. Caches the embedding of a user query
in Redis keyed by `(model_name, query_text_hash)`. Reused both by the
relevance gate and by the retrieval tool within the same turn, and by
repeat queries within 24 hours.

Why cache: same text + same model = deterministic embedding, so reuse is
safe. The win is **latency** (50–150ms → sub-millisecond) more than cost.
Cache hits also conserve OpenAI rate-limit headroom under load.

Why 24 hours: long enough to catch repeat queries within a business day;
short enough that stale entries flush within a day if the embedding
model is swapped (the model name is part of the key, so model swaps
create new keys naturally — TTL is the cleanup mechanism). Tunable: 1h
if you swap models often, 7d/30d for max hit rate.

### Q10. Why both `no_context` (runtime flag) and `refuse_when_no_context` (config)? And why separate `max_turns_per_session` from `rate_limit_per_minute`?

**`no_context` vs `refuse_when_no_context` — different layers:**

- `no_context`: a runtime *fact* set by Python in the retrieval guard.
  Did vector search return zero usable chunks? Quantitative,
  deterministic.
- `refuse_when_no_context`: a static *policy* set by the operator at
  startup. When that fact occurs, what should we do? Refuse, or fall
  back to general knowledge?

Smoke detector vs sprinkler: one reports the fact, the other decides the
response. Keeping them separate lets policy change without touching the
detector, and lets the same detector serve different deployments
(strict prod vs permissive demo) with different policies.

**`max_turns_per_session: int = 50`** — caps the *length* of one
conversation.
- Cost: every turn re-sends the full history; long sessions grow
  quadratically.
- Quality drift: long conversations wander, accumulate confusing
  references, degrade focus.
- Attack surface: chained prompt-injection often takes many turns to
  steer the bot off-rails.
- Healthy customer-care conversations resolve in 3–8 turns; 50 is a
  soft ceiling that signals "something is off."

**`rate_limit_per_minute: int = 30`** — caps the *frequency* of incoming
messages, enforced by slowapi per session_id and per IP.
30/min = one every 2 seconds — faster than humans naturally type.
Catches:
- Scripts/bots probing for prompt-injection or scraping responses.
- Runaway clients with retry-loop bugs.
- Cost-bombing attacks burning OpenAI quota.

Different vectors, different fixes — both cheap to add, hard to retrofit
if skipped.

### Q11. How does `enough_context` (in the LLM's structured output) compare to `no_context` and `refuse_when_no_context`?

Three different layers; each catches a failure mode the others can't see.

| Signal | Type | Set by | Set when |
|---|---|---|---|
| `refuse_when_no_context` | Static config | Operator (env var) | App startup |
| `no_context` | Runtime fact | Python (retrieval guard) | Right after vector search |
| `enough_context` | LLM self-assessment | The LLM | While generating the answer |

**Important: `enough_context` is not a new concept.** It already exists
in `SynthesizedResponse` (`app/services/synthesizer.py`):

```python
class SynthesizedResponse(BaseModel):
    thought_process: List[str]
    answer: str
    enough_context: bool
```

The chatbot's `ChatAnswer` (Phase 3) should mirror this exact pattern —
the existing system prompt already trains the LLM to set it honestly
("Be transparent when there is insufficient information to fully answer
the question").

`no_context` catches "retrieval totally missed" (cheap, pre-LLM,
deterministic). `enough_context` catches "retrieval returned something
but it's off-target" (one LLM call to discover, but a failure mode
`no_context` can't see — e.g., user asks about shipping but retrieval
returns warranty chunks). `refuse_when_no_context` is the policy dial
that decides how strict to be when either signal fires.

Per-request flow with all three:

```
1. Startup:    load refuse_when_no_context from env  (policy)
2. Retrieval:  Python sets no_context = (chunks_count == 0)
3. If no_context AND refuse_when_no_context:
     → canned out-of-scope reply, never call LLM
4. Else:       call agent → ChatAnswer with enough_context flag
5. If not enough_context:
     → suppress answer body, return "couldn't confidently answer —
       want a human?"; optionally trigger escalate_to_human
```

### Q12. Why split settings into ChatbotSettings, RetrievalSettings, GuardrailSettings, OpsSettings instead of one flat blob?

Each category groups settings by *what concern they tune, who tunes them,
when they change, and how badly they break things if wrong.*

| Category | Question it answers | Owner |
|---|---|---|
| `ChatbotSettings` | "How does the agent behave?" | AI/ML engineer |
| `RetrievalSettings` | "What data does the agent see?" | Search engineer |
| `GuardrailSettings` | "What's allowed in/out, what's the policy?" | Safety / policy owner |
| `OpsSettings` | "What production capabilities are on?" | SRE / DevOps |

Five reasons separation pays off:

1. **Different humans own different categories** — each finds their
   knobs in one place.
2. **Different change cadence** — ops per deploy, chatbot per model
   upgrade, retrieval per corpus tuning, guardrails reactively after
   incidents.
3. **Different blast radius** — a wrong `temperature` is recoverable;
   a wrong `block_pii_in_input` is a real incident. Stricter review
   for higher-risk categories.
4. **Clean env-var prefixes** — `CHATBOT_*`, `RETRIEVAL_*`,
   `GUARDRAIL_*`, `OPS_*` — and clean code organization
   (`input_guards.py` only reads `settings.guardrails`, etc.;
   least-privilege within the codebase).
5. **Composability** — dev/staging/prod can override one category
   without touching the others.

Cost: one extra layer of indirection (`settings.guardrails.x` vs
`settings.x`). Benefit: a config surface that scales to 50+ fields
without becoming unmanageable.

### Q13. Why are input guards synchronous but the relevance gate asynchronous, and why is the gate kept separate from `evaluate_input` rather than folded into it?

The five input guards (length, language, PII, jailbreak, profanity) are
pure CPU-only string operations measured in microseconds — `len()`,
`re.Pattern.search()`, set membership. There is no I/O. Making them
async would buy nothing and would force every caller and test into an
`await` chain unnecessarily.

The relevance gate is different on both axes. It awaits an OpenAI
embedding call (network) and a Postgres similarity query (network),
each tens to hundreds of milliseconds. A sync implementation would
block the entire event loop for that whole window, freezing every
other concurrent request the worker is serving. Async is the only
sensible choice.

Folding the gate into `evaluate_input` would force the orchestrator to
be async too — async is infectious — and would put a slow network
call inside what is otherwise a microsecond-scale tripwire. Keeping
them separate has three benefits: the orchestrator stays pure
(testable without mocking a vector store), the route handler controls
call ordering explicitly (cheap fail-fast first, then the network
gate), and Langfuse gets two distinct trace spans instead of one fat
span.

The route handler in Phase 10 will run them in this fixed order:
`evaluate_input(message, policy)` first; if `allowed=True`, then
`await relevance_gate(message, vector_store, policy)`.

### Q14. How does the chatbot handle multi-threading / concurrency, and when should we revisit?

The application is **async-first, not threaded.** FastAPI + uvicorn
run an asyncio event loop per worker process; each `/chat` request is
a coroutine that yields the loop on every `await`. While request A is
waiting on OpenAI, request B's coroutine runs on the same thread. No
threads, no locks, no GIL contention — the right model for an
I/O-bound workload (embeddings, vector lookups, LLM calls, Redis).

Horizontal scaling is process-level: multiple uvicorn workers
(`--workers N`), each with its own event loop, `VectorStore`
singleton, and DB connection pool. They don't share in-process state
— they coordinate only through Redis and Postgres. Same pattern for
Celery workers on the ingestion side. Connection-pool math from Q6/Q7
still governs the total: sum of all process-level pools must stay
under Postgres `max_connections`.

Concurrency-safety of shared objects:

- `GuardrailPolicy`: frozen dataclass, immutable, safe across all coroutines.
- `Settings`: lru_cached Pydantic model, immutable in practice.
- `VectorStore` singleton: backed by connection pools that are themselves thread/coroutine-safe.

Where concurrency *can* bite us, deferred to later phases:

- **Same-session-id races** (Phase 8 concern). Two requests for the
  same `session_id` could load history, run the agent, and append
  turns concurrently — risking lost-turn or out-of-order writes to
  Redis. The slowapi rate limiter (30/min default) makes this
  statistically rare; for true correctness `memory.py` should use
  either a per-session-id Redis lock around the load-run-append cycle,
  or atomic `RPUSH` for persist plus read-modify-write tolerance on
  the load step.
- **Embedding cache stampedes** (Phase 8 concern). Two simultaneous
  identical queries both miss the cache and both compute the
  embedding. Harmless redundancy; can be eliminated with `SETNX` or an
  in-process `asyncio.Lock` keyed by query hash if metrics ever show
  it mattering.
- **Connection-pool sizing.** Already governed by Q6/Q7 math; revisit
  when the deployment shape (number of uvicorn workers × pool size +
  Celery total) is finalized.

Bottom line: no explicit threading work is needed now. Async +
multi-process scaling is the concurrency story. Revisit session-level
locking when `memory.py` lands.

### Q15. Why are regex patterns precompiled into `re.Pattern` objects on the policy, instead of storing raw strings or importing the `_PII_PATTERNS` constant directly in `input_guards.py`?

Two separate questions inside this one.

**Why compile at construction time, not at call time:**

1. *Deterministic performance.* Python's `re` module has a global LRU
   cache of compiled patterns, so `re.search(pattern_string, text)`
   may hit the cache. But that cache is shared with everything else
   in the process, can be evicted, and adds hash + dict lookup cost
   per call. Owning the `re.Pattern` objects ourselves means each
   `pattern.search(message)` is a direct C-level call into the
   compiled NFA — predictable and faster, especially for the
   jailbreak loop with 10+ patterns per request.
2. *Flag binding.* The jailbreak patterns compile with
   `re.IGNORECASE`. If we stored raw strings, every consumer would
   have to remember to pass that flag at the call site. Forget once
   and "Ignore Previous Instructions" silently fails to match.
   Compile-time flag binding makes the flag inseparable from the
   pattern.
3. *Fail-fast validation.* A typo in a regex string is a `re.error`.
   Compiling at startup inside `from_settings()` crashes the process
   during boot — loud and obvious. Compiling at first request
   crashes a customer's `/chat` call in production.
4. *Type signaling.* `tuple[tuple[str, re.Pattern], ...]` documents
   to the reader: patterns are ready to use.
   `tuple[tuple[str, str], ...]` would say nothing about whose
   responsibility compilation is.
5. *Consumer uniformity.* PII and jailbreak patterns are both
   `re.Pattern`, so `input_guards.py` iterates them with the
   identical idiom. Mixed types would force consumers to branch.

**Why expose them on the policy at all, instead of importing the
module-level `_PII_PATTERNS` constant directly:**

The leading underscore signals "private to this module." Reaching
across that boundary breaks the override seam that
`GuardrailPolicy.from_settings(...)` exists to provide. Putting
patterns on the policy makes one place the single source of truth,
enables loading custom rules from config / per-tenant patterns /
stricter-prod-than-staging variants, and lets unit tests construct a
policy with a trivial test pattern without monkey-patching module
globals. Code reviewers asking "what guard policy is in effect?"
read one struct.

The cost is a few extra characters per field. The benefit is
overridability and a clean encapsulation boundary.

### Q-C. Where do retrieval-guard knobs (top_k, max_context_tokens, metadata allowlists) live — on `RetrievalSettings`, on the policy, or passed directly into the orchestrator?

They live on `GuardrailPolicy`. `RetrievalSettings` remains the
operator-facing env-bound source, but the orchestrator
`apply_retrieval_guards(df, policy)` reads everything from the
already-compiled policy instance.

Four reasons.

**Symmetry with the existing distance threshold.** The relevance gate
already reads `policy.relevance_distance_threshold`, which is sourced
from `RetrievalSettings.distance_threshold` inside
`GuardrailPolicy.from_settings(...)`. The retrieval guards reuse that
same threshold for the per-chunk distance filter. Putting `top_k`,
`max_context_tokens`, and the metadata allowlists on the policy
alongside the threshold keeps every value the retrieval-side guards
look at in one struct, in the same shape, on the same import.

**Single source of truth + override seam (Q15 logic).** The policy is
already the place where guard knobs are normalized: regex strings get
compiled, wordlists get frozen, optional lists become `frozenset` or
`None`. Allowlists are exactly the same kind of thing — startup
work to convert `Optional[list[str]]` from settings into
`Optional[frozenset[str]]` for fast `in` checks, lowercased once.
Doing that work inside `from_settings(...)` means every consumer
gets the cooked, immutable form by construction, and tests can
build a custom policy with one-shot overrides
(`replace(policy, retrieval_top_k=2)` or rebuild via `from_settings`)
without monkey-patching settings.

**Frozen-set construction happens once.** The policy is built once at
app startup. Building `frozenset(retrieval.metadata_filetype_allowlist)`
on every `/chat` turn would be wasteful and would also force the
orchestrator to take settings as input — pulling the
`pydantic_settings.BaseSettings` import surface into a CPU-only
filter module that should not need it.

**Consumer uniformity.** Every guard module — `input_guards.py`,
`retrieval_guards.py`, and the upcoming `output_guards.py` — reads
exclusively from `policy: GuardrailPolicy`. None of them import
`RetrievalSettings` or `GuardrailSettings` directly. That is the
"least-privilege within the codebase" rule from Q12 applied
consistently: each guard module sees one type, not the whole
settings tree.

The cost is two more fields on the policy and four more lines in
`from_settings(...)`. The benefit is that the entire guardrails
package has exactly one input shape (`GuardrailPolicy`) and one
construction seam (`from_settings`). Phase 6's `output_guards.py`
follows the same convention.

### Q-D. When does the bot say "I don't have info on that" versus escalate to a human?

Three orthogonal signals decide it, gated by one operator policy. The
phrasing the user actually sees comes from a small lookup table over
the combined state.

**The three signals.**

| Signal | Type | Set by | Set when |
|---|---|---|---|
| `no_context` | Runtime fact | Python (retrieval guard) | Right after retrieval guards run — `len(chunks) == 0` |
| `enough_context` | LLM self-assessment | The LLM | While generating `ChatAnswer` — "did the chunks I got actually answer the question?" |
| `needs_human` | LLM-set flag | The LLM (via `escalate_to_human` tool + `ChatAnswer.needs_human`) | Whenever the LLM judges the user is asking for a human, is angry, is stuck, or the topic is out of policy bounds |

These three signals catch three different failure modes. `no_context`
is "retrieval found nothing" — cheap, deterministic, pre-LLM.
`enough_context=False` is "retrieval found something but it's
off-target" — only the LLM can spot this (e.g. asked about
shipping, retrieved warranty chunks). `needs_human` is "this should
not be answered by a bot at all" — judgment call by the LLM,
typically because the user explicitly asked or because the
conversation has clearly stalled.

**The policy switch.**

`refuse_when_no_context` (operator config, set at startup) decides
how strict the bot is when `no_context` fires. With it on, the bot
returns a canned out-of-scope reply and never calls the LLM. With it
off, the bot would fall back to general knowledge (not the v1
default — v1 ships with it on).

**Current implicit behavior (v1 — what Phase 6 + Phase 7 will wire).**

| Combined state | What the user sees | LLM called? |
|---|---|---|
| `no_context=True` AND `refuse_when_no_context=True` | Canned out-of-scope reply (`policy.relevance_out_of_scope_message`) | No — short-circuit before agent invocation |
| `enough_context=False` (regardless of `no_context`) | Soft off-ramp: "I couldn't confidently answer that — would you like to talk to a human?" — answer body suppressed | Yes (the answer was generated, then suppressed by output guards) |
| `enough_context=True` AND `needs_human=True` | The LLM's polite handoff message (the agent itself frames the transition after calling the `escalate_to_human` tool) | Yes — and the tool ran |
| `enough_context=True` AND `needs_human=False` | The LLM's grounded answer with citations | Yes |

The "I don't have info" path and the "let me get a human" path are
intentionally distinct user experiences. "I don't know" is a content
verdict (we have no documents that cover this); the soft off-ramp
on `enough_context=False` is *also* a content verdict but with a
human option dangled because the failure was less clear-cut;
`escalate_to_human` is a flow change initiated by the LLM after
weighing the conversation as a whole.

**Why escalation is LLM-judged in v1.**

Per Q1, escalation is a judgment call based on the whole turn's
context, and routing it through a tool gives schema-validated
reasons, side-effect cleanliness, and Langfuse traceability. v1
relies entirely on the LLM judging when to call the tool, with the
hardened system prompt (Phase 7) telling it the criteria. The output
guard (Phase 6) does not initiate escalation — it only respects the
LLM's `needs_human` flag and the canned refusal text.

**Deterministic triggers — deferred to Phase 7 alongside the system prompt.**

Pure-LLM judgment is the v1 baseline, but four deterministic triggers
are queued for Phase 7 as belt-and-suspenders:

1. **Keyword auto-escalate.** If the user message contains
   "human", "agent", "representative", "manager" (configurable
   wordlist on the policy), set `needs_human=True` regardless of
   what the LLM says. Customers asking explicitly should not be
   talked out of it by the model.
2. **Repeated-miss escalation.** Track per-session counts of
   `no_context` and `enough_context=False` turns in Redis (Phase 8
   memory layer). After N consecutive misses (default N=2), force
   `needs_human=True` instead of returning yet another soft
   off-ramp. The user is stuck; switching to a human is the right
   move.
3. **Topic-based escalation.** If the retrieved chunks' metadata
   indicates a topic that policy says must always go to a human
   (e.g., billing disputes, legal questions), force escalation.
   Reuses the metadata-allowlist machinery from Phase 5a/5b in
   inverted form (escalation list vs answer-from list).
4. **Sentiment trigger.** If a cheap sentiment classifier
   (off in v1; `gpt-4o-mini` judge or a small local model is the
   candidate) flags strong frustration / anger, force escalation.
   Highest-cost trigger to add; lowest priority of the four.

All four are *additive* on top of the LLM-judged path. None of them
override an LLM that already set `needs_human=True`; they only force
it on when the LLM didn't.

**Where each signal lives in code (for Phase 6/7 cross-reference).**

- `no_context`: set on `RetrievalGuardResult` by
  `apply_retrieval_guards` (Phase 5b — already shipped).
- `enough_context`: field on `ChatAnswer` (Phase 3 — already
  shipped). Output guard in Phase 6 reads it and decides whether to
  return the answer body or the soft off-ramp.
- `needs_human`: field on `ChatAnswer` (Phase 3 — already shipped).
  Set by the LLM, optionally as a result of calling the
  `escalate_to_human` tool (Phase 7). Phase 6 output guards do not
  modify it; they pass it through to `ChatResponse`.
- `refuse_when_no_context`: field on `GuardrailPolicy` (Phase 4 —
  already shipped). Read by the route handler (Phase 10) to decide
  the short-circuit before agent invocation.

The Phase 6 output guards therefore sit at exactly one of these
junctions: the `enough_context=False` soft off-ramp. They do not
touch the `no_context` short-circuit (that happens earlier, in the
route handler, before the agent runs) and they do not initiate
escalation (only honor it).

---

## 10. Where we are right now

- Architecture and decisions are **locked in**.
- **Phase 1 (settings additions): applied.** `ChatbotSettings`,
  `RetrievalSettings`, `GuardrailSettings`, `OpsSettings` exist in
  `app/config/settings.py` and the documented env-var overrides are
  appended to `app/example.env`.
- **Phase 2 (requirements.txt): applied.**
  `pydantic-ai-slim[openai]`, `pydantic-settings`, `slowapi`, and
  `python-json-logger` added to `requirements.txt`.
- **Phase 2.5 (BaseSettings migration): applied.** The four new
  settings classes (`ChatbotSettings`, `RetrievalSettings`,
  `GuardrailSettings`, `OpsSettings`) now inherit from
  `pydantic_settings.BaseSettings` with per-class `env_prefix`
  (`CHATBOT_`, `RETRIEVAL_`, `GUARDRAIL_`, `OPS_`), `env_file=".env"`,
  and `extra="ignore"`. The documented env-var overrides in
  `app/example.env` are now actually honored. The four pre-existing
  classes (`OpenAISettings`, `DatabaseSettings`, `RedisSettings`,
  `LangfuseSettings`) are intentionally left on `BaseModel` with
  `os.getenv` defaults — already working, no migration needed; can be
  unified later as a separate cleanup.
- **Phase 3 (chatbot/models.py): applied.** Created
  `app/chatbot/__init__.py` (package marker) and `app/chatbot/models.py`
  with four Pydantic schemas: `Citation`, `ChatAnswer` (mirroring the
  `SynthesizedResponse` pattern with `enough_context` + `thought_process`,
  plus `citations` and `needs_human`), `ChatRequest` (with UUID
  `session_id` and non-empty `message` validators), and `ChatResponse`
  (flattens `ChatAnswer` for client ergonomics, omits `thought_process`,
  adds `refused_reason` so clients can distinguish LLM-answered from
  guardrail-refused turns).
- **Phase 4 (chatbot/guardrails/__init__.py + policy.py + input_guards.py):
  applied.** Frozen `GuardrailPolicy` dataclass with `from_settings(...)`
  seam, precompiled PII/jailbreak `re.Pattern` tuples, frozen profanity
  wordlist, and all input + relevance + (parked) output thresholds in one
  immutable struct (Q15). `input_guards.py` ships sync per-guard pure
  functions (`check_length`, `check_language`, `check_pii_in_input`,
  `check_jailbreak`, `check_profanity`) plus the `evaluate_input(...)`
  orchestrator with deterministic short-circuit order, structured
  `GuardrailDecision` results for Langfuse, and a canned-message mapper.
  The async `relevance_gate(...)` lives in the same file but is kept
  separate from `evaluate_input` per Q13 so the route handler can call
  cheap sync guards first, then the network gate. Language guard ships
  as a permissive no-op stub in v1 (Q-B); activation deferred.
- **Phase 5a (policy.py additive extension): applied.** Added
  retrieval-side fields to `GuardrailPolicy`: `retrieval_top_k`,
  `retrieval_max_context_tokens`,
  `retrieval_metadata_filename_allowlist` as
  `Optional[frozenset[str]]`, `retrieval_metadata_filetype_allowlist`
  as `frozenset[str]`. Populated in `from_settings(...)` from the
  `RetrievalSettings` instance already passed in. Single source of
  truth for guard knobs is preserved (Q15) and the relevance-gate
  threshold field on the policy gives Phase 5b symmetry.
- **Phase 5b (chatbot/guardrails/retrieval_guards.py): applied.** New
  file with `RetrievedChunk` and `RetrievalGuardResult` Pydantic
  models; per-stage pure functions `_frame_to_chunks`, `_drop_empty`,
  `_filter_by_metadata`, `_filter_by_distance`, `_apply_top_k`,
  `_apply_token_budget`; `apply_retrieval_guards(...)` orchestrator
  that runs them in fixed order and returns a structured result with
  surviving chunks, `no_context` flag, running token total, and
  per-stage drop counts for Langfuse traces. Token counting via
  tiktoken `cl100k_base` encoder lazy-cached at module level. Sync
  CPU-only (mirrors `input_guards.py`'s sync guards; contrast with the
  async `relevance_gate`). All knobs read from the policy.
- **Phase 6 (chatbot/guardrails/output_guards.py): applied.** New
  file with `OutputGuardResult` Pydantic result type carrying the
  full metadata surface (Decision A: `answer`, `final_body`,
  `body_suppressed`, `dropped_citations`, `pii_replacement_counts`,
  `profanity_replacement_count`, `flagged_stages`); public per-stage
  pure functions `filter_citations`, `is_ungrounded`,
  `scrub_pii_in_text`, `scrub_profanity_in_text`; and an
  `apply_output_guards(...)` orchestrator running the five concerns
  in fixed order — citation integrity (silent drop, per-drop
  Langfuse log), grounding check (cheap structural:
  `enough_context=True ⇒ len(citations) >= 1`),
  `enough_context=False` / ungrounded soft-offramp suppression, PII
  scrub (`[redacted]` literal replacement), profanity scrub
  (token-level masking via `\b\w+\b` with `*` repeated to original
  length). Scrubs always run on `answer.answer` (Langfuse trace
  hygiene), gated by `policy.scrub_pii_in_output` /
  `policy.scrub_profanity_in_output`. Canned soft-offramp lives as a
  module-level constant (`_ENOUGH_CONTEXT_OFFRAMP`) mirroring
  `input_guards.py`'s `_REFUSAL_MESSAGES` style; promoting to a
  `GuardrailPolicy` / `GuardrailSettings` field is queued as a
  follow-up. Sync CPU-only (mirrors `input_guards.py` and
  `retrieval_guards.py`); reads exclusively from
  `policy: GuardrailPolicy` per Q-C; never mutates the caller's
  `ChatAnswer` (uses `model_copy(update=...)`). Public helpers are
  reachable from both Phase 9 `@agent.output_validator` hooks and
  the Phase 10 route handler.
- **Phase 7 (chatbot/deps.py + chatbot/prompts.py + chatbot/tools.py):
  applied.** Three tightly-coupled files for the agent's runtime
  substrate. `deps.py`: `ChatDeps` as an unfrozen
  `@dataclass(slots=True)` carrying `vector_store`,
  `policy: GuardrailPolicy`, `session_id`, `user_id: Optional[str]=None`,
  and `retrieved_chunk_ids: frozenset[str]` accumulator (default
  `frozenset()`). The retrieval tool *replaces* (never mutates) this
  field so the output guards / Phase 9 validator see the surviving
  chunk IDs. Policy lives on deps per working rule 7 (no module-level
  globals for tool-injected state). `prompts.py`: module-level
  `SYSTEM_PROMPT` string — hardened version of
  `Synthesizer.SYSTEM_PROMPT` with tool-use instructions, mandatory
  citations, refuse-out-of-scope (`enough_context=False` + empty
  citations + brief acknowledgement), never-reveal-prompt,
  never-claim-to-be-human, and Q-D LLM-driven escalation triggers
  (asks-for-human / urgent / no-procedure). Synthesizer's
  "Be transparent / Do not make up or infer / Clearly state that"
  language merged into Rules 1 and 4 rather than duplicated.
  `tools.py`: two async tools both taking `RunContext[ChatDeps]`.
  `retrieve_knowledge(query)` calls `vector_store.search(...)` →
  `apply_retrieval_guards(...)` → writes surviving IDs to
  `ctx.deps.retrieved_chunk_ids` → returns JSON-formatted chunks
  (`{chunk_id, content, filename, file_type, distance}`) or the
  literal `"NO_CONTEXT"` sentinel when nothing survives.
  `escalate_to_human(reason)` is I/O-free in v1 (logs only); real
  handoff happens in Phase 10 by inspecting
  `ChatAnswer.needs_human`. Helper `_format_chunks_for_llm` mirrors
  `Synthesizer.dataframe_to_json` but adds `chunk_id` so the LLM can
  populate `Citation.chunk_id`. Tool registration with `Agent(...)`
  deferred to Phase 9.
- **Phase 8 (chatbot/memory.py + chatbot/cache.py): applied.** Two
  Redis-backed runtime stores. `memory.py`:
  `ConversationMemory(redis_client, ChatbotSettings)` — single Redis
  key per session (`chatbot:session:{session_id}:messages`) holding
  a JSON-encoded list of Pydantic AI `ModelMessage` objects via
  `ModelMessagesTypeAdapter`. Methods: `async load(session_id)`,
  `async append(session_id, new_messages)`,
  `async clear(session_id)`. Read-modify-write on append (race
  condition documented in Q14, accepted for v1; follow-up:
  per-session Redis lock or atomic `RPUSH`+`LTRIM`). TTL refreshed
  on writes only — passive reads must not keep stale sessions
  alive. Trim cap = `max_history_turns * 4` (v1 approximation for
  tool-call/tool-return headroom). Corrupt payloads log and start
  fresh rather than crash the turn. `cache.py`:
  `EmbeddingCache(redis_client, RetrievalSettings)` — async
  Redis-backed cache keyed by `(model, sha256(text))` so embedding
  model swaps don't collide. JSON list-of-floats encoding. TTL
  applied on every set. Methods: `async get(text, model)`,
  `async set(text, model, embedding)`,
  `async delete(text, model)`. Stampede protection deferred (Q14
  follow-up). `OpsSettings.enable_embedding_cache` is honored at
  the call site (Phase 9/10), not inside the class. Wiring into
  `VectorStore.get_embedding_async` deferred to Phase 9/10.
- **Open follow-ups (deferred):**
  - Production-grade dependency pinning: generate `requirements-lock.txt`
    via `pip freeze` once the chatbot is feature-complete.
  - Add `psycopg2-binary` to `requirements.txt` so fresh installs don't
    fail on machines without `pg_config` (Postgres dev tools).
  - Rotate `OPENAI_API_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_PUBLIC_KEY`
    in `app/.env` (briefly exposed during Phase 2.5 verification).
  - Run end-to-end env-binding REPL test from `app/` directory to confirm
    `CHATBOT_*` / `RETRIEVAL_*` / `GUARDRAIL_*` / `OPS_*` overrides work.
  - **Session-level concurrency (Phase 8 follow-up):** when `memory.py`
    lands, decide between per-session-id Redis lock vs. atomic `RPUSH`
    + read-modify-write tolerance for the load-run-append cycle. The
    slowapi rate limiter is the v1 defense; correctness for double-send
    / retry-storm cases needs the lock pattern. See Q14.
  - **Language detection (Q-B):** v1 ships with `check_language` as a
    no-op stub. Activate when an operator needs a branded "we don't
    support that language" message, or when relevance-gate failures on
    non-English traffic become a UX problem. Detector library choice
    (lingua / langdetect / fasttext / cld3) deferred.
  - **Escalation deterministic triggers (Phase 7 follow-up):** to be
    wired alongside the system prompt + `escalate_to_human` tool after
    Q-D is captured. Candidates: keyword-based auto-escalate on
    "human"/"agent"/"representative"/"manager"; repeated-miss
    escalation after N consecutive `no_context` or
    `enough_context=False` turns; topic-based escalation by metadata;
    sentiment trigger.
- Next action: **Phase 9 — `app/chatbot/agent.py` (Pydantic AI Agent
  wiring).** Single file constructing the `Agent` instance:
  `model=ChatbotSettings.model`, `deps_type=ChatDeps`,
  `output_type=ChatAnswer`, `system_prompt=SYSTEM_PROMPT` (from
  `chatbot.prompts`), `tools=[retrieve_knowledge, escalate_to_human]`
  (from `chatbot.tools`), and a `UsageLimits(...)` carrying
  `max_tool_iterations` from `ChatbotSettings`. Plus
  `@agent.output_validator` hook(s) that wrap
  `apply_output_guards(...)` from Phase 6 — runs the
  citation-integrity / grounding / off-ramp / PII-scrub /
  profanity-scrub pipeline against the LLM's `ChatAnswer` and
  `ctx.deps.retrieved_chunk_ids` before the route handler ever sees
  it. Open Phase 9 design questions: (a) raise `ModelRetry` on
  grounding failure (force the LLM to retry with the same context)
  vs. silently apply the soft off-ramp; (b) whether to wire a
  keyword-based deterministic escalation auto-detector (Q-D
  follow-up) as a pre-validator on the user message inside Phase 9
  or push that to Phase 10 route logic. Per rule 3 the agent file
  will be drafted first and shown for approval before write.

---

*Document generated as a working note. All file paths are relative to the
project root.*
