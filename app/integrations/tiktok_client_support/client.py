from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any

from playwright.sync_api import BrowserContext, Error, Page, TimeoutError, sync_playwright

from app.config import AppConfig
from app.integrations.browser_providers import BrowserProviderError, build_provider_client
from app.integrations.tiktok_client_support.interaction import TikTokInteractionMixin
from app.integrations.tiktok_client_support.publishing import TikTokPublishingMixin
from app.integrations.tiktok_client_support.runtime import ManagedBrowserSession, TikTokClientError
from app.integrations.tiktok_client_support.scraping import TikTokScrapingMixin
from app.integrations.tiktok_client_support.session import TikTokSessionMixin
from app.models import AccountHealthCheckResult, OutgoingComment, ScrapedComment, SendResult, TikTokAccountConfig


class TikTokPlaywrightClient(
    TikTokSessionMixin,
    TikTokInteractionMixin,
    TikTokScrapingMixin,
    TikTokPublishingMixin,
):
    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        account: TikTokAccountConfig,
    ) -> None:
        self._config = config
        self._logger = logger
        self._account = account
        self._active_video_url = ""
        self._active_account_username = self._normalize_username(account.tiktok_username)

    def health_check(self) -> AccountHealthCheckResult:
        provider_name = self._account.browser_provider.name
        api_url = self._account.browser_provider.api_url
        notes: list[str] = []
        try:
            provider_client = build_provider_client(self._account.browser_provider, self._logger)
            if provider_client is None:
                notes.append("Using the built-in Playwright persistent profile flow.")
            else:
                api_url = provider_client.api_url
                notes.append(provider_client.health_check())

            username = self.ensure_session_ready()
            if username:
                notes.append(f"TikTok session is ready as @{username}.")
            else:
                notes.append("TikTok session is ready, but the username could not be resolved.")
            return AccountHealthCheckResult(
                account_name=self._account.name,
                provider_name=provider_name,
                success=True,
                details=" ".join(notes),
                resolved_username=username,
                api_url=api_url,
            )
        except Exception as error:
            return AccountHealthCheckResult(
                account_name=self._account.name,
                provider_name=provider_name,
                success=False,
                details=str(error),
                api_url=api_url,
            )

    def ensure_session_ready(self) -> str | None:
        self._logger.info("Opening account session.")
        with sync_playwright() as playwright:
            session = self._open_session(playwright)
            page = self._create_working_page(session)
            try:
                try:
                    page = self._goto_with_retry(page, self._account.login_url, wait_until="domcontentloaded")
                except (TimeoutError, Error) as error:
                    raise TikTokClientError(f"Failed to open the TikTok login page: {error}") from error

                page.wait_for_timeout(2_000)
                self._dismiss_overlays(page)
                page = self._ensure_logged_in(page)
                username = self._resolve_account_username(page)
                if username:
                    self._logger.info("Session ready as @%s.", username)
                else:
                    self._logger.info("Session ready.")
                return username
            finally:
                self._close_session(session)

    def scrape_comments(self, video_url: str) -> list[ScrapedComment]:
        self._logger.info("Starting comment collection for %s.", video_url)
        self._active_video_url = video_url
        with sync_playwright() as playwright:
            session = self._open_session(playwright)
            page = self._create_working_page(session)
            collected: dict[str, ScrapedComment] = {}

            def handle_response(response: Any) -> None:
                self._collect_from_response(response, collected)

            page.on("response", handle_response)
            try:
                page = self._open_video_page(page, video_url, require_login=True)
                self._active_account_username = self._resolve_account_username(page)
                self._wait_for_comment_content(page)
                initial_dom_count = self._collect_comments_from_dom(page, collected)
                if initial_dom_count:
                    self._logger.info("Initial DOM pass added %s comments.", initial_dom_count)

                page = self._scroll_for_comments(page, collected)
                final_dom_count = self._collect_comments_from_dom(page, collected)
                if final_dom_count:
                    self._logger.info("Final DOM pass added %s more comments.", final_dom_count)

                if not collected:
                    self._logger.info("No comments were returned by either network or DOM collection.")

                comments = [self._apply_account_reply_flag(comment) for comment in collected.values()]
                unreplied_comments = [comment for comment in comments if not comment.has_account_reply]
                self._logger.info(
                    "Collected %s comments total. Eligible without this account's reply: %s.",
                    len(comments),
                    len(unreplied_comments),
                )
                return unreplied_comments
            finally:
                self._close_session(session)

    def send_comments(self, comments: Iterable[OutgoingComment]) -> list[SendResult]:
        comment_batch = list(comments)
        if not comment_batch:
            raise ValueError("The outgoing comment batch is empty.")

        self._logger.info("Starting send flow for %s comments.", len(comment_batch))
        with sync_playwright() as playwright:
            session = self._open_session(playwright)
            page = self._create_working_page(session)
            current_video_url: str | None = None
            current_target_username: str | None = None
            results: list[SendResult] = []

            try:
                for item in comment_batch:
                    if item.delay_seconds > 0:
                        self._logger.info(
                            "Waiting %s seconds before comment #%s.",
                            item.delay_seconds,
                            item.order,
                        )
                        time.sleep(item.delay_seconds)

                    normalized_target_username = self._normalize_username(item.target_username)
                    if (
                        current_video_url != item.video_url
                        or current_target_username != normalized_target_username
                    ):
                        page = self._open_video_page(page, item.video_url, require_login=True)
                        page = self._prepare_comment_panel(page, video_url=item.video_url, require_input=True)
                        current_video_url = item.video_url
                        current_target_username = normalized_target_username

                    page, result = self._send_single_comment(page, item)
                    results.append(result)

                return results
            finally:
                self._close_session(session)

    def _open_session(self, playwright: Any) -> ManagedBrowserSession:
        provider_client = build_provider_client(self._account.browser_provider, self._logger)
        if provider_client is None:
            return self._launch_local_context(playwright)
        return self._launch_provider_context(playwright, provider_client)

    def _launch_local_context(self, playwright: Any) -> ManagedBrowserSession:
        browser_type = getattr(playwright, self._account.browser_type, None)
        if browser_type is None:
            raise TikTokClientError(
                f"Unsupported browser_type in the account config: {self._account.browser_type}."
            )

        self._account.user_data_dir.mkdir(parents=True, exist_ok=True)
        launch_kwargs: dict[str, Any] = {
            "headless": self._account.headless,
            "slow_mo": self._account.slow_mo_ms,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if self._account.browser_channel:
            launch_kwargs["channel"] = self._account.browser_channel

        self._logger.info(
            "Launching %s with persistent profile %s.",
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
        return ManagedBrowserSession(context=context, persist_storage_state=True)

    def _launch_provider_context(self, playwright: Any, provider_client: Any) -> ManagedBrowserSession:
        self._logger.info(
            "Launching provider-backed browser via %s for profile %s.",
            self._account.browser_provider.name,
            self._account.browser_provider.profile_id,
        )
        try:
            launch_result = provider_client.launch_profile()
        except BrowserProviderError as error:
            raise TikTokClientError(str(error)) from error

        browser = playwright.chromium.connect_over_cdp(launch_result.cdp_endpoint)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        context.set_default_timeout(self._config.browser_action_timeout_ms)
        context.set_default_navigation_timeout(self._config.navigation_timeout_ms)
        return ManagedBrowserSession(
            context=context,
            browser=browser,
            close_callback=provider_client.stop_profile,
            persist_storage_state=False,
            reuse_existing_page=True,
        )

    def _create_working_page(self, session: ManagedBrowserSession) -> Page:
        if session.reuse_existing_page and session.context.pages:
            return session.context.pages[0]
        return self._create_working_page_from_context(session.context)

    @staticmethod
    def _create_working_page_from_context(context: BrowserContext) -> Page:
        return context.new_page()

    def _close_session(self, session: ManagedBrowserSession) -> None:
        try:
            if session.persist_storage_state:
                self._persist_storage_state(session.context)
        finally:
            try:
                if session.browser is not None:
                    session.browser.close()
                else:
                    session.context.close()
            except Error:
                pass

            if session.close_callback is not None:
                try:
                    session.close_callback()
                except Exception as error:
                    self._logger.warning("Failed to stop the provider-backed browser profile cleanly: %s", error)
