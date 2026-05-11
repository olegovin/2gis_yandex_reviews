"""
services/telegram_bot.py — outbound-only Telegram notifier (aiogram 3.x).

Two audiences:
  - Clients  → new review notifications (HTML, with optional AI reply)
  - Admin    → parser error alerts

No Dispatcher / polling — this is a pure send-only bot.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramBadRequest,
)

logger = logging.getLogger(__name__)

# ── visual helpers ─────────────────────────────────────────────────────────────

_SOURCE_META: dict[str, tuple[str, str]] = {
    "yandex": ("🗺", "Яндекс.Карты"),
    "2gis":   ("🅖", "2ГИС"),
}


def _stars(rating: int) -> str:
    """Return e.g. '★★★★☆' for rating=4."""
    return "★" * max(0, rating) + "☆" * max(0, 5 - rating)


def _fmt_review(
    *,
    organization_name: str,
    source: str,
    author: str,
    rating: int,
    text: str,
    review_date: str,
    ai_response: str | None,
) -> str:
    """
    Build the client-facing HTML review message.

    Layout:
      🆕 New review | 🗺 Яндекс.Карты
      🏢 Org name

      👤 Author
      ⭐ 4/5 (★★★★☆)
      📅 date

      💬 Review text (blockquote)

      🤖 Proposed reply (code block) OR ⚠️ Low rating warning
    """
    emoji, src_name = _SOURCE_META.get(source, ("📍", source))

    header = (
        f"🆕 <b>Новый отзыв</b> | {emoji} {src_name}\n"
        f"🏢 {_esc(organization_name)}\n"
    )

    meta = (
        f"\n👤 <b>{_esc(author)}</b>\n"
        f"⭐ {rating}/5 ({_stars(rating)})\n"
        f"📅 {_esc(review_date)}\n"
    )

    body = f"\n💬 <i>Текст отзыва:</i>\n<blockquote>{_esc(text or '—')}</blockquote>\n"

    if rating >= 4:
        if ai_response:
            reply_block = (
                f"\n🤖 <b>Предлагаемый ответ:</b>\n"
                f"<code>{_esc(ai_response)}</code>\n"
                f"\n👆 Нажмите на ответ, чтобы скопировать"
            )
        else:
            reply_block = ""
    else:
        reply_block = "\n⚠️ <b>Низкая оценка, требует ручного ответа</b>"

    return header + meta + body + reply_block


def _fmt_error(
    *,
    client_name: str,
    source: str,
    error: str,
    timestamp: datetime,
) -> str:
    """Build the admin-facing error alert HTML message."""
    ts = timestamp.strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"🚨 <b>Ошибка парсера</b>\n"
        f"Клиент: {_esc(client_name)}\n"
        f"Источник: {_esc(source)}\n"
        f"Ошибка: <code>{_esc(error)}</code>\n"
        f"Время: {ts}"
    )


def _esc(text: str) -> str:
    """
    Minimal HTML escaping for user-supplied strings inside HTML parse_mode.
    Only &, <, > need escaping; aiogram does NOT auto-escape template values.
    """
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ── notifier ──────────────────────────────────────────────────────────────────

class TelegramNotifier:
    """
    Send-only Telegram bot wrapper.

    Usage:
        notifier = TelegramNotifier(token="...", admin_chat_id=123)
        await notifier.test_connection()
        await notifier.send_review_to_client(chat_id=..., review=..., org_name=...)
        await notifier.send_error_to_admin(client_name=..., source=..., error=...)
        await notifier.close()
    """

    def __init__(self, token: str, admin_chat_id: int) -> None:
        self._bot = Bot(
            token=token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self._admin_chat_id = admin_chat_id

    # ── public API ────────────────────────────────────────────────────────────

    async def send_review_to_client(
        self,
        *,
        chat_id: int,
        organization_name: str,
        source: str,
        author: str,
        rating: int,
        text: str,
        review_date: str,
        ai_response: str | None = None,
        # Optional DB callback: called when the user has blocked the bot
        on_user_blocked: "asyncio.coroutines.Coroutine | None" = None,
    ) -> bool:
        """
        Send a new-review notification to a client's Telegram chat.

        Returns True on success, False on unrecoverable error (e.g. bot blocked).
        FloodWait is handled automatically (sleep + retry, up to 3 attempts).
        """
        text_html = _fmt_review(
            organization_name=organization_name,
            source=source,
            author=author,
            rating=rating,
            text=text,
            review_date=review_date,
            ai_response=ai_response,
        )
        return await self._send_with_retry(
            chat_id=chat_id,
            text=text_html,
            on_user_blocked=on_user_blocked,
            context=f"review org={organization_name} author={author}",
        )

    async def send_error_to_admin(
        self,
        *,
        client_name: str,
        source: str,
        error: str,
        timestamp: datetime | None = None,
    ) -> bool:
        """Send a parser-error alert to the admin chat."""
        ts = timestamp or datetime.now(tz=timezone.utc)
        text_html = _fmt_error(
            client_name=client_name,
            source=source,
            error=error,
            timestamp=ts,
        )
        return await self._send_with_retry(
            chat_id=self._admin_chat_id,
            text=text_html,
            context=f"error client={client_name}",
        )

    async def test_connection(self) -> str:
        """
        Verify bot token and connectivity on startup.
        Returns the bot's username if successful, raises on failure.
        Call this in main.py after creating TelegramNotifier.
        """
        me = await self._bot.get_me()
        logger.info("TelegramNotifier: connected as @%s (id=%d)", me.username, me.id)
        return me.username or ""

    async def close(self) -> None:
        """Release the underlying aiohttp session."""
        await self._bot.session.close()

    # ── internal send with FloodWait retry ───────────────────────────────────

    async def _send_with_retry(
        self,
        *,
        chat_id: int,
        text: str,
        context: str = "",
        on_user_blocked: "asyncio.coroutines.Coroutine | None" = None,
        max_flood_retries: int = 3,
    ) -> bool:
        """
        Send a message with automatic FloodWait handling.

        Retry schedule on TelegramRetryAfter:
          sleep exactly retry_after seconds (Telegram tells us how long),
          then retry.  Up to max_flood_retries times.

        On TelegramForbiddenError (user blocked bot):
          - Log the event
          - Call on_user_blocked() if provided (should set client.is_active=False)
          - Return False (don't raise — one blocked client must not stop others)
        """
        attempt = 0
        while attempt <= max_flood_retries:
            try:
                await self._bot.send_message(chat_id=chat_id, text=text)
                logger.debug("TelegramNotifier: sent [%s] → chat_id=%d", context, chat_id)
                return True

            except TelegramRetryAfter as exc:
                attempt += 1
                wait_sec = exc.retry_after + 1   # +1 s safety margin
                logger.warning(
                    "TelegramNotifier: FloodWait %d s (attempt %d/%d) [%s]",
                    wait_sec, attempt, max_flood_retries, context,
                )
                if attempt > max_flood_retries:
                    logger.error(
                        "TelegramNotifier: giving up after %d FloodWait retries [%s]",
                        max_flood_retries, context,
                    )
                    return False
                await asyncio.sleep(wait_sec)

            except TelegramForbiddenError:
                # User blocked the bot — deactivate client in DB
                logger.warning(
                    "TelegramNotifier: bot blocked by chat_id=%d [%s] — marking inactive",
                    chat_id, context,
                )
                if on_user_blocked is not None:
                    try:
                        await on_user_blocked
                    except Exception as cb_exc:
                        logger.error(
                            "TelegramNotifier: on_user_blocked callback failed: %s", cb_exc
                        )
                return False

            except TelegramBadRequest as exc:
                logger.error("TelegramNotifier: BadRequest chat_id=%d [%s]: %s", chat_id, context, exc)
                return False

            except Exception as exc:
                # Сетевые ошибки — retry
                attempt += 1
                logger.warning(
                    "TelegramNotifier: network error attempt %d/%d chat_id=%d: %s",
                    attempt, max_flood_retries, chat_id, exc,
                )
                if attempt > max_flood_retries:
                    logger.error("TelegramNotifier: giving up after network errors [%s]", context)
                    return False
                await asyncio.sleep(10)

        return False   # unreachable but satisfies type checker
