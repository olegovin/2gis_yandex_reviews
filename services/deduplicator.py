"""
services/deduplicator.py — hash-based deduplication for parsed reviews.

Design goals:
  - calculate_review_hash  : deterministic SHA-256, normalised input
  - filter_new_reviews     : one DB round-trip for the entire batch (bulk IN query)
  - 200 reviews processed in <100 ms outside parser time
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ReviewSource
from database.repository import ReviewRepository

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ── Normalisation ─────────────────────────────────────────────────────────────

_WHITESPACE_RE = re.compile(r"\s+")


def _normalise(value: str) -> str:
    """Lowercase → strip → collapse internal whitespace."""
    return _WHITESPACE_RE.sub(" ", value.strip().lower())


# ── Hash ──────────────────────────────────────────────────────────────────────

def calculate_review_hash(author: str, review_date: str, text: str) -> str:
    """
    Stable 64-char hex SHA-256 fingerprint for a review.

    Fields chosen deliberately:
      - author      : identifies who wrote it
      - review_date : same author can post multiple reviews at different times
      - text        : catches edits (hash changes → treated as new review)

    All inputs are normalised before hashing so that minor whitespace/case
    differences in source HTML don't produce phantom duplicates.

    Returns a 64-character hex string (SHA-256 digest).
    """
    normalised = "|".join([
        _normalise(author),
        _normalise(review_date),
        _normalise(text or ""),   # text can be None for rating-only reviews
    ])
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


# ── Freshness filter ──────────────────────────────────────────────────────────

_MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

def _is_fresh(review_date: str, max_days: int = 1) -> bool:
    """
    Возвращает True если отзыв свежее max_days дней.
    Форматы дат: "8 мая", "3 августа 2025", "вчера", "2 дня назад", "сегодня".
    Если дату не удалось распознать — считаем свежим (не фильтруем).
    """
    if not review_date:
        return True
    
    d = review_date.lower().strip()
    
    # Относительные даты — всегда свежие
    for fresh_word in ["сегодня", "вчера", "час", "мин", "только что", "дн", "недел"]:
        if fresh_word in d:
            return True
    
    now = datetime.now()
    
    # Формат "8 мая 2025" или "8 мая"
    parts = d.replace(",", "").replace("изменён", "").split()
    try:
        if len(parts) >= 2:
            day = int(parts[0])
            month = _MONTHS_RU.get(parts[1])
            year = int(parts[2]) if len(parts) >= 3 else now.year
            if month:
                review_dt = datetime(year, month, day)
                delta = now - review_dt
                return delta.days <= max_days
    except (ValueError, IndexError):
        pass
    
    return True  # не смогли распознать — не фильтруем


# ── Deduplication ─────────────────────────────────────────────────────────────

async def filter_new_reviews(
    session: AsyncSession,
    org_id: int,
    source: ReviewSource,
    parsed_reviews: list[dict],
) -> list[dict]:
    """
    Given a batch of freshly parsed review dicts, return only the ones
    that are NOT yet stored in the database.

    Steps:
      1. Compute SHA-256 hash for every parsed review (pure Python, ~0 ms).
      2. Fire ONE bulk SELECT … WHERE hash IN (…) to get already-known hashes.
      3. Return only reviews whose hash is not in the DB set.

    Each input dict must contain at minimum:
        author (str), review_date (str), text (str | None)
    The computed hash is injected into the returned dicts under "review_hash".

    Performance: 200 reviews → ~0.1 ms hashing + ~5 ms single PG round-trip.
    """
    if not parsed_reviews:
        return []

    # Step 0 — фильтруем старые отзывы (старше 90 дней не отправляем)
    fresh_reviews = [r for r in parsed_reviews if _is_fresh(r.get("review_date", ""))]
    stale_count = len(parsed_reviews) - len(fresh_reviews)
    if stale_count > 0:
        logger.debug("filter_new_reviews: filtered out %d stale reviews", stale_count)
    parsed_reviews = fresh_reviews

    if not parsed_reviews:
        return []

    # Step 1 — attach hashes to every review (no DB, pure CPU)
    for review in parsed_reviews:
        review["review_hash"] = calculate_review_hash(
            author=review.get("author", ""),
            review_date=review.get("review_date", ""),
            text=review.get("text") or "",
        )

    all_hashes: list[str] = [r["review_hash"] for r in parsed_reviews]

    # Step 2 — single bulk query
    repo = ReviewRepository(session)
    existing_hashes: set[str] = await repo.bulk_check_existing_hashes(
        org_id=org_id,
        source=source,
        hashes=all_hashes,
    )

    # Step 3 — filter in Python (O(1) set lookup per review)
    new_reviews = [r for r in parsed_reviews if r["review_hash"] not in existing_hashes]

    logger.info(
        "filter_new_reviews org=%d source=%s: %d parsed, %d existing, %d new",
        org_id, source.value,
        len(parsed_reviews), len(existing_hashes), len(new_reviews),
    )
    return new_reviews
