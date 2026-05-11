"""
scheduler/runner.py — ParserOrchestrator: sequential task runner.

WHY SEQUENTIAL (not asyncio.gather):
  - Single-core VPS: parallel Playwright contexts fight for the same CPU.
    Sequential keeps CPU ~60-70% busy instead of 100% + scheduler jitter.
  - Yandex rate-limits by IP, not by session. Parallel requests from the same
    IP increase captcha probability significantly.
  - Predictable memory: one context at a time, ~200 MB peak vs ~600 MB parallel.
  - APScheduler max_instances=1 already prevents overlap — gather adds no benefit.

WHY APScheduler OVER SYSTEM CRON (answer requested in prompt):
  - Chromium lives between cycles. cron would cold-start it every hour (+5 s,
    +80 MB RSS per invocation). APScheduler starts it once at process boot.
  - SQLAlchemy connection pool is reused across cycles — no reconnect overhead.
  - Signal handling and graceful shutdown are trivial in-process.
  - max_instances=1 + coalesce=True give the same "no overlap" guarantee as
    cron's single-instance semantics, with zero external configuration.
  - Misfire detection: if the VPS is briefly overloaded and misses the :00 mark,
    misfire_grace_time=300 allows a delayed run rather than skipping silently.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession
from app_config import settings
from database.engine import AsyncSessionFactory
from database.models import Client, Organization, ParseStatus, ReviewSource
from database.repository import (
    ClientRepository,
    ParseLogRepository,
    ReviewRepository,
)
from parsers.exceptions import CaptchaFailedException, PageLoadException, ParserException
from parsers.two_gis import TwoGisParser
from parsers.yandex_maps import YandexMapsParser
from services.ai_responder import AIResponder
from services.browser_pool import BrowserManager
from services.deduplicator import calculate_review_hash, filter_new_reviews
from services.telegram_bot import TelegramNotifier

logger = logging.getLogger(__name__)

# Pause between consecutive parse tasks (anti-ban + CPU breathing room)
_INTER_TASK_SLEEP: float = 2.0

# Pause between individual Telegram messages to the same client
_INTER_MESSAGE_SLEEP: float = 0.5

# On first run: only forward the N most recent reviews, silently save the rest
_FIRST_RUN_SEND_LIMIT: int = 3


# ── orchestrator ──────────────────────────────────────────────────────────────

class ParserOrchestrator:
    """
    Coordinates the full parse→deduplicate→AI→notify cycle.

    All dependencies are injected so they can be shared across cycles
    (browser pool, DB session factory, etc.).
    """

    def __init__(
        self,
        browser_manager: BrowserManager,
        ai_responder: AIResponder,
        tg_notifier: TelegramNotifier,
    ) -> None:
        self.browser_manager = browser_manager
        self.ai_responder = ai_responder
        self.tg_notifier = tg_notifier

    # ── public: called by APScheduler ─────────────────────────────────────────

    async def run_cycle(self) -> None:
        """
        One full hourly cycle.  Estimated duration: 6-10 minutes for 15 clients
        × 2 sources (sequential, ~20-30 s per source including scroll time).
        """
        cycle_start = time.monotonic()
        logger.info("=== Cycle started ===")

        # Fetch clients + orgs in a short-lived session
        async with AsyncSessionFactory() as session:
            clients = await ClientRepository(session).get_active_clients_with_orgs()

        # Build flat task list: (client, org, source_enum, url)
        tasks: list[tuple[Client, Organization, ReviewSource, str]] = []
        for client in clients:
            for org in client.organizations:
                if org.yandex_url:
                    tasks.append((client, org, ReviewSource.yandex, org.yandex_url))
                if org.two_gis_url:
                    tasks.append((client, org, ReviewSource.two_gis, org.two_gis_url))

        logger.info("Total tasks this cycle: %d", len(tasks))

        for idx, (client, org, source, url) in enumerate(tasks, start=1):
            logger.info(
                "[%d/%d] Processing %s / %s / %s",
                idx, len(tasks), client.name, org.name, source.value,
            )
            try:
                await self._process_single(client, org, source, url)
            except Exception:
                logger.exception(
                    "Task failed: client=%s org=%s source=%s",
                    client.name, org.name, source.value,
                )
                await self.tg_notifier.send_error_to_admin(
                    client_name=client.name,
                    source=source.value,
                    error="Необработанное исключение — см. логи сервера",
                )

            # Без паузы между задачами - прокси решают проблему банов
            # Паузы отключены для ускорения парсинга через прокси
            # if idx < len(tasks):
            #     if source == ReviewSource.yandex:
            #         pause = random.uniform(15, 45)
            #     else:
            #         pause = random.uniform(5, 15)
            #     logger.info("Пауза %.1f сек перед следующей задачей", pause)
            #     await asyncio.sleep(pause)

        duration = time.monotonic() - cycle_start
        logger.info("=== Cycle finished in %.1f s ===", duration)

    # ── private: one client × one source ─────────────────────────────────────

    async def _process_single(
        self,
        client: Client,
        org: Organization,
        source: ReviewSource,
        url: str,
    ) -> None:
        """
        Full pipeline for a single (organisation, source) pair:
          1. Parse (with retry inside parser via tenacity)
          2. First-run detection
          3. Hash + deduplicate
          4. First-run capping
          5. AI response generation (parallel, semaphore-limited)
          6. Bulk DB insert
          7. Sequential Telegram delivery
          8. ParseLog
        """
        task_start = time.monotonic()
        status = ParseStatus.success
        error_msg: str | None = None
        parsed_reviews: list[dict] = []
        new_reviews: list[dict] = []
        saved_reviews = []

        try:
            # ── 1. Parse ──────────────────────────────────────────────────────
            page, context = await self.browser_manager.get_page()
            try:
                parser = (
                    TwoGisParser(page)
                    if source == ReviewSource.two_gis
                    else YandexMapsParser(page)
                )
                parsed_reviews = await parser.parse(url)
            finally:
                await self.browser_manager.release_page(page, context)

            logger.info(
                "_process_single: parsed %d reviews org=%s src=%s",
                len(parsed_reviews), org.name, source.value,
            )

            async with AsyncSessionFactory() as session:
                async with session.begin():
                    review_repo = ReviewRepository(session)

                    # ── 2. First-run detection ─────────────────────────────
                    is_first_run = await review_repo.is_first_run(org.id, source)

                    # ── 3. Hash + deduplicate ──────────────────────────────
                    # Inject hash + metadata before passing to filter
                    for r in parsed_reviews:
                        r["review_hash"] = calculate_review_hash(
                            r.get("author", ""),
                            r.get("review_date", ""),
                            r.get("text") or "",
                        )
                        r["organization_id"] = org.id
                        r["source"] = source

                    new_reviews = await filter_new_reviews(
                        session=session,
                        org_id=org.id,
                        source=source,
                        parsed_reviews=parsed_reviews,
                    )

                    # ── 4. First-run cap ───────────────────────────────────
                    if is_first_run and len(new_reviews) > _FIRST_RUN_SEND_LIMIT:
                        logger.info(
                            "_process_single: first run, capping to %d (silent-saving %d)",
                            _FIRST_RUN_SEND_LIMIT,
                            len(new_reviews) - _FIRST_RUN_SEND_LIMIT,
                        )
                        silent = new_reviews[_FIRST_RUN_SEND_LIMIT:]
                        await review_repo.bulk_insert_new_reviews(
                            [_to_row(r, org.id, source, mark_sent=True) for r in silent]
                        )
                        new_reviews = new_reviews[:_FIRST_RUN_SEND_LIMIT]

                    # ── 5. AI responses ────────────────────────────────────
                    if new_reviews:
                        ai_results, ai_stats = await self.ai_responder.generate_responses_bulk(
                            reviews=new_reviews,
                            organization_name=org.name,
                        )
                        for review, ai_text in zip(new_reviews, ai_results):
                            review["ai_response"] = ai_text   # may be None
                        logger.info(
                            "_process_single: AI done — %s", ai_stats
                        )

                    # ── 6. Bulk DB insert ──────────────────────────────────
                    rows_to_insert = [_to_row(r, org.id, source, mark_sent=False) for r in new_reviews]
                    logger.info("Inserting %d rows into reviews", len(rows_to_insert))
                    try:
                        saved_reviews = await review_repo.bulk_insert_new_reviews(rows_to_insert)
                        logger.info("Inserted %d reviews successfully", len(saved_reviews))
                    except Exception as insert_exc:
                        logger.exception("bulk_insert FAILED: %s", insert_exc)
                        raise

            # ── 7. Sequential Telegram delivery ───────────────────────────
            # Separate session: delivery is independent of parse transaction
            if saved_reviews:
                await self._deliver_to_client(client, org, saved_reviews)

        except CaptchaFailedException as exc:
            status = ParseStatus.captcha_failed
            error_msg = str(exc)
            logger.error(
                "_process_single: captcha failed org=%s src=%s",
                org.name, source.value,
            )
            await self.tg_notifier.send_error_to_admin(
                client_name=client.name,
                source=source.value,
                error=f"Капча: {exc}",
            )

        except (PageLoadException, ParserException) as exc:
            status = ParseStatus.error
            error_msg = str(exc)
            logger.error(
                "_process_single: parser error org=%s src=%s: %s",
                org.name, source.value, exc,
            )
            await self.tg_notifier.send_error_to_admin(
                client_name=client.name,
                source=source.value,
                error=str(exc),
            )

        finally:
            # ── 8. ParseLog — always written, even on failure ──────────────
            duration_ms = int((time.monotonic() - task_start) * 1000)
            async with AsyncSessionFactory() as session:
                async with session.begin():
                    await ParseLogRepository(session).create_log(
                        organization_id=org.id,
                        source=source,
                        status=status,
                        started_at=__import__("datetime").datetime.now(
                            tz=__import__("datetime").timezone.utc
                        ),
                        reviews_found=len(parsed_reviews),
                        new_reviews_count=len(saved_reviews),
                        error_message=error_msg,
                    )
            logger.info(
                "_process_single: done org=%s src=%s status=%s new=%d ms=%d",
                org.name, source.value, status.value,
                len(saved_reviews), duration_ms,
            )

    # ── Telegram delivery helper ──────────────────────────────────────────────

    async def _deliver_to_client(
        self,
        client: Client,
        org: Organization,
        reviews: list,
    ) -> None:
        """
        Send reviews one by one with a short sleep between messages.
        Stops immediately if client has blocked the bot.
        """
        sent_ids: list[int] = []
        client_blocked = False

        for review in reviews:
            if client_blocked:
                break

            async def _on_blocked() -> None:
                nonlocal client_blocked
                client_blocked = True
                async with AsyncSessionFactory() as s:
                    async with s.begin():
                        from sqlalchemy import update as _upd
                        from database.models import Client as _C
                        await s.execute(
                            _upd(_C)
                            .where(_C.id == client.id)
                            .values(is_active=False)
                            .execution_options(synchronize_session=False)
                        )
                logger.warning(
                    "_deliver_to_client: client id=%d blocked bot — deactivated",
                    client.id,
                )
                await self.tg_notifier.send_error_to_admin(
                    client_name=client.name,
                    source="system",
                    error=f"Клиент {client.name} заблокировал бота — деактивирован",
                )

            ok = await self.tg_notifier.send_review_to_client(
                chat_id=client.telegram_chat_id,
                organization_name=org.name,
                source=review.source.value,
                author=review.author,
                rating=review.rating,
                text=review.text or "",
                review_date=review.review_date,
                ai_response=review.ai_response,
            )
            if ok:
                sent_ids.append(review.id)

            await asyncio.sleep(_INTER_MESSAGE_SLEEP)

        if sent_ids:
            async with AsyncSessionFactory() as session:
                async with session.begin():
                    await ReviewRepository(session).mark_as_sent(sent_ids)


# ── helpers ───────────────────────────────────────────────────────────────────

def _to_row(
    r: dict,
    org_id: int,
    source: ReviewSource,
    mark_sent: bool,
) -> dict:
    """Convert a parsed-review dict to a ReviewRepository insert row."""
    return {
        "organization_id": org_id,
        "source": source,
        "review_hash": r["review_hash"],
        "author": r.get("author", "Anonymous"),
        "rating": r.get("rating", 0),
        "text": r.get("text"),
        "review_date": r.get("review_date", ""),
        "user_status": r.get("user_status"),
        "ai_response": r.get("ai_response"),
        "is_sent_to_client": mark_sent,
    }


# ── ReviewRepository extension: is_first_run ─────────────────────────────────
# Monkey-patched here to keep models/repository untouched.
# In a larger project this would live in ReviewRepository directly.

from sqlalchemy import select, func                  # noqa: E402
from database.models import Review as _Review        # noqa: E402


async def _is_first_run(
    self: "ReviewRepository",
    org_id: int,
    source: ReviewSource,
) -> bool:
    """Return True if no reviews exist yet for this org+source combination."""
    stmt = select(func.count()).where(
        _Review.organization_id == org_id,
        _Review.source == source,
    )
    result = await self._s.execute(stmt)
    return (result.scalar() or 0) == 0


ReviewRepository.is_first_run = _is_first_run       # type: ignore[attr-defined]


# ── APScheduler factory ───────────────────────────────────────────────────────

async def start_scheduler(orchestrator: ParserOrchestrator):
    """
    Configure APScheduler with a CronTrigger (every hour at :00).

    Key options:
      max_instances=1   — never overlap two cycles
      coalesce=True     — if we missed a tick, run once (don't catch up)
      misfire_grace_time=300 — allow up to 5-min late start before skipping
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        orchestrator.run_cycle,
        trigger = IntervalTrigger(minutes=settings.PARSE_INTERVAL_MINUTES),
        id="main_parse_cycle",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
        replace_existing=True,
    )
    scheduler.start()
    logger.info("APScheduler started — CronTrigger every hour at :00 UTC")
    return scheduler
