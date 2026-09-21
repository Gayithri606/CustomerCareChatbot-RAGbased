"""Query condenser for the customer-care chatbot (ADR-001, Option B).

Rewrites a follow-up message ("And what about ductwork for that?") into a
standalone question ("What ductwork is required for a range hood?") using
the conversation history. The relevance gate then embeds the rewritten
query, fixing the gate's history-blindness.

This is the standard production "history-aware retrieval" pattern. Uses
ChatbotSettings.cheap_model (gpt-4o-mini) — one small call every turn,
including the first: even a standalone first message can use casual
wording that lands far from the documentation in embedding space.

Design notes:
- History is rendered as a plain-text transcript into the prompt, NOT
  passed as Pydantic AI message_history. Passing message_history would
  suppress this agent's own system prompt (Pydantic AI reuses the
  history's), turning the condenser into a second chatbot. See ADR-004
  for the full reasoning and the two failure modes it prevents.
- The main agent's replies are structured output (tool-call based), so
  the transcript is mostly customer turns — which is what matters for
  resolving "that"/"it" references anyway.
- Fail-open: any error returns the raw message unchanged. A broken
  condenser must never block the chat pipeline (mirrors relevance_gate's
  error handling).
"""

from __future__ import annotations

import logging

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from config.settings import get_settings

logger = logging.getLogger(__name__)

_cs = get_settings().chatbot

CONDENSER_SYSTEM_PROMPT = """\
You rewrite a customer's latest chat message into ONE standalone, clear
question. If a conversation transcript is provided, use it to resolve
references like "that", "it", or "the second one".

You also rephrase casual or vague wording into the more precise technical
terms a support document would use for the same meaning — for example,
"the vent pipe" -> "the duct", or "it's really loud" -> "it is making
excessive noise". Only do this when the customer's own words already
convey that meaning.

Rules:
- Output ONLY the rewritten question. No preamble, no quotes, no notes.
- Preserve the customer's intent exactly. Never add topics, products, or
  details that are not already in the message or the transcript. Do not
  guess at a specific product or part the customer did not name.
- If the message is already clear and precise, return it unchanged.
"""

# Module-level singleton, mirroring agent.py Decision C1. No tools, no
# deps — a pure text-in/text-out helper on the cheap model.
condenser_agent: Agent[None, str] = Agent(
    model=_cs.cheap_model,
    output_type=str,
    system_prompt=CONDENSER_SYSTEM_PROMPT,
)


def _render_transcript(
    history: list[ModelMessage],
    max_lines: int = 6,
) -> str:
    """Flatten message history into 'Customer:/Assistant:' lines.

    Allowlist extraction (see ADR-004): only UserPromptPart and TextPart
    are rendered. System prompts and tool traffic never match the filter,
    so they can never leak into the condenser's prompt. Keeps only the
    most recent `max_lines` lines to bound token cost.
    """
    lines: list[str] = []
    for msg in history:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    lines.append(f"Customer: {part.content}")
        elif isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, TextPart) and part.content:
                    lines.append(f"Assistant: {part.content}")
    return "\n".join(lines[-max_lines:])


async def condense_query(
    message: str,
    history: list[ModelMessage],
) -> str:
    """Return a standalone, vocabulary-normalized version of `message`.

    Runs on every turn, including the first — see module docstring.
    Fails open: any error returns the raw message.
    """
    transcript = _render_transcript(history)

    if transcript:
        prompt = (
            f"Conversation transcript:\n{transcript}\n\n"
            f"Latest customer message:\n{message}"
        )
    else:
        prompt = f"Customer message:\n{message}"

    try:
        result = await condenser_agent.run(prompt)
        condensed = result.output.strip()
    except Exception as exc:
        logger.warning(
            "condenser_failed err=%s — using raw message",
            exc.__class__.__name__,
        )
        return message

    if not condensed:
        return message

    logger.info("condensed_query original=%r condensed=%r", message, condensed)
    return condensed