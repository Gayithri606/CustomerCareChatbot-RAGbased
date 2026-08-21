# Technical Q&A — Learnings Log

Questions asked during development, with the answers that clarified them.
Companion to `customer-care-chatbot-design-notes.md` section 9 (Q1–Q15,
Q-A–Q-D) — this file continues the habit for the Phase 10+ work. Entries
are numbered L1, L2, … and written to be understandable at a glance months
later.

---

## L1. Who actually does what in a vector search?

The embedding model does NOT search. Three parties, three jobs:

| Who | Does what |
|---|---|
| OpenAI embedding model (`text-embedding-3-small`) | Translation only: text → 1536 numbers. The only AI step, the only OpenAI call. Never sees the database. |
| Our Python code | Orchestration: send text to OpenAI, send the returned vector to the DB, collect results. Does no comparing itself. |
| TimescaleDB / pgvectorscale | The actual search: distance math (`ORDER BY embedding <=> query_vector`) over stored vectors, accelerated by an index. Pure arithmetic, no AI. |

Why it matters: the embedding is the cacheable/costable part (external API
call); the search is fast and local. This is also why the relevance gate is
"cheap" — one translation + one DB query, no reasoning model involved.

---

## L2. `cheap_model` vs the embedding model — two different "cheap" things

- The relevance gate uses the **embedding model** (`text-embedding-3-small`):
  embed the message, compare a distance number to a threshold. No chat model
  in that path.
- `ChatbotSettings.cheap_model` (`gpt-4o-mini`) is a small **chat/reasoning
  model**, configured since Phase 1 and never called by any code until the
  query condenser (ADR-001).
- Gotcha: the README/design notes mention an "optional LLM-judge
  (gpt-4o-mini)" for the gate. The `enable_llm_judge` settings flag exists,
  but no code ever implemented or read it. Documentation described an
  intention, not the code. Verify with: `grep -rn "cheap_model" app/`.

Learning: when docs and code disagree, grep is the referee.

---

## L3. Why the condenser must NOT receive Pydantic AI `message_history`

Every agent run sends the model a message list; the first (system) message
defines the model's job. Pydantic AI adds the agent's own system prompt
ONLY when no `message_history` is passed. If history is passed, it assumes
the history is already a complete conversation — including its system
prompt — and does not add this agent's own.

Concretely, passing the chatbot's history to the condenser sends:

    1. System: "You are a customer-care AI assistant..."   ← chatbot's job
    2. User: "What CFM do I need for a range hood?"
    3. Assistant: "For a gas grill, 1,200 CFM..."
    4. User: "And what about ductwork for that?"

The condenser's own instructions appear nowhere — so gpt-4o-mini reads
"I'm a customer-care assistant" and ANSWERS the ductwork question (with no
retrieval, no guardrails), instead of rewriting it. You wanted a rewritten
question; you got a second, worse chatbot.

Fix used in `condenser.py`: render the history as plain text INSIDE the
condenser's prompt. The transcript becomes reference material for the
rewrite job, not a conversation to continue.

---

## L4. Fail-open vs fail-closed

When a component errors, choose what the failure does downstream:

- **Fail-closed**: error → stop everything. Bank vault door — if the
  mechanism breaks, it stays locked.
- **Fail-open**: error → step aside, let things continue. Supermarket
  door — if the sensor breaks, it stays open, because trapping people
  inside is worse than one unscanned entry.

Pick by asking: which is worse here — continuing without the component, or
halting?

In this codebase:
- **Condenser: fail-open.** If gpt-4o-mini errors, return the raw message
  and continue. Worst case: the gate misjudges one follow-up. A broken
  optional polish step must never take the chat surface down.
- **Relevance gate: fail-open** (pre-existing) — a flaky DB shouldn't kill
  chat; the agent has its own retrieval guards downstream.
- **Input guards: fail-closed.** A PII or jailbreak hit stops the turn.
  Continuing (leaking PII to the LLM) is worse than stopping.

Rule of thumb: helpers fail open; safety checks fail closed.

---

## L5. Caching fixes cost, never correctness

Asked during the relevance-gate debate: "if the Redis embedding cache were
wired up, would keeping the (history-blind) gate be okay?"

No. The cache makes the gate's duplicate embedding cheap; it does not make
the gate's wrong refusal right. A cached wrong answer is still a wrong
answer — just faster. Also practical: the cache keys on exact text
(sha256), and the gate embeds the customer's raw words while the agent
searches with its own rephrasing — different text, different key, few hits
between the two anyway.

