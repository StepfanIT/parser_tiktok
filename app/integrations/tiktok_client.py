from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Error, Locator, Page, TimeoutError, sync_playwright

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
            browser = self._launch_browser(playwright)
            context = self._create_context(browser, use_saved_state=True)
            page = context.new_page()
            collected: dict[str, ScrapedComment] = {}

            def handle_response(response: Any) -> None:
                self._collect_from_response(response, collected)

            page.on("response", handle_response)
            try:
                self._open_video_page(page, video_url)
                self._dismiss_overlays(page)
                self._scroll_for_comments(page)

                if not collected:
                    self._logger.info("Network capture returned no comments, falling back to DOM parsing.")
                    for comment in self._extract_comments_from_dom(page):
                        collected[comment.comment_id] = comment

                comments = list(collected.values())
                self._logger.info("Collected %s comments for %s", len(comments), video_url)
                return comments
            finally:
                context.close()
                browser.close()

    def send_comments(self, comments: Iterable[OutgoingComment]) -> list[SendResult]:
        comment_batch = list(comments)
        if not comment_batch:
            raise ValueError("The outgoing comment list is empty.")

        self._logger.info("Starting comment posting for %s comments.", len(comment_batch))
        with sync_playwright() as playwright:
            browser = self._launch_browser(playwright)
            context = self._create_context(
                browser,
                use_saved_state=True,
                require_login=True,
            )
            page = context.new_page()
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
                        self._open_video_page(page, item.video_url)
                        self._dismiss_overlays(page)
                        self._prepare_comment_panel(page)
                        current_video_url = item.video_url

                    results.append(self._send_single_comment(page, item))

                context.storage_state(path=str(self._account.storage_state_path))
                return results
            finally:
                context.close()
                browser.close()

    def _launch_browser(self, playwright: Any) -> Browser:
        browser_type = getattr(playwright, self._account.browser_type, None)
        if browser_type is None:
            raise TikTokClientError(
                f"Unsupported browser_type '{self._account.browser_type}' in account config."
            )

        launch_kwargs: dict[str, Any] = {
            "headless": self._account.headless,
            "slow_mo": self._account.slow_mo_ms,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if self._account.browser_channel:
            launch_kwargs["channel"] = self._account.browser_channel

        self._logger.info("Launching %s browser.", self._account.browser_type)
        return browser_type.launch(**launch_kwargs)

    def _create_context(
        self,
        browser: Browser,
        *,
        use_saved_state: bool,
        require_login: bool = False,
    ) -> BrowserContext:
        context_kwargs: dict[str, Any] = {}
        if use_saved_state and self._account.storage_state_path.exists():
            context_kwargs["storage_state"] = str(self._account.storage_state_path)
            self._logger.info("Loading saved storage state from %s", self._account.storage_state_path)

        context = browser.new_context(**context_kwargs)
        context.set_default_timeout(self._config.browser_action_timeout_ms)
        context.set_default_navigation_timeout(self._config.navigation_timeout_ms)

        if require_login and not self._account.storage_state_path.exists():
            if not self._account.bootstrap_login_if_missing:
                raise TikTokLoginRequiredError(
                    "Login session not found and bootstrap_login_if_missing is disabled."
                )
            self._bootstrap_login(context)

        return context

    def _bootstrap_login(self, context: BrowserContext) -> None:
        self._logger.info("No storage state found. Opening manual login flow.")
        page = context.new_page()
        page.goto(self._account.login_url, wait_until="domcontentloaded")
        print()
        print("Сесію TikTok ще не збережено.")
        print("Відкрито браузер. Увійдіть у TikTok вручну в цьому вікні.")
        input("Після успішного входу натисніть Enter тут, щоб зберегти сесію... ")
        self._account.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(self._account.storage_state_path))
        self._logger.info("Storage state saved to %s", self._account.storage_state_path)

    def _open_video_page(self, page: Page, video_url: str) -> None:
        self._logger.info("Opening video page %s", video_url)
        page.goto(video_url, wait_until="domcontentloaded")
        page.wait_for_timeout(3_000)

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

    def _prepare_comment_panel(self, page: Page) -> None:
        self._close_shortcuts_modal(page)
        self._wait_for_verification_if_needed(page)

        if self._find_comment_input(page) is not None:
            return

        self._logger.info("Comment input is hidden. Opening comments panel.")
        self._open_comments_panel(page)
        self._close_shortcuts_modal(page)
        self._wait_for_verification_if_needed(page)

        if self._find_comment_input(page) is None:
            raise TikTokLoginRequiredError(
                "Comment input not found after opening comments. "
                "Check whether commenting is available for this video."
            )

    def _open_comments_panel(self, page: Page) -> None:
        selectors = [
            'button[aria-label*="Read or add comments" i]',
            'button[aria-label*="comments" i]',
            'button:has-text("Comments")',
            '[role="button"][aria-label*="comments" i]',
        ]

        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.is_visible(timeout=2_000):
                    locator.click()
                    page.wait_for_timeout(2_000)
                    self._logger.info("Opened comments panel via selector %s", selector)
                    return
            except (TimeoutError, Error):
                continue

    def _close_shortcuts_modal(self, page: Page) -> None:
        try:
            shortcuts_title = page.locator("text=Introducing keyboard shortcuts!").first
            if not shortcuts_title.is_visible(timeout=500):
                return
        except (TimeoutError, Error):
            return

        close_selectors = [
            'div[class*="DivKeyboardShortcutContainer"] div[class*="DivXMarkWrapper"]',
            'button[aria-label="Close"]',
            'button:has-text("Close")',
        ]
        for selector in close_selectors:
            locator = page.locator(selector).first
            try:
                if locator.is_visible(timeout=500):
                    locator.click(force=True)
                    page.wait_for_timeout(500)
                    self._logger.info("Closed keyboard shortcuts modal.")
                    return
            except (TimeoutError, Error):
                continue

        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            self._logger.info("Closed keyboard shortcuts modal with Escape.")
        except (TimeoutError, Error):
            pass

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

    def _scroll_for_comments(self, page: Page) -> None:
        for round_number in range(1, self._config.default_scrape_scroll_rounds + 1):
            self._logger.info("Scrolling for comments, round %s", round_number)
            page.mouse.wheel(0, 2_000)
            page.wait_for_timeout(int(self._config.default_scroll_pause_seconds * 1_000))

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
              const selectors = [
                '[data-e2e="comment-level-1"]',
                '[data-e2e="comment-item"]',
                'div[class*="CommentItem"]',
                'li[class*="CommentItem"]'
              ];

              const elements = [];
              for (const selector of selectors) {
                for (const element of document.querySelectorAll(selector)) {
                  if (!elements.includes(element)) {
                    elements.push(element);
                  }
                }
              }

              return elements.map((element, index) => {
                const anchor = element.querySelector('a[href*="/@"]');
                const possibleTextNode =
                  element.querySelector('[data-e2e="comment-level-1"] span') ||
                  element.querySelector('[data-e2e="comment-item"] span') ||
                  element.querySelector('p') ||
                  element.querySelector('span');

                const username = anchor ? anchor.textContent.trim().replace(/^@/, '') : 'unknown';
                const displayName = username;
                const text = possibleTextNode ? possibleTextNode.textContent.trim() : '';
                const likeNode = Array.from(element.querySelectorAll('span, strong')).find(node => /^\\d+$/.test(node.textContent.trim()));
                const likes = likeNode ? likeNode.textContent.trim() : '';

                return {
                  comment_id: element.getAttribute('data-comment-id') || `${username}:${index}:${text}`,
                  author_username: username,
                  author_display_name: displayName,
                  text,
                  likes,
                  published_at: ''
                };
              }).filter(item => item.text);
            }
            """
        )

        comments: list[ScrapedComment] = []
        for row in rows:
            likes = int(row["likes"]) if str(row.get("likes") or "").isdigit() else None
            comments.append(
                ScrapedComment(
                    comment_id=str(row["comment_id"]),
                    author_username=str(row["author_username"]),
                    author_display_name=str(row["author_display_name"]),
                    text=str(row["text"]),
                    likes=likes,
                    published_at=str(row.get("published_at") or "") or None,
                )
            )
        return comments

    def _send_single_comment(self, page: Page, outgoing_comment: OutgoingComment) -> SendResult:
        self._logger.info("Sending comment #%s to %s", outgoing_comment.order, outgoing_comment.video_url)
        self._prepare_comment_panel(page)
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
        return SendResult(
            outgoing_comment=outgoing_comment,
            success=success,
            details=details,
        )

    def _find_comment_input(self, page: Page) -> Locator | None:
        selectors = [
            '[data-e2e="comment-input"] div[contenteditable="true"][role="textbox"]',
            'div[contenteditable="true"][role="textbox"]',
            'div[contenteditable="true"][data-e2e*="comment"]',
            'textarea[data-e2e="comment-input"]',
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

    def _fill_comment_input(self, page: Page, input_locator: Locator, text: str) -> None:
        self._wait_for_verification_if_needed(page)
        input_locator = self._resolve_comment_input(input_locator)
        content_editable = input_locator.get_attribute("contenteditable")
        try:
            input_locator.click()
        except (TimeoutError, Error) as error:
            self._close_shortcuts_modal(page)
            self._wait_for_verification_if_needed(page)
            try:
                input_locator.click(timeout=3_000)
            except (TimeoutError, Error):
                raise TikTokClientError(f"Unable to focus the comment input: {error}") from error

        if content_editable == "true":
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.keyboard.type(text)
        else:
            input_locator.fill(text)

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
