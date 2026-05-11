"""
main.py — application entry point.

Startup sequence:
  1. Logging
  2. DB connectivity check
  3. Chromium launch (one process, lives until shutdown)
  4. Telegram bot self-test
  5. OpenAI connectivity check
  6. APScheduler (CronTrigger, every hour at :00)
  7. Immediate first cycle (asyncio.create_task)
  8. Block on stop_event (SIGINT / SIGTERM)
  9. Graceful shutdown in reverse order
"""
from __future__ import annotations

import asyncio
import logging
import signal

from app_config import settings, setup_logging
from database.engine import engine, init_db
from scheduler.runner import ParserOrchestrator, start_scheduler
from services.ai_responder import AIResponder
from services.browser_pool import BrowserManager
from services.telegram_bot import TelegramNotifier

logger = logging.getLogger(__name__)


# ── startup checks ────────────────────────────────────────────────────────────

async def _check_openai(responder: AIResponder) -> None:
    """
    Lightweight OpenAI connectivity check — lists available models.
    Raises if the API key is invalid or the service is unreachable.
    """
    models = await responder._client.models.list()
    model_ids = [m.id for m in models.data]
    if settings.OPENAI_MODEL not in model_ids:
        logger.warning(
            "OpenAI check: model %r not found in account — check OPENAI_MODEL setting",
            settings.OPENAI_MODEL,
        )
    else:
        logger.info("OpenAI check: OK (model %s available)", settings.OPENAI_MODEL)


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    # ── 1. Logging ────────────────────────────────────────────────────────────
    setup_logging(settings.LOG_LEVEL)
    logger.info("=== Review Parser starting up ===")
    logger.info(
        "Config: interval=%d min  max_parsers=%d  model=%s",
        settings.PARSE_INTERVAL_MINUTES,
        settings.MAX_CONCURRENT_PARSERS,
        settings.OPENAI_MODEL,
    )

    # ── 2. Database ───────────────────────────────────────────────────────────
    logger.info("Checking database connectivity…")
    await init_db()
    logger.info("Database: OK")

    # ── 3. Browser ────────────────────────────────────────────────────────────
    browser_manager = BrowserManager()
    await browser_manager.startup()

    # ── 4. Telegram ───────────────────────────────────────────────────────────
    tg_notifier = TelegramNotifier(
        token=settings.TELEGRAM_BOT_TOKEN,
        admin_chat_id=settings.TELEGRAM_ADMIN_CHAT_ID,
    )
    bot_username = await tg_notifier.test_connection()
    logger.info("Telegram bot: @%s OK", bot_username)

    # ── 5. OpenAI ─────────────────────────────────────────────────────────────
    ai_responder = AIResponder(
        api_key=settings.OPENAI_API_KEY,
        model=settings.OPENAI_MODEL,
    )
    try:
        await _check_openai(ai_responder)
    except Exception as exc:
        # Non-fatal: log and continue — AI generation will fail gracefully per review
        logger.warning("OpenAI startup check failed (will retry per request): %s", exc)

    # ── 6. Orchestrator + scheduler ───────────────────────────────────────────
    orchestrator = ParserOrchestrator(
        browser_manager=browser_manager,
        ai_responder=ai_responder,
        tg_notifier=tg_notifier,
    )

    scheduler = await start_scheduler(orchestrator)

    # ── 7. Immediate first cycle ──────────────────────────────────────────────
    # create_task so the signal handlers below are registered before the cycle
    # starts. The cycle itself is non-blocking from main()'s perspective.
    first_run_task = asyncio.create_task(
        orchestrator.run_cycle(),
        name="first_run_cycle",
    )

    # ── 8. Signal handling + wait ─────────────────────────────────────────────
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    logger.info("=== Review Parser running — waiting for stop signal ===")
    await stop_event.wait()

    # ── 9. Graceful shutdown ──────────────────────────────────────────────────
    logger.info("Shutting down…")

    # Cancel first-run task if it's still going (e.g. very early SIGTERM)
    if not first_run_task.done():
        first_run_task.cancel()
        try:
            await first_run_task
        except asyncio.CancelledError:
            pass

    # Stop scheduler — wait=True waits for any currently running job to finish
    scheduler.shutdown(wait=True)
    logger.info("Scheduler: stopped")

    await browser_manager.shutdown()
    logger.info("Browser: stopped")

    await tg_notifier.close()
    logger.info("Telegram session: closed")

    await engine.dispose()
    logger.info("DB pool: disposed")

    logger.info("=== Bye ===")


if __name__ == "__main__":
    asyncio.run(main())
