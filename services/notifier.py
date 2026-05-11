"""
services/notifier.py — high-level delivery orchestrator.

Wires TelegramNotifier + ReviewRepository + AIResponder together.
Called from scheduler/runner.py after bulk_insert_new_reviews().
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update

from database.models import Client, Review
from database.repository import ReviewRepository
from services.telegram_bot import TelegramNotifier

logger = logging.getLogger(__name__)


async def deliver_reviews_to_client(
    *,
    session: AsyncSession,
    notifier: TelegramNotifier,
    client: Client,
    reviews: list[Review],
) -> None:
    """
    Send each review to the client's Telegram chat, then bulk-mark as sent.

    on_user_blocked: sets client.is_active = False in the same session.
    """
    sent_ids: list[int] = []

    async def _deactivate_client() -> None:
        """Called if the client has blocked the bot."""
        from sqlalchemy import update as sa_update
        stmt = (
            sa_update(Client)
            .where(Client.id == client.id)
            .values(is_active=False)
            .execution_options(synchronize_session=False)
        )
        await session.execute(stmt)
        await session.flush()
        logger.warning(
            "deliver_reviews_to_client: client id=%d marked inactive (blocked bot)",
            client.id,
        )

    for review in reviews:
        org = review.organization
        ok = await notifier.send_review_to_client(
            chat_id=client.telegram_chat_id,
            organization_name=org.name if org else "Организация",
            source=review.source.value,
            author=review.author,
            rating=review.rating,
            text=review.text or "",
            review_date=review.review_date,
            ai_response=review.ai_response,
            on_user_blocked=_deactivate_client(),
        )
        if ok:
            sent_ids.append(review.id)

    if sent_ids:
        repo = ReviewRepository(session)
        await repo.mark_as_sent(sent_ids)
        logger.info(
            "deliver_reviews_to_client: client=%d sent %d/%d reviews",
            client.id, len(sent_ids), len(reviews),
        )
