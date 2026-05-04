import logging
import os
from datetime import timedelta
from functools import lru_cache
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict




load_dotenv(dotenv_path="./.env")


def setup_logging():
    """Configure basic logging for the application."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )


class LLMSettings(BaseModel):
    """Base settings for Language Model configurations."""

    temperature: float = 0.0
    max_tokens: Optional[int] = None
    max_retries: int = 3


class OpenAISettings(LLMSettings):
    """OpenAI-specific settings extending LLMSettings."""

    api_key: str = Field(default_factory=lambda: os.getenv("OPENAI_API_KEY"))
    default_model: str = Field(default="gpt-4o")
    embedding_model: str = Field(default="text-embedding-3-small")


class DatabaseSettings(BaseModel):
    """Database connection settings."""

    service_url: str = Field(default_factory=lambda: os.getenv("TIMESCALE_SERVICE_URL"))


class VectorStoreSettings(BaseModel):
    """Settings for the VectorStore."""

    #table_name: str = "embeddings"
    table_name: str = "document_embeddings"  # changed to this temporarily for testing
    embedding_dimensions: int = 1536
    time_partition_interval: timedelta = timedelta(days=7)

class ChunkingSettings(BaseModel):
    """Settings for the HybridChunker."""
    embedding_model: str = "text-embedding-3-small"  # must match your OpenAI embedding model
    max_tokens: int = 8191  # text-embedding-3-small supports up to 8191 tokens
    heading_token_reserve: int = 512  # headroom reserved for contextualize() heading prefix

class RedisSettings(BaseModel):
    """Settings for the Redis broker/backend."""
    url: str = Field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))

class LangfuseSettings(BaseModel):
    """Settings for Langfuse observability."""
    public_key: str = Field(default_factory=lambda: os.getenv("LANGFUSE_PUBLIC_KEY", ""))
    secret_key: str = Field(default_factory=lambda: os.getenv("LANGFUSE_SECRET_KEY", ""))
    host: str = Field(default_factory=lambda: os.getenv("LANGFUSE_HOST", "https://us.cloud.langfuse.com"))

class ChatbotSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CHATBOT_",
        env_file=".env",
        extra="ignore",
    )

    model: str = "openai:gpt-4o"
    cheap_model: str = "openai:gpt-4o-mini"
    temperature: float = 0.0
    max_output_tokens: int = 800
    max_history_turns: int = 20
    session_ttl_seconds: int = 3600
    max_tool_iterations: int = 4
    request_timeout_seconds: int = 30

class RetrievalSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RETRIEVAL_",
        env_file=".env",
        extra="ignore",
    )

    top_k: int = 5
    distance_threshold: float = 0.45
    max_context_tokens: int = 6000
    metadata_filename_allowlist: Optional[list[str]] = None
    metadata_filetype_allowlist: list[str] = Field(default_factory=lambda: [".pdf", ".docx"])
    embedding_cache_ttl_seconds: int = 86400

class GuardrailSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GUARDRAIL_",
        env_file=".env",
        extra="ignore",
    )

    # Input
    max_input_chars: int = 2000
    min_input_chars: int = 1
    block_pii_in_input: bool = True
    block_jailbreak_attempts: bool = True
    allowed_languages: list[str] = Field(default_factory=lambda: ["en"])
    enable_llm_judge: bool = False
    # Relevance gate
    relevance_gate_enabled: bool = True
    relevance_out_of_scope_message: str = (
        "I can only help with topics covered in my knowledge base. "
        "Could you rephrase your question or ask something more specific?"
    )
    # Output
    require_citations: bool = True
    scrub_pii_in_output: bool = True
    scrub_profanity_in_output: bool = True
    refuse_when_no_context: bool = True
    # Conversation / operational
    max_turns_per_session: int = 50
    rate_limit_per_minute: int = 30


class OpsSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPS_",
        env_file=".env",
        extra="ignore",
    )

    enable_structured_logs: bool = True
    enable_rate_limiting: bool = True
    enable_readiness_probe: bool = True
    enable_embedding_cache: bool = True
    enable_otel_tracing: bool = False
    
class Settings(BaseModel):
    """Main settings class combining all sub-settings."""

    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    vector_store: VectorStoreSettings = Field(default_factory=VectorStoreSettings)
    chunking: ChunkingSettings = Field(default_factory=ChunkingSettings)  
    redis: RedisSettings = Field(default_factory=RedisSettings) 
    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)
    chatbot: ChatbotSettings = Field(default_factory=ChatbotSettings)         
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)  
    guardrails: GuardrailSettings = Field(default_factory=GuardrailSettings)  
    ops: OpsSettings = Field(default_factory=OpsSettings)                     



@lru_cache()
def get_settings() -> Settings:
    """Create and return a cached instance of the Settings."""
    settings = Settings()
    setup_logging()
    return settings
