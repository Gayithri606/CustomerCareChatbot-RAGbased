# Architecture Decision Log

Decisions, the debate behind them, and what we learned. One entry per decision,
newest at the top. Each entry answers four questions: what was the problem,
what options did we consider, what did we pick, and what does it cost us.

> Format note: this follows the industry "ADR" (Architecture Decision Record)
> convention, kept deliberately lightweight. Decision IDs are stable and can be
> referenced from code comments and commit messages (e.g. "see ADR-001").

---

## ADR-001 — Remove the relevance gate; let the agent handle scope

**Date:** 2026-08-06 · **Status:** Accepted · **Files:** `app/api/routes/chat.py`

### The problem we hit

The first end-to-end test of `/chat` exposed a bug:

- Turn 1: *"What CFM do I need for a range hood?"* → answered correctly, with citations.
- Turn 2: *"And what about ductwork for that?"* → **refused** as out-of-scope.

The relevance gate embeds only the current message and checks the distance to
the nearest chunk. The words "and what about ductwork for that?" carry almost
no meaning on their own — "that" refers to the range hood, but only the
conversation history knows this, and the gate never sees the history. The gate
was designed in Phase 4, when the chatbot had no memory. Multi-turn memory
arrived in Phase 8. The two features were built on different assumptions and
collided the first time they ran in the same request.

**Impact:** natural follow-up questions — the whole point of a multi-turn
chatbot — were being refused. The gate defeated the headline feature.

### Options considered

| Option | Idea | Verdict |
|---|---|---|
| A — Skip gate when history exists | One `if` statement; follow-ups bypass the gate | Works, free, but off-topic questions mid-conversation are no longer refused cheaply — and the gate remains a second retrieval philosophy bolted onto an agentic design |
| B — Query condensation | A cheap model (`gpt-4o-mini`) rewrites the follow-up into a standalone question using history; gate and retrieval both use the rewrite | The classic "history-aware retriever" pattern. Correct, but adds a per-turn LLM call, a new prompt to maintain, and a new failure mode (the rewrite can distort the question) |
| C — Embed last message + current together | Concatenate before embedding | Crude; the combined embedding is an average of two questions and lands close to neither |
| **D — Remove the gate entirely** | Trust the agent and the downstream guards | **Chosen** |

### The debate, in short

Three realizations drove the decision:

1. **The gate's safety job is already done downstream.** Retrieval guards drop
   chunks beyond the distance threshold; the tool returns `NO_CONTEXT` when
   nothing survives; the output guard then serves the canned "I don't have
   information on that" off-ramp. The gate was an *optimization* (refuse
   off-topic messages before paying for a GPT-4o call), not a correctness
   mechanism.

2. **A Redis embedding cache would not have saved it.** The cache fixes the
   *cost* of the gate (duplicate embedding), not its *correctness* (history
   blindness). A cached wrong refusal is still a wrong refusal. Also, the
   cache keys on exact text — the gate embeds the customer's raw words while
   the agent searches with its own rephrasing, so gate/tool cache hits would
   be rare anyway.

3. **In an agentic design, the agent IS the condenser.** This was the
   deciding insight. We chose Pydantic AI with `retrieve_knowledge` as a
   tool precisely so the LLM forms its own search queries. On a follow-up
   turn, GPT-4o reads the history, resolves "that" to "range hood", and
   calls `retrieve_knowledge("range hood ductwork requirements")` on its
   own. Option B would have paid a second model to do a job the main model
   already does. The gate was classic-RAG thinking (retrieve once, up
   front) living inside an agentic architecture — the mismatch, not the
   gate's code, was the real bug.

### Decision

Remove the relevance gate call from the `/chat` route. Out-of-scope handling
is delegated to the layers that already do it: retrieval guards →
`NO_CONTEXT` sentinel → output-guard soft off-ramp. The `relevance_gate`
function remains in `input_guards.py` (unused, harmless, and removing
committed guardrail code is a separate decision).

### Pros

- Follow-up questions work on every turn — the bug is gone by subtraction,
  not by adding machinery.
- One less concept, one less threshold to tune, no duplicate
  embed-and-search on every allowed turn.
- No new failure modes (option B's rewrite-distortion risk never enters the
  system).
- The design becomes internally consistent: one retrieval philosophy
  (agentic) instead of two.

### Cons — accepted knowingly

- An off-topic question now costs one GPT-4o call (~$0.01) before being
  refused, instead of ~$0.000002. Accepted because off-topic messages are
  rare in this deployment, and the refusal itself still happens reliably.
- No cheap pre-LLM scope filter exists anymore. If abuse or cost ever
  becomes real, the right re-introduction is option B (condense, then gate)
  — documented here so future-us doesn't reinvent the debate.

### Learnings

- **When two features are built in different phases, re-test the older one's
  assumptions.** The gate was correct in Phase 4 and wrong by Phase 8;
  nothing forced the collision until the route ran both in one request.
- **Ask what job a component actually does before fixing it.** Every fix
  (A, B, C) assumed the gate must stay. The best fix was noticing its job
  was already covered.
- **Caching fixes cost, never correctness.** "Would the cache save it?" was
  the right question to ask — and the answer clarified everything.

---

## ADR-002 — Citation source/score backfill *(pending)*

**Status:** Open. Citations currently return `"source": null, "score": null`
— the LLM cites `chunk_id`s, but the route can't map them back to filenames
because `ChatDeps` carries only the surviving chunk IDs, not the chunk
bodies. Fix requires an additive change to `deps.py` + `tools.py` (store the
surviving chunks, not just their IDs) and a small backfill loop in the
route. Scheduled after ADR-001 is implemented and verified.

---

## ADR-003 — Shared `document_embeddings` table across projects *(pending)*

**Status:** Open, accepted for now. This project and
DocumentProcessingPipeline-RAGbased share one TimescaleDB container and the
same hardcoded `document_embeddings` table. Kept shared during Phase 10
bring-up (one variable at a time; the existing data was useful for testing).
Planned fix: make `VectorStoreSettings.table_name` env-driven, point this
project at its own `customercare_embeddings` table, re-ingest
customer-care-appropriate documents. Redis needs no change — keys are
already namespaced under `chatbot:*`.

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
