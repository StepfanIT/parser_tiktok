from __future__ import annotations

import logging
import random
import time
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from app.config import AppConfig
from app.integrations.tiktok_client import TikTokClientError, TikTokPlaywrightClient
from app.logging_config import get_account_logger
from app.models import (
    AccountHealthCheckResult,
    OutgoingComment,
    RunAccountSummary,
    ScrapedComment,
    SendResult,
    TikTokAccountConfig,
)
from app.repositories.account_repository import AccountRepository
from app.repositories.csv_repository import CSVRepository
from app.repositories.report_repository import ReportRepository
from app.services.health_check_service import AccountHealthService
from app.services.send_policy import SendExecutionPolicy


class TikTokCommentService:
    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        account_repository: AccountRepository,
        csv_repository: CSVRepository,
        report_repository: ReportRepository | None = None,
        health_service: AccountHealthService | None = None,
        send_policy: SendExecutionPolicy | None = None,
    ) -> None:
        self._config = config
        self._logger = logger
        self._account_repository = account_repository
        self._csv_repository = csv_repository
        self._report_repository = report_repository or ReportRepository(config)
        self._health_service = health_service or AccountHealthService(
            config=config,
            logger=logger,
            account_repository=account_repository,
        )
        self._send_policy = send_policy or SendExecutionPolicy(config)

    def list_available_account_paths(self) -> list[Path]:
        return self._account_repository.list_account_paths()

    def suggest_account_name(self, slot_index: int) -> str:
        return self._account_repository.suggest_account_name(slot_index)

    def create_account_config(
        self,
        *,
        account_name: str,
        provider_name: str = "playwright_local",
        profile_id: str | None = None,
        api_url: str | None = None,
        api_token: str | None = None,
        api_key: str | None = None,
    ) -> Path:
        return self._account_repository.create_account_config(
            account_name=account_name,
            provider_name=provider_name,
            profile_id=profile_id,
            api_url=api_url,
            api_token=api_token,
            api_key=api_key,
        )

    def run_health_check(
        self,
        *,
        account_paths: list[Path] | None = None,
    ) -> tuple[list[AccountHealthCheckResult], Path]:
        _accounts, results = self._health_service.check_accounts(account_paths)
        summaries = self._build_run_summaries(results)
        report_path = self._write_run_report(
            action="health_check",
            summaries=summaries.values(),
            notes=("Health-check command executed from the CLI.",),
        )
        return results, report_path

    def collect_comments(
        self,
        video_url: str,
        *,
        account_paths: list[Path] | None = None,
        output_path: Path | None = None,
    ) -> Path:
        accounts, health_results = self._health_service.check_accounts(account_paths)
        summaries = self._build_run_summaries(health_results)
        self._ensure_any_healthy_accounts(accounts)
        self._logger.info("All selected accounts are ready. Starting comment collection.")
        merged_comments: dict[str, ScrapedComment] = {}
        report_notes = [f"Collection target: {video_url}"]

        try:
            for index, account in enumerate(accounts, start=1):
                account_logger = get_account_logger(self._logger, account.name)
                account_logger.info(
                    "Starting collection pass %s/%s.",
                    index,
                    len(accounts),
                )
                client = TikTokPlaywrightClient(self._config, account_logger, account)
                comments = client.scrape_comments(video_url)
                account_logger.info(
                    "Collected %s comments without a reply from this account.",
                    len(comments),
                )

                summaries[account.name] = replace(
                    summaries[account.name],
                    collected_comments=len(comments),
                )

                for comment in comments:
                    existing = merged_comments.get(comment.comment_id)
                    if existing is None:
                        merged_comments[comment.comment_id] = comment
                        continue

                    merged_comments[comment.comment_id] = self._merge_scraped_comments(existing, comment)

            comments_to_export = list(merged_comments.values())
            comments_to_export.sort(key=lambda item: item.published_at or "", reverse=True)
            if not comments_to_export:
                raise TikTokClientError(
                    "No eligible comments were collected from the selected accounts."
                )

            export_path = self._csv_repository.export_scraped_comments(comments_to_export, output_path)
            report_notes.append(f"Exported CSV: {export_path}")
            self._logger.info(
                "Collection finished. Exported %s rows to %s.",
                len(comments_to_export),
                export_path,
            )
            return export_path
        finally:
            self._write_run_report(
                action="collect_comments",
                summaries=summaries.values(),
                notes=report_notes,
            )

    def send_comments(
        self,
        *,
        account_paths: list[Path] | None = None,
        csv_path: Path | None = None,
    ) -> list[SendResult]:
        accounts, health_results = self._health_service.check_accounts(account_paths)
        summaries = self._build_run_summaries(health_results)
        self._ensure_any_healthy_accounts(accounts)
        self._logger.info("All selected accounts are ready. Starting comment sending.")
        pending_comments = self._csv_repository.load_outgoing_comments(csv_path)
        if not pending_comments:
            raise ValueError("The outgoing CSV does not contain any comments.")

        self._validate_comment_account_restrictions(pending_comments, accounts)
        rng = random.Random()
        pending_pool = pending_comments.copy()
        rng.shuffle(pending_pool)
        report_notes = [f"Source CSV: {csv_path or self._config.default_outgoing_comments_csv}"]

        states = self._send_policy.build_states(accounts, rng)

        for state in states:
            account_logger = get_account_logger(self._logger, state.account.name)
            account_logger.info(
                "Daily limit=%s, hourly limit=%s.",
                state.daily_limit,
                state.hourly_limit,
            )

        results: list[SendResult] = []
        try:
            while pending_pool:
                self._send_policy.refresh_state_windows(states)
                eligible_states = self._send_policy.select_eligible_states(states, pending_pool)

                if not eligible_states:
                    wait_seconds = self._send_policy.seconds_until_next_available_slot(states)
                    if wait_seconds is None:
                        raise TikTokClientError(
                            "All selected accounts reached their safe send window or do not match the remaining rows."
                        )

                    self._logger.info(
                        "All accounts are cooling down. Waiting %s seconds before the next scheduling pass.",
                        wait_seconds,
                    )
                    time.sleep(wait_seconds)
                    continue

                state = rng.choice(eligible_states)
                batch = self._send_policy.take_batch_for_account(pending_pool, state, rng)
                account_logger = get_account_logger(self._logger, state.account.name)
                account_logger.info(
                    "Starting batch %s with %s comments. Queue remaining after selection: %s.",
                    state.batch_index + 1,
                    len(batch),
                    len(pending_pool),
                )

                client = TikTokPlaywrightClient(self._config, account_logger, state.account)
                batch_results = client.send_comments(batch)
                results.extend(batch_results)

                successful_count = sum(1 for result in batch_results if result.success)
                attempts_count = len(batch_results)
                state.sent_today += attempts_count
                state.sent_this_hour += attempts_count
                state.batch_index += 1

                summaries[state.account.name] = replace(
                    summaries[state.account.name],
                    attempted_comments=summaries[state.account.name].attempted_comments + attempts_count,
                    successful_comments=summaries[state.account.name].successful_comments + successful_count,
                    failed_comments=summaries[state.account.name].failed_comments + (attempts_count - successful_count),
                    statuses=summaries[state.account.name].statuses
                    + tuple(result.status for result in batch_results),
                )

                account_logger.info(
                    "Finished batch %s. Successful sends: %s/%s.",
                    state.batch_index,
                    successful_count,
                    attempts_count,
                )

                pause_seconds = self._send_policy.schedule_next_cooldown(
                    state,
                    has_pending_comments=bool(pending_pool),
                    rng=rng,
                )
                if pause_seconds is not None:
                    account_logger.info("Next batch cooldown: %s seconds.", pause_seconds)

            self._logger.info(
                "Sending finished. Successful comments: %s/%s.",
                sum(1 for result in results if result.success),
                len(results),
            )
            return results
        finally:
            self._write_run_report(
                action="send_comments",
                summaries=summaries.values(),
                notes=report_notes,
            )

    def _merge_scraped_comments(self, left: ScrapedComment, right: ScrapedComment) -> ScrapedComment:
        eligible_accounts = tuple(
            sorted(set(left.eligible_account_names) | set(right.eligible_account_names))
        )
        reply_authors = tuple(
            sorted(set(left.reply_author_usernames) | set(right.reply_author_usernames))
        )
        published_at = left.published_at or right.published_at
        likes = left.likes if left.likes is not None else right.likes
        return replace(
            left,
            eligible_account_names=eligible_accounts,
            reply_author_usernames=reply_authors,
            published_at=published_at,
            likes=likes,
        )

    def _validate_comment_account_restrictions(
        self,
        comments: list[OutgoingComment],
        accounts: list[TikTokAccountConfig],
    ) -> None:
        available_account_names = {account.name for account in accounts}
        for comment in comments:
            if not comment.allowed_account_names:
                continue

            if available_account_names.intersection(comment.allowed_account_names):
                continue

            allowed = ", ".join(comment.allowed_account_names)
            raise ValueError(
                f"Comment #{comment.order} requires one of these accounts: {allowed}, "
                "but none of them were selected for this run."
            )

    def _ensure_any_healthy_accounts(self, accounts: list[TikTokAccountConfig]) -> None:
        if accounts:
            return
        raise TikTokClientError("No accounts passed the health check for this run.")

    def _build_run_summaries(
        self,
        health_results: list[AccountHealthCheckResult],
    ) -> dict[str, RunAccountSummary]:
        summaries: dict[str, RunAccountSummary] = {}
        for result in health_results:
            summaries[result.account_name] = RunAccountSummary(
                account_name=result.account_name,
                provider_name=result.provider_name,
                health_status="passed" if result.success else "failed",
                notes=(result.details,),
            )
        return summaries

    def _write_run_report(
        self,
        *,
        action: str,
        summaries: Iterable[RunAccountSummary],
        notes: tuple[str, ...] | list[str],
    ) -> Path:
        report_path = self._report_repository.write_run_report(
            action=action,
            summaries=summaries,
            notes=notes,
        )
        self._logger.info("Run report saved to %s.", report_path)
        return report_path