"Would a cache save it?" turned out to be the question that separated the
gate's two problems (cost vs correctness) cleanly.

---

## L6. In production, off-topic messages are common — and the defense is layered

Demo intuition said off-topic questions are rare. Production reality:
customer-facing bots get a steady stream of greetings, chitchat, venting,
probing, wrong-department questions, and spam — a large minority of
traffic. No single component handles this; layers do, each catching what
the cheaper layer above it missed:

| Layer | Catches | Cost per message | Status |
|---|---|---|---|
| Rate limiting (slowapi) | volume abuse | ~free | planned (Guardrail G) |
| Input guards | jailbreak, PII, length | ~free | built |
| Repeated-miss counter (Q-D trigger 2) | repeated off-topic from one session | one Redis INCR | designed, not built |
| Condense + relevance gate (ADR-001) | individual off-topic, incl. follow-ups | one cheap-model call + one embed | chosen, being built |
| Retrieval guards → NO_CONTEXT → off-ramp | anything that slips through | one GPT-4o call | built |

Key insight on repeat offenders: you don't need to detect bad questions
upfront — each off-topic turn already ends in a refusal signal
(`enough_context=False` / `NO_CONTEXT`). Count consecutive refusals per
session in Redis; after N, stop calling the agent and offer a human. The
refusals themselves are the detector.

---

## L7. "On disk" vs "in git" are different kinds of added

A file written to the project folder is immediately real to Python — the
import system reads the filesystem and knows nothing about git. `git add`
only tells git to start tracking the file for the next commit. This is why
`from api.routes import chat` worked while `git status` still showed
chat.py as untracked — and why the right order is: write file → verify it
imports/runs → THEN commit. Verify before it enters history.

---

## L8. Folder depth is geography; layers are the import direction

`database/` and `chatbot/` sit at the same directory depth inside `app/` —
yet `chatbot/` is the *higher* layer. Layering has nothing to do with
where folders sit; it is defined by which way the import arrows point
(docs/architecture.md: entrypoints → routes → orchestrators → services →
infrastructure).

What makes a module "low" is how little it needs to know. `vector_store.py`
imports only config + libraries — it could be copied into another project
unchanged (it literally was). `chatbot/tools.py` drags along deps, policy,
guardrails, and vector_store — it knows everything beneath it. **The
module many things depend on is low; the module that depends on many
things is high.**

Why breaking the rule is a real crash, not just style: `chatbot/deps.py`
already imports `database.vector_store`. If `vector_store.py` imported
`chatbot.cache`, loading either package would start loading the other
while half-built — a circular import, which can die with `ImportError:
cannot import name ... (most likely due to a circular import)` depending
on load order. Fragile in the worst way: works today, breaks when someone
reorders an import.

The fix used for the embedding cache: **dependency injection** — chat.py
(already above both) builds the cache and passes it into
`VectorStore(embedding_cache=...)`. The low layer accepts "some object
with async get/set" and imports nothing from above.

Quick test for "who's higher?": if I deleted this folder, which other
folders would stop importing? Delete `chatbot/` → `database/` doesn't
notice. Delete `database/` → `chatbot/` breaks. The survivor is the lower
layer.

---

## L9. Anatomy of a caught shortcut: claim vs receipts (the repeated-question failure)

Verification test: repeat a question the bot answered two minutes earlier,
word for word. Result: `agent:ungrounded_after_retry` safe fallback. The
log tells the whole story — and every guard behaved correctly.

**What happened:** the model saw its previous answer in conversation
history, judged it already knew, and **skipped calling retrieve_knowledge
entirely** (evidence: no `tool_retrieve_knowledge` line in that turn's
log). It recited its old answer and cited the old chunk IDs from memory.

**Q: Who set `enough_context=True` if no retrieval happened?**
The model itself — `enough_context` is a self-assessment field in the
LLM's structured output, not something code computes. From the model's
view it *did* have context (its own history). Meanwhile
`retrieved_chunk_ids` is Python-side bookkeeping written only by the
tool — this turn, an empty set. Two views of one turn: the model's
**claim** vs Python's **receipts**. The output validator exists to compare
them.

**Q: How does "True with zero valid citations" arise?**
The model cited two chunk IDs — stale ones from the earlier turn.
Citation-integrity checked each against this turn's (empty) retrieved set
and dropped both. Zero *valid* citations is produced by the filter, not by
the model citing nothing.

