from __future__ import annotations

import json
import base64
import hashlib
import hmac
import re
import struct
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

        auto_login_used = self._try_auto_login_with_2fa_bundle(page)
        if auto_login_used:
            page.wait_for_timeout(2_000)
            self._dismiss_overlays(page)
            self._wait_for_verification_if_needed(page)
            if not self._is_login_required(page):
                self._persist_storage_state(page.context)
                if return_url:
                    page = self._goto_with_retry(page, return_url, wait_until="domcontentloaded")
                    page.wait_for_timeout(3_000)
                return page

        for attempt in range(1, 4):
            print()
            print("The TikTok session is inactive or expired.")
            print("Sign in to TikTok manually in the already opened browser profile.")
            if attempt > 1:
                print(
                    "TikTok still shows login required. "
                    "Finish captcha/2FA and press Enter to re-check."
                )
            input("Press Enter here after the login succeeds to continue... ")

            page.wait_for_timeout(2_000)
            self._dismiss_overlays(page)
            self._wait_for_verification_if_needed(page)
            if not self._is_login_required(page):
                break

            if attempt == 3:
                raise TikTokLoginRequiredError(
                    "TikTok still requires login after multiple manual sign-in checks."
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

    def _try_auto_login_with_2fa_bundle(self, page: Page) -> bool:
        username = (self._account.login_username or "").strip()
        password = (self._account.login_password or "").strip()
        totp_secret = (self._account.login_totp_secret or "").strip()
        if not username or not password or not totp_secret:
            return False

        self._logger.info("Attempting auto-login via saved username/password/2FA secret.")
        if not self._fill_first_visible(
            page,
            [
                'input[name="username"]',
                'input[name="email"]',
                'input[autocomplete="username"]',
                'input[type="text"]',
            ],
            username,
        ):
            return False

        if not self._fill_first_visible(
            page,
            [
                'input[name="password"]',
                'input[autocomplete="current-password"]',
                'input[type="password"]',
            ],
            password,
        ):
            return False

        if not self._click_first_visible(
            page,
            [
                'button[type="submit"]',
                'button:has-text("Log in")',
                'button:has-text("Continue")',
            ],
        ):
            return False

        page.wait_for_timeout(2_500)
        code = self._generate_totp_code(totp_secret)
        if code and self._fill_first_visible(
            page,
            [
                'input[autocomplete="one-time-code"]',
                'input[name*="code" i]',
                'input[type="tel"]',
            ],
            code,
        ):
            self._click_first_visible(
                page,
                [
                    'button[type="submit"]',
                    'button:has-text("Verify")',
                    'button:has-text("Continue")',
                ],
            )
        return True

    def _fill_first_visible(self, page: Page, selectors: list[str], value: str) -> bool:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() == 0 or not locator.is_visible(timeout=800):
                    continue
                locator.fill(value, timeout=2_000)
                return True
            except (TimeoutError, Error):
                continue
        return False

    def _click_first_visible(self, page: Page, selectors: list[str]) -> bool:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() == 0 or not locator.is_visible(timeout=800):
                    continue
                locator.click(timeout=2_000)
                return True
            except (TimeoutError, Error):
                continue
        return False

    @staticmethod
    def _generate_totp_code(secret: str) -> str | None:
        normalized = str(secret or "").strip().replace(" ", "").upper()
        if not normalized:
            return None
        padding = "=" * ((8 - len(normalized) % 8) % 8)
        try:
            key = base64.b32decode(normalized + padding, casefold=True)
        except Exception:
            return None

        timestep = int(time.time() // 30)
        counter = struct.pack(">Q", timestep)
        digest = hmac.new(key, counter, hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
        return str(binary % 1_000_000).zfill(6)

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
