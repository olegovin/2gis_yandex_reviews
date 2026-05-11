"""
services/ai_responder.py — GPT-4o-mini response generator for positive reviews.

Design:
  - rating < 4  → skip (return None), never call OpenAI
  - rating >= 4 → generate a warm 2-3 sentence reply
  - Retry on RateLimit / 5xx via tenacity
  - Semaphore(5) in bulk mode to stay under OpenAI RPM limits
  - Every request logs token usage + estimated USD cost
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from openai import AsyncOpenAI, APIStatusError, RateLimitError
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# ── gpt-4o-mini pricing (USD per 1 000 tokens, as of mid-2024) ───────────────
_PRICE_INPUT_PER_1K  = 0.000_150   # $0.150 / 1M input tokens
_PRICE_OUTPUT_PER_1K = 0.000_600   # $0.600 / 1M output tokens

# ── system prompt template ────────────────────────────────────────────────────
_SYSTEM_TMPL = (
    "Ты — представитель организации {org_name}. "
    "Отвечай на положительные отзывы клиентов кратко (2-3 предложения), "
    "тепло, без шаблонности. "
    "Обращайся по имени если возможно. "
    "Не используй эмодзи. Без подписи в конце."
)


# ── cost tracking ─────────────────────────────────────────────────────────────

@dataclass
class UsageStats:
    """Accumulated token usage and cost for a session / bulk job."""
    input_tokens:  int   = 0
    output_tokens: int   = 0
    requests_made: int   = 0
    requests_skipped: int = 0

    @property
    def total_cost_usd(self) -> float:
        return (
            self.input_tokens  / 1000 * _PRICE_INPUT_PER_1K +
            self.output_tokens / 1000 * _PRICE_OUTPUT_PER_1K
        )

    def add(self, input_t: int, output_t: int) -> None:
        self.input_tokens  += input_t
        self.output_tokens += output_t
        self.requests_made += 1

    def __str__(self) -> str:
        return (
            f"requests={self.requests_made} skipped={self.requests_skipped} "
            f"input_tokens={self.input_tokens} output_tokens={self.output_tokens} "
            f"cost=${self.total_cost_usd:.4f}"
        )


# ── tenacity helpers ──────────────────────────────────────────────────────────

def _log_retry(state: RetryCallState) -> None:
    exc = state.outcome.exception() if state.outcome else None
    logger.warning(
        "OpenAI retry #%d — %s: %s",
        state.attempt_number, type(exc).__name__, exc,
    )


# ── main class ────────────────────────────────────────────────────────────────

class AIResponder:

    def __init__(
        self,
        api_key: str,
        model: str = "openai/gpt-4o",
    ) -> None:
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://polza.ai/api/v1",
            timeout=15.0,
            max_retries=0,
        )
        self._model = model
        self._bulk_semaphore = asyncio.Semaphore(5)  # max 5 parallel calls

    # ── single review ─────────────────────────────────────────────────────────

    async def generate_response(
        self,
        review: dict,
        organization_name: str,
        stats: Optional[UsageStats] = None,
    ) -> str | None:
        """
        Generate a reply for one review.

        Returns:
          - None  if rating < 4 (skip negative/neutral reviews)
          - str   the generated reply text
          - None  if OpenAI call fails after all retries (logged, not raised)
        """
        rating: int = review.get("rating", 0)

        if rating < 4:
            if stats:
                stats.requests_skipped += 1
            logger.debug(
                "generate_response: skipped (rating=%d < 4) author=%s",
                rating, review.get("author", "?"),
            )
            return None

        author   = review.get("author") or "Гость"
        text     = review.get("text") or ""
        source   = review.get("source", "")

        try:
            response_text, usage = await self._call_openai(
                org_name=organization_name,
                author=author,
                rating=rating,
                text=text,
            )
        except Exception as exc:
            # All retries exhausted — log and return None so one bad review
            # doesn't kill the entire bulk job.
            logger.error(
                "generate_response: OpenAI failed after retries "
                "author=%s org=%s: %s",
                author, organization_name, exc,
            )
            return None

        if stats and usage:
            stats.add(usage["input"], usage["output"])

        logger.info(
            "generate_response: ok | org=%s author=%s rating=%d src=%s "
            "in=%d out=%d cost=$%.5f",
            organization_name, author, rating, source,
            usage["input"], usage["output"],
            usage["input"] / 1000 * _PRICE_INPUT_PER_1K +
            usage["output"] / 1000 * _PRICE_OUTPUT_PER_1K,
        )
        return response_text

    # ── bulk ──────────────────────────────────────────────────────────────────

    async def generate_responses_bulk(
        self,
        reviews: list[dict],
        organization_name: str,
    ) -> tuple[list[str | None], UsageStats]:
        """
        Process a list of reviews in parallel, respecting semaphore(5).

        Returns:
          - list[str | None]  parallel to input (None = skipped or failed)
          - UsageStats        aggregated token usage and cost
        """
        stats = UsageStats()

        async def _one(review: dict) -> str | None:
            async with self._bulk_semaphore:
                return await self.generate_response(review, organization_name, stats)

        results: list[str | None] = await asyncio.gather(
            *[_one(r) for r in reviews],
            return_exceptions=False,   # errors already handled inside generate_response
        )

        logger.info(
            "generate_responses_bulk: org=%s total=%d %s",
            organization_name, len(reviews), stats,
        )
        return results, stats

    # ── internal OpenAI call with retry ──────────────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=4, max=30),
        retry=retry_if_exception_type((RateLimitError, APIStatusError)),
        before_sleep=_log_retry,
        reraise=True,
    )
    async def _call_openai(
        self,
        *,
        org_name: str,
        author: str,
        rating: int,
        text: str,
    ) -> tuple[str, dict]:
        """
        Single OpenAI chat completion call.
        Retried up to 3× on RateLimitError or 5xx APIStatusError.
        Returns (reply_text, {input: N, output: N}).
        """
        completion = await self._client.chat.completions.create(
            model=self._model,
            temperature=0.8,
            max_tokens=200,
            messages=[
                {
                    "role": "system",
                    "content": _SYSTEM_TMPL.format(org_name=org_name),
                },
                {
                    "role": "user",
                    "content": (
                        f"Имя: {author}\n"
                        f"Оценка: {rating}/5\n"
                        f"Отзыв: {text}"
                    ),
                },
            ],
        )

        reply = (completion.choices[0].message.content or "").strip()
        usage = {
            "input":  completion.usage.prompt_tokens     if completion.usage else 0,
            "output": completion.usage.completion_tokens if completion.usage else 0,
        }
        return reply, usage
