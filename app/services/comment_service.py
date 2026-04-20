from __future__ import annotations

import logging
import random
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from app.config import AppConfig
from app.integrations.tiktok_client import TikTokClientError, TikTokPlaywrightClient
from app.logging_config import get_account_logger
from app.models import (
    AccountSendState,
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

    def resolve_account_identifier(self, identifier: str) -> Path | None:
        return self._account_repository.resolve_account_identifier(identifier)

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

    def ensure_account_session(self, *, account_path: Path) -> AccountHealthCheckResult:
        _accounts, results = self._health_service.check_accounts([account_path])
        if not results:
            raise TikTokClientError(f"Could not validate account config: {account_path}")
        return results[0]

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
        return self.collect_comments_for_videos(
            video_urls=[video_url],
            account_paths=account_paths,
            output_path=output_path,
        )

    def collect_comments_for_videos(
        self,
        *,
        video_urls: list[str],
        account_paths: list[Path] | None = None,
        output_path: Path | None = None,
    ) -> Path:
        normalized_urls = [url.strip() for url in video_urls if str(url).strip()]
        if not normalized_urls:
            raise ValueError("At least one TikTok video URL is required for comment collection.")

        accounts, health_results = self._health_service.check_accounts(account_paths)
        summaries = self._build_run_summaries(health_results)
        self._ensure_any_healthy_accounts(accounts)
        self._logger.info("All selected accounts are ready. Starting comment collection.")
        merged_comments: dict[str, ScrapedComment] = {}
        report_notes = [f"Collection targets: {', '.join(normalized_urls)}"]

        try:
            for account_index, account in enumerate(accounts, start=1):
                account_logger = get_account_logger(self._logger, account.name)
                client = TikTokPlaywrightClient(self._config, account_logger, account)
                account_total = 0
                for video_index, video_url in enumerate(normalized_urls, start=1):
                    account_logger.info(
                        "Starting collection pass account %s/%s on video %s/%s.",
                        account_index,
                        len(accounts),
                        video_index,
                        len(normalized_urls),
                    )
                    comments = client.scrape_comments(video_url)
                    account_total += len(comments)
                    account_logger.info(
                        "Collected %s comments without a reply from this account on this video.",
                        len(comments),
                    )

                    for comment in comments:
                        existing = merged_comments.get(comment.comment_id)
                        if existing is None:
                            merged_comments[comment.comment_id] = comment
                            continue

                        merged_comments[comment.comment_id] = self._merge_scraped_comments(existing, comment)

                summaries[account.name] = replace(
                    summaries[account.name],
                    collected_comments=account_total,
                )

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
        mode: str = "distribute",
    ) -> list[SendResult]:
        accounts, health_results = self._health_service.check_accounts(account_paths)
        summaries = self._build_run_summaries(health_results)
        self._ensure_any_healthy_accounts(accounts)
        self._logger.info("All selected accounts are ready. Starting comment sending.")
        pending_comments = self._csv_repository.load_outgoing_comments(csv_path)
        if not pending_comments:
            raise ValueError("The outgoing CSV does not contain any comments.")

        pending_comments = self._normalize_comment_account_restrictions(pending_comments, accounts)
        self._validate_comment_account_restrictions(pending_comments, accounts)
        if mode == "distribute" and all(
            not comment.allowed_account_names for comment in pending_comments
        ):
            self._logger.info(
                "No account restrictions found in CSV. Switching send mode to all_accounts automatically."
            )
            mode = "all_accounts"

        if mode == "all_accounts":
            results = self._send_comments_for_all_accounts(
                accounts=accounts,
                pending_comments=pending_comments,
                summaries=summaries,
            )
            self._logger.info(
                "Sending finished (all_accounts mode). Successful comments: %s/%s.",
                sum(1 for result in results if result.success),
                len(results),
            )
            return results

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

                rng.shuffle(eligible_states)
                fair_batch_cap = max(
                    1,
                    (len(pending_pool) + len(eligible_states) - 1) // len(eligible_states),
                )
                scheduled_batches: list[tuple[AccountSendState, list[OutgoingComment]]] = []
                for state in eligible_states:
                    batch = self._send_policy.take_batch_for_account(
                        pending_pool,
                        state,
                        rng,
                        max_batch_size_cap=fair_batch_cap,
                    )
                    if not batch:
                        continue
                    account_logger = get_account_logger(self._logger, state.account.name)
                    account_logger.info(
                        "Starting batch %s with %s comments. Queue remaining after selection: %s.",
                        state.batch_index + 1,
                        len(batch),
                        len(pending_pool),
                    )
                    scheduled_batches.append((state, batch))

                if not scheduled_batches:
                    wait_seconds = self._send_policy.seconds_until_next_available_slot(states)
                    if wait_seconds is None:
                        raise TikTokClientError(
                            "No batches could be scheduled for eligible accounts."
                        )
                    self._logger.info(
                        "Eligible accounts found, but no batches were scheduled. Waiting %s seconds.",
                        wait_seconds,
                    )
                    time.sleep(wait_seconds)
                    continue

                max_workers = max(1, min(len(scheduled_batches), 6))
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_map = {
                        executor.submit(self._send_batch_for_state, state, batch): (state, batch)
                        for state, batch in scheduled_batches
                    }
                    for future in as_completed(future_map):
                        state, batch_items = future_map[future]
                        try:
                            batch_results = future.result()
                        except Exception as error:
                            account_logger = get_account_logger(self._logger, state.account.name)
                            account_logger.exception("Batch failed with an exception: %s", error)
                            batch_results = self._build_failed_send_results(
                                account_name=state.account.name,
                                comments=batch_items,
                                error=error,
                                status="batch_error",
                            )
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
                            failed_comments=summaries[state.account.name].failed_comments
                            + (attempts_count - successful_count),
                            statuses=summaries[state.account.name].statuses
                            + tuple(result.status for result in batch_results),
                        )

                        account_logger = get_account_logger(self._logger, state.account.name)
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

    def _send_batch_for_state(
        self,
        state: AccountSendState,
        batch: list[OutgoingComment],
    ) -> list[SendResult]:
        account_logger = get_account_logger(self._logger, state.account.name)
        client = TikTokPlaywrightClient(self._config, account_logger, state.account)
        return client.send_comments(batch)

    def _send_comments_for_all_accounts(
        self,
        *,
        accounts: list[TikTokAccountConfig],
        pending_comments: list[OutgoingComment],
        summaries: dict[str, RunAccountSummary],
    ) -> list[SendResult]:
        prepared_batches: list[tuple[TikTokAccountConfig, list[OutgoingComment]]] = []
        for index, account in enumerate(accounts):
            account_comments = [
                comment for comment in pending_comments if self._send_policy.comment_matches_account(comment, account)
            ]
            if not account_comments:
                continue

            rng = random.Random(f"{account.name}:{index}:{len(account_comments)}")
            rng.shuffle(account_comments)
            prepared: list[OutgoingComment] = []
            for comment in account_comments:
                prepared.append(
                    replace(
                        comment,
                        text=self._send_policy.resolve_comment_text(comment, rng),
                        delay_seconds=comment.delay_seconds
                        if comment.delay_seconds > 0
                        else self._send_policy.resolve_delay_seconds(rng),
                    )
                )
            prepared_batches.append((account, prepared))

        if not prepared_batches:
            raise TikTokClientError("No eligible comment rows matched selected accounts.")

        results: list[SendResult] = []
        max_workers = max(1, min(len(prepared_batches), 6))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(self._send_comments_for_account_batch, account, comments): (account, comments)
                for account, comments in prepared_batches
            }
            for future in as_completed(future_map):
                account, comments = future_map[future]
                try:
                    account_results = future.result()
                except Exception as error:
                    account_logger = get_account_logger(self._logger, account.name)
                    account_logger.exception("Account batch failed with an exception: %s", error)
                    account_results = self._build_failed_send_results(
                        account_name=account.name,
                        comments=comments,
                        error=error,
                        status="batch_error",
                    )
                results.extend(account_results)

                successful_count = sum(1 for item in account_results if item.success)
                attempts_count = len(account_results)
                summaries[account.name] = replace(
                    summaries[account.name],
                    attempted_comments=summaries[account.name].attempted_comments + attempts_count,
                    successful_comments=summaries[account.name].successful_comments + successful_count,
                    failed_comments=summaries[account.name].failed_comments + (attempts_count - successful_count),
                    statuses=summaries[account.name].statuses + tuple(item.status for item in account_results),
                )
                account_logger = get_account_logger(self._logger, account.name)
                account_logger.info(
                    "Finished all_accounts mode. Successful sends: %s/%s.",
                    successful_count,
                    attempts_count,
                )

        return results

    def _send_comments_for_account_batch(
        self,
        account: TikTokAccountConfig,
        comments: list[OutgoingComment],
    ) -> list[SendResult]:
        account_logger = get_account_logger(self._logger, account.name)
        account_logger.info(
            "Starting all_accounts mode for %s comments.",
            len(comments),
        )
        client = TikTokPlaywrightClient(self._config, account_logger, account)
        return client.send_comments(comments)

    def _build_failed_send_results(
        self,
        *,
        account_name: str,
        comments: list[OutgoingComment],
        error: Exception,
        status: str,
    ) -> list[SendResult]:
        details = str(error) or error.__class__.__name__
        return [
            SendResult(
                account_name=account_name,
                outgoing_comment=comment,
                success=False,
                details=details,
                status=status,
            )
            for comment in comments
        ]

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

    def _normalize_comment_account_restrictions(
        self,
        comments: list[OutgoingComment],
        accounts: list[TikTokAccountConfig],
    ) -> list[OutgoingComment]:
        alias_to_account_name: dict[str, str] = {}
        for account in accounts:
            canonical = account.name.strip()
            if not canonical:
                continue
            alias_to_account_name[canonical.lower()] = canonical
            if account.tiktok_username:
                username = account.tiktok_username.strip().lstrip("@").lower()
                if username:
                    alias_to_account_name[username] = canonical
                    alias_to_account_name[f"@{username}"] = canonical

        normalized_comments: list[OutgoingComment] = []
        for comment in comments:
            if not comment.allowed_account_names:
                normalized_comments.append(comment)
                continue

            resolved_names: list[str] = []
            for raw_name in comment.allowed_account_names:
                normalized_key = raw_name.strip().lower()
                normalized_key = normalized_key or raw_name
                canonical = alias_to_account_name.get(normalized_key)
                if canonical is None:
                    canonical = alias_to_account_name.get(normalized_key.lstrip("@"))
                resolved_names.append(canonical or raw_name)

            deduped_names = tuple(dict.fromkeys(name for name in resolved_names if str(name).strip()))
            normalized_comments.append(
                replace(comment, allowed_account_names=deduped_names)
            )
        return normalized_comments

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
