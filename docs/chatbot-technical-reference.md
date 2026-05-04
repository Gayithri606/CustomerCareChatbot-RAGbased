# Technical Reference — Connection Pools, Pydantic AI Agents, RunContext & Deps

A standalone reference for the technical concepts that came up while
planning the customer-care chatbot extension. Use this when you want a
quick refresher on **how** these pieces work, separately from the
project-specific design plan.

---

## 1. Database connection pools (TimescaleDB / Postgres)

### 1.1 What a connection pool is

A connection pool is a small, application-managed set of pre-opened
database connections kept alive in memory. When code needs to run a query
it borrows an idle connection, runs the query, then returns the connection
to the pool — **the connection itself is not closed.**

The reason this matters is that opening a fresh Postgres connection is not
cheap. Each new connection involves:

- A TCP handshake.
- TLS negotiation if SSL is on.
- Postgres authentication.
- Server-side session setup (a backend process is forked on the server).

Combined, this is typically 10–50ms of overhead per new connection. If
your application opened a fresh connection for every query, you'd waste
that overhead on every call. A pool amortises it across many queries.

### 1.2 Default sizes (rough numbers)

Defaults vary by library, but typical values are:

- **Minimum idle:** 2–4 connections kept warm at all times.
- **Maximum:** 10–20 connections per pool by default.
- **Postgres server-side ceiling:** `max_connections` is usually 100 out of the box.

Both `psycopg` (sync) and `psycopg`/`asyncpg` (async) maintain pools
internally. The `timescale-vector` client library wraps these. You don't
typically interact with the pool directly — you just call `.search()` or
`.upsert()` and the library borrows/returns a connection for you.

### 1.3 Behaviour under threads (single process)

Python's GIL is **not** a bottleneck for database I/O. When a thread is
waiting on a network round-trip to the database, it releases the GIL, so
other threads can run. Concretely:

- 8 threads each calling `.search()` can run their queries in parallel.
- Each thread borrows its own connection from the pool while it's working.
- If 12 threads ask but the pool max is 8, the extra 4 wait their turn.
- The actual parallelism comes from the database doing 8 query plans
  simultaneously — Python is mostly idle, just waiting for I/O.

So thread-based parallelism scales well as long as `pool_max ≥ thread count`.

### 1.4 Behaviour under async (single process)

The async pattern looks different but has the same effect:

- All coroutines run on a single thread (the event loop).
- A coroutine awaiting on a query releases the loop while waiting.
- Many coroutines can be in-flight at once, each holding a borrowed
  connection only during the actual `await` window.
- A small pool (say 4 connections) can serve dozens of concurrent
  coroutines because each coroutine only holds a connection for the
  millisecond-scale duration of its query.

This is why the async client typically needs a smaller pool than a
thread-pool design would.

### 1.5 Behaviour across processes (the one that bites people)

**Each Python process has its own pool. Pools never span process
boundaries.** This is true regardless of whether the processes are:

- A FastAPI server with multiple uvicorn workers.
- Multiple Celery worker child processes (forked by `celery worker --concurrency=N`).
- A separate cron job, script, or notebook.

So total Postgres connections at peak across your whole system =
sum of every per-process pool's `max_size`.

Worked example:

| Component | Processes | Pool max each | Connections at peak |
|---|---|---|---|
| FastAPI (uvicorn `--workers=1`) | 1 | 10 | 10 |
| Celery (`--concurrency=8`) | 8 | 10 | 80 |
| Cron job | 1 | 5 | 5 |
| **Total** | | | **95** |

That's already brushing Postgres's default ceiling of 100, leaving no
headroom for `psql` debugging, monitoring, or backups. The fix is to
**size each pool with the total in mind**, not in isolation. A common
guideline: `(per-process pool_max) × (total processes) < (postgres max_connections − 20)`.

### 1.6 Project-specific notes

In this project:

- The **FastAPI process** instantiates `VectorStore()` once at startup
  (a module-level singleton). One process → one pool → ~10 connections.
- Each **Celery worker child process** instantiates its own `VectorStore`
  inside `IngestionPipeline.__init__()`. So `--concurrency=8` means up
  to 8 separate pools.
- For local dev, set `celery -A worker worker --concurrency=2` to keep
  total connections low while you iterate.
- The library used (`timescale-vector`) wraps psycopg connection
  management — you don't need to tune pools manually for most workloads.

---

## 2. Pydantic AI Agent — the essentials

### 2.1 The Agent is the central object

You construct an Agent in your code. Pydantic AI does **not** ship a
preconfigured one. Think of the Agent as a configured worker: you specify
its model, its instructions, its tools, the type of dependencies it will
receive, and the schema of the answer it must produce.

### 2.2 Five parameters that matter when constructing an Agent

