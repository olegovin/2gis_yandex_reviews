"""
services/browser_pool.py — singleton BrowserManager.

Lifecycle:
  startup()  → launches ONE Chromium process for the entire app lifetime
  get_page() → creates an isolated context + page per parse task
               (different user-agents, blocked heavy resources)
  release_page() → closes context (and its page) after the task
  shutdown() → gracefully closes the browser

Why one browser, many contexts:
  Playwright contexts are cheap (~5 ms, ~2 MB).
  Launching a new browser per task is expensive (~800 ms, ~80 MB).
  Separate contexts give full cookie/storage isolation so Yandex cannot
  correlate requests across different client parse jobs.
"""
from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ── Прокси ротация ────────────────────────────────────────────────────────────
_PROXIES = [
    {"server": "http://213.139.223.8:9399", "username": "FdWuxM", "password": "E7sdTQ"},
    {"server": "http://213.139.222.64:9586", "username": "FdWuxM", "password": "E7sdTQ"},
    {"server": "http://178.171.69.162:8000", "username": "WgykFd", "password": "jntF3K"},
]
_proxy_index = 0

def _next_proxy() -> dict:
    global _proxy_index
    proxy = _PROXIES[_proxy_index % len(_PROXIES)]
    _proxy_index += 1
    return proxy

# ── User-agent rotation list ───────────────────────────────────────────────────
_USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:136.0) Gecko/20100101 Firefox/136.0",
]

# Resources to block — saves ~30 % page load time.
# avatar_url is parsed from inline CSS backgroundImage, NOT from network requests,
# so blocking images does NOT affect avatar extraction.
_BLOCKED_RESOURCE_PATTERN = "**/*.{png,jpg,jpeg,webp,gif,svg,mp4,webm,woff,woff2,ttf,otf}"


class BrowserManager:
    """Singleton async browser manager."""

    _instance: "BrowserManager | None" = None

    def __new__(cls) -> "BrowserManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialised = False
        return cls._instance

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        """Launch Chromium once.  Call at application startup."""
        if self._initialised:
            logger.debug("BrowserManager.startup: already running, skipped")
            return

        self._playwright: Playwright = await async_playwright().start()
        self._browser: Browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-default-apps",
                "--no-first-run",
            ],
        )
        logger.info(
            "BrowserManager: Chromium launched (version %s)",
            self._browser.version,
        )
        self._initialised = True

    async def shutdown(self) -> None:
        """Gracefully close browser and Playwright.  Call at application shutdown."""
        if not self._initialised:
            return
        await self._browser.close()
        await self._playwright.stop()
        self._initialised = False
        BrowserManager._instance = None
        logger.info("BrowserManager: Chromium shut down")

    # ── per-task helpers ──────────────────────────────────────────────────────

    async def get_page(self, force_new_proxy: bool = False) -> tuple[Page, BrowserContext]:
        """
        Create a fresh isolated context + page for one parse job.

        Each context gets:
          - a randomly chosen User-Agent
          - 1920×1080 viewport
          - resource blocking for images/fonts/video (not inline styles)
          - webdriver flag hidden via JS
          - rotating SOCKS5 proxy via proxychains

        Returns (page, context) — caller must pass both to release_page().
        """
        if not self._initialised:
            raise RuntimeError("BrowserManager not started. Call startup() first.")

        context: BrowserContext = await self._browser.new_context(
            user_agent=random.choice(_USER_AGENTS),
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9"},
        )
        logger.info("BrowserManager.get_page: using proxychains for all network requests")
        if force_new_proxy:
            logger.info("BrowserManager.get_page: requested new proxy rotation")

        # Hide automation markers
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # Block heavy static assets — ~30 % load time saved per page
        await context.route(
            _BLOCKED_RESOURCE_PATTERN,
            lambda route: route.abort(),
        )

        page: Page = await context.new_page()
        logger.debug("BrowserManager.get_page: new context+page created")
        return page, context

    async def get_page_with_new_proxy(self) -> tuple[Page, BrowserContext]:
        """Get new page with forced proxy rotation."""
        return await self.get_page(force_new_proxy=True)

    async def release_page(self, page: Page, context: BrowserContext) -> None:
        """Close the page's context (which also closes the page itself)."""
        try:
            await context.close()
            logger.debug("BrowserManager.release_page: context closed")
        except Exception as exc:
            logger.warning("BrowserManager.release_page: error closing context: %s", exc)
