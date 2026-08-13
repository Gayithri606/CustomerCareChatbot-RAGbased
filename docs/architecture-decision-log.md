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