**Q: Does that combination retry?**
Yes — `enough_context=True` + zero surviving citations is exactly the
ModelRetry trigger (Decision A2): a claim without receipts is a fixable
structural slip. `enough_context=False` never retries — that's an honest
"I don't know."

**Q: How many attempts really ran?**
Two generations: the original + exactly one retry (`Agent(retries=1)`).
The log *looks* like four because each attempt emits ~5 similar lines,
including TWO `citation_dropped` lines (one per stale citation) and
near-duplicate summaries from two loggers. Count generations by
`output_guards` blocks with distinct timestamps, not by lines.

**Q: Why exactly one retry?**
Deliberate budget (Decision A2): one targeted correction is cheap and
often works; failing the same check twice means a third identical attempt
mostly burns money. `retries=1` is the knob; the route's 30s
`asyncio.wait_for` is the hard wall around everything.

**Why the retry failed identically:** the retry message said "re-read the
context returned by retrieve_knowledge" — but no tool call had happened,
so there was nothing to re-read. Fix: harden the system prompt (must call
the tool EVERY turn; never reuse chunk_ids from earlier turns) and reword
the ModelRetry message to command a tool call rather than assume one.

**The takeaway:** the guardrails converted a silent wrongness (confidently
recycled answer with stale citations) into a visible, safe failure with
`needs_human=True` — and the failed turn was correctly NOT written to
memory. Defense in depth did its job; the agent's laziness on repeated
questions is a real production scenario (customers re-ask!) and gets its
own prompt-level fix.

---

## L10. One retry message cannot fit all failures — and what "cheap retry" really costs

Follow-up to L9. The proposed fix for the repeated-question failure was to
change the grounding ModelRetry message to "Call retrieve_knowledge NOW."
The review question that improved it: **is that right for every way
"enough_context=True + zero valid citations" can happen?**

No. Four causes, two different correct instructions:

| Cause | Context present this turn? | Correct instruction |
|---|---|---|
| 1. Forgot to populate citations | Yes | Re-read it, cite properly |
| 2. Mangled/invented chunk IDs | Yes | Re-read it, cite properly |
| 3. Reused stale IDs from an earlier turn | No | Call the tool now |
| 4. Never retrieved, answered from history | No | Call the tool now |

The validator can tell the groups apart without guessing:
`ctx.deps.retrieved_chunk_ids` is empty exactly when no retrieval
survived this turn. Hence ADR-005: the ModelRetry branches on that check,
so each failure mode receives the cheapest instruction that can actually
succeed. (The original message — "re-read the context returned by
retrieve_knowledge" — was an impossible instruction in cases 3/4, which
is why the retry failed identically in testing.)

**On "cheap":** a retry always re-runs the full LLM generation — that's
the dominant cost and happens regardless of wording. The tool call, when
commanded, adds only: an embedding (~$0.000002, often a cache hit since
the question repeats), a local vector search (milliseconds), and the
chunk JSON re-entering context. "Cheap" = one bounded extra attempt, not
a free one.

Meta-lesson: retry messages are prompts. Write them for the model that
made the mistake, and make sure the instruction is executable in the
state the model is actually in.

---

## L11. Do the new system-prompt rule and the branched retry conflict?

Asked before committing ADR-005: the system prompt now says *"you MUST
call retrieve_knowledge in EVERY turn... never answer from conversation
history alone"* — but the retry's re-read branch says *"re-read the
context already returned, don't re-fetch."* Contradiction?

No — they operate at two different moments, and the wording keeps them
aligned:

- The **system prompt is a standing policy about turns**: retrieve before
  you answer, once per turn. It governs the first attempt.
- The **retry message is a situational correction within one turn**, and
  its two branches agree with the policy:
  - *"Call the tool NOW"* fires when the model BROKE the standing rule
    (no retrieval this turn) — the retry enforces the same rule.
  - *"Re-read, don't re-fetch"* fires when the model FOLLOWED the rule
    (tool was called this turn) and only botched the citing. "Once per
    turn" is satisfied; the rule says once, not twice.

The wording trap checked: could this turn's tool output count as
forbidden "history"? No — both texts draw the same line in the same
place: **this turn's retrieval = valid; earlier turns = invalid.** The
retry says "in THIS turn"; the prompt forbids chunk_ids "from earlier
turns." Consistent vocabulary, no ambiguity.

Worst realistic case is harmless: a cautious model re-calls the tool on a
re-read retry → same query, same chunks, citations validate. One slightly
wasteful call, not a failure.

