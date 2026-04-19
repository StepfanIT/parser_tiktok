from __future__ import annotations

import re
import time

from playwright.sync_api import Error, Locator, Page, TimeoutError

from app.integrations.tiktok_client_support.runtime import (
    TikTokClientError,
    TikTokLoginRequiredError,
    TikTokVerificationRequiredError,
)
from app.models import ScrapedComment


class TikTokInteractionMixin:
    def _dismiss_overlays(self, page: Page) -> None:
        selectors = [
            'button:has-text("Accept all")',
            'button:has-text("Accept")',
            'button:has-text("Allow all")',
            'button:has-text("Close")',
            'button[aria-label="Close"]',
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.is_visible(timeout=1_000):
                    locator.click()
                    self._logger.info("Dismissed overlay via selector %s.", selector)
                    page.wait_for_timeout(500)
            except (TimeoutError, Error):
                continue

    def _prepare_comment_panel(
        self,
        page: Page,
        *,
        video_url: str | None = None,
        require_input: bool,
    ) -> Page:
        self._close_shortcuts_modal(page)
        self._wait_for_verification_if_needed(page)
        if require_input:
            page = self._ensure_logged_in(page, return_url=video_url)

        for _attempt in range(2):
            if self._comment_surface_ready(page, require_input=require_input):
                return page

            self._logger.info("Comment panel is hidden. Opening it now.")
            self._open_comments_panel(page)
            self._dismiss_overlays(page)
            self._close_shortcuts_modal(page)
            self._wait_for_verification_if_needed(page)
            self._wait_for_comment_content(page)

        if self._has_verification_challenge(page):
            raise TikTokVerificationRequiredError(
                "TikTok triggered a verification challenge while opening comments."
            )
        if require_input and self._is_login_required(page):
            raise TikTokLoginRequiredError(
                "The comment input is unavailable because TikTok requested a session refresh."
            )

        self._dump_comment_surface_debug(
            page,
            reason="comment_input_missing" if require_input else "comment_panel_missing",
        )
        message = (
            "The comment input could not be found after opening the panel. "
            "Check whether commenting is available for this video."
            if require_input
            else "The comment panel never reached a ready state for collection."
        )
        raise TikTokClientError(message)

    def _comment_surface_ready(self, page: Page, *, require_input: bool) -> bool:
        if self._find_comment_input(page) is not None:
            return True
        if not require_input and self._find_comment_item(page) is not None:
            return True
        if not require_input and self._has_comment_zero_state(page):
            return True
        return False

    def _open_comments_panel(self, page: Page) -> None:
        for description, locator in self._iter_comment_trigger_candidates(page):
            if self._click_locator(locator, description=description, force=False):
                page.wait_for_timeout(1_500)
                self._logger.info("Opened the comment panel via %s.", description)
                return

            if self._click_locator(locator, description=description, force=True):
                page.wait_for_timeout(1_500)
                self._logger.info("Opened the comment panel via forced click on %s.", description)
                return

        clicked = self._click_comment_trigger_with_javascript(page)
        if clicked:
            self._logger.info("Opened the comment panel via JavaScript fallback: %s.", clicked)
            page.wait_for_timeout(1_500)
            return

        self._logger.warning("Could not find a visible comment-panel trigger on the page.")

    def _iter_comment_trigger_candidates(self, page: Page) -> list[tuple[str, Locator]]:
        return [
            (
                "selector button[aria-label*='Read or add comments' i]",
                page.locator('button[aria-label*="Read or add comments" i]').first,
            ),
            (
                "selector button[aria-label*='comment' i]",
                page.locator('button[aria-label*="comment" i]').first,
            ),
            (
                "selector [role='button'][aria-label*='comment' i]",
                page.locator('[role="button"][aria-label*="comment" i]').first,
            ),
            ("selector button:has-text('Comments')", page.locator('button:has-text("Comments")').first),
            (
                "selector [role='button']:has-text('Comments')",
                page.locator('[role="button"]:has-text("Comments")').first,
            ),
            ("selector [data-e2e='comment-icon']", page.locator('[data-e2e="comment-icon"]').first),
            (
                "selector [data-e2e='browse-comment-icon']",
                page.locator('[data-e2e="browse-comment-icon"]').first,
            ),
            ("role button /comment/i", page.get_by_role("button", name=re.compile("comment", re.I)).first),
            ("label /comment/i", page.get_by_label(re.compile("comment", re.I)).first),
            ("text /^Comments$/", page.get_by_text(re.compile("^Comments$", re.I)).first),
        ]

    def _find_comment_trigger(self, page: Page) -> Locator | None:
        for _description, locator in self._iter_comment_trigger_candidates(page):
            try:
                if locator.count() > 0 and locator.is_visible(timeout=500):
                    return locator
            except (TimeoutError, Error):
                continue
        return None

    def _click_locator(self, locator: Locator, *, description: str, force: bool) -> bool:
        try:
            if locator.count() == 0:
                return False
        except Error:
            return False

        target = locator.first
        try:
            target.scroll_into_view_if_needed(timeout=1_000)
        except (TimeoutError, Error):
            pass

        try:
            if not force and not target.is_visible(timeout=800):
                return False
        except (TimeoutError, Error):
            if not force:
                return False

        try:
            target.click(timeout=2_000, force=force)
            return True
        except (TimeoutError, Error) as error:
            self._logger.debug("Failed to click %s (force=%s): %s", description, force, error)
            return False

    def _click_comment_trigger_with_javascript(self, page: Page) -> str | None:
        try:
            result = page.evaluate(
                """
                () => {
                  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                  const isVisible = (element) => {
                    if (!element) {
                      return false;
                    }

                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return (
                      style.visibility !== 'hidden' &&
                      style.display !== 'none' &&
                      rect.width > 0 &&
                      rect.height > 0
                    );
                  };

                  const candidates = [];
                  const selector = 'button, [role="button"], a, div';
                  for (const element of document.querySelectorAll(selector)) {
                    if (!isVisible(element)) {
                      continue;
                    }

                    const aria = normalize(element.getAttribute('aria-label'));
                    const text = normalize(element.innerText || element.textContent);
                    const dataE2e = normalize(element.getAttribute('data-e2e'));
                    const combined = `${aria} ${text} ${dataE2e}`.toLowerCase();
                    if (!combined.includes('comment')) {
                      continue;
                    }

                    let score = 0;
                    if (aria.toLowerCase().includes('read or add comments')) score += 120;
                    if (aria.toLowerCase().includes('comment')) score += 70;
                    if (dataE2e.toLowerCase().includes('comment')) score += 50;
                    if (text.toLowerCase() === 'comments') score += 35;
                    if (/^\\d+$/.test(text) && aria.toLowerCase().includes('comment')) score += 25;
                    if (element.tagName === 'BUTTON') score += 20;
                    if (element.closest('header, nav, aside')) score -= 30;

                    candidates.push({
                      element,
                      score,
                      aria,
                      text,
                      dataE2e,
                      className: normalize(element.className),
                    });
                  }

                  candidates.sort((left, right) => right.score - left.score);
                  const best = candidates[0];
                  if (!best) {
                    return null;
                  }

                  best.element.click();
                  return JSON.stringify({
                    aria: best.aria,
                    text: best.text,
                    data_e2e: best.dataE2e,
                    class_name: best.className,
                    score: best.score,
                  });
                }
                """
            )
        except Error:
            return None

        return str(result) if result else None

    def _wait_for_comment_content(self, page: Page, timeout_ms: int = 12_000) -> None:
        deadline = time.monotonic() + (timeout_ms / 1_000)
        while time.monotonic() < deadline:
            if self._comment_surface_ready(page, require_input=False):
                return
            if self._has_verification_challenge(page) or self._is_login_required(page):
                return
            page.wait_for_timeout(500)

    def _find_comment_item(self, page: Page) -> Locator | None:
        selectors = [
            '[data-e2e="comment-level-1"]',
            '[data-e2e="comment-item"]',
            'div[class*="CommentItem"]',
            'li[class*="CommentItem"]',
        ]
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.is_visible(timeout=1_000):
                    return locator
            except (TimeoutError, Error):
                continue
        return None

    def _has_comment_zero_state(self, page: Page) -> bool:
        selectors = [
            'text="Be the first to comment"',
            'text="No comments yet"',
            'text="No comments"',
        ]
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.is_visible(timeout=300):
                    return True
            except (TimeoutError, Error):
                continue
        return False

    def _close_shortcuts_modal(self, page: Page) -> None:
        if not self._has_shortcuts_modal(page):
            return

        close_selectors = [
            'div[class*="DivKeyboardShortcutContainer"] div[class*="DivXMarkWrapper"]',
            'div[class*="DivFixedBottomContainer"] div[class*="DivXMarkWrapper"]',
            'button[aria-label="Close"]',
            'button:has-text("Close")',
        ]
        for selector in close_selectors:
            locator = page.locator(selector).first
            try:
                if locator.is_visible(timeout=500):
                    locator.click(force=True)
                    page.wait_for_timeout(500)
                    if not self._has_shortcuts_modal(page):
                        self._logger.info("Closed the keyboard shortcuts modal.")
                        return
            except (TimeoutError, Error):
                continue

        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            if not self._has_shortcuts_modal(page):
                self._logger.info("Closed the keyboard shortcuts modal with Escape.")
                return
        except (TimeoutError, Error):
            pass

        removed_count = self._force_hide_shortcuts_modal(page)
        if removed_count > 0:
            self._logger.info("Force-hidden the keyboard shortcuts modal via JavaScript.")

    def _has_shortcuts_modal(self, page: Page) -> bool:
        selectors = [
            "text=Introducing keyboard shortcuts!",
            'div[class*="DivKeyboardShortcutContainer"]',
            'div[class*="DivFixedBottomContainer"] div[class*="DivKeyboardShortcutContent"]',
        ]
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.is_visible(timeout=300):
                    return True
            except (TimeoutError, Error):
                continue
        return False

    def _force_hide_shortcuts_modal(self, page: Page) -> int:
        try:
            return int(
                page.evaluate(
                    """
                    () => {
                      let removed = 0;
                      const selectors = [
                        'div[class*="DivKeyboardShortcutContainer"]',
                        'div[class*="DivFixedBottomContainer"]'
                      ];

                      for (const selector of selectors) {
                        for (const element of document.querySelectorAll(selector)) {
                          const text = (element.innerText || element.textContent || '').toLowerCase();
                          if (!text.includes('keyboard shortcut')) {
                            continue;
                          }

                          element.remove();
                          removed += 1;
                        }
                      }

                      return removed;
                    }
                    """
                )
            )
        except Error:
            return 0

    def _wait_for_verification_if_needed(self, page: Page) -> None:
        if not self._has_verification_challenge(page):
            return

        if self._account.headless:
            raise TikTokVerificationRequiredError(
                "TikTok triggered a verification challenge. "
                "Run the browser with headless disabled, complete the challenge manually, and retry."
            )

        print()
        print("TikTok displayed a security challenge in the browser.")
        print("Solve the puzzle or verification step manually in the browser window.")
        input("Press Enter here after the challenge is completed to continue... ")

        page.wait_for_timeout(1_500)
        self._dismiss_overlays(page)
        self._close_shortcuts_modal(page)
        if self._has_verification_challenge(page):
            raise TikTokVerificationRequiredError(
                "The TikTok verification challenge is still active. Complete it in the browser and try again."
            )

    def _has_verification_challenge(self, page: Page) -> bool:
        challenge_texts = [
            "Drag the slider to fit the puzzle",
            "Verify to continue",
            "Complete the security check",
        ]
        for text in challenge_texts:
            locator = page.locator(f"text={text}").first
            try:
                if locator.is_visible(timeout=300):
                    self._logger.warning("TikTok displayed a security challenge: %s.", text)
                    return True
            except (TimeoutError, Error):
                continue
        return False

    def _scroll_for_comments(self, page: Page, collected: dict[str, ScrapedComment]) -> Page:
        round_number = 0
        idle_rounds = 0
        best_count = len(collected)

        while True:
            round_number += 1
            self._logger.info(
                "Comment scroll pass %s: loading the next portion.",
                round_number,
            )
            page = self._prepare_comment_panel(page, video_url=page.url, require_input=False)
            self._wait_for_verification_if_needed(page)
            try:
                page.evaluate(
                    """
                    (delta) => {
                      const rootSelectors = [
                        '[data-e2e="comment-level-1"]',
                        '[data-e2e="comment-item"]',
                        '[data-e2e="comment-input"]',
                        'div[class*="CommentList"]',
                        'div[class*="CommentPanel"]'
                      ];

                      const seen = new Set();
                      const candidates = [];
                      const push = (node) => {
                        if (node && !seen.has(node)) {
                          seen.add(node);
                          candidates.push(node);
                        }
                      };

                      for (const selector of rootSelectors) {
                        const root = document.querySelector(selector);
                        let current = root;
                        while (current) {
                          push(current);
                          current = current.parentElement;
                        }
                      }

                      const isScrollable = (node) => {
                        if (!node) {
                          return false;
                        }

                        const style = window.getComputedStyle(node);
                        const overflowY = style.overflowY;
                        return (
                          node.scrollHeight > node.clientHeight + 40 &&
                          ['auto', 'scroll', 'overlay'].includes(overflowY)
                        );
                      };

                      for (const node of candidates) {
                        if (isScrollable(node)) {
                          node.scrollBy(0, delta);
                          return true;
                        }
                      }

                      window.scrollBy(0, delta);
                      return false;
                    }
                    """,
                    1_800,
                )
            except Error:
                page.mouse.wheel(0, 2_000)
            page.wait_for_timeout(int(self._config.default_scroll_pause_seconds * 1_000))
            self._wait_for_comment_content(page)
            self._collect_comments_from_dom(page, collected)
            current_count = len(collected)

            if current_count > best_count:
                self._logger.info(
                    "Comment scroll pass %s: total comments grew from %s to %s.",
                    round_number,
                    best_count,
                    current_count,
                )
                best_count = current_count
                idle_rounds = 0
            else:
                idle_rounds += 1
                self._logger.info(
                    "Comment scroll pass %s: no new comments found (%s/%s idle passes).",
                    round_number,
                    idle_rounds,
                    self._config.default_scrape_idle_rounds,
                )

            if (
                round_number >= self._config.default_scrape_scroll_rounds
                and idle_rounds >= self._config.default_scrape_idle_rounds
            ):
                self._logger.info(
                    "Stopping collection after %s passes because no more comments are loading.",
                    round_number,
                )
                return page
