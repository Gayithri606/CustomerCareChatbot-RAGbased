# How I Built a Production-Ready AI Document Q&A System — And What Makes It Different

Almost every client I talk to has some version of the same complaint. "We have all the information. We just can't find it fast enough." The contract is *somewhere* on the shared drive. The policy update is buried in the third revision of a 40-page PDF. Someone on the team spends two hours digging through a report to answer a question that, honestly, should take two minutes.

AI is supposed to fix this. And sometimes it does — but a lot of what I see out there is either a weekend Jupyter notebook pretending to be a product, or a flashy cloud demo that quietly bleeds money the moment you try to run it at real scale. I wanted to build something in between: a system that's actually usable in production, that I'd be comfortable handing to a paying client, and that doesn't fall apart the moment someone uploads a real document.

So that's what I did. Here's the walkthrough.

## Why a document processing pipeline specifically

A document processing pipeline isn't just one project — it's the foundation for at least three services I'm actively building toward: a RAG-based assistant, a customer care chatbot, and workflow automation. All three live or die on the same thing: the ability to reliably extract meaning from unstructured documents and make it queryable. Get the pipeline right and the rest becomes an architecture problem, not a data problem. This project is me getting the pipeline right.

## What it actually does

You upload a PDF or a Word doc. You ask it a question in plain English. It answers.

That's the whole user experience, and I'm genuinely trying not to dress it up more than that. Want to know what the termination clause says in a 90-page contract? Ask. Need to pull a specific risk disclosure from a prospectus? Ask. Trying to check the parental leave policy without opening the handbook? Ask.

And importantly — it tells you when it doesn't have enough context to answer properly. I'd rather have a system that says "I'm not sure, this document doesn't really cover that" than one that cheerfully makes something up and sounds confident about it.

## How it works, under the hood

The pipeline has four real jobs to do: read the document, break it up sensibly, turn those pieces into something searchable, and then — when a question comes in — retrieve the right pieces and write an answer.

