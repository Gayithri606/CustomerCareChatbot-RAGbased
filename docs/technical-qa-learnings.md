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
