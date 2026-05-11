"""
parsers/yandex_maps.py — Yandex.Maps review parser.

Retry with fresh context happens at orchestrator level (scheduler/runner.py).
This class handles a single attempt on a given Page.
"""
from __future__ import annotations

import time

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from parsers.base import BaseParser
from parsers.captcha import try_auto_click_captcha
from parsers.exceptions import (
    CaptchaFailedException,
    NoReviewsFoundException,
    PageLoadException,
)

# ── Selectors ─────────────────────────────────────────────────────────────────
_REVIEW_BLOCK     = ".business-review-view"
_SCROLLABLE_DIV   = ".business-reviews-card-view__reviews-container, .scroll__container"
_EXPAND_BUTTON    = ".spoiler-view__button, .business-review-view__expand"
_CAPTCHA_SELECTORS = ".CheckboxCaptcha, #checkbox-captcha-form, .smart-captcha"
_TOTAL_COUNT      = ".card-section-header__title"


class YandexMapsParser(BaseParser):

    def __init__(self, page: Page) -> None:
        super().__init__(page)

    async def parse(self, url: str) -> list[dict]:
        """
        Full Yandex.Maps review scrape.

        Steps:
          1. Navigate (domcontentloaded)
          2. Detect & handle captcha
          3. Wait for first review block (smart wait)
          4. Read declared total
          5. Smart scroll
          6. Expand spoilers via single JS evaluate
          7. Extract all data in one page.evaluate()
        """
        t0 = time.monotonic()
        self._log.info("YandexMapsParser.parse: start → %s", url)

        page = self._page

        # ── 1. Navigate ───────────────────────────────────────────────────────
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError as exc:
            raise PageLoadException(f"Yandex page load timeout: {url}") from exc

        # ── 2. Captcha ────────────────────────────────────────────────────────
        captcha_el = await page.query_selector(_CAPTCHA_SELECTORS)
        if captcha_el:
            self._log.warning("YandexMapsParser: captcha detected, attempting auto-solve")
            solved = await try_auto_click_captcha(page)
            if not solved:
                raise CaptchaFailedException(
                    "Yandex captcha could not be solved automatically"
                )
            
            # После решения капчи ждем дольше перед возвратом
            await page.wait_for_timeout(7000)
            
            # Принудительно закрываем все модальные окна капчи
            await page.evaluate("""
                () => {
                    // Закрываем все возможные модальные окна капчи
                    const modals = document.querySelectorAll('[class*="modal"], [class*="popup"], [class*="dialog"], [class*="overlay"]');
                    modals.forEach(modal => {
                        try {
                            modal.style.display = 'none';
                            modal.remove();
                        } catch(e) {}
                    });
                    
                    // Закрываем оверлеи
                    const overlays = document.querySelectorAll('[class*="overlay"], [class*="backdrop"]');
                    overlays.forEach(overlay => {
                        try {
                            overlay.style.display = 'none';
                            overlay.remove();
                        } catch(e) {}
                    });
                    
                    // Убираем блокировку скролла
                    document.body.style.overflow = '';
                    document.body.classList.remove('modal-open', 'overflow-hidden');
                }
            """)
            
            await page.wait_for_timeout(2000)
            # После капчи Яндекс редиректит на главную — идём обратно на URL с отзывами
            await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_timeout(5000)
            
            # Пробуем найти отзывы несколькими способами
            review_selectors = [
                '[class*="review"]',
                '[class*="Review"]', 
                '[data-review-id]',
                '.review-card',
                '[class*="card"]',
                '[class*="ReviewCard"]',
                '[class*="review-item"]'
            ]
            
            reviews_found = False
            for selector in review_selectors:
                try:
                    await page.wait_for_selector(selector, timeout=5000)
                    await page.wait_for_timeout(1000)
                    self._log.info("YandexMapsParser: reviews found with selector: %s", selector)
                    reviews_found = True
                    break
                except:
                    continue
            
            if not reviews_found:
                # Делаем скриншот для отладки
                try:
                    await page.screenshot(path=f"debug_yandex_after_captcha_{int(time.time())}.png")
                    self._log.warning("YandexMapsParser: screenshot saved after captcha for debugging")
                except:
                    pass
                self._log.warning("YandexMapsParser: reviews not loaded, trying to scroll and wait")
                # Пробуем скроллить и ждать
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
                await page.wait_for_timeout(3000)
                
                for selector in review_selectors:
                    try:
                        await page.wait_for_selector(selector, timeout=5000)
                        self._log.info("YandexMapsParser: reviews found after scroll with selector: %s", selector)
                        reviews_found = True
                        break
                    except:
                        continue
                
                if not reviews_found:
                    # Делаем скриншот перед полным обновлением
                    try:
                        await page.screenshot(path=f"debug_yandex_before_refresh_{int(time.time())}.png")
                        self._log.warning("YandexMapsParser: screenshot saved before refresh for debugging")
                    except:
                        pass
                    self._log.warning("YandexMapsParser: still no reviews, trying full refresh")
                    await page.reload(wait_until="domcontentloaded", timeout=60_000)
                    await page.wait_for_timeout(5000)
                    
                    for selector in review_selectors:
                        try:
                            await page.wait_for_selector(selector, timeout=8000)
                            self._log.info("YandexMapsParser: reviews found after refresh with selector: %s", selector)
                            reviews_found = True
                            break
                        except:
                            continue
                    
                    if not reviews_found:
                        self._log.error("YandexMapsParser: no reviews found after all attempts, trying with new proxy")
                        # Пробуем с новым прокси - создаем новую страницу
                        from services.browser_pool import BrowserManager
                        # Закрываем текущую страницу и контекст
                        await self._page.close()
                        browser_mgr = BrowserManager()
                        page, context = await browser_mgr.get_page_with_new_proxy()
                        self._page = page
                        self._context = context
                        
                        # Повторяем попытку с новым прокси
                        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                        await page.wait_for_timeout(5000)
                        
                        for selector in review_selectors:
                            try:
                                await page.wait_for_selector(selector, timeout=8000)
                                self._log.info("YandexMapsParser: reviews found with new proxy using selector: %s", selector)
                                reviews_found = True
                                break
                            except:
                                continue
                        
                        if not reviews_found:
                            self._log.error("YandexMapsParser: still no reviews even with new proxy, giving up")
                            # Это означает сложную капчу - пробуем еще раз с новым прокси
                            self._log.warning("YandexMapsParser: complex captcha detected, trying fresh proxy from scratch")
                            await self._page.close()
                            browser_mgr = BrowserManager()
                            page, context = await browser_mgr.get_page_with_new_proxy()
                            self._page = page
                            self._context = context
                            
                            # Начинаем с начала - без капчи hopefully
                            await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                            await page.wait_for_timeout(5000)
                            
                            # Проверяем есть ли капча сразу
                            captcha_check = await page.query_selector(_CAPTCHA_SELECTORS)
                            if captcha_check:
                                self._log.warning("YandexMapsParser: captcha again on fresh proxy, giving up for this URL")
                                return []
                            
                            # Ищем отзывы
                            for selector in review_selectors:
                                try:
                                    await page.wait_for_selector(selector, timeout=8000)
                                    self._log.info("YandexMapsParser: reviews found on fresh proxy using selector: %s", selector)
                                    reviews_found = True
                                    break
                                except:
                                    continue
                            
                            if not reviews_found:
                                self._log.error("YandexMapsParser: no reviews even on fresh proxy, skipping this URL")
                                # Делаем скриншот для отладки
                                try:
                                    await page.screenshot(path=f"debug_yandex_{int(time.time())}.png")
                                    self._log.warning("YandexMapsParser: screenshot saved for debugging")
                                except:
                                    pass
                                return []

        # ── 3. Сортировка по новизне ──────────────────────────────────────────
        try:
            # Сначала ждём появления кнопки сортировки
            await self._wait_smart('.rating-ranking-view', timeout=8000)
            await page.wait_for_timeout(500)
            
            # Проверяем текущую сортировку перед кликом
            current_sort = await page.evaluate("() => document.querySelector('.rating-ranking-view')?.innerText")
            self._log.info("YandexMapsParser: current sort = %s", current_sort)
            
            sorted_ok = await page.evaluate("""
                async () => {
                    const btn = document.querySelector('.rating-ranking-view');
                    if (!btn) return 'no_button';
                    btn.click();
                    await new Promise(r => setTimeout(r, 1000));
                    const lines = document.querySelectorAll('.rating-ranking-view__popup-line');
                    if (!lines.length) return 'no_options';
                    for (const line of lines) {
                        if (line.innerText && line.innerText.includes('новизн')) {
                            line.click();
                            await new Promise(r => setTimeout(r, 2000));
                            return 'ok';
                        }
                    }
                    return 'option_not_found';
                }
            """)
            self._log.info("YandexMapsParser: sort result = %s", sorted_ok)
        except Exception as e:
            self._log.warning("YandexMapsParser: could not sort: %s", e)

        # ── 4. Wait for reviews (smart, not blind) ────────────────────────────
        appeared = await self._wait_smart(_REVIEW_BLOCK, timeout=15_000)
        if not appeared:
            raise NoReviewsFoundException(f"Yandex: no review blocks at {url}")

        await self._close_modal_if_exists()

        # ── 4. Total count ────────────────────────────────────────────────────
        total: int = await page.evaluate(
            """
            () => {
                const el = document.querySelector('.card-section-header__title');
                if (!el) return 0;
                const m = el.innerText.match(/(\\d+)/);
                return m ? parseInt(m[1]) : 0;
            }
            """
        )
        self._log.info("YandexMapsParser: total reviews declared = %d", total)

        # ── 5. Smart scroll ───────────────────────────────────────────────────
        scrollable = await page.query_selector(_SCROLLABLE_DIV)
        scrollable_sel = (
            ".business-reviews-card-view__reviews-container, .scroll__container"
            if scrollable else None
        )

        final_count = await self._smart_scroll(
            scrollable_selector=scrollable_sel,
            blocks_selector=_REVIEW_BLOCK,
            max_no_change=3,
            expected_total=20,  # нам нужны только последние 20
            scroll_pause_ms=300,
        )
        self._log.info("YandexMapsParser: %d blocks after scroll", final_count)

        # ── 6. Expand spoilers (single JS, not Python loop) ───────────────────
        await self._expand_spoilers(_EXPAND_BUTTON)
        await page.wait_for_timeout(200)  # let expanded text render

        # ── 7. Extract ────────────────────────────────────────────────────────
        raw: list[dict] = await page.evaluate(
            """
            () => {
                const reviews = [];
                const allBlocks = document.querySelectorAll('.business-review-view');
                        const blocks = [...allBlocks].slice(0, 20);

                for (const block of blocks) {
                    try {
                        const nameEl = block.querySelector(
                            '.business-review-view__author-name span'
                        );
                        const author = nameEl ? nameEl.innerText.trim() : 'Anonymous';

                        const rating = block.querySelectorAll(
                            '.business-rating-badge-view__star._full'
                        ).length;

                        let text = '';
                        const body = block.querySelector('.business-review-view__body');
                        if (body) {
                            const spoiler = body.querySelector('.spoiler-view__text');
                            if (spoiler) {
                                const span = spoiler.querySelector('span');
                                text = span ? span.innerText : spoiler.innerText;
                                text = text.replace(/…$/, '').trim();
                            }
                            if (!text) {
                                const alt = body.querySelector(
                                    '[itemprop="reviewBody"] span:first-child'
                                );
                                if (alt) text = alt.innerText;
                            }
                        }
                        text = text.replace(/ещё/g, '').replace(/…/g, '').trim();

                        const dateEl = block.querySelector(
                            '.business-review-view__date span:first-child'
                        );
                        const review_date = dateEl ? dateEl.innerText.trim() : '';

                        let user_status = null;
                        const captionEl = block.querySelector(
                            '.business-review-view__author-caption'
                        );
                        if (captionEl) {
                            const t = captionEl.innerText.trim();
                            if (t && !t.match(/^\\d+\\s*отзыв/i)) user_status = t;
                        }

                        if (text || rating > 0) {
                            reviews.push({ author, rating, text, review_date, user_status });
                        }
                    } catch(e) {}
                }
                return reviews;
            }
            """
        )

        result = [{"source": "yandex", **r} for r in raw]

        duration_ms = int((time.monotonic() - t0) * 1000)
        self._log.info(
            "YandexMapsParser.parse: done — %d reviews in %d ms", len(result), duration_ms
        )
        return result