```python
from pydantic_ai import Agent

agent = Agent(
    model="openai:gpt-4o",            # 1. which LLM
    system_prompt="You are a ...",    # 2. instructions / role
    deps_type=ChatDeps,                # 3. the type of run-time deps
    output_type=ChatAnswer,            # 4. structured output schema
    # plus tools registered via @agent.tool decorator
)
```

| Parameter | What it does |
|---|---|
| `model` | The provider:model string. Pydantic AI handles the API call. |
| `system_prompt` | Pinned instructions that frame every run. Can be static or dynamic. |
| `deps_type` | The type of the dependencies object you'll pass at run time. The framework uses it for type-safe tool access. **You design this class yourself.** |
| `output_type` | A Pydantic model that the final answer must match. Pydantic AI validates and retries on schema failure. |
| Tools | Functions registered with `@agent.tool` (or passed in `tools=[...]`). The LLM can decide to call them. |

### 2.3 Two channels of context at run time

When you call the agent, two pieces of run-time context go in through
different doors:

```python
result = await agent.run(
    user_message,
    deps=chat_deps,                    # for tools (via RunContext)
    message_history=past_turns,         # for the conversation context
)
```

- `deps=` is consumed by tools.
- `message_history=` is consumed by the framework to build the prompt.

Don't conflate them. Don't put history in deps. Don't put tool resources
in `message_history`.

### 2.4 The agent's run loop (mental model)

When `agent.run()` is invoked, conceptually Pydantic AI does:

1. Build the prompt from `system_prompt` + `message_history` + the user message.
2. Call the LLM.
3. If the LLM responds with a tool-call, execute the tool (pass it a
   `RunContext` with your `deps`), feed the tool's return value back in,
   and call the LLM again.
4. Repeat until the LLM returns a final answer.
5. Validate the final answer against `output_type`. Retry if invalid.
6. Return the validated result.

Steps 3–4 are why `RunContext` exists — the framework needs a clean way
to pass your deps into each tool call as the loop runs.

---

## 3. RunContext — what it is and when to use it

### 3.1 The framework constructs it; you don't

You never write `RunContext(...)` yourself. When you call
`agent.run(message, deps=my_deps)`, Pydantic AI internally wraps `my_deps`
in a `RunContext[YourDepsType]` and passes it to every tool. Your job is
to declare the parameter on your tool function, not to build the object.

### 3.2 What's inside `ctx`

The `RunContext` is a small carrier object. The fields you'll typically
use:

- `ctx.deps` — your dependencies object, exactly as you passed it to
  `agent.run(deps=...)`. This is the main attraction.
- `ctx.usage` — a running tally of tokens used so far in this run.
  Useful for enforcing budgets inside a tool.
- `ctx.model` — info about the active model. Rarely needed.
- `ctx.retry` — current retry attempt count for this tool call.
- `ctx.prompt` / `ctx.messages` — the current conversation messages.
  Read-only; rarely needed in a tool.

### 3.3 When a tool should take `RunContext`

Two simple rules:

| Tool's needs | Take `ctx`? |
|---|---|
| Needs an external resource (DB, HTTP client, vector store) | **Yes** |
| Needs request-scoped state (session_id, user_id, request_id, logger) | **Yes** |
| Needs framework-managed info (current usage, retry count) | **Yes** |
| Pure computation on its arguments only (e.g. add two numbers) | **No** |

### 3.4 Tool signatures with and without `RunContext`

```python
# Needs deps -> takes ctx
@agent.tool
async def retrieve_knowledge(
    ctx: RunContext[ChatDeps], query: str
) -> list[Chunk]:
    return await ctx.deps.vector_store.search(query)

# Pure computation -> no ctx
@agent.tool_plain
def add_numbers(a: int, b: int) -> int:
    return a + b
```

Pydantic AI provides two decorators: `@agent.tool` (gets a `RunContext`)
and `@agent.tool_plain` (no context). Use whichever matches your needs.

---

## 4. The Deps class — design and intent

### 4.1 Deps is your class, not a framework class

`ChatDeps` (or whatever you name it) is a class **you** define. Typically
a `dataclass` or a Pydantic `BaseModel`. The framework only knows about
its **type** (so it can make `ctx.deps` strongly-typed); it doesn't
construct it for you and doesn't impose a structure on it.

```python
from dataclasses import dataclass
from app.database.vector_store import VectorStore

@dataclass
class ChatDeps:
    vector_store: VectorStore
    session_id: str
    user_id: str | None = None
```

### 4.2 What goes in deps

Anything a tool needs from outside its own arguments. Typical members:

- DB / HTTP / vector-store clients.
- Configuration (settings, feature flags, thresholds).
- Per-request context (session_id, user_id, request_id, tenant_id).
- Pre-warmed caches or in-memory state for the request.
- Loggers configured with request-scoped fields.

### 4.3 What does NOT go in deps

