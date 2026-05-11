"""
parsers/base.py — abstract base for Yandex / 2GIS parsers.

All shared Playwright helpers live here so concrete parsers
only contain site-specific selectors and JS evaluation.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from playwright.async_api import Page

logger = logging.getLogger(__name__)


class BaseParser(ABC):
    """
    Abstract parser.  Concrete subclasses receive a ready Page from
    BrowserManager and implement parse().

    Retry logic lives in the ORCHESTRATOR (scheduler/runner.py), not here,
    because each retry must use a fresh context — the old one may be
    flagged by Yandex.  That means we never retry inside a single page instance.
    """

    def __init__(self, page: Page) -> None:
        self._page = page
        self._log = logging.getLogger(self.__class__.__name__)

    # ── abstract ──────────────────────────────────────────────────────────────

    @abstractmethod
    async def parse(self, url: str) -> list[dict]:
        """
        Scrape all reviews from *url*.
        Returns a list of dicts with keys:
          source, author, rating, text, review_date, user_status
        (avatar_url is intentionally omitted — images are blocked at context level)
        """

    # ── shared helpers ────────────────────────────────────────────────────────

    async def _close_modal_if_exists(self) -> bool:
        """Click the first visible close/accept button in a modal, if any."""
        try:
            selector = (
                'button:has-text("Хорошо"), button:has-text("OK"), '
                'button:has-text("Закрыть"), button:has-text("Close"), '
                'button:has-text("Принять")'
            )
            btn = await self._page.query_selector(selector)
            if btn:
                await btn.click()
                await self._page.wait_for_timeout(150)
                self._log.debug("_close_modal_if_exists: modal dismissed")
                return True
        except Exception as exc:
            self._log.debug("_close_modal_if_exists: %s", exc)
        return False

    async def _wait_smart(self, selector: str, timeout: int = 5000) -> bool:
        """
        Wait for *selector* to appear instead of a blind wait_for_timeout.
        Returns True if the element appeared, False on timeout (no exception).

        Replaces patterns like:
            await page.wait_for_timeout(2000)  # hoping the page loaded
        with:
            await self._wait_smart('.review-block')  # actually waits for content
        """
        try:
            await self._page.wait_for_selector(selector, timeout=timeout)
            return True
        except Exception:
            self._log.debug("_wait_smart: selector %r not found within %d ms", selector, timeout)
            return False

    async def _smart_scroll(
        self,
        scrollable_selector: str | None,
        blocks_selector: str,
        max_no_change: int = 5,
        expected_total: int = 0,
        scroll_pause_ms: int = 300,
    ) -> int:
        """
        Scroll a container (or window) until no new review blocks appear.

        Args:
            scrollable_selector: CSS selector for the scrollable div.
                                  Pass None to scroll the window itself.
            blocks_selector:     CSS selector for individual review blocks.
            max_no_change:       Stop after this many scrolls with no new blocks.
            expected_total:      If >0, stop early once we have enough blocks.
            scroll_pause_ms:     Short pause after each scroll so lazy content loads.
                                 Keep ≥200 ms — the DOM needs time to render new nodes.

        Returns the final review block count.
        """
        page = self._page
        previous_count = 0
        no_change_streak = 0

        while no_change_streak < max_no_change:
            # Scroll
            if scrollable_selector:
                await page.evaluate(
                    f"document.querySelector('{scrollable_selector}')?.scrollBy(0, 99999)"
                )
            else:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

            # Short real pause — reviews need to render after network fetch
            await page.wait_for_timeout(scroll_pause_ms)

            current_count = await page.locator(blocks_selector).count()

            if current_count == previous_count:
                no_change_streak += 1
            else:
                no_change_streak = 0
                previous_count = current_count
                self._log.debug("_smart_scroll: %d blocks loaded", current_count)

            if expected_total > 0 and current_count >= expected_total:
                self._log.debug("_smart_scroll: reached expected total %d", expected_total)
                break

        return previous_count

    async def _expand_spoilers(self, selector: str) -> int:
        """
        Click all "expand" / "read more" buttons in ONE JS evaluate call
        instead of a Python loop with individual click()s.

        A Python loop like:
            for btn in buttons:
                await btn.click()          # ← N round-trips to browser process
                await page.wait_for_timeout(100)
        costs ~100 ms * N.  A single evaluate() costs ~2 ms regardless of N.

        Returns the number of buttons clicked.
        """
        clicked: int = await self._page.evaluate(
            """
            (selector) => {
                const buttons = document.querySelectorAll(selector);
                buttons.forEach(btn => {
                    try { btn.click(); } catch(e) {}
                });
                return buttons.length;
            }
            """,
            selector,
        )
        self._log.debug("_expand_spoilers: clicked %d buttons (%r)", clicked, selector)
        return clicked
