from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from playwright.sync_api import Error, Locator, Page, TimeoutError

from app.integrations.tiktok_client_support.runtime import TikTokClientError, TikTokLoginRequiredError
from app.models import OutgoingComment, PublishOutcome, ScrapedComment, SendResult


class TikTokPublishingMixin:
    def _send_single_comment(self, page: Page, outgoing_comment: OutgoingComment) -> tuple[Page, SendResult]:
        self._logger.info(
            "Sending comment #%s to %s.",
            outgoing_comment.order,
            outgoing_comment.video_url,
        )
        page = self._prepare_comment_panel(page, video_url=outgoing_comment.video_url, require_input=True)
        if outgoing_comment.target_username:
            page = self._activate_reply_mode(page, outgoing_comment.target_username)
        input_locator = self._find_comment_input(page)
        if input_locator is None:
            raise TikTokLoginRequiredError(
                "The comment input was not found. Refresh the session or reopen the comment panel."
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
            outcome = self._describe_response(response)
        except TimeoutError:
            self._logger.warning(
                "Timed out while waiting for the publish response for comment #%s.",
                outgoing_comment.order,
            )
            page.wait_for_timeout(2_500)
            outcome = PublishOutcome(
                success=False,
                status="publish_timeout",
                details="Publish response was not captured after clicking the send button.",
            )

        self._logger.info(
            "Comment #%s finished with status=%s. %s",
            outgoing_comment.order,
            outcome.status,
            outcome.details,
        )
        return page, SendResult(
            account_name=self._account.name,
            outgoing_comment=outgoing_comment,
            success=outcome.success,
            details=outcome.details,
            status=outcome.status,
        )

    def _activate_reply_mode(self, page: Page, target_username: str) -> Page:
        normalized_target = self._normalize_username(target_username)
        if not normalized_target:
            return page

        self._logger.info("Looking for @%s to open reply mode.", normalized_target)
        max_attempts = max(self._config.default_scrape_scroll_rounds, 3)
        for attempt in range(1, max_attempts + 1):
            self._wait_for_verification_if_needed(page)
            if self._click_reply_trigger_for_username(page, normalized_target):
                self._logger.info("Reply mode opened for @%s.", normalized_target)
                page.wait_for_timeout(800)
                return page

            self._logger.info(
                "Reply target @%s was not visible yet. Scroll pass %s/%s.",
                normalized_target,
                attempt,
                max_attempts,
            )
            self._scroll_comment_surface_for_reply(page, 1_500)
            page.wait_for_timeout(int(self._config.default_scroll_pause_seconds * 1_000))
            self._wait_for_comment_content(page)

        raise TikTokClientError(f"Could not find a comment from @{normalized_target} to reply to.")

    def _click_reply_trigger_for_username(self, page: Page, target_username: str) -> bool:
        try:
            return bool(
                page.evaluate(
                    """
                    (targetUsername) => {
                      const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().replace(/^@/, '').toLowerCase();
                      const rowSelectors = [
                        '[data-e2e="comment-level-1"]',
                        '[data-e2e="comment-item"]',
                        'div[class*="CommentItem"]',
                        'li[class*="CommentItem"]'
                      ];

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

                      const extractUsername = (node) => {
                        const href = node ? (node.getAttribute('href') || '') : '';
                        const hrefMatch = href.match(/\\/@([^/?]+)/);
                        if (hrefMatch) {
                          return normalize(hrefMatch[1]);
                        }
                        return normalize(node ? (node.innerText || node.textContent || '') : '');
                      };

                      const rows = [];
                      const seen = new Set();
                      for (const selector of rowSelectors) {
                        for (const row of document.querySelectorAll(selector)) {
                          if (!seen.has(row)) {
                            seen.add(row);
                            rows.push(row);
                          }
                        }
                      }

                      for (const row of rows) {
                        const authorAnchor = row.querySelector('a[href*="/@"]');
                        if (!authorAnchor) {
                          continue;
                        }

                        const authorUsername = extractUsername(authorAnchor);
                        if (authorUsername !== targetUsername) {
                          continue;
                        }

                        const candidates = Array.from(row.querySelectorAll('button, [role="button"], a, span, div'));
                        for (const candidate of candidates) {
                          if (!isVisible(candidate)) {
                            continue;
                          }

                          const text = normalize(candidate.innerText || candidate.textContent || '');
                          const aria = normalize(candidate.getAttribute('aria-label'));
                          const dataE2e = normalize(candidate.getAttribute('data-e2e'));
                          const combined = `${text} ${aria} ${dataE2e}`;
                          if (!combined.includes('reply')) {
                            continue;
                          }

                          candidate.click();
                          return true;
                        }
                      }

                      return false;
                    }
                    """,
                    target_username,
                )
            )
        except Error:
            return False

    def _scroll_comment_surface_for_reply(self, page: Page, delta: int) -> None:
        try:
            page.evaluate(
                """
                (scrollDelta) => {
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
                      node.scrollBy(0, scrollDelta);
                      return true;
                    }
                  }

                  window.scrollBy(0, scrollDelta);
                  return false;
                }
                """,
                delta,
            )
        except Error:
            page.mouse.wheel(0, delta)

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
            self._logger.warning("Saved comment surface debug dump to %s.", debug_path)
        except Exception as error:
            self._logger.warning("Could not write the comment surface debug dump: %s", error)

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
                raise TikTokClientError(f"Failed to fill the comment input: {error}") from error

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

            self._logger.debug("Could not move focus to the comment input via %s.", description)

        if last_error is not None:
            raise TikTokClientError(f"Failed to focus the comment input: {last_error}") from last_error
        raise TikTokClientError("Failed to focus the comment input.")

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

    def _apply_account_reply_flag(self, comment: ScrapedComment) -> ScrapedComment:
        username = self._active_account_username
        has_account_reply = False
        if username:
            author_username = self._normalize_username(comment.author_username)
            reply_usernames = {
                normalized
                for normalized in (
                    self._normalize_username(item) for item in comment.reply_author_usernames
                )
                if normalized
            }
            has_account_reply = author_username == username or username in reply_usernames

        eligible_account_names = () if has_account_reply else (self._account.name,)
        return replace(
            comment,
            has_account_reply=has_account_reply,
            eligible_account_names=eligible_account_names,
        )

    def _extract_reply_usernames_from_payload(self, payload: Any) -> tuple[str, ...]:
        results: set[str] = set()
        reply_keys = {
            "reply_comment",
            "reply_comments",
            "replycomment",
            "replycomments",
            "reply_comment_list",
            "replycommentlist",
            "reply_list",
            "replies",
        }

        def walk(node: Any, *, inside_reply: bool = False) -> None:
            if isinstance(node, dict):
                next_inside_reply = inside_reply
                for key, value in node.items():
                    normalized_key = key.lower()
                    if normalized_key in reply_keys:
                        next_inside_reply = True
                        walk(value, inside_reply=True)
                        continue

                    if inside_reply and normalized_key in {"user", "user_info", "author"}:
                        username = self._extract_username_from_user_payload(value)
                        if username:
                            results.add(username)

                    walk(value, inside_reply=next_inside_reply)
                return

            if isinstance(node, list):
                for item in node:
                    walk(item, inside_reply=inside_reply)

        walk(payload)
        return tuple(sorted(results))

    def _extract_username_from_user_payload(self, payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None

        username = self._first_non_empty(
            payload.get("unique_id"),
            payload.get("uniqueId"),
            payload.get("username"),
            payload.get("user_name"),
        )
        return self._normalize_username(username)

    @staticmethod
    def _extract_username_from_href(href: str | None) -> str | None:
        value = href or ""
        match = re.search(r"/@([^/?]+)", value)
        if not match:
            return None
        return match.group(1).strip().lstrip("@").lower() or None

    @staticmethod
    def _normalize_username(value: Any) -> str | None:
        if value in (None, ""):
            return None
        normalized = str(value).strip().lstrip("@").lower()
        return normalized or None

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

    def _describe_response(self, response: Any) -> PublishOutcome:
        try:
            payload = response.json()
        except (Error, json.JSONDecodeError):
            success = bool(response.ok)
            status = "posted" if success else "http_error"
            return PublishOutcome(success=success, status=status, details=f"HTTP {response.status}")

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
        normalized_message = str(message or "").strip().lower()
        normalized_status = str(status_code).strip().lower()
        success = bool(response.ok) and normalized_status in {"0", "ok", "success", "200"}
        status = "posted"

        if any(keyword in normalized_message for keyword in ("captcha", "verify", "verification", "security check")):
            success = False
            status = "verification_required"
        elif any(keyword in normalized_message for keyword in ("muted", "banned", "suspended", "blocked")):
            success = False
            status = "blocked"
        elif any(keyword in normalized_message for keyword in ("review", "under review", "processing")):
            success = False
            status = "under_review"
        elif any(keyword in normalized_message for keyword in ("spam", "too fast", "limit", "rate")):
            success = False
            status = "rate_limited"
        elif not success:
            status = "rejected"

        return PublishOutcome(
            success=success,
            status=status,
            details=f"status={status_code}, message={message or 'ok'}",
        )

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