General principle: a system prompt is a standing rule; a retry message is
an on-the-spot correction from the referee. Models resolve them the way
an employee resolves "company policy says X" vs "my manager, watching
right now, says do X-prime for this case" — the specific, most recent
instruction clarifies the general one.

---

## L12. What "industry-standard logging" actually means, and how much of it we can do here

Asked before starting the Phase 10 structured-logging unit: *"JSON logs are
industry standard — but what IS the standard, and can we do it here?"*

It is not one thing. It is eight practices, and they have different answers:

| # | Practice | Verdict here |
|---|---|---|
| 1 | Structured events as JSON Lines (one object per line) | Yes — `python-json-logger` already pinned |
| 2 | Log to stdout only; never write or rotate files (Twelve-Factor XI) | Already true — uvicorn → stdout → `docker logs` |
| 3 | Stable schema on every line: `timestamp` (ISO-8601 UTC), `level`, `logger`, `event`, plus `service` / `version` / `env` | Yes |
| 4 | Correlation id per request, honoring an inbound `X-Request-ID` | Yes — `ContextVar` + middleware |
| 5 | Event name + fields, not prose sentences (`logger.info("chat_turn_ok", extra={...})`) | Yes |
| 6 | **Canonical log line / "wide event"** — ONE rich summary event per request | Yes, and highest value for us (see below) |
| 7 | Never log secrets, payloads, or PII | One violation found (see below) |
| 8 | Aggregation, indexing, retention, alerting (Loki / ELK / CloudWatch / Datadog) | **No — needs infrastructure we don't have** |

### The Python specifics

The conventional Python answer is **stdlib `logging`, configured once via
`logging.config.dictConfig`, with a JSON formatter**. `dictConfig` is the
standard because it is declarative and configures *every* logger in one
statement — including uvicorn's `uvicorn.access` / `uvicorn.error`, which
otherwise keep printing plain text alongside our JSON and produce a mixed,
unparseable stream.

`structlog` is the modern alternative (processor pipeline, native context
binding). Not chosen: `python-json-logger` is already in
`requirements.lock.txt`, and stdlib logging done properly is the more
transferable skill.

### Why the canonical log line matters to us specifically

One `/chat` turn currently scatters ~10 log lines across `chat.py`,
`condenser.py`, `tools.py`, `retrieval_guards.py`, `output_guards.py`, and
`memory.py`. L9 records the consequence: working out how many LLM
generations had run required counting `output_guards` blocks by timestamp
*by hand*. A single wide event at the end of the turn —

    {"event":"chat_turn","request_id":"3f9c…","session_id":"a1b2…",
     "duration_ms":4210,"outcome":"ok","generations":2,"retried":true,
     "gate_distance":0.31,"retrieval_survived":4,"citations":2}

— answers that question with one grep. The per-stage lines stay for detail;
the wide event is what gets read day to day.

### Two findings from the audit

1. **`main.py`'s logging config was dead code.** Logging was configured
   twice: `get_settings()` → `settings.setup_logging()` →
   `logging.basicConfig(...)`, and then again directly in `main.py`.
   `basicConfig` only configures the ROOT logger and **silently no-ops when
   the root logger already has handlers** (absent `force=True`). So the
   format in effect came from `settings.py`; the `main.py` call did nothing.
   Two configs, one silently ignored — the kind of thing that produces a
   long "why isn't my format changing?" session later. `dictConfig`
   collapses both into one declaration.

2. **`condenser.py` logged raw customer messages.**
   `logger.info("condensed_query original=%r condensed=%r", message, condensed)`
   wrote the customer's verbatim text into the log stream at INFO. It was
   the only such line in the codebase — every other call logs identifiers,
   categories, counts, and distances. Partly mitigated by our own ordering
   (input guards fail closed on PII at Stage 2, before condensation at
   Stage 4.5), but PII detection is regex and therefore best-effort, and
   retaining full customer utterances in logs is a data-retention decision
   regardless. Standard treatment: drop to DEBUG, or log a length/hash
   instead of the text.

### What we cannot do locally, and how to be honest about it

Practice 8 needs a log shipper and a backend that indexes fields so you can
query `event="chat_turn" AND outcome!="ok"`. On a laptop, JSON logs land in
a terminal where they are genuinely *harder* to read than plain text. Hence
the standard resolution, which `OpsSettings.enable_structured_logs` already
anticipated: **console format in development, JSON in production**, one env
var apart.

