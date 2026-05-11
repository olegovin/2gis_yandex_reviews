"""
parsers/captcha.py — Yandex SmartCaptcha / CheckboxCaptcha auto-solver.

Extracted from the original ReviewParser.try_auto_click_captcha().
Returns True if captcha appears to be solved, False otherwise.
"""
from __future__ import annotations

import logging
import random

from playwright.async_api import Page

logger = logging.getLogger(__name__)


async def try_auto_click_captcha(page: Page) -> bool:
    """
    Attempt to click the "Я не робот" checkbox captcha.

    Strategy:
      1. Human-like mouse movement to a random point.
      2. Slow scroll to mid-page (avoids instant-bot detection).
      3. Locate checkbox by CSS selector or visible text.
      4. Move cursor to element center with random step count.
      5. Dispatch mousedown/mouseup/click sequence.
      6. Wait and check URL — if 'checkcaptcha' is gone, we succeeded.

    Returns True if URL no longer contains captcha markers, False otherwise.
    """
    try:
        await page.mouse.move(
            random.randint(100, 500),
            random.randint(100, 400),
            steps=random.randint(10, 30),
        )
        await page.wait_for_timeout(random.randint(200, 500))

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.3)")
        await page.wait_for_timeout(random.randint(300, 700))

        checkbox = await page.query_selector(
            ".CheckboxCaptcha-Button, #js-button, .CheckboxCaptcha-Checkbox"
        )
        if not checkbox:
            checkbox = await page.query_selector("text=Я не робот")

        if not checkbox:
            logger.warning("try_auto_click_captcha: checkbox element not found")
            return False

        box = await checkbox.bounding_box()
        if not box:
            logger.warning("try_auto_click_captcha: could not get bounding box")
            return False

        await page.mouse.move(
            box["x"] + box["width"] / 2,
            box["y"] + box["height"] / 2,
            steps=random.randint(20, 40),
        )
        await page.wait_for_timeout(random.randint(100, 300))

        await checkbox.dispatch_event("mousedown")
        await page.wait_for_timeout(random.randint(50, 150))
        await checkbox.dispatch_event("mouseup")
        await checkbox.click()

        # Ждем решения капчи и проверяем несколько раз
        for attempt in range(10):  # до 10 секунд ожидания
            await page.wait_for_timeout(1000)
            
            # Проверяем URL
            current_url = page.url
            if "checkcaptcha" not in current_url and "smart-captcha" not in current_url:
                logger.info("try_auto_click_captcha: solved successfully")
                return True
            
            # Проверяем появилась ли кнопка подтверждения
            confirm_button = await page.query_selector(
                "button:has-text('Продолжить'), button:has-text('Подтвердить'), .CheckboxCaptcha-Button"
            )
            if confirm_button:
                try:
                    await confirm_button.click()
                    await page.wait_for_timeout(1000)
                    logger.info("try_auto_click_captcha: clicked confirm button")
                except:
                    pass
            
            # Проверяем на продвинутую капчу
            advanced = await page.query_selector(".AdvancedCaptcha, .Captcha")
            if advanced:
                logger.warning("try_auto_click_captcha: advanced captcha appeared — giving up")
                return False

        logger.warning("try_auto_click_captcha: timeout waiting for captcha resolution")
        return False

    except Exception as exc:
        logger.exception("try_auto_click_captcha: unexpected error: %s", exc)
        return False
