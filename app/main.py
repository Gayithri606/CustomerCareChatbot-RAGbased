import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from config.settings import get_settings
from api.routes import ingest, query, documents ,jobs, chat

get_settings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs startup logic before the app starts accepting requests,
    and shutdown logic after the last request is handled."""
    logging.info("Starting up RAG API...")
    yield
    logging.info("Shutting down RAG API...")


app = FastAPI(
    title="Document RAG API",
    description="RAG-based document processing and Q&A pipeline",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(ingest.router)
app.include_router(query.router)
app.include_router(documents.router)
app.include_router(jobs.router)
app.include_router(chat.router) 


@app.get("/health", tags=["health"])
def health_check():
    """Quick liveness check — returns 200 if the server is up."""
    return {"status": "ok"}
