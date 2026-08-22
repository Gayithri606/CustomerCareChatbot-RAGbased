import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse

from config.settings import get_settings
from api.routes import ingest, query, documents ,jobs, chat

settings = get_settings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs startup logic before the app starts accepting requests,
    and shutdown logic after the last request is handled."""
    logging.info("Starting up RAG API...")
    yield
    logging.info("Shutting down RAG API...")


app = FastAPI(
    title="Customer Care Chatbot — RAG API",
    description=(
        "A retrieval-augmented customer-care chatbot: layered guardrails, "
        "session memory, and grounded answers with citations. Also exposes "
        "the document ingestion and retrieval pipeline it is built on."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Router order determines the section order on /docs. Chat first: it is what
# this project is about, and it is what a reader should see without scrolling.
app.include_router(chat.router)
app.include_router(documents.router)
app.include_router(ingest.router)
app.include_router(jobs.router)
app.include_router(query.router) 


@app.get("/health", tags=["health"])
def health_check():
    """Quick liveness check — returns 200 if the server is up."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Demo page
# ---------------------------------------------------------------------------
# A single static HTML page for showing the chatbot to non-technical viewers:
# conversation bubbles, citations, and visible guardrail refusals.
#
# Served by this app on purpose. The page and POST /chat/ then share an
# origin, so no CORS middleware is needed — opening the file directly from
# disk would require one.
#
# Read at request time rather than at import time so edits to the page show
# up on refresh without restarting the server. include_in_schema=False keeps
# it out of /docs: it is a demo client, not part of the API contract.

_DEMO_PAGE = Path(__file__).parent / "static" / "demo.html"


@app.get("/demo", response_class=HTMLResponse, include_in_schema=False)
def demo_page() -> HTMLResponse:
    """Serve the demo chat page."""
    return HTMLResponse(_DEMO_PAGE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Readiness probe
# ---------------------------------------------------------------------------
# /health is LIVENESS — "is this process alive?" A failure tells an
# orchestrator to RESTART the container.
# /readyz is READINESS — "can this process serve a request right now?" A
# failure tells it to ROUTE AROUND us without restarting. Keeping them
# separate means a five-second Redis blip briefly removes us from rotation
# instead of triggering a restart loop.
#
# Checked: Redis and TimescaleDB — both required by POST /chat, both ours.
# Deliberately NOT checked: OpenAI. Readiness must never depend on a third
# party we don't control; one OpenAI blip would eject every container at
# once and cause an outage rather than report one.

_READINESS_TIMEOUT_SECONDS = 2.0

# Dedicated client for the probe. `from_url` is lazy — no socket opens until
# the first command — so this costs nothing at import time.
_probe_redis = aioredis.from_url(settings.redis.url, decode_responses=False)


async def _check_redis() -> None:
    """Raise if Redis is unreachable within the timeout."""
    await asyncio.wait_for(_probe_redis.ping(), timeout=_READINESS_TIMEOUT_SECONDS)


async def _check_database() -> None:
    """Raise if TimescaleDB is unreachable within the timeout.

    Uses asyncpg rather than psycopg deliberately: asyncpg is the driver
    `timescale_vector`'s async client already uses for the vector search in
    POST /chat, so this probe exercises the same connection path we
    actually serve with — a probe should test what the service really runs
    on, not a second path that could pass while the real one is broken.
    """

    async def _select_one() -> None:
        conn = await asyncpg.connect(settings.database.service_url)
        try:
            await conn.execute("SELECT 1")
        finally:
            await conn.close()

    await asyncio.wait_for(_select_one(), timeout=_READINESS_TIMEOUT_SECONDS)


async def readiness_check(response: Response) -> dict:
    """Report whether every dependency needed to serve /chat is reachable.

    200 with per-dependency detail when ready, 503 when not — the detail is
    what makes the probe self-diagnosing instead of a bare red light.

    Only the exception CLASS NAME is reported, never its message: driver
    errors can embed the full connection string, password included.
    """
    checks: dict[str, str] = {}

    for name, check in (("redis", _check_redis), ("database", _check_database)):
        try:
            await check()
            checks[name] = "ok"
        except asyncio.TimeoutError:
            checks[name] = f"error: timeout after {_READINESS_TIMEOUT_SECONDS}s"
        except Exception as exc:
            checks[name] = f"error: {exc.__class__.__name__}"

    ready = all(status == "ok" for status in checks.values())
    if not ready:
        logging.warning("readiness_not_ready checks=%s", checks)
        response.status_code = 503

    return {"status": "ready" if ready else "not_ready", "checks": checks}


# Registered conditionally so OPS_ENABLE_READINESS_PROBE=false actually
# removes the endpoint. `add_api_route` is what the @app.get decorator calls
# underneath — the decorator simply can't be wrapped in an `if`.
if settings.ops.enable_readiness_probe:
    app.add_api_route(
        "/readyz",
        readiness_check,
        methods=["GET"],
        tags=["health"],
    )
