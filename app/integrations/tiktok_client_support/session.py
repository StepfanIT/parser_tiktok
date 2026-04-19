from __future__ import annotations

import json
import re
import time
from typing import Any

from playwright.sync_api import BrowserContext, Error, Page, TimeoutError

from app.integrations.tiktok_client_support.runtime import TikTokClientError, TikTokLoginRequiredError


class TikTokSessionMixin:
    def _restore_storage_state_backup(self, context: BrowserContext) -> None:
        self._logger.info("Restoring storage state from %s.", self._account.storage_state_path)
        try:
            payload = json.loads(self._account.storage_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            self._logger.warning("Could not read the backup storage state: %s", error)
            return

        cookies = payload.get("cookies") or []
        if cookies:
            try:
                context.add_cookies(cookies)
            except Error as error:
                self._logger.warning("Could not restore TikTok cookies from storage state: %s", error)

        origins = payload.get("origins") or []
        if not origins:
            return

        page = self._create_working_page_from_context(context)
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
                    self._logger.warning("Could not restore localStorage for %s: %s", origin_url, error)
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
            self._logger.warning("Could not save the backup storage state: %s", error)

    def _open_video_page(self, page: Page, video_url: str, *, require_login: bool) -> Page:
        self._logger.info("Opening video page %s.", video_url)
        try:
            page = self._goto_with_retry(page, video_url, wait_until="domcontentloaded")
        except (TimeoutError, Error) as error:
            raise TikTokClientError(f"Failed to open the TikTok video page: {error}") from error

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
                "TikTok requested a login, but the browser is running in headless mode. "
                "Disable headless mode and sign in manually once."
            )

        if not self._account.bootstrap_login_if_missing:
            raise TikTokLoginRequiredError(
                "The TikTok session is inactive. Enable bootstrap_login_if_missing or refresh the profile."
            )

        self._logger.info("The TikTok session is inactive. Opening manual login flow.")
        try:
            page = self._goto_with_retry(page, self._account.login_url, wait_until="domcontentloaded")
        except (TimeoutError, Error) as error:
            raise TikTokClientError(f"Failed to open the TikTok login page: {error}") from error
        print()
        print("The TikTok session is inactive or expired.")
        print("Sign in to TikTok manually in the already opened browser profile.")
        input("Press Enter here after the login succeeds to continue... ")

        page.wait_for_timeout(1_500)
        self._dismiss_overlays(page)
        self._wait_for_verification_if_needed(page)
        if self._is_login_required(page):
            raise TikTokLoginRequiredError(
                "TikTok still requires login after the manual sign-in step."
            )

        self._persist_storage_state(page.context)
        if return_url:
            try:
                page = self._goto_with_retry(page, return_url, wait_until="domcontentloaded")
            except (TimeoutError, Error) as error:
                raise TikTokClientError(
                    f"Failed to return to the video page after login: {error}"
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
                    "Timed out while opening %s, attempt %s/%s with wait_until=%s. Retrying.",
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
                    "Navigation to %s was aborted on attempt %s/%s with wait_until=%s. Retrying on a fresh tab.",
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
        return self._create_working_page_from_context(context)

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

    def _resolve_account_username(self, page: Page) -> str | None:
        if self._account.tiktok_username:
            return self._normalize_username(self._account.tiktok_username)

        candidates = [
            page.locator('a[data-e2e="nav-profile"]').first,
            page.locator('a[href^="/@"][data-e2e*="profile"]').first,
            page.locator('a[href^="/@"][aria-label*="Profile" i]').first,
        ]
        for locator in candidates:
            try:
                if locator.count() == 0:
                    continue
                href = locator.get_attribute("href") or ""
                username = self._extract_username_from_href(href)
                if username:
                    return username
            except (TimeoutError, Error):
                continue

        try:
            username = page.evaluate(
                """
                () => {
                  const links = Array.from(document.querySelectorAll('a[href^="/@"]'));
                  const score = (element) => {
                    const dataE2e = (element.getAttribute('data-e2e') || '').toLowerCase();
                    const aria = (element.getAttribute('aria-label') || '').toLowerCase();
                    const text = (element.innerText || element.textContent || '').toLowerCase();
                    let value = 0;
                    if (dataE2e.includes('profile')) value += 100;
                    if (aria.includes('profile')) value += 80;
                    if (text.includes('profile')) value += 40;
                    if (element.closest('nav, header, aside')) value += 20;
                    return value;
                  };

                  links.sort((left, right) => score(right) - score(left));
                  const target = links[0];
                  if (!target) {
                    return null;
                  }

                  const href = target.getAttribute('href') || '';
                  const match = href.match(new RegExp('/@([^/?]+)'));
                  return match ? match[1] : null;
                }
                """
            )
            return self._normalize_username(username)
        except Error:
            return None