**Reading the document properly.** Most tools treat a PDF like a pile of text. That's fine until you hit a table, or a nested heading, or a bulleted list where the structure carries the meaning. I'm using [Docling](https://github.com/DS4SD/docling) here, which gives me the structured document object — headings, tables, lists, hierarchy intact — not just a flat string.

**Chunking that doesn't butcher the document.** This is where a lot of RAG demos quietly fall apart. They split text every N tokens, which is fast and easy and completely ignores what the document is actually saying. A sentence gets cut in half. A table row ends up in two different chunks. Docling's `HybridChunker` respects the document's natural boundaries, so a heading stays with its paragraph, a table row stays together, and each chunk gets *contextualized* with its parent heading before it gets embedded:

```python
for chunk in raw_chunks:
    raw_text = chunk.text
    contextualized_text = self.chunker.contextualize(chunk=chunk)
    # contextualized_text → what we embed (richer meaning)
    # raw_text           → what we store and show the user (clean)
```

That dual-text trick is small but it matters. The model sees "Section 4.2 — Termination Clause: either party may..." when it's deciding similarity, but the user sees just "either party may..." when the answer comes back. Best of both.

**Embedding everything in one shot.** Every chunk has to be turned into a vector so we can do semantic search on it. The lazy way is to call the embeddings API once per chunk. On a 60-chunk document, that's 60 round trips, 60 chances for rate limits, 60 chances for something to go wrong. I batch them:

```python
response = self.openai_client.embeddings.create(
    input=texts,  # all 60 chunks at once
    model="text-embedding-3-small",
)
embeddings = [item.embedding for item in response.data]
```

One call. Same result. Faster, cheaper, less stuff that can break.

**Storing vectors in Postgres, not in a fancy dedicated vector DB.** I'm using [TimescaleDB](https://www.timescale.com/) with the `pgvectorscale` extension. This is a deliberate choice. Timescale's own benchmarks put it ahead of Pinecone on performance at about 75% lower cost — and more importantly, it's just Postgres. Any engineer your client already has can back it up, monitor it, query it, migrate it. No new vendor lock-in, no mystery black box.

**Retrieving and answering.** When a question comes in, it gets embedded, compared against the stored vectors, and the closest matches get handed to GPT-4o along with the original question. I'm using the [Instructor](https://github.com/jxnl/instructor) library to force the model's output into a typed Pydantic model:

```python
class SynthesizedResponse(BaseModel):
    thought_process: List[str]
    answer: str
    enough_context: bool
```

No regex parsing. No "please output valid JSON, I'm begging you" in the system prompt. If the response doesn't match the shape, Instructor retries it automatically. The API either gives you a clean, validated object or it gives you an honest error.

![A real question answered — structured response with answer, thought process, and enough_context: true](images/04-query-good-answer.png)

![A second query, different angle — same reliable structured output](images/05-query-another-good-answer.png)

The `enough_context` flag is the part I'm most proud of. When the retrieved chunks don't actually support an answer, the model sets it to `false` and says so — instead of confabulating something plausible. Most AI systems fail loudly or lie quietly. This one tells you when it doesn't know.

![The system admitting it doesn't have enough context to answer — no hallucination, just honesty](images/06-query-not-enough-context.png)

## Why I keep calling this "production-ready"

I don't love that phrase either, but most AI document demos really do fall over the moment you push them past a toy workload. So let me be specific about what's different here.

The whole FastAPI layer is async. Genuinely async — embedding calls, vector search, LLM calls, all non-blocking. The event loop isn't sitting there twiddling its thumbs while we wait on OpenAI. That means the server can happily juggle a lot of concurrent users without turning into a bottleneck.

Document ingestion happens in the background. This is one of those things that sounds obvious but trips people up constantly. Processing a real document isn't instant — parsing, chunking, embedding, storing, it takes maybe 10 to 30 seconds. If your upload endpoint blocks for that long, your server hates you and so does your user. So when a file comes in, I save it to a temp location, push the job to a [Celery](https://docs.celeryq.dev/) worker via Redis, and immediately return a `job_id`. The client polls a separate `/jobs/{job_id}` endpoint whenever it wants to check in.

```python
task = ingest_document_task.delay(tmp_path, file.filename)
return IngestResponse(
    job_id=task.id,
    message="Ingestion queued. Poll /jobs/{task.id} for status.",
)
```

![Uploading a real document — the API responds instantly with a job ID, no blocking](images/01-ingest-upload.png)

![Polling the job endpoint — status flips to SUCCESS with a chunk count once the worker finishes](images/02-ingest-job-success.png)

![The document is now listed and queryable, with filename and chunk count](images/03-documents-list.png)

Everything is observable. Every LLM call, every embedding request, every token, every dollar — it's all going through [Langfuse](https://langfuse.com/). I'm not adding this later as an afterthought; the OpenAI clients are wrapped with Langfuse's drop-in replacements, and key functions are decorated with `@observe()`. When a client asks me "what's this actually costing us per month," I can give them a real answer.

And the whole thing is broken into clean, swappable pieces — `DocumentProcessor`, `Chunker`, `VectorStore`, `LLMFactory`, `Synthesizer`. Want to swap OpenAI for Anthropic? The `LLMFactory` already supports both. Want to try a different chunking strategy six months from now when something better comes out? You can, without ripping the rest of the system apart. That kind of flexibility only exists when you bother to draw the lines in the right places up front.

It all runs in Docker. TimescaleDB, Redis, everything. `docker compose up` and you're going. No cloud account needed to develop locally, no weird drift between "works on my machine" and production.

It also fails cleanly when it should. Wrong file type, empty input, or a frustrated user venting — the system handles each case explicitly rather than quietly doing the wrong thing.

![Uploading an unsupported file type — rejected immediately with a clear 400 error](images/07-ingest-wrong-filetype.png)

![Submitting an empty question — caught at the API layer before it ever reaches the model](images/08-query-empty-question.png)

![A frustrated, emotionally charged question — the system responds with empathy and correctly flags enough_context: false rather than guessing](images/09-query-angry-graceful.png)

## Who this is actually for

Honestly, anyone whose business runs on documents. Law firms with contract libraries and due diligence piles. Finance teams combing through filings for a specific number. HR teams tired of fielding the same policy questions over and over. Healthcare, where the wrong guideline pulled at the wrong moment has real consequences. Consulting firms with knowledge bases so big that even senior people can't remember what's in there.

The common thread is simple. The information already exists. The cost is sitting in how long it takes to dig it out — and in how often people just give up and guess.

## If this sounds like your problem

If your team is spending more time hunting through documents than actually using what's in them — or if you've tried an AI tool that felt unreliable, expensive, or both — I'd be happy to have a real conversation about it. Reach out directly. I'll tell you honestly whether this is a fit for what you're dealing with, and if it is, we can scope something that works for your stack and your budget.

---

*Built with: FastAPI · Celery · Redis · TimescaleDB (pgvectorscale) · OpenAI · Docling · Instructor · Langfuse · Docker*
