# Architecture Decision Log

Decisions, the debate behind them, and what we learned. One entry per decision,
newest at the top. Each entry answers four questions: what was the problem,
what options did we consider, what did we pick, and what does it cost us.

> Format note: this follows the industry "ADR" (Architecture Decision Record)
> convention, kept deliberately lightweight. Decision IDs are stable and can be
> referenced from code comments and commit messages (e.g. "see ADR-001").

---

## ADR-001 — Fix the history-blind relevance gate with query condensation

**Date:** 2026-08-06 · **Status:** Accepted · **Files:** `app/chatbot/condenser.py` (new), `app/api/routes/chat.py`

### The problem

First end-to-end test of `/chat`: turn 1 ("What CFM do I need for a range
hood?") answered correctly; turn 2 ("And what about ductwork for that?")
was wrongly refused as out-of-scope. The relevance gate embeds only the
current message — "that" is meaningless without history, which the gate
never sees. The gate was built in Phase 4 (no memory existed); memory
arrived in Phase 8; the collision surfaced the first time both ran in one
request. As shipped, the gate defeated multi-turn conversation.

### Options considered

| Option | Idea | Verdict |
|---|---|---|
| A — Skip gate when history exists | One `if`; follow-ups bypass the gate | Free, but off-topic questions mid-conversation lose their cheap refusal |
| **B — Query condensation** | Cheap model rewrites the follow-up into a standalone question using history; gate embeds the rewrite | **Chosen** |
| C — Embed last + current message together | Concatenate before embedding | Crude; the blended embedding lands close to neither question |
| D — Remove the gate | Rely on downstream retrieval guards + NO_CONTEXT off-ramp | Seriously considered — see below |

### The debate

Option D was nearly chosen, on three arguments: (1) the gate's safety job
is duplicated downstream (retrieval guards → NO_CONTEXT → output
off-ramp); (2) in an agentic design the agent already condenses — it
reads history and forms its own tool queries; (3) the condenser taxes
every turn to save cost on off-topic turns.

What reversed it was the production lens. In real customer-facing
deployments, off-topic traffic is not rare — greetings, chitchat,
probing, venting, and spam are a large minority of messages. Production
systems keep a cheap pre-LLM scope check for exactly this reason, and the
standard shape of that check is condense-then-gate ("history-aware
retrieval"). Removing the gate would have optimized for the demo, not the
deployment the project claims to mimic. Decision principle: build the
production pattern and document it, rather than take the shortcut.

### Decision

Option B. A new `condenser.py` module uses `cheap_model` (gpt-4o-mini) to
rewrite follow-ups into standalone questions; the gate embeds the
rewrite. First turns skip condensation (nothing to resolve). The
condenser fails open — on any error the raw message is used, so a broken
condenser can never block the chat surface. The agent still receives the
raw message plus full history; the rewrite exists only for the gate's
embedding, so a distorted rewrite can cause at worst a wrong gate
verdict, never a wrong answer in the customer's name.

### Pros

- Follow-ups pass the gate; multi-turn works on every turn.
- Off-topic messages are still refused before the expensive model —
  including mid-conversation.
- Matches the production-standard pattern; `cheap_model` (configured
  since Phase 1, never used) finally earns its place.

### Cons — accepted knowingly

- Every follow-up turn pays one cheap-model call (~$0.0001, ~300ms).
- The rewrite can distort intent; mitigated by first-turn skip,
  fail-open, a strict "never add details" prompt, and by never feeding
  the rewrite to the answering agent.

### Learnings

- When two features are built in different phases, re-test the older
  one's assumptions against the newer one.
- Caching fixes cost, never correctness — "would a cache save it?" was
  the clarifying question.
- Decide for the system you claim to be building. The demo said "remove
  it"; the production story said "fix it properly." We chose the latter,
  on purpose.
- (Meta) This entry was first drafted recording option D as accepted,
  before the decision was truly final — and was even committed in that
  state, so for a few commits the decision log contradicted the code.
  Rewritten. Log decisions when they are decided, not while they are
  leaning.

---

## ADR-002 — Citation source/score backfill

**Date:** 2026-08-13 · **Status:** Accepted, implemented · **Files:** `app/chatbot/deps.py`, `app/chatbot/tools.py`, `app/api/routes/chat.py`

Citations returned `"source": null, "score": null` — the LLM cites
`chunk_id`s (all it sees), and the route could not map them back to
filenames because `ChatDeps` carried only the surviving chunk IDs, not
the chunk objects holding filename and distance.

**Fix (carry the chunks, not just their IDs):** `ChatDeps` gained
`retrieved_chunks: tuple[RetrievedChunk, ...]` alongside the existing
`retrieved_chunk_ids` (whose output-guard contract stays untouched);
`retrieve_knowledge` writes both; the route's new Stage 8.5 enriches
each surviving citation with the chunk's filename and distance via
`model_copy` before responding. Safe by construction: the output
validator guarantees every surviving citation is in this turn's
retrieved set, so the lookup cannot miss. Resolves route Decision E4.

Verified: citations now return real filenames and distance scores.

---

## ADR-003 — Shared dev database, table name to become configurable

**Status:** Accepted for development · **Files:** `app/config/settings.py`

This project currently shares one local TimescaleDB container and the
hardcoded `document_embeddings` table with a sibling learning project.
This is a development-environment convenience, not a production design:
it kept Phase 10 bring-up to one moving variable and reused already-
ingested documents for testing. Not applicable to any real deployment,
where the service would own its database.

The one real code item hiding here: `VectorStoreSettings.table_name` is a
hardcoded literal while every other setting is env-driven. Future chore
(not a decision): make it configurable via env var (e.g.
`VECTOR_STORE_TABLE_NAME`), point this project at its own table, and
re-ingest customer-care-appropriate documents. Redis needs no change —
keys are already namespaced under `chatbot:*`.

---

## ADR-004 — Condenser reads history as a rendered transcript, never as `message_history`

**Date:** 2026-08-06 · **Status:** Accepted · **Files:** `app/chatbot/condenser.py` · **Companion to:** ADR-001, learnings L3

### Context

The query condenser (ADR-001) needs the conversation history to resolve
references like "that" in *"And what about ductwork for that?"*. There are
two ways to give a Pydantic AI agent history, and the obvious one is a trap.
This entry records why the code does it the non-obvious way, in enough
detail that nobody "simplifies" it back into the bug.

### The API shape (and the intuition it defeats)

The intuitive design would be three slots: system prompt + new message +
history, all combined at call time. Pydantic AI's real shape:

| Conceptual slot | Where it actually lives |
|---|---|
| System prompt | `Agent(system_prompt=...)` — fixed at construction |
| User message | first argument of `.run(message)` |
| History | `.run(..., message_history=history)` |

The trap: when `message_history` is passed, Pydantic AI **skips the agent's
own `system_prompt` entirely.** It assumes the history is a complete
conversation that already contains its opening — including its system
prompt. That assumption is correct for an agent resuming *its own*
conversation (this is exactly right in `chat.py` for the main agent). It is
wrong when handing **agent A's history to agent B**.

### Mistake 1 — passing `message_history` (job replacement)

```python
result = await condenser_agent.run(message, message_history=history)  # WRONG
```

Looks idiomatic. What gpt-4o-mini actually receives:

    SYSTEM:    "You are a customer-care AI assistant..."   ← chatbot's prompt!
    USER:      "What CFM do I need for a range hood?"
    ASSISTANT: "For a gas grill, a 1,200 CFM..."
    USER:      "And what about ductwork for that?"

The condenser's instructions never reach the model. Its job *is now* "be
the chatbot", so it **answers the ductwork question** — on the cheap model,
with no retrieval, no guardrails, no citations. Nothing crashes; the output
even looks plausible. Silent, guaranteed wrong behavior.

### Mistake 2 — naive transcript dump (noise in the paperwork)

Rendering history to text but with a catch-all
(`hasattr(part, "content")`) leaks the chatbot's `SystemPromptPart` into
the transcript:

    SYSTEM:  "You rewrite messages into standalone questions..."  ← correct job
    USER:    "Transcript:
              You are a customer-care AI assistant...             ← leaked text
              What CFM do I need for a range hood? ..."

Less severe: the condenser's instructions still hold the system slot, and
models mostly treat quoted text as data, not commands. Output is almost
certainly still a rewrite — but the leak is noise with a residual risk of
swaying the model (the same mechanism prompt injection exploits).

**The principle separating the two:** every LLM call has a control channel
(the system slot — obeyed) and a data channel (the user prompt — read).
Mistake 1 puts foreign instructions in the control channel (someone swapped
your job description). Mistake 2 quotes them in the data channel (a
confusing memo in the paperwork). Different severity, both avoidable.

### Alternative considered: filter the history, then pass it

Strip the `SystemPromptPart` from the history and pass the rest as
`message_history`. Rejected:

1. The filtered history still replays the main agent's tool calls, tool
   returns, and retrieved-chunk JSON — token noise and cost for a task
   needing six lines of text.
2. Surgically rewriting another agent's message objects is fragile against
   Pydantic AI schema changes.

(Pydantic AI's newer `instructions=` parameter re-applies even with
history, but doesn't help here: the history's embedded system prompt would
still be replayed, giving the model two competing instruction sets.)

### Decision

`_render_transcript()` builds a short plain-text transcript by
**allowlisting** exactly two part types — `UserPromptPart` → "Customer:",
`TextPart` → "Assistant:" — capped at the last 6 lines. Everything else
(system prompts, tool traffic) is excluded by simply never matching the
filter. The transcript is pasted *inside* the condenser's user prompt; the
condenser's own system prompt keeps the control channel.

Analogy that captures it: `message_history` is a verbatim photocopy of
another employee's entire case file, stapled memos included; the transcript
is a six-line summary written for the new employee. For "resolve what
'that' refers to", the summary is strictly better — cheaper, cleaner, and
immune to inheriting the wrong job description.

### Learnings

- `message_history` means "resume this agent's own conversation" — never
  "share context across agents". Same parameter, opposite consequence,
  depending on whose history it is.
- Allowlist extraction (`isinstance` on specific part types) beats
  catch-all dumping (`hasattr`) whenever converting structured messages to
  text: exclusion-by-default is what keeps foreign instructions out.
- Control channel vs data channel is the lens: instructions in the system
  slot are obeyed; instructions quoted in data are (mostly) just read.
  Prompt injection is the exploitation of "mostly".

---

## ADR-005 — Grounding retry message branches on whether retrieval happened this turn

**Date:** 2026-08-07 · **Status:** Accepted · **Files:** `app/chatbot/agent.py`, `app/chatbot/prompts.py` · **Companion to:** learnings L9, L10

### Context

Verification of ADR-001 exposed a repeated-question failure: on a
word-for-word repeat, the agent skipped `retrieve_knowledge`, recited its
earlier answer, and cited stale chunk IDs from the previous turn. The
output validator correctly dropped the citations and fired its one
`ModelRetry` — but the retry failed identically, because the retry
message said *"re-read the context returned by retrieve_knowledge"* when
no retrieval had happened. There was nothing to re-read.

### The brainstorm that shaped the fix

The first proposed fix replaced the message with *"Call retrieve_knowledge
NOW."* Review challenge (the decisive question): **is commanding a tool
call correct for every way this failure arises?** Enumerating the causes
of `enough_context=True` + zero valid citations:

1. Model retrieved, answered well, **forgot to populate citations**.
2. Model retrieved but **mangled/invented chunk IDs**.
3. Model cited **stale IDs from an earlier turn**.
4. Model **never retrieved at all** and answered from history/priors.

For cases 1–2 the retrieved context is already in the conversation —
*re-read and cite* is the correct, cheaper instruction; forcing a fresh
tool call re-fetches what the model already has. For cases 3–4 there is
no current context — *call the tool* is the only instruction that can
succeed. One fixed message is therefore wrong in one direction or the
other.

### Decision

The validator can distinguish the cases: `ctx.deps.retrieved_chunk_ids`
is empty exactly when no retrieval survived this turn. So the
`ModelRetry` branches:

- **Empty set** (cases 3–4): command the model to call
  `retrieve_knowledge` now, then cite from its output or honestly set
  `enough_context=False`.
- **Non-empty set** (cases 1–2): command the model to re-read THIS
  turn's already-returned context and cite exact chunk_ids from it —
  no re-fetch.

Companion change in `prompts.py`: the system prompt now mandates calling
`retrieve_knowledge` every turn and forbids reusing chunk_ids from
earlier turns, reducing how often the retry is needed at all.

### Cost clarification (recorded because it was a point of confusion)

A retry always re-runs the full LLM generation — that is the dominant
cost (~1 cent, seconds) and is unavoidable. "Cheap retry" means one
bounded extra attempt versus losing a good answer or retrying without
limit. The marginal cost of a commanded tool call on top of that is
small: the query embedding is ~free (often an ADR-cache hit), vector
search is local milliseconds; the only real addition is the chunk JSON
re-entering the context. The branching also minimizes this: the re-read
path skips even that.

### Learnings

- Error messages to an LLM are instructions, not diagnostics: an
  instruction the model cannot execute ("re-read" what doesn't exist)
  produces an identical failure, burning the retry budget.
- When a validator can observe which failure mode occurred, give each
  mode its own targeted correction rather than one generic message.
- The review question "is this fix right for ALL the ways the bug
  happens?" upgraded the fix from workable to correct.

---

## ADR-006 — Split liveness and readiness; readiness checks only what we own

**Date:** 2026-08-14 · **Status:** Accepted, implemented · **Files:** `app/main.py`, `README.md`

### The problem

`/health` returned `{"status":"ok"}` whenever the Python process was alive —
including when Redis and TimescaleDB were both unreachable and every
`POST /chat` would fail. One endpoint was being asked to answer two
different operational questions.

### Options considered

| Option | Idea | Verdict |
|---|---|---|
| A | Extend `/health` to check dependencies | Rejected — conflates the two questions (see below) |
| **B** | **Add `/readyz`; leave `/health` as pure liveness** | **Chosen** |
| C | `/readyz` checking Redis + DB + OpenAI | Rejected — see "what is deliberately not checked" |
| D | Bare 200/503 with no body | Rejected — a red light that can't say which bulb blew |

### Why the split matters

The two probes tell an orchestrator to do opposite things:

- **Liveness** (`/health`) — "is this process alive?" A failure means
  **restart the container.**
- **Readiness** (`/readyz`) — "can this process serve a request right now?"
  A failure means **route traffic elsewhere, do not restart.**

Merging them (option A) means a five-second Redis blip triggers a restart
loop instead of a brief removal from rotation — the cure is worse than the
symptom, and the restart destroys in-process state for no reason.

### What is deliberately NOT checked

OpenAI. Readiness must never depend on a third party we do not control: an
OpenAI incident would mark every replica unready simultaneously, ejecting a
fleet that is otherwise perfectly capable of serving cached, guarded, and
refused turns. A readiness probe that fails on someone else's outage causes
an outage rather than reporting one. Third-party health belongs in alerting
and traces, not in a routing decision.

### Decision

`GET /readyz` checks Redis (`PING`) and TimescaleDB (`SELECT 1`), each
wrapped in a 2-second `asyncio.wait_for`, and returns per-dependency detail:

    200  {"status":"ready","checks":{"redis":"ok","database":"ok"}}
    503  {"status":"not_ready","checks":{"redis":"error: ConnectionError",
                                         "database":"ok"}}

Three supporting choices:

1. **Timeout per check.** A dependency that *hangs* is worse than one that
   is down — without a cap the probe hangs too and the orchestrator waits
   instead of getting an answer. Sequential checks (worst case 4s) keep
   failure attribution obvious; readiness is not a latency-sensitive path.
2. **Exception class names only**, never messages. Driver errors routinely
   embed the full connection string, password included, and probe output is
   the least-protected surface in the service.
3. **Conditional registration.** The endpoint is attached via
   `app.add_api_route(...)` inside an `if settings.ops.enable_readiness_probe`
   block, so `OPS_ENABLE_READINESS_PROBE=false` yields a real 404 rather than
   a route that exists but refuses. (`@app.get` is only sugar over
   `add_api_route` and cannot be wrapped in a conditional.)

### The driver choice: asyncpg, not psycopg

The first implementation used `psycopg` at module scope, matching
`vector_store.list_documents()`. It broke the app on import: `psycopg` is
present in the venv with no working libpq backend (no `psycopg_c`, no
`psycopg_binary`, no system libpq).

Rewritten to use `asyncpg`, which is already in `requirements.lock.txt` and
is the driver `timescale_vector`'s async client uses for the `/chat` vector
search. This is better than a workaround: **a readiness probe should
exercise the driver the service actually serves with.** Checking the health
of a connection path no request uses would be a probe that can pass while
the real path is broken.

### Pros

- A dependency outage removes the service from rotation without restarts.
- The probe diagnoses itself — the failing dependency is named in the body.
- No new dependencies; the checked path is the served path.
- Costs nothing when nothing calls it, and one PING + one `SELECT 1` when
  something does.

### Cons — accepted knowingly

- Nothing calls `/readyz` in the current setup (local uvicorn, no
  orchestrator, no load balancer). Its value today is manual diagnosis and
  deployment-readiness; it earns its keep when this is containerized.
- Two sequential checks mean a worst case of ~4s under total outage.
- The probe opens its own Redis client and a fresh DB connection per call
  rather than reusing the chat path's pool — deliberate isolation, at the
  cost of one extra connection per probe.

### Learnings

- Liveness and readiness are different questions with opposite remedies;
  one endpoint cannot answer both.
- Excluding third parties from readiness is not laziness — including them
  converts someone else's outage into your own.
- Probe bodies are an information-disclosure surface: report classes, not
  messages.
- Verifying a probe means proving all three states: healthy, correctly
  unhealthy (with the right dependency named), and switched off. Only the
  middle one actually exercises the logic.

---

## ADR-008 — Asking for a human is never out of scope: escalate from inside the relevance-gate refusal

**Date:** 2026-08-24 · **Status:** Accepted, implemented · **Files:** `app/api/routes/chat.py`, `app/static/demo.html` · **Companion to:** learnings L14, L15

> ADR-007 is deliberately reserved for the relevance-gate boundary decision,
> which is blocked on measurement. This entry was written first; the IDs are
> stable labels, not a timeline.

### The problem

A customer who types *"I need a human agent."* is refused by the relevance
gate at stage 5 and never reaches the deterministic keyword escalation at
stage 7. The bot replies *"I can only help with topics covered in my
knowledge base"* and sets `needs_human=False` — the single most important
message a customer-care bot can receive, dropped on the floor.

This is **not** the threshold problem of L14, and raising the threshold
cannot fix it. Asking for a human is a **routing request**, not a
knowledge-base question. No document answers it, so its embedding is far
from every chunk by construction. Any threshold loose enough to admit it
would admit everything.

### Options considered

| Option | Idea | Verdict |
|---|---|---|
| A | Raise the relevance threshold until handoff requests pass | Rejected — the distance is large *correctly*; no line can separate a routing request from genuine noise |
| B | Move the keyword check before the gate and short-circuit | Rejected — see below |
| **C** | **Check the escalation keywords inside the gate's refusal branch** | **Chosen** |
| D | Leave the code; reword the demo scenario to stay on-topic | Rejected — this is exactly the near-miss L14 records: adjusting the input until the defect stops showing |

Option B is the intuitive one and it is wrong in a way worth recording. A
handoff check that runs *before* the gate and returns immediately would break
Decision E3's additive escalation: a customer asking *"What CFM do I need for
a gas range? Can I speak to a person?"* would be routed to a human **instead
of** getting the grounded answer they also asked for. The existing stage 7
already handles that case correctly. Only the refusal path is broken, so only
the refusal path should change.

### Decision

In the stage 5 refusal branch, call the existing `_mentions_human_handoff()`
on the **raw** message (not the condensed rewrite — the condenser
demonstrably moves the handoff clause to the front of the sentence, and stage
7 already uses the raw message, so both escalation paths now read the same
input). When true, return `_HANDOFF_MESSAGE` with `needs_human=True` and
`refused_reason="relevance_gate:out_of_scope+handoff"`.

Both paths now work: on-topic + handoff passes the gate and stage 7 sets the
flag; off-topic + handoff refuses **and** escalates.

### The debate — two review objections that changed the patch

The first draft was accepted in shape and rejected in both of its strings.

**Objection 1 — the message answered a question nobody asked.** The proposed
text was *"I can't answer that from my knowledge base, but I can put you
through to a human agent."* For a bare *"I need a human agent."* the first
clause apologises for a question the customer never asked — a bot sounding
inattentive at precisely the moment the customer has already given up on it.

Two situations reach this branch: a **pure** routing request, and an
out-of-scope question that *also* requests routing. Telling them apart means
answering "does this message also contain a knowledge question?" — the same
problem the gate is already failing at (L14) — so any detector would be a
regex that is wrong some of the time, and L10 established that a message
wrong in one of its branches is worse than one branch that is always right.

Resolution: **narrow the claim until it is true in both cases.** The
knowledge-base clause was deleted rather than made conditional.

**Objection 2 — `refused_reason` was true about the mechanism and misleading
about the meaning.** Leaving `relevance_gate:out_of_scope` on an escalated
turn correctly reports that the gate ended the turn before any LLM call, and
incorrectly implies the customer asked something out of scope. Asking for a
human is never out of scope.

The defence initially offered — "`needs_human` carries the other fact, so no
information is lost" — was true about information and false about honesty,
and its real motive was that `demo.html` renders its guardrail banner off
this field. A field value chosen to protect a screenshot is not a decision.

Four values were weighed (full table in L15). `null` was rejected for
breaking the field's documented contract — no LLM ran, so the turn *was*
guarded. `escalation:human_requested` was the runner-up and has a real
argument: it is the only option that stops filing a successful escalation
under a `refused_*` label, which matters if these values ever reach a
dashboard. It was not chosen because it discards the fact that the
*relevance gate* is what stopped the turn — precisely the signal ADR-007
exists to study.

Chosen: `relevance_gate:out_of_scope+handoff`. Both facts, one field,
greppable, and `demo.html`'s prefix matching still fires.

### The rendering change is part of the decision, not scope creep

`guardLabel()` in `demo.html` labels any `relevance_gate*` reason *"outside
the knowledge base, refused before any language-model call."* Shipping the
new value without a matching branch would put the exact sentence just deleted
from the customer-facing message back on the screen, one line higher. A
status value and its rendering are one change.

The new branch is inserted **above** the generic one — `indexOf(...) === 0`
is first-match-wins, and `"relevance_gate"` would otherwise swallow the more
specific value.

### Verification — what was actually observed

Three cases on `/demo`, one session, engineering detail on:

| Message | Banner | Badge | `refused_reason` |
|---|---|---|---|
| *"I need a human agent."* | new escalation wording | yes | `relevance_gate:out_of_scope+handoff` |
| *"What is the weather in San Jose today?"* | original out-of-scope wording | **no** | `relevance_gate:out_of_scope` |
| *"What CFM do I need for a range hood over a gas range? Can I speak to a person?"* | none | yes | `null` |

Case 1 is the fix. Case 2 proves the branch is conditional rather than
universal. Case 3 proves escalation stayed additive — the customer still
receives the grounded answer with its citation, which is the property option
B would have destroyed.

Case 3 carries one limit worth stating: the badge alone does not distinguish
the model setting `needs_human` itself from stage 7's keyword override
forcing it. Only the presence of `chat_escalation_forced_by_keyword` in the
server log settles which fired, and the response body cannot show it.

### Pros

- The one message a customer-care bot must never ignore is now handled on
  every path through the route.
- Costs nothing: a set intersection over an already-lowercased message, on a
  branch that had already decided to refuse. No embedding, no model call.
- Reuses the existing `_mentions_human_handoff` helper — one definition of
  "asked for a human", called from both stage 5 and stage 7.
- The response is self-describing: `refused_reason` names both the mechanism
  and the outcome, so a log reader a year from now needs no context.

### Cons — accepted knowingly

- **Keyword matching stays a wordlist.** *"I need a person to look at this"*
  fires; *"get me someone who knows what they're doing"* does not. False
  negatives remain, and the honest framing is that this catches the common
  phrasings cheaply, not all of them.
- The wordlist is still a module-level constant in `chat.py` rather than a
  `GuardrailPolicy` field. It is now read from two places in that file, which
  strengthens the case for promoting it beside the profanity list — still
  queued, still not done.
- **`_HANDOFF_MESSAGE` promises routing this service does not perform.** It
  sets a flag for a surrounding system that does not exist in this project.
  That remains the correct contract — signalling is the API's job — but the
  text is written for the deployment, not for the demo, and a reader deserves
  to know that (L15).
- Input-guard refusals (PII, jailbreak) still do not escalate. *"My SSN is
  …, get me a human"* refuses without routing. That is a separate judgment
  call and deliberately not bundled here.

### Learnings

- A similarity threshold can only answer questions that documents answer.
  Messages *about* the conversation — asking for a human, complaining,
  withdrawing consent — are a different kind of input and need a check that
  does not run through the embedding at all.
- When one string has to describe two situations, branch it if the situations
  are reliably distinguishable and narrow it if they are not. Writing for the
  case you had in mind and letting it apply to the case you didn't is the
  failure mode, and it is invisible in testing because the misdescribed case
  is the one nobody tried.
- If the reason a value keeps its current form is that changing it would make
  a screenshot look worse, that is not a reason — it is the screenshot asking
  to be re-taken.
- A status code and the UI that renders it are one decision. Shipping the
  first without the second relocates the bug rather than fixing it.
