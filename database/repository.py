"""
database/repository.py — async repository layer (bulk-first, no per-row loops).

Performance targets:
  - bulk_check_existing_hashes : single SELECT ... WHERE hash IN (...)
  - bulk_insert_new_reviews    : single INSERT with returning
  - mark_as_sent               : single UPDATE WHERE id IN (...)
All operations are designed to handle 200 reviews in <100 ms outside parser time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from datetime import timezone as _tz
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import Client, Organization, ParseLog, ParseStatus, Review, ReviewSource

logger = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# ── ClientRepository ──────────────────────────────────────────────────────────

class ClientRepository:
    """Read-side queries for clients and their organisations."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_active_clients_with_orgs(self) -> list[Client]:
        # Уровень 1: внутри функции (4 пробела)
            stmt = (
                select(Client)
                .where(Client.is_active.is_(True))
                .options(selectinload(Client.organizations))
                .order_by(Client.id)
                )

            result = await self._s.execute(stmt)
        # Вот здесь мы создаем 'clients'. Все, что ниже, должно быть СТРОГО под этой строкой.
            clients = result.scalars().unique().all()

        # Фильтруем неактивные орги в Python
            for client in clients:
        # Уровень 2: внутри цикла (8 пробелов)
                client.organizations = [o for o in client.organizations if o.is_active]

            logger.debug("get_active_clients_with_orgs → %d clients", len(clients))

        # Уровень 1: возврат из функции (снова 4 пробела, СТРОГО под 'stmt' и 'result')
            return list(clients) 


# ── ReviewRepository ──────────────────────────────────────────────────────────

class ReviewRepository:
    """Bulk-oriented write + read for reviews."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # ── deduplication ─────────────────────────────────────────────────────────

    async def bulk_check_existing_hashes(
        self,
        org_id: int,
        source: ReviewSource,
        hashes: list[str],
    ) -> set[str]:
        """
        Single SELECT to fetch which of the given hashes already exist in DB.
        Returns a set for O(1) membership checks.

        Why not EXISTS / NOT EXISTS per hash: that would be N round-trips.
        One WHERE-IN query is consistently <5 ms for 500 hashes on PG.
        """
        if not hashes:
            return set()

        stmt = select(Review.review_hash).where(
            Review.organization_id == org_id,
            Review.source == source,
            Review.review_hash.in_(hashes),
        )
        result = await self._s.execute(stmt)
        existing: set[str] = set(result.scalars().all())
        logger.debug(
            "bulk_check_existing_hashes org=%d source=%s: %d/%d already exist",
            org_id, source.value, len(existing), len(hashes),
        )
        return existing

    # ── insert ────────────────────────────────────────────────────────────────

    async def bulk_insert_new_reviews(
        self,
        reviews: list[dict],
    ) -> list[Review]:
        """
        Single INSERT … RETURNING using PostgreSQL dialect.
        Handles ON CONFLICT DO NOTHING as a safety net (race condition between
        concurrent parsers for the same org).

        `reviews` items must include:
          organization_id, source, review_hash, author, rating,
          review_date, [text], [avatar_url], [user_status]
        """
        if not reviews:
            return []

        now = _utcnow().replace(tzinfo=None)  # БД ожидает naive datetime
        rows = [
            {
                **r,
                "parsed_at": now,
                "created_at": now,
                "is_sent_to_client": False,
            }
            for r in reviews
        ]

        stmt = (
            pg_insert(Review)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=["organization_id", "source", "review_hash"]
            )
            .returning(Review)
        )

        result = await self._s.execute(stmt)
        inserted: list[Review] = list(result.scalars().all())
        await self._s.flush()

        logger.info(
            "bulk_insert_new_reviews: inserted %d / attempted %d",
            len(inserted), len(reviews),
        )
        return inserted

    # ── read for delivery ─────────────────────────────────────────────────────

    async def get_unsent_reviews_for_client(
        self,
        client_id: int,
    ) -> list[Review]:
        """
        Fetch all unsent reviews for a given client across all their orgs.
        Ordered oldest-first so delivery is chronologically correct.
        """
        stmt = (
            select(Review)
            .join(Review.organization)
            .where(
                Organization.client_id == client_id,
                Review.is_sent_to_client.is_(False),
            )
            .order_by(Review.parsed_at.asc())
        )
        result = await self._s.execute(stmt)
        reviews = list(result.scalars().all())
        logger.debug(
            "get_unsent_reviews_for_client client=%d → %d reviews",
            client_id, len(reviews),
        )
        return reviews

    # ── bulk updates ──────────────────────────────────────────────────────────

    async def mark_as_sent(self, review_ids: list[int]) -> None:
        """
        Single UPDATE … WHERE id IN (…).
        Never loops — one round-trip regardless of list size.
        """
        if not review_ids:
            return

        stmt = (
            update(Review)
            .where(Review.id.in_(review_ids))
            .values(
                is_sent_to_client=True,
                sent_at=_utcnow().replace(tzinfo=None),
            )
            # synchronize_session=False is required for bulk updates in async
            .execution_options(synchronize_session=False)
        )
        await self._s.execute(stmt)
        await self._s.flush()
        logger.debug("mark_as_sent: %d review(s) marked", len(review_ids))

    async def update_ai_response(
        self,
        review_id: int,
        response: str,
    ) -> None:
        """Set AI-generated response text for a single review."""
        stmt = (
            update(Review)
            .where(Review.id == review_id)
            .values(
                ai_response=response,
                ai_generated_at=_utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        await self._s.execute(stmt)
        await self._s.flush()
        logger.debug("update_ai_response: review_id=%d updated", review_id)


# ── ParseLogRepository ────────────────────────────────────────────────────────

class ParseLogRepository:
    """Write-only repository for parse audit logs."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create_log(
        self,
        *,
        source: ReviewSource,
        status: ParseStatus,
        started_at: datetime,
        organization_id: int | None = None,
        reviews_found: int = 0,
        new_reviews_count: int = 0,
        error_message: str | None = None,
    ) -> ParseLog:
        """
        Insert a single parse audit record.
        duration_ms is calculated automatically from started_at → now.
        """
        duration_ms = int(
            (_utcnow() - started_at).total_seconds() * 1000
        )
        # Strip timezone — колонка TIMESTAMP WITHOUT TIME ZONE
        started_at_naive = started_at.replace(tzinfo=None) if started_at.tzinfo else started_at
        log = ParseLog(
            organization_id=organization_id,
            source=source,
            status=status,
            reviews_found=reviews_found,
            new_reviews_count=new_reviews_count,
            error_message=error_message,
            duration_ms=duration_ms,
            started_at=started_at_naive,
        )
        self._s.add(log)
        await self._s.flush()
        logger.debug(
            "ParseLog created: org=%s source=%s status=%s new=%d ms=%d",
            organization_id, source.value, status.value,
            new_reviews_count, duration_ms,
        )
        return log
