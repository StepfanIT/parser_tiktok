from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from playwright.sync_api import BrowserContext, Error, Locator, Page, TimeoutError, sync_playwright

from app.config import AppConfig
from app.models import OutgoingComment, ScrapedComment, SendResult, TikTokAccountConfig


class TikTokClientError(RuntimeError):
    """Raised when TikTok automation fails."""


class TikTokLoginRequiredError(TikTokClientError):
    """Raised when the user needs to refresh or create the login session."""


class TikTokVerificationRequiredError(TikTokClientError):
    """Raised when TikTok asks for a manual verification challenge."""


class TikTokPlaywrightClient:
    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        account: TikTokAccountConfig,
    ) -> None:
        self._config = config
        self._logger = logger
        self._account = account

    def scrape_comments(self, video_url: str) -> list[ScrapedComment]:
        self._logger.info("Starting comment scrape for %s", video_url)
        with sync_playwright() as playwright:
            context = self._open_context(playwright)
            page = self._create_working_page(context)
            collected: dict[str, ScrapedComment] = {}

            def handle_response(response: Any) -> None:
                self._collect_from_response(response, collected)

            page.on("response", handle_response)
            try:
                page = self._open_video_page(page, video_url, require_login=False)
                page = self._scroll_for_comments(page)
                self._wait_for_comment_content(page)

                if not collected:
                    self._logger.info("Network capture returned no comments, falling back to DOM parsing.")
                    for comment in self._extract_comments_from_dom(page):
                        collected[comment.comment_id] = comment

                comments = list(collected.values())
                self._logger.info("Collected %s comments for %s", len(comments), video_url)
                return comments
            finally:
                self._persist_storage_state(context)
                context.close()

    def send_comments(self, comments: Iterable[OutgoingComment]) -> list[SendResult]:
        comment_batch = list(comments)
        if not comment_batch:
            raise ValueError("The outgoing comment list is empty.")

        self._logger.info("Starting comment posting for %s comments.", len(comment_batch))
        with sync_playwright() as playwright:
            context = self._open_context(playwright)
            page = self._create_working_page(context)
            current_video_url: str | None = None
            results: list[SendResult] = []

            try:
                for item in comment_batch:
                    if item.delay_seconds > 0:
                        self._logger.info(
                            "Waiting %s seconds before sending comment #%s.",
                            item.delay_seconds,
                            item.order,
                        )
                        time.sleep(item.delay_seconds)

                    if current_video_url != item.video_url:
                        page = self._open_video_page(page, item.video_url, require_login=True)
                        page = self._prepare_comment_panel(page, video_url=item.video_url, require_input=True)
                        current_video_url = item.video_url

                    page, result = self._send_single_comment(page, item)
                    results.append(result)

                return results
            finally:
                self._persist_storage_state(context)
                context.close()

    def _open_context(self, playwright: Any) -> BrowserContext:
        browser_type = getattr(playwright, self._account.browser_type, None)
        if browser_type is None:
            raise TikTokClientError(
                f"Unsupported browser_type '{self._account.browser_type}' in account config."
            )

        profile_exists = self._account.user_data_dir.exists() and any(self._account.user_data_dir.iterdir())
        self._account.user_data_dir.mkdir(parents=True, exist_ok=True)

        launch_kwargs: dict[str, Any] = {
            "headless": self._account.headless,
            "slow_mo": self._account.slow_mo_ms,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if self._account.browser_channel:
            launch_kwargs["channel"] = self._account.browser_channel

        self._logger.info(
            "Launching %s browser with persistent profile %s.",
            self._account.browser_type,
            self._account.user_data_dir,
        )
        context = browser_type.launch_persistent_context(
            user_data_dir=str(self._account.user_data_dir),
            **launch_kwargs,
        )
        context.set_default_timeout(self._config.browser_action_timeout_ms)
        context.set_default_navigation_timeout(self._config.navigation_timeout_ms)

        if self._account.storage_state_path.exists():
            self._restore_storage_state_backup(context)

        return context

    def _create_working_page(self, context: BrowserContext) -> Page:
        return context.new_page()

    def _restore_storage_state_backup(self, context: BrowserContext) -> None:
        self._logger.info("Restoring storage state backup from %s", self._account.storage_state_path)
        try:
            payload = json.loads(self._account.storage_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            self._logger.warning("Unable to load storage state backup: %s", error)
            return

        cookies = payload.get("cookies") or []
        if cookies:
            try:
                context.add_cookies(cookies)
            except Error as error:
                self._logger.warning("Unable to restore TikTok cookies from storage backup: %s", error)

        origins = payload.get("origins") or []
        if not origins:
            return

        page = self._create_working_page(context)
        try:
            for origin_payload in origins:
                origin_url = str(origin_payload.get("origin") or "").strip()
                local_storage = origin_payload.get("localStorage") or []
                if not origin_url or not local_storage:
                    continue

                try:
                    page = self._goto_with_retry(page, origin_url, wait_until="commit")
                    page.evaluate(
                        """
                        (items) => {
                          for (const item of items) {
                            if (item?.name) {
                              localStorage.setItem(item.name, item.value ?? "");
                            }
                          }
                        }
                        """,
                        local_storage,
                    )
                except (TimeoutError, Error) as error:
                    self._logger.warning("Unable to restore local storage for %s: %s", origin_url, error)
        finally:
            try:
                if not page.is_closed():
                    page.close()
            except Error:
                pass

    def _persist_storage_state(self, context: BrowserContext) -> None:
        try:
            self._account.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(self._account.storage_state_path))
        except (OSError, Error) as error:
            self._logger.warning("Unable to save storage state backup: %s", error)

    def _open_video_page(self, page: Page, video_url: str, *, require_login: bool) -> Page:
        self._logger.info("Opening video page %s", video_url)
        try:
            page = self._goto_with_retry(page, video_url, wait_until="domcontentloaded")
        except (TimeoutError, Error) as error:
            raise TikTokClientError(f"Unable to open TikTok video page: {error}") from error

        page.wait_for_timeout(3_000)
        self._wait_for_video_surface(page)
        self._dismiss_overlays(page)
        if require_login:
            page = self._ensure_logged_in(page, return_url=video_url)
            self._wait_for_video_surface(page)
            self._dismiss_overlays(page)
        self._wait_for_verification_if_needed(page)
        return page

    def _ensure_logged_in(self, page: Page, *, return_url: str | None = None) -> Page:
        if not self._is_login_required(page):
            return page

        if self._account.headless:
            raise TikTokLoginRequiredError(
                "TikTok requires login, but the browser runs headless. "
                "Disable headless mode and log in manually once."
            )

        if not self._account.bootstrap_login_if_missing:
            raise TikTokLoginRequiredError(
                "TikTok session is not active. Enable bootstrap_login_if_missing or refresh the profile."
            )

        self._logger.info("TikTok session is not active. Opening manual login flow.")
        try:
            page = self._goto_with_retry(page, self._account.login_url, wait_until="domcontentloaded")
        except (TimeoutError, Error) as error:
            raise TikTokClientError(f"Unable to open TikTok login page: {error}") from error
        print()
        print("Сесія TikTok неактивна або протухла.")
        print("Увійдіть у TikTok вручну в уже відкритому профілі браузера.")
        input("Після успішного входу натисніть Enter тут, щоб продовжити... ")

        page.wait_for_timeout(1_500)
        self._dismiss_overlays(page)
        self._wait_for_verification_if_needed(page)
        if self._is_login_required(page):
            raise TikTokLoginRequiredError(
                "TikTok login is still required after manual authentication."
            )

        self._persist_storage_state(page.context)
        if return_url:
            try:
                page = self._goto_with_retry(page, return_url, wait_until="domcontentloaded")
            except (TimeoutError, Error) as error:
                raise TikTokClientError(
                    f"Unable to return to the TikTok video page after login: {error}"
                ) from error
            page.wait_for_timeout(3_000)
        return page

    def _goto_with_retry(self, page: Page, url: str, *, wait_until: str) -> Page:
        wait_strategies = [wait_until, "commit", "commit"]
        last_error: Error | TimeoutError | None = None

        for attempt, wait_strategy in enumerate(wait_strategies, start=1):
            try:
                page.goto(url, wait_until=wait_strategy)
                return page
            except TimeoutError as error:
                last_error = error
                if attempt == len(wait_strategies):
                    raise
                self._logger.warning(
                    "Timed out opening %s on attempt %s/%s with wait_until=%s. Retrying.",
                    url,
                    attempt,
                    len(wait_strategies),
                    wait_strategy,
                )
            except Error as error:
                if not self._is_navigation_aborted(error):
                    raise

                if self._urls_match(page.url, url):
                    self._logger.warning(
                        "Navigation to %s was aborted on attempt %s, but the page already reached %s.",
                        url,
                        attempt,
                        page.url,
                    )
                    return page

                last_error = error
                if attempt == len(wait_strategies):
                    raise
                self._logger.warning(
                    "Navigation to %s was aborted on attempt %s/%s with wait_until=%s. Retrying on a fresh page.",
                    url,
                    attempt,
                    len(wait_strategies),
                    wait_strategy,
                )

            page.wait_for_timeout(1_000)
            page = self._replace_page(page)

        if last_error is not None:
            raise last_error
        return page

    def _replace_page(self, page: Page) -> Page:
        context = page.context
        try:
            if not page.is_closed():
                page.close()
        except Error:
            pass
        return self._create_working_page(context)

    @staticmethod
    def _is_navigation_aborted(error: Error) -> bool:
        return "net::ERR_ABORTED" in str(error)

    @staticmethod
    def _urls_match(current_url: str, target_url: str) -> bool:
        def normalize(url: str) -> str:
            return url.split("#", 1)[0].split("?", 1)[0].rstrip("/")

        current = normalize(current_url or "")
        target = normalize(target_url or "")
        return bool(current) and current == target

    def _is_login_required(self, page: Page) -> bool:
        if "login" in page.url.lower():
            return True

        selectors = [
            '[data-e2e="top-login-button"]',
            'button:has-text("Log in")',
            'button:has-text("Log in to TikTok")',
            'a[href*="/login"]',
            'text="Log in to TikTok"',
            'text="Continue with phone/email/username"',
            'text="Log in for a better experience"',
        ]
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.is_visible(timeout=500):
                    return True
            except (TimeoutError, Error):
                continue

        try:
            login_button = page.get_by_role("button", name=re.compile("log in", re.I)).first
            if login_button.is_visible(timeout=500):
                return True
        except (TimeoutError, Error):
            pass
        return False

    def _wait_for_video_surface(self, page: Page, timeout_ms: int = 12_000) -> None:
        deadline = time.monotonic() + (timeout_ms / 1_000)
        while time.monotonic() < deadline:
            if self._find_comment_input(page) is not None or self._find_comment_trigger(page) is not None:
                return

            if self._is_login_required(page) or self._has_verification_challenge(page):
                return

            try:
                video = page.locator("video").first
                if video.is_visible(timeout=300):
                    return
            except (TimeoutError, Error):
                pass

            page.wait_for_timeout(500)

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
                    self._logger.info("Dismissed overlay via selector %s", selector)
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

            self._logger.info("Comment surface is hidden. Opening comments panel.")
            self._open_comments_panel(page)
            self._dismiss_overlays(page)
            self._close_shortcuts_modal(page)
            self._wait_for_verification_if_needed(page)
            self._wait_for_comment_content(page)

        if self._has_verification_challenge(page):
            raise TikTokVerificationRequiredError(
                "TikTok showed a verification challenge while opening comments."
            )
        if require_input and self._is_login_required(page):
            raise TikTokLoginRequiredError(
                "Comment input is unavailable because TikTok requires a fresh login session."
            )

        self._dump_comment_surface_debug(page, reason="comment_input_missing" if require_input else "comment_panel_missing")
        message = (
            "Comment input not found after opening comments. "
            "Check whether commenting is available for this video."
            if require_input
            else "Comment panel did not become ready for scraping."
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
                self._logger.info("Opened comments panel via %s", description)
                return

            if self._click_locator(locator, description=description, force=True):
                page.wait_for_timeout(1_500)
                self._logger.info("Opened comments panel via forced %s", description)
                return

        clicked = self._click_comment_trigger_with_javascript(page)
        if clicked:
            self._logger.info("Opened comments panel via javascript fallback: %s", clicked)
            page.wait_for_timeout(1_500)
            return

        self._logger.warning("Unable to find a visible comments trigger on the current TikTok page.")

    def _iter_comment_trigger_candidates(self, page: Page) -> list[tuple[str, Locator]]:
        return [
            ("selector button[aria-label*='Read or add comments' i]", page.locator('button[aria-label*="Read or add comments" i]').first),
            ("selector button[aria-label*='comment' i]", page.locator('button[aria-label*="comment" i]').first),
            ("selector [role='button'][aria-label*='comment' i]", page.locator('[role="button"][aria-label*="comment" i]').first),
            ("selector button:has-text('Comments')", page.locator('button:has-text("Comments")').first),
            ("selector [role='button']:has-text('Comments')", page.locator('[role="button"]:has-text("Comments")').first),
            ("selector [data-e2e='comment-icon']", page.locator('[data-e2e="comment-icon"]').first),
            ("selector [data-e2e='browse-comment-icon']", page.locator('[data-e2e="browse-comment-icon"]').first),
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
                        self._logger.info("Closed keyboard shortcuts modal.")
                        return
            except (TimeoutError, Error):
                continue

        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            if not self._has_shortcuts_modal(page):
                self._logger.info("Closed keyboard shortcuts modal with Escape.")
                return
        except (TimeoutError, Error):
            pass

        removed_count = self._force_hide_shortcuts_modal(page)
        if removed_count > 0:
            self._logger.info("Force-hidden keyboard shortcuts modal via javascript.")

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
                "TikTok showed a verification challenge. "
                "Run in headed mode, solve it manually, then retry."
            )

        print()
        print("TikTok показав перевірку безпеки в браузері.")
        print("Розв'яжіть puzzle/verification вручну у вікні браузера.")
        input("Після цього натисніть Enter тут, щоб продовжити... ")

        page.wait_for_timeout(1_500)
        self._dismiss_overlays(page)
        self._close_shortcuts_modal(page)
        if self._has_verification_challenge(page):
            raise TikTokVerificationRequiredError(
                "Перевірка TikTok все ще активна. Завершіть її в браузері й спробуйте ще раз."
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
                    self._logger.info("Detected TikTok verification challenge: %s", text)
                    return True
            except (TimeoutError, Error):
                continue
        return False

    def _scroll_for_comments(self, page: Page) -> Page:
        for round_number in range(1, self._config.default_scrape_scroll_rounds + 1):
            self._logger.info("Scrolling for comments, round %s", round_number)
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
        return page

    def _collect_from_response(self, response: Any, collected: dict[str, ScrapedComment]) -> None:
        url = response.url.lower()
        if "comment" not in url:
            return

        content_type = (response.headers or {}).get("content-type", "").lower()
        if "json" not in content_type and "text/plain" not in content_type:
            return

        try:
            payload = response.json()
        except (Error, json.JSONDecodeError):
            return

        for comment in self._extract_comments_from_payload(payload):
            collected.setdefault(comment.comment_id, comment)

    def _extract_comments_from_payload(self, payload: Any) -> list[ScrapedComment]:
        results: dict[str, ScrapedComment] = {}

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    normalized_key = key.lower()
                    if normalized_key in {"comments", "comment_list", "commentlist"} and isinstance(value, list):
                        for item in value:
                            comment = self._normalize_comment_payload(item)
                            if comment:
                                results.setdefault(comment.comment_id, comment)
                    walk(value)
                return

            if isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)
        return list(results.values())

    def _normalize_comment_payload(self, payload: Any) -> ScrapedComment | None:
        if not isinstance(payload, dict):
            return None

        comment_text = str(payload.get("text") or payload.get("comment") or "").strip()
        if not comment_text:
            return None

        user_payload = payload.get("user") or payload.get("user_info") or payload.get("author") or {}
        username = (
            self._first_non_empty(
                user_payload.get("unique_id"),
                user_payload.get("uniqueId"),
                user_payload.get("username"),
                payload.get("unique_id"),
            )
            or "unknown"
        )
        display_name = (
            self._first_non_empty(
                user_payload.get("nickname"),
                user_payload.get("display_name"),
                user_payload.get("displayName"),
                username,
            )
            or username
        )

        comment_id = str(
            self._first_non_empty(
                payload.get("cid"),
                payload.get("id"),
                payload.get("comment_id"),
                f"{username}:{comment_text}",
            )
        )

        likes_raw = self._first_non_empty(
            payload.get("digg_count"),
            payload.get("like_count"),
            payload.get("likes"),
        )
        likes = int(likes_raw) if likes_raw not in (None, "") else None

        published_at = self._parse_timestamp(
            self._first_non_empty(payload.get("create_time"), payload.get("created_at"))
        )
        return ScrapedComment(
            comment_id=comment_id,
            author_username=str(username),
            author_display_name=str(display_name),
            text=comment_text,
            likes=likes,
            published_at=published_at,
        )

    def _extract_comments_from_dom(self, page: Page) -> list[ScrapedComment]:
        rows = page.evaluate(
            """
            () => {
              const rowSelectors = [
                '[data-e2e="comment-level-1"]',
                '[data-e2e="comment-item"]',
                'div[class*="CommentItem"]',
                'li[class*="CommentItem"]'
              ];

              const elements = [];
              const seen = new Set();
              for (const selector of rowSelectors) {
                for (const element of document.querySelectorAll(selector)) {
                  if (!seen.has(element)) {
                    seen.add(element);
                    elements.push(element);
                  }
                }
              }

              const normalizeLine = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              const looksLikeMeta = (value) => {
                return (
                  /^\\d+$/.test(value) ||
                  /^\\d+[smhdwy]$/.test(value.toLowerCase()) ||
                  ['reply', 'like', 'liked', 'pinned'].includes(value.toLowerCase())
                );
              };

              return elements.map((element, index) => {
                const anchor = element.querySelector('a[href*="/@"]');
                const href = anchor ? (anchor.getAttribute('href') || '') : '';
                const usernameMatch = href.match(new RegExp('/@([^/?]+)'));
                const username = usernameMatch
                  ? usernameMatch[1]
                  : normalizeLine(anchor ? anchor.textContent : '').replace(/^@/, '') || 'unknown';
                const displayName = normalizeLine(anchor ? anchor.textContent : '') || username;
                const textNode =
                  element.querySelector('[data-e2e*="comment-text"]') ||
                  element.querySelector('p') ||
                  element.querySelector('span[data-text="true"]');
                const lines = Array.from(
                  new Set(
                    (element.innerText || element.textContent || '')
                      .split(/\\n+/)
                      .map(normalizeLine)
                      .filter(Boolean)
                  )
                );
                const text =
                  normalizeLine(textNode ? textNode.textContent : '') ||
                  lines.find((line) => {
                    return (
                      line !== displayName &&
                      line !== username &&
                      line !== `@${username}` &&
                      !looksLikeMeta(line)
                    );
                  }) ||
                  '';
                const likeNode = Array.from(element.querySelectorAll('span, strong')).find((node) => {
                  const value = normalizeLine(node.textContent);
                  return /^\\d+$/.test(value);
                });
                const likes = likeNode ? normalizeLine(likeNode.textContent) : '';
                const timeNode = element.querySelector('time') || element.querySelector('[data-e2e*="comment-time"]');
                const publishedAt = normalizeLine(
                  timeNode ? (timeNode.getAttribute('datetime') || timeNode.textContent) : ''
                );

                return {
                  comment_id: element.getAttribute('data-comment-id') || `${username}:${index}:${text}`,
                  author_username: username,
                  author_display_name: displayName,
                  text,
                  likes,
                  published_at: publishedAt
                };
              }).filter(item => item.text);
            }
            """
        )

        comments_by_id: dict[str, ScrapedComment] = {}
        for row in rows:
            likes = int(row["likes"]) if str(row.get("likes") or "").isdigit() else None
            comment = ScrapedComment(
                comment_id=str(row["comment_id"]),
                author_username=str(row["author_username"]),
                author_display_name=str(row["author_display_name"]),
                text=str(row["text"]),
                likes=likes,
                published_at=str(row.get("published_at") or "") or None,
            )
            comments_by_id.setdefault(comment.comment_id, comment)
        return list(comments_by_id.values())

    def _send_single_comment(self, page: Page, outgoing_comment: OutgoingComment) -> tuple[Page, SendResult]:
        self._logger.info("Sending comment #%s to %s", outgoing_comment.order, outgoing_comment.video_url)
        page = self._prepare_comment_panel(page, video_url=outgoing_comment.video_url, require_input=True)
        input_locator = self._find_comment_input(page)
        if input_locator is None:
            raise TikTokLoginRequiredError(
                "Comment input not found. Refresh storage state or reopen the comments panel."
            )

        self._fill_comment_input(page, input_locator, outgoing_comment.text)

        try:
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and "comment" in response.url.lower()
                and any(keyword in response.url.lower() for keyword in ("publish", "post", "submit")),
                timeout=15_000,
            ) as response_info:
                self._submit_comment(page)

            response = response_info.value
            details = self._describe_response(response)
            success = response.ok
        except TimeoutError:
            self._logger.warning(
                "No publish response captured for comment #%s.",
                outgoing_comment.order,
            )
            page.wait_for_timeout(2_500)
            details = "No publish response captured; submit action was triggered."
            success = False

        self._logger.info("Comment #%s result: %s", outgoing_comment.order, details)
        return page, SendResult(
            outgoing_comment=outgoing_comment,
            success=success,
            details=details,
        )

    def _find_comment_input(self, page: Page) -> Locator | None:
        selectors = [
            '[data-e2e="comment-input"] div[contenteditable="true"][role="textbox"]',
            '[data-e2e="comment-input"] div[contenteditable="true"]',
            'div.public-DraftEditor-content[contenteditable="true"]',
            'div[class*="DraftEditor-content"][contenteditable="true"]',
            'div[contenteditable="true"][role="textbox"]',
            'div[contenteditable="true"][data-e2e*="comment"]',
            'textarea[data-e2e="comment-input"]',
            'div[class*="comment-panel-input"]',
            '[data-e2e="comment-input"]',
        ]
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.is_visible(timeout=3_000):
                    return locator
            except (TimeoutError, Error):
                continue
        return None

    def _dump_comment_surface_debug(self, page: Page, *, reason: str) -> None:
        try:
            payload = {
                "reason": reason,
                "url": page.url,
                "title": page.title(),
                "login_required": self._is_login_required(page),
                "verification_required": self._has_verification_challenge(page),
                "body_text_snippet": (page.locator("body").inner_text(timeout=2_000) or "")[:4000],
                "comment_trigger_candidates": page.evaluate(
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

                      const rows = [];
                      for (const element of document.querySelectorAll('button, [role="button"], a, div')) {
                        const aria = normalize(element.getAttribute('aria-label'));
                        const text = normalize(element.innerText || element.textContent);
                        const dataE2e = normalize(element.getAttribute('data-e2e'));
                        const combined = `${aria} ${text} ${dataE2e}`.toLowerCase();
                        if (!combined.includes('comment')) {
                          continue;
                        }

                        rows.push({
                          tag: element.tagName,
                          aria_label: aria,
                          text,
                          data_e2e: dataE2e,
                          visible: isVisible(element),
                          class_name: normalize(element.className),
                        });
                        if (rows.length >= 25) {
                          break;
                        }
                      }
                      return rows;
                    }
                    """
                ),
            }
            debug_path = self._config.logs_dir / "comment_surface_debug.json"
            debug_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._logger.warning("Saved comment surface debug dump to %s", debug_path)
        except Exception as error:
            self._logger.warning("Unable to write comment surface debug dump: %s", error)

    def _fill_comment_input(self, page: Page, input_locator: Locator, text: str) -> None:
        self._wait_for_verification_if_needed(page)
        self._close_shortcuts_modal(page)
        input_locator = self._resolve_comment_input(input_locator)
        content_editable = input_locator.get_attribute("contenteditable")

        if content_editable == "true":
            self._focus_comment_editor(page, input_locator)
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.keyboard.type(text, delay=40)
        else:
            try:
                input_locator.focus()
                input_locator.fill(text)
            except (TimeoutError, Error) as error:
                raise TikTokClientError(f"Unable to fill the comment input: {error}") from error

        self._wait_for_comment_post_button(page)

    def _focus_comment_editor(self, page: Page, input_locator: Locator) -> None:
        targets = [
            ("editor", input_locator),
            ("comment-input", page.locator('[data-e2e="comment-input"]').first),
            (
                "editor-container",
                input_locator.locator("xpath=ancestor-or-self::*[@data-e2e='comment-input'][1]").first,
            ),
            ("placeholder", page.locator('[data-e2e="comment-input"] [id^="placeholder-"]').first),
        ]

        last_error: Exception | None = None
        for description, target in targets:
            self._close_shortcuts_modal(page)
            self._wait_for_verification_if_needed(page)
            try:
                input_locator.focus()
                if self._is_comment_editor_focused(input_locator):
                    return
            except (TimeoutError, Error) as error:
                last_error = error

            try:
                if target.count() == 0:
                    continue
            except Error:
                continue

            try:
                target.click(timeout=2_000, force=True)
            except (TimeoutError, Error) as error:
                last_error = error

            try:
                input_locator.evaluate(
                    """
                    (element) => {
                      element.focus();
                    }
                    """
                )
                page.wait_for_timeout(150)
                if self._is_comment_editor_focused(input_locator):
                    return
            except (TimeoutError, Error) as error:
                last_error = error

            self._logger.debug("Unable to focus comment editor via %s.", description)

        if last_error is not None:
            raise TikTokClientError(f"Unable to focus the comment input: {last_error}") from last_error
        raise TikTokClientError("Unable to focus the comment input.")

    def _is_comment_editor_focused(self, input_locator: Locator) -> bool:
        try:
            return bool(
                input_locator.evaluate(
                    """
                    (element) => {
                      const active = document.activeElement;
                      return active === element || element.contains(active);
                    }
                    """
                )
            )
        except (TimeoutError, Error):
            return False

    def _wait_for_comment_post_button(self, page: Page, timeout_ms: int = 5_000) -> None:
        deadline = time.monotonic() + (timeout_ms / 1_000)
        locator = page.locator('button[data-e2e="comment-post"]').first
        while time.monotonic() < deadline:
            try:
                if locator.is_enabled(timeout=250):
                    return
            except (TimeoutError, Error):
                return

            page.wait_for_timeout(200)

    def _submit_comment(self, page: Page) -> None:
        self._wait_for_verification_if_needed(page)
        selectors = [
            'button[data-e2e="comment-post"]',
            'button:has-text("Post")',
            'button:has-text("Send")',
        ]
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.is_enabled(timeout=2_000):
                    locator.click()
                    return
            except (TimeoutError, Error):
                continue

        page.keyboard.press("Enter")

    def _resolve_comment_input(self, input_locator: Locator) -> Locator:
        content_editable = input_locator.get_attribute("contenteditable")
        if content_editable == "true":
            return input_locator

        tag_name = (input_locator.evaluate("(element) => element.tagName") or "").upper()
        if tag_name in {"TEXTAREA", "INPUT"}:
            return input_locator

        nested_selectors = [
            'div[contenteditable="true"][role="textbox"]',
            'div[contenteditable="true"]',
            'textarea',
            'input',
        ]
        for selector in nested_selectors:
            nested_locator = input_locator.locator(selector).first
            try:
                if nested_locator.is_visible(timeout=500):
                    return nested_locator
            except (TimeoutError, Error):
                continue

        return input_locator

    def _describe_response(self, response: Any) -> str:
        try:
            payload = response.json()
        except (Error, json.JSONDecodeError):
            return f"HTTP {response.status}"

        message = self._first_non_empty(
            payload.get("message"),
            payload.get("status_msg"),
            payload.get("msg"),
        )
        status_code = self._first_non_empty(
            payload.get("status_code"),
            payload.get("code"),
            response.status,
        )
        return f"status={status_code}, message={message or 'ok'}"

    @staticmethod
    def _first_non_empty(*values: Any) -> Any:
        for value in values:
            if value not in (None, ""):
                return value
        return None

    @staticmethod
    def _parse_timestamp(value: Any) -> str | None:
        if value in (None, ""):
            return None

        try:
            timestamp = int(value)
        except (TypeError, ValueError):
            return str(value)

        if timestamp > 10_000_000_000:
            timestamp //= 1_000

        return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()
