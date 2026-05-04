# CustomerCareChatbot — RAG-based Conversational AI

A production-grade **customer care chatbot** built on top of a RAG (Retrieval-Augmented Generation) document ingestion pipeline. The chatbot holds multi-turn conversations, grounds every answer in ingested documents, applies guardrails at every phase of the pipeline, and is designed to mimic a real production architecture using local, open-source components.

Built with **Pydantic AI** as the agent framework, **FastAPI** for the HTTP layer, **TimescaleDB (pgvectorscale)** for vector search, **Redis** for session memory and caching, and **Langfuse** for full observability.

---

## What it does

The system has two sides: an **ingestion pipeline** that processes documents, and a **chatbot** that answers questions grounded in those documents.

### Document ingestion

1. **Upload** a PDF or DOCX via the REST API
2. **Parse** the document using Docling's structure-aware HybridChunker
3. **Embed** all chunks in a single batched OpenAI API call
4. **Store** vectors in TimescaleDB (pgvectorscale) with metadata

### Customer care chatbot

1. **Receive** a customer message via `POST /chat`
2. **Guard the input** — length check, PII detection, jailbreak detection, language check, relevance gate (LLM-judge, optional)
3. **Retrieve** the most relevant document chunks from the vector store (with retrieval guardrails: distance threshold, file-type allowlist, token budget)
4. **Run the Pydantic AI agent** — GPT-4o reasons over retrieved context and conversation history, calls tools as needed
5. **Guard the output** — citation integrity check, grounding check, PII scrub, profanity scrub
6. **Persist** the new turn to Redis (session memory with configurable TTL)
7. **Return** a structured response with answer, citations, and confidence flag

---

## Tech stack

| Layer | Technology |
|---|---|
| Language | Python 3.13 |
| API | FastAPI + uvicorn |
| Agent framework | Pydantic AI |
| LLM | OpenAI `gpt-4o` (chat), `gpt-4o-mini` (guardrail judge) |
| Embeddings | OpenAI `text-embedding-3-small` (batched) |
| Background ingestion | Celery + Redis |
| Vector database | TimescaleDB (pgvectorscale) in Docker |
| Session memory | Redis (turn history, TTL-based expiry) |
| Response cache | Redis (embedding + retrieval cache) |
| Document parsing | Docling |
| Chunking | Docling HybridChunker + tiktoken |
| Observability | Langfuse |

---

## Project structure

```
CustomerCareChatbot-RAGbased/
├── docker/
│   └── docker-compose.yml          # TimescaleDB + Redis
├── app/
│   ├── main.py                     # FastAPI entrypoint
│   ├── worker.py                   # Celery entrypoint
│   ├── pipeline.py                 # IngestionPipeline orchestrator
│   ├── config/
│   │   └── settings.py             # All settings (Pydantic, env-driven)
│   ├── database/
│   │   └── vector_store.py         # Sync + async TimescaleDB clients
│   ├── services/                   # Ingestion helpers
│   │   ├── document_processor.py
│   │   ├── chunker.py
│   │   ├── llm_factory.py
│   │   └── synthesizer.py
│   ├── api/routes/                 # HTTP route handlers
│   │   ├── ingest.py               # POST /ingest
│   │   ├── query.py                # POST /query (stateless Q&A)
│   │   ├── documents.py            # GET /documents
│   │   └── jobs.py                 # GET /jobs/{job_id}
│   └── chatbot/                    # ← Customer care chatbot (new)
│       ├── agent.py                # Pydantic AI Agent singleton
│       ├── deps.py                 # ChatDeps — dependency injection
│       ├── models.py               # ChatAnswer and request/response models
│       ├── prompts.py              # System prompt
│       ├── tools.py                # retrieve_knowledge, escalate_to_human
│       ├── memory.py               # Redis-backed session memory
│       ├── cache.py                # Embedding + retrieval response cache
│       └── guardrails/
│           ├── input_guards.py     # Length, PII, jailbreak, language, relevance
│           ├── retrieval_guards.py # Distance threshold, token budget, allowlist
│           ├── output_guards.py    # Citation integrity, grounding, PII/profanity scrub
│           └── policy.py          # GuardrailPolicy — immutable config object
├── data/
│   └── faq_dataset.csv             # Seed data for vector store
├── docs/
│   ├── architecture.md             # File layout and layering rules
│   ├── chatbot-technical-reference.md  # Pydantic AI, RunContext, Deps, connection pools
│   └── customer-care-chatbot-design-notes.md  # Full design decisions log
├── requirements.txt
├── app/example.env                 # Template — copy to app/.env and fill in keys
└── LICENCE
```

---

## How to run

### Prerequisites