README wording should therefore be "emits structured JSON logs suitable for
ingestion by a log aggregator" — not "centralized logging", which we have
not built.

### General principle

"Industry standard" is rarely a single artifact you either have or lack. It
is a list of practices with different costs; the engineering judgment is
knowing which ones your environment can actually support, doing those
properly, and describing the rest accurately rather than aspirationally.

---
---

## L13. "It works on my machine" was literally true — one codebase, two interpreters

> **Correction note.** This entry originally claimed `GET /documents` was
> broken and had been failing silently for months. That was wrong, and it was
> committed to `main` before being checked. The endpoint returned 200 the
> whole time. What follows is the corrected account, kept in place of the
> original because the mistake and its cause are the useful part.

### What happened

While adding `/readyz`, a top-level `import psycopg` in `main.py` crashed the
app:

    ImportError: no pq wrapper available.
    - couldn't import psycopg 'c' implementation: No module named 'psycopg_c'
    - couldn't import psycopg 'binary' implementation: No module named 'psycopg_binary'
    - couldn't import psycopg 'python' implementation: libpq library not found

`vector_store.list_documents()` contains that same import, so the conclusion
looked obvious: `GET /documents` must be broken too. It wasn't — `curl` to
the running server returned all five documents, 200.

### Why both observations were correct

Two different Python interpreters were involved.

| | Interpreter | `import psycopg` |
|---|---|---|
| `python -c ...` in the shell | project `.venv` | **fails** |
| the uvicorn server on :8888 | homebrew Python 3.13 | **works** |

`lsof -ti :8888 | xargs ps` settled it: the running server was
`/opt/homebrew/Cellar/python@3.13/.../Python -m uvicorn`, not
`.venv/bin/python`. It had been started six days earlier from a terminal
where the venv was not active, and homebrew's site-packages happened to have
a working psycopg backend.

So the endpoint worked *on the interpreter serving requests* and would have
failed *on the interpreter the project pins*. Same code, opposite results.

### Why psycopg 3 in particular

psycopg 3 ships as a wrapper plus a **separately installed backend**:

- `pip install psycopg` → wrapper only, nothing to wrap.
- `pip install "psycopg[binary]"` → wrapper + `psycopg_binary` (libpq bundled).
- `pip install "psycopg[c]"` → wrapper + `psycopg_c` (compiled against system libpq).

`requirements.txt` listed the bare `psycopg`, so the venv got a wrapper with
no backend. The project's other two drivers cannot fail this way: `asyncpg`
speaks the Postgres wire protocol directly and never touches libpq, and
`psycopg2`'s wheel bundles it.

**Resolution:** `pip install "psycopg[binary]"` in the venv;
`requirements.txt` now pins `psycopg[binary]`; the lock file regenerated.
No code change was needed.

### The lazy import's actual role

`list_documents()` imports psycopg *inside* the function:

```python
async def list_documents(self) -> List[dict]:
    ...
    import psycopg          # runs only when this endpoint is called
```

This did not cause the bug, but it is why the environment mismatch could
persist unnoticed: a module-level import is checked at startup, loudly, on
whichever interpreter is actually running. A function-level import defers
that check until someone calls the function — so a venv missing the backend
would have started cleanly, served `/chat` perfectly, and only failed when
someone touched one rarely used endpoint.

### The lessons

- **"It works" is not a claim until you name the environment.** Every result
  in a debugging session belongs to a specific interpreter; a result without
  one is not evidence. `lsof -ti :PORT | xargs -I{} ps -o pid=,command= -p {}`
  answers "what is actually serving this port?" in one line, and it should be
  the first question when two observations disagree.
- **A lock file only means something if you run what it pins.** The venv was
  the pinned environment; the server was not running in it. Both facts were
  invisible until something forced them into the open.
- **Verify on the interpreter you ship.** A stale server from a previous
  session is a silent lie — it holds old code, old imports, and possibly an
  entirely different Python.
- **Module-level imports are a startup smoke test.** Deferring an import into
  a function trades a loud failure at boot for a quiet one at call time.
  Justified for genuinely optional or slow dependencies, and it should say so
  in a comment; otherwise prefer the top of the file.
- (Meta) The original version of this entry asserted a bug from one failing
  command without ever calling the endpoint — and reached `main` that way.
  The review question that caught it was simply *"everything works when I
  test it, so why are we changing this?"* One curl settled what a paragraph
  of plausible reasoning had gotten backwards. Test the claim, then write it
  down — not the other way around.
