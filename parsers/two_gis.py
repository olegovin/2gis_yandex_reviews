"""
parsers/two_gis.py — 2GIS review parser.

Inherits scroll/modal/spoiler helpers from BaseParser.
parse() is decorated with tenacity retry at the CALLER level (see scheduler).
"""
from __future__ import annotations

import logging
import time

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from parsers.base import BaseParser
from parsers.exceptions import NoReviewsFoundException, PageLoadException

logger = logging.getLogger(__name__)

# ── Selectors (single source of truth) ────────────────────────────────────────
_REVIEW_BLOCK     = "._1rowqpjv"
_SCROLLABLE_DIV   = "._8hh56jx"
_EXPAND_BUTTON    = "._1e65qgv"
_TOTAL_COUNT      = "._4v626nk span, ._46hyo9"
_FIRST_REVIEW     = _REVIEW_BLOCK  # used for _wait_smart


class TwoGisParser(BaseParser):

    def __init__(self, page: Page) -> None:
        super().__init__(page)

    async def parse(self, url: str) -> list[dict]:
        """
        Full 2GIS review scrape.

        Steps:
          1. Navigate (domcontentloaded — fastest safe option)
          2. Wait for first review block (smart wait, not blind timeout)
          3. Dismiss modal if present
          4. Read total review count
          5. Smart-scroll until all reviews loaded
          6. Expand spoilers via single JS evaluate
          7. Extract all data in one page.evaluate()
        """
        t0 = time.monotonic()
        self._log.info("TwoGisParser.parse: start → %s", url)

        page = self._page

        # ── 1. Navigate ───────────────────────────────────────────────────────
        max_retries = 3
        for attempt in range(max_retries):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                break
            except (PlaywrightTimeoutError, Exception) as exc:
                self._log.warning("2GIS navigation attempt %d failed: %s", attempt + 1, exc)
                if attempt == max_retries - 1:
                    raise PageLoadException(f"2GIS page load failed after {max_retries} attempts: {url}") from exc
                await page.wait_for_timeout(3000)

        await page.wait_for_timeout(4000)

        # ── 2. Wait for reviews to appear (replaces blind wait_for_timeout) ───
        appeared = await self._wait_smart(_FIRST_REVIEW, timeout=15_000)
        if not appeared:
            # Пробуем альтернативные селекторы 2GIS
            alt_selectors = [
                "[class*='_1rowqpjv']", 
                "[class*='review']", 
                "._1ggk29z",
                "[class*='_1f9j']",
                "[class*='review-card']",
                "[data-testid='review']",
                ".reviews-list [class*='item']",
                "[class*='review-item']"
            ]
            
            for alt in alt_selectors:
                self._log.info("TwoGisParser: trying alternative selector: %s", alt)
                appeared = await self._wait_smart(alt, timeout=5_000)
                if appeared:
                    self._log.info("TwoGisParser: found reviews with selector: %s", alt)
                    break
                    
            if not appeared:
                # Проверим может страница просто без отзывов
                no_reviews_indicators = [
                    "[class*='no-reviews']",
                    "[class*='empty']",
                    "[class*='not-found']",
                    ".reviews-empty",
                    "[class*='no-reviews-message']",
                    "[class*='empty-state']",
                    ".reviews__empty",
                    "[class*='not-found-message']"
                ]
                
                has_no_reviews = False
                for indicator in no_reviews_indicators:
                    if await page.query_selector(indicator):
                        has_no_reviews = True
                        self._log.info("TwoGisParser: page has no reviews indicator: %s", indicator)
                        break
                
                # Дополнительная проверка - ищем текст "нет отзывов" или "пока нет отзывов"
                page_text = await page.evaluate("() => document.body.innerText")
                if any(phrase in page_text.lower() for phrase in ["нет отзывов", "пока нет отзывов", "no reviews", "ещё нет отзывов"]):
                    has_no_reviews = True
                    self._log.info("TwoGisParser: found 'no reviews' text in page content")
                
                if not has_no_reviews:
                    # Сделаем скриншот для отладки
                    try:
                        await page.screenshot(path=f"debug_2gis_{int(time.time())}.png")
                        self._log.warning("TwoGisParser: screenshot saved for debugging")
                    except:
                        pass
                
                if has_no_reviews:
                    # Возвращаем пустой список вместо исключения, если реально нет отзывов
                    self._log.info("TwoGisParser: no reviews available, returning empty list")
                    return []
                else:
                    raise NoReviewsFoundException(
                        f"2GIS: no review blocks found at {url} - page structure may have changed"
                    )

        # ── 3. Dismiss modal ──────────────────────────────────────────────────
        await self._close_modal_if_exists()
        
        # Закрываем специфичные модальные окна 2GIS
        await self._close_2gis_modals()
        
        # Принудительно закрываем все модальные окна через JavaScript
        await page.evaluate("""
            () => {
                // Закрываем все модальные окна и попапы
                const modals = document.querySelectorAll('[class*="modal"], [class*="popup"], [class*="dialog"], [class*="overlay"], [class*="notification"]');
                modals.forEach(modal => {
                    try {
                        modal.style.display = 'none';
                        modal.remove();
                    } catch(e) {}
                });
                
                // Закрываем оверлеи
                const overlays = document.querySelectorAll('[class*="overlay"], [class*="backdrop"], [class*="mask"]');
                overlays.forEach(overlay => {
                    try {
                        overlay.style.display = 'none';
                        overlay.remove();
                    } catch(e) {}
                });
                
                // Убираем блокировку скролла
                document.body.style.overflow = '';
                document.body.classList.remove('modal-open', 'overflow-hidden', 'no-scroll');
                
                // Закрываем все кнопки "пропустить" если они видны
                const skipButtons = document.querySelectorAll('button');
                skipButtons.forEach(btn => {
                    const text = btn.innerText.toLowerCase();
                    if (text.includes('пропустить') || text.includes('позже') || text.includes('не сейчас')) {
                        try {
                            btn.click();
                        } catch(e) {}
                    }
                });
            }
        """)
        
        # Ждем загрузки после закрытия модальных окон
        await page.wait_for_timeout(3000)
        
        # Проверяем появились ли отзывы после закрытия модалок
        appeared = await self._wait_smart(_FIRST_REVIEW, timeout=8000)
        if not appeared:
            self._log.info("TwoGisParser: checking for reviews after closing modals")
            # Пробуем еще раз с альтернативными селекторами
            for alt in alt_selectors:
                appeared = await self._wait_smart(alt, timeout=3000)
                if appeared:
                    self._log.info("TwoGisParser: found reviews after modals with selector: %s", alt)
                    break

        # ── 4. Total count ────────────────────────────────────────────────────
        total: int = await page.evaluate(
            """
            () => {
                const selectors = ['._4v626nk span', '._46hyo9'];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const m = el.innerText.match(/(\\d+)/);
                        if (m) return parseInt(m[1]);
                    }
                }
                return 0;
            }
            """
        )
        self._log.info("TwoGisParser: total reviews declared = %d", total)

        # ── 5. Smart scroll ───────────────────────────────────────────────────
        scrollable = await page.query_selector(_SCROLLABLE_DIV)
        scrollable_sel = _SCROLLABLE_DIV if scrollable else None

        final_count = await self._smart_scroll(
            scrollable_selector=scrollable_sel,
            blocks_selector=_REVIEW_BLOCK,
            max_no_change=3,
            expected_total=20,  # нам нужны только последние 20
            scroll_pause_ms=300,
        )
        self._log.info("TwoGisParser: %d blocks after scroll", final_count)

        # ── 6. Expand spoilers (single JS, not Python loop) ───────────────────
        await self._expand_spoilers(_EXPAND_BUTTON)
        await page.wait_for_timeout(200)  # let expanded text render

        # ── 7. Extract ────────────────────────────────────────────────────────
        raw: list[dict] = await page.evaluate(
            """
            () => {
                const reviews = [];
                const allBlocks = document.querySelectorAll('._1rowqpjv');
                const blocks = [...allBlocks].slice(0, 20);
                
                console.log(`Found ${allBlocks.length} review blocks, processing first ${blocks.length}`);

                for (const block of blocks) {
                    try {
                        // Пробуем разные селекторы для имени автора
                        let author = 'Anonymous';
                        const nameSelectors = ['._1k1c6j4', '[class*="author"]', '[class*="name"]', 'a[href*="/users/"]'];
                        for (const sel of nameSelectors) {
                            const nameEl = block.querySelector(sel);
                            if (nameEl && nameEl.innerText.trim()) {
                                author = nameEl.innerText.trim();
                                break;
                            }
                        }

                        // Пробуем разные селекторы для рейтинга
                        let rating = 0;
                        const ratingSelectors = [
                            '._1xgyz6i._full',
                            '[class*="star"]',
                            '[class*="rating"]',
                            '[class*="_full"]'
                        ];
                        for (const sel of ratingSelectors) {
                            const stars = block.querySelectorAll(sel);
                            if (stars.length > 0) {
                                rating = stars.length;
                                break;
                            }
                        }

                        // Пробуем разные селекторы для текста отзыва
                        let text = '';
                        const textSelectors = [
                            '._1bmq5z',
                            '[class*="text"]',
                            '[class*="comment"]',
                            '[class*="review-text"]',
                            '[itemprop="reviewBody"]'
                        ];
                        
                        for (const sel of textSelectors) {
                            const body = block.querySelector(sel);
                            if (body) {
                                // Пробуем найти спойлер
                                const spoiler = body.querySelector('._1e65qgv, [class*="spoiler"]');
                                if (spoiler) {
                                    const span = spoiler.querySelector('span');
                                    text = span ? span.innerText : spoiler.innerText;
                                    text = text.replace(/…$/, '').trim();
                                }
                                if (!text) {
                                    // Берем весь текст из body
                                    text = body.innerText || body.textContent || '';
                                }
                                if (text) break;
                            }
                        }
                        
                        text = text.replace(/ещё/g, '').replace(/…/g, '').trim();

                        // Пробуем разные селекторы для даты
                        let review_date = '';
                        const dateSelectors = [
                            '._1j2wfx4',
                            '[class*="date"]',
                            '[class*="time"]',
                            'time',
                            '[datetime]'
                        ];
                        for (const sel of dateSelectors) {
                            const dateEl = block.querySelector(sel);
                            if (dateEl) {
                                review_date = dateEl.innerText.trim() || dateEl.getAttribute('datetime') || '';
                                if (review_date) break;
                            }
                        }

                        // Пробуем найти статус пользователя
                        let user_status = null;
                        const statusSelectors = [
                            '._1k1c6j5',
                            '[class*="status"]',
                            '[class*="caption"]'
                        ];
                        for (const sel of statusSelectors) {
                            const captionEl = block.querySelector(sel);
                            if (captionEl) {
                                const t = captionEl.innerText.trim();
                                if (t && !t.match(/^\\d+\\s*отзыв/i)) {
                                    user_status = t;
                                    break;
                                }
                            }
                        }

                        if (text || rating > 0) {
                            reviews.push({ author, rating, text, review_date, user_status });
                            console.log(`Extracted review: author=${author}, rating=${rating}, text=${text.substring(0, 50)}...`);
                        }
                    } catch(e) {
                        console.log('Error extracting review:', e);
                    }
                }
                console.log(`Total extracted reviews: ${reviews.length}`);
                return reviews;
            }
            """
        )

        result = [{"source": "2gis", **r} for r in raw]

        duration_ms = int((time.monotonic() - t0) * 1000)
        self._log.info(
            "TwoGisParser.parse: done — %d reviews in %d ms", len(result), duration_ms
        )
        return result

    async def _close_2gis_modals(self) -> None:
        """Закрывает специфичные модальные окна 2GIS: 'Хорошо' и 'Обновление браузера'"""
        page = self._page  # Используем page из экземпляра класса
        
        try:
            # Закрываем попап "Хорошо"
            ok_selectors = [
                '[class*="modal"] button:has-text("Хорошо")',
                '[class*="popup"] button:has-text("Хорошо")', 
                'button:has-text("Хорошо")',
                '.modal button:has-text("Хорошо")'
            ]
            
            for selector in ok_selectors:
                try:
                    btn = await page.query_selector(selector, timeout=2000)
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(500)
                        self._log.info("TwoGisParser: closed 'Хорошо' modal")
                        break
                except:
                    continue
            
            # Закрываем попап "Обновление браузера"
            skip_selectors = [
                'button:has-text("Пропустить")',
                'button:has-text("Пропустить обновление")', 
                'button:has-text("Пропустить обновление браузера")',
                'button:has-text("Обновить браузер")',
                'button:has-text("Не сейчас")',
                'button:has-text("Позже")',
                '[class*="modal"] button:has-text("Пропустить")',
                '[class*="popup"] button:has-text("Пропустить")',
                '[class*="modal"] button:has-text("Пропустить обновление")',
                '.modal button:has-text("Пропустить")',
                '.popup button:has-text("Пропустить")',
                'button[data-testid*="skip"]',
                'button[data-testid*="later"]',
                'button[class*="skip"]',
                'button[class*="close"]'
            ]
            
            for selector in skip_selectors:
                try:
                    btn = await page.query_selector(selector, timeout=2000)
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(500)
                        self._log.info("TwoGisParser: skipped browser update modal")
                        break
                except:
                    continue
                    
        except Exception as exc:
            self._log.debug("TwoGisParser: _close_2gis_modals error: %s", exc)