- Docker Desktop
- Python 3.13
- OpenAI API key
- Langfuse account (free tier works; used for observability)

### 1 — Copy and fill in environment variables

```bash
cp app/example.env app/.env
# Open app/.env and fill in your OpenAI and Langfuse keys
```

### 2 — Start Docker (TimescaleDB + Redis)

```bash
cd docker && docker-compose up -d
```

### 3 — Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 4 — Start the FastAPI server

```bash
cd app
python -m uvicorn main:app --port 8888
```

Swagger UI: **http://127.0.0.1:8888/docs**

### 5 — Start the Celery worker (separate terminal)

```bash
cd app
celery -A worker worker --loglevel=info --concurrency=2
```

> Use `--concurrency=2` for local dev to keep DB connection count low.

---

## API endpoints

### Ingestion

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/ingest` | Upload a PDF or DOCX — returns a `job_id` instantly |
| `GET` | `/jobs/{job_id}` | Poll ingestion status (`PENDING / STARTED / SUCCESS / FAILURE`) |
| `GET` | `/documents` | List all ingested documents with chunk counts |

### Chatbot

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/chat` | Send a message — returns a grounded answer with citations |
| `POST` | `/query` | Stateless single-shot Q&A (no session memory) |

### Example: ingest a document

```bash
curl -X POST http://127.0.0.1:8888/ingest \
  -F "file=@yourfile.pdf"
# Returns: { "job_id": "abc-123", "filename": "yourfile.pdf", ... }
```

### Example: chat

```bash
curl -X POST http://127.0.0.1:8888/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "What is your return policy?",
    "session_id": "user-session-42"
  }'
# Returns: { "answer": "...", "citations": [...], "enough_context": true }
```

---

## Guardrails — what gets checked and when

The chatbot applies guardrails at every stage of the pipeline, not just at the input or output.

**Input guards** (before the agent runs):
- Message length (min/max character limits)
- PII detection — blocks inputs containing phone numbers, emails, or ID-like patterns
- Jailbreak detection — pattern-based check for prompt injection attempts
- Language check — English only by default (configurable)
- Relevance gate — optional LLM-judge call (`gpt-4o-mini`) to reject off-topic messages

**Retrieval guards** (before context is passed to the agent):
- Distance threshold — chunks beyond the similarity cutoff are dropped
- File-type allowlist — only chunks from permitted document types are returned
- Token budget — retrieved context is trimmed to fit within the max token limit

**Output guards** (inside the Pydantic AI output validator):
- Citation integrity — drops any cited `chunk_id` not in the retrieved set for this turn
- Grounding check — if `enough_context=True` but no valid citations remain, fires a `ModelRetry` (one retry allowed)
- Soft off-ramp — when context is insufficient, suppresses the LLM body and returns a canned response
- PII scrub — regex replacement on the generated answer
- Profanity scrub — token-level masking on the generated answer

---

## Key design decisions

**Pydantic AI as the agent framework**
The chatbot uses Pydantic AI rather than LangChain or bare `instructor`. Pydantic AI's `Agent`, `RunContext`, and `deps_type` pattern keeps every external resource (vector store, Redis, policy config) out of module-level globals and injected cleanly per request. This makes the agent trivially testable via `agent.override(deps=FakeDeps())`.

**Guardrails at every phase**
Rather than a single input/output filter, guards run at four distinct stages: input, retrieval, output (inside the agent validator), and conversation (rate limiting, session turn cap). This layered approach catches different failure modes at the point where they're cheapest to handle.

**Session memory in Redis**
Conversation history is stored in Redis keyed by `session_id`, with a configurable TTL (default 1 hour). The route handler loads history before calling `agent.run(message_history=...)` and persists the new turn immediately after. History never lives in the agent or in module-level state.

**Two execution paths kept strictly separate**
The FastAPI path is fully async (`AsyncOpenAI`, async TimescaleDB client). The Celery ingestion path stays sync (`OpenAI`, sync TimescaleDB client). Each path has its own clients — no shared state, no event loop conflicts.

**Batch embeddings**
All chunks in a document are embedded in a single OpenAI API call rather than one call per chunk. For a 50-chunk document this is 1 round-trip instead of 50.

**One ModelRetry on grounding failure**
If the LLM returns `enough_context=True` but cites no valid chunks, the output validator raises `ModelRetry` with a targeted correction message. The agent retries once. If the retry also fails, `UnexpectedModelBehavior` is caught by the route handler, which returns a safe canned response.

---

## Database connection (TablePlus or any Postgres client)

| Field | Value |
|---|---|
| Host | localhost |
| Port | 5435 |
| User | postgres |
| Password | password |
| Database | postgres |