- **Conversation history.** Pydantic AI handles it via the dedicated
  `message_history=` parameter on `agent.run()`. Don't duplicate it.
- **The current user message.** That's the prompt argument to `agent.run()`.
- **Per-tool internal state.** Tools should be stateless functions; state
  belongs either in deps (request-scoped) or in external storage
  (session-scoped, in Redis/DB).

### 4.4 Why we put `vector_store` in deps (the four reasons)

1. **Cost of construction.** A `VectorStore` opens a DB pool plus OpenAI
   clients. Build it once at app startup and pass a reference; don't
   reconstruct it per tool call.
2. **Testability.** With deps, unit tests use
   `agent.override(deps=ChatDeps(vector_store=FakeVectorStore()))` and
   never hit the DB or OpenAI.
3. **No hidden globals.** The function signature reveals every external
   dependency. No reaching into module-level singletons or globals.
4. **Per-request flexibility.** Different requests can use different
   stores (multi-tenant, A/B testing). The tool body doesn't change.

### 4.5 The lifecycle of deps in a single chat turn

1. The HTTP route handler receives the request.
2. The handler builds a `ChatDeps` instance for this request, pulling
   the singleton `VectorStore` from app state and adding the session_id
   etc. from the request body.
3. The handler calls `agent.run(message, deps=chat_deps, message_history=...)`.
4. Pydantic AI wraps `chat_deps` in a `RunContext`.
5. Each time the LLM decides to call a tool, the framework hands the
   `RunContext` to the tool. The tool reads `ctx.deps.<thing>` and does
   its work.
6. When the agent returns, the request is done. The `ChatDeps` instance
   is discarded. The next request builds a fresh one.

So deps are **per-request**, not per-process and not persistent.

---

## 5. Celery worker count — defaults and tuning

### 5.1 Two layers of "worker"

The word "worker" overloads two things in Celery:

1. **A `celery worker` command** — one OS process you launched (per
   terminal, per Docker container, per systemd unit).
2. **The child processes inside that command** — controlled by
   `--concurrency=N`. Each child is what actually picks up and runs a
   task.

A single `celery -A worker worker --concurrency=8` command on an 8-core
machine gives you 1 main supervisor process + 8 child worker processes
that can run 8 tasks in parallel.

### 5.2 Default if you configure nothing

Celery picks `os.cpu_count()` for `--concurrency`. On a 10-core MacBook
that silently spins up 10 child processes, each instantiating its own
`VectorStore` and its own DB connection pool. Nobody pre-configures it
for you — Celery's built-in default is the only thing in play.

### 5.3 Three places to configure it

Pick one:

- **CLI (simplest, per-launch):** `celery -A worker worker --concurrency=4`
- **In code (project default):** `celery_app.conf.update(worker_concurrency=4)`
- **Environment variable (12-factor / production):** `CELERY_WORKER_CONCURRENCY=4`

### 5.4 Sensible numbers

| Scenario | Suggested concurrency |
|---|---|
| Local dev / iterating | 2 |
| Real ingestion workload (I/O- and CPU-bound mix) | 4–8 |
| Pure I/O-bound, no CPU pressure | Higher is fine; bottleneck moves to DB |

Going beyond physical cores rarely helps for CPU-bound work like Docling
parsing — it just multiplies DB connections.

### 5.5 The connection-pool budget formula

Total Postgres connections at peak:

```
total =
    (uvicorn workers * FastAPI pool_max)
  + (celery commands * --concurrency * Celery pool_max)
  + (any cron / scripts / debug sessions)
```

Constraint: `total < postgres max_connections − headroom`.

Default Postgres `max_connections` is 100. Reserve ~20 for headroom
(monitoring, `psql`, backups). So aim for `total ≤ 80`.

---

## 6. Putting it all together — one mental model

When `POST /chat` arrives:

1. The route looks up the singleton `VectorStore` from app state (one
   pool per FastAPI process, established at startup).
2. The route builds a `ChatDeps(vector_store=..., session_id=..., user_id=...)`
   for this request only.
3. The route calls `agent.run(message, deps=chat_deps, message_history=...)`.
4. Pydantic AI wraps `chat_deps` in a `RunContext` and starts its loop.
5. The LLM decides to call `retrieve_knowledge(query=...)`. Pydantic AI
   passes the `RunContext` in. The tool reads `ctx.deps.vector_store`
   and calls `search()`. Behind the scenes, the vector store borrows a
   connection from the pool, runs the query, returns the connection.
6. The agent finishes, the route persists the new turn to Redis, and
   responds. `ChatDeps` is discarded; the next request builds fresh.

That's the entire mental model. Three concepts working together: **deps
inject what tools need**, **RunContext is the framework-built carrier
that delivers them**, and **connection pools live for the lifetime of the
process** so they can be safely shared across many requests.

---

*Reference document. Update as we encounter new clarifying questions
during implementation.*
