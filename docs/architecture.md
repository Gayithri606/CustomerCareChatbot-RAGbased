# Architecture — where does each file live and why

A reference for deciding where new code goes. Grounded in the actual layout of `app/`.

## Current layout

```
app/
├── main.py                  # FastAPI entrypoint (uvicorn launches this)
├── worker.py                # Celery entrypoint (celery launches this)
├── pipeline.py              # IngestionPipeline — orchestrator
├── similarity_search.py     # dev-time script (not part of the running app)
├── insert_vectors.py        # dev-time script (seeds FAQ data)
├── api/
│   └── routes/              # HTTP route handlers (ingest, query, documents, jobs)
├── services/                # single-responsibility helpers
│   ├── chunker.py
│   ├── document_processor.py
│   ├── synthesizer.py
│   └── llm_factory.py
├── database/
│   └── vector_store.py      # talks to TimescaleDB / pgvector
└── config/
    └── settings.py
```

## The three reasons something lives at the top of `app/`

The top level mixes three different roles. Recognising which role a file plays is the key to the layout.

**1. Entrypoints — processes you launch**
- `main.py` is what `uvicorn app.main:app` runs.
- `worker.py` is what `celery -A worker` runs.

Entrypoints sit one level up from the modules they import. They are the "front doors" of the app.

**2. Orchestrators — coordinators, not leaves**
- `pipeline.py` (`IngestionPipeline`) does not do one thing. It pulls `DocumentProcessor`, `Chunker`, and `VectorStore` together into a flow.

A service is a leaf (chunker chunks, synthesizer synthesizes). An orchestrator is the conductor. With only one orchestrator, putting it next to the entrypoints is fine. Once there are 3+, promote it to `app/pipelines/`.

**3. Dev-time scripts — not part of the running app**
- `similarity_search.py` and `insert_vectors.py` are command-line scratch scripts. They live in `app/` only because that is where they were written. A cleaner home is `scripts/` at the repo root.

## Where does a new file go? — decision order

Ask these in order. The first "yes" wins.

1. **Is it an entrypoint?** (a process you launch) → top of `app/`.
2. **Does it do one thing with a clear name?** (chunk, embed, synthesize, parse) → `services/`.
3. **Does it talk to a specific external system?** (DB, vector store, S3, an LLM API) → `database/`, `storage/`, `clients/`.
4. **Does it handle HTTP?** → `api/routes/`.
5. **Does it coordinate several of the above?** → orchestrator. Top of `app/` until there are 3+, then make a folder.
6. **Is it a one-off or dev-time script?** → `scripts/` at the repo root, not `app/`.

## When to make a new folder

Trigger: **3 files of the same kind**. Not 1. Not 2.

`services/` earned a folder because there are 4 files in it. `pipeline.py` is alone, so it does not. Premature folders are as confusing as missing ones — they imply a category that does not yet exist.

## Quick "what is this file?" test

If you cannot finish this sentence in five words, the file is doing too much:

> "This file is responsible for ___."

Examples from this repo:
- `chunker.py` — splitting documents into chunks.
- `synthesizer.py` — turning context + question into an answer.
- `vector_store.py` — talking to the vector database.
- `pipeline.py` — coordinating ingestion end-to-end.
- `main.py` — wiring the FastAPI app.
- `worker.py` — running Celery background tasks.

If a file's answer is "a bunch of stuff" or has an "and" in it, split it.

## Layering rule (who can import whom)

Imports flow downward. A lower layer must not import from a higher one.

```
entrypoints  (main.py, worker.py)
   ↓ import
routes  (api/routes/*)
   ↓ import
orchestrators  (pipeline.py)
   ↓ import
services  (services/*)
   ↓ import
infrastructure  (database/, config/)
```

If `services/chunker.py` ever needs to import from `api/routes/`, something is wrong — the dependency is pointing the wrong way. The fix is usually to pass the needed value in as an argument instead of reaching upward.
