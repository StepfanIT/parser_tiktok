from __future__ import annotations

import logging
import random
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from app.config import AppConfig
from app.integrations.tiktok_client import TikTokClientError, TikTokPlaywrightClient
from app.models import AccountSendState, OutgoingComment, ScrapedComment, SendResult, TikTokAccountConfig
from app.repositories.account_repository import AccountRepository
from app.repositories.csv_repository import CSVRepository


class TikTokCommentService:
    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        account_repository: AccountRepository,
        csv_repository: CSVRepository,
    ) -> None:
        self._config = config
        self._logger = logger
        self._account_repository = account_repository
        self._csv_repository = csv_repository

    def list_available_account_paths(self) -> list[Path]:
        return self._account_repository.list_account_paths()

    def collect_comments(
        self,
        video_url: str,
        *,
        account_paths: list[Path] | None = None,
        output_path: Path | None = None,
    ) -> Path:
        accounts = self._prepare_accounts(account_paths, action_label="збирання")
        self._logger.info("Усі вибрані акаунти активовані. Починаю збирання.")
        merged_comments: dict[str, ScrapedComment] = {}

        for index, account in enumerate(accounts, start=1):
            self._logger.info(
                "Акаунт %s: починаю збір коментарів (%s/%s).",
                account.name,
                index,
                len(accounts),
            )
            client = TikTokPlaywrightClient(self._config, self._logger, account)
            comments = client.scrape_comments(video_url)
            self._logger.info(
                "Акаунт %s: знайшов %s коментарів без своєї відповіді.",
                account.name,
                len(comments),
            )

            for comment in comments:
                existing = merged_comments.get(comment.comment_id)
                if existing is None:
                    merged_comments[comment.comment_id] = comment
                    continue

                merged_comments[comment.comment_id] = self._merge_scraped_comments(existing, comment)

            if index < len(accounts):
                pause_seconds = random.randint(6, 15)
                self._logger.info(
                    "Пауза між акаунтами після збору: %s с.",
                    pause_seconds,
                )
                time.sleep(pause_seconds)

        comments_to_export = list(merged_comments.values())
        comments_to_export.sort(key=lambda item: item.published_at or "", reverse=True)
        if not comments_to_export:
            raise TikTokClientError(
                "Не вдалося зібрати жодного коментаря без відповіді вибраних акаунтів."
            )

        export_path = self._csv_repository.export_scraped_comments(comments_to_export, output_path)
        self._logger.info("Збір завершено. Експортовано %s рядків у %s.", len(comments_to_export), export_path)
        return export_path

    def send_comments(
        self,
        *,
        account_paths: list[Path] | None = None,
        csv_path: Path | None = None,
    ) -> list[SendResult]:
        accounts = self._prepare_accounts(account_paths, action_label="надсилання")
        self._logger.info("Усі вибрані акаунти активовані. Починаю надсилання.")
        pending_comments = self._csv_repository.load_outgoing_comments(csv_path)
        if not pending_comments:
            raise ValueError("CSV для надсилання не містить коментарів.")

        self._validate_comment_account_restrictions(pending_comments, accounts)
        rng = random.Random()
        pending_pool = pending_comments.copy()
        rng.shuffle(pending_pool)

        states = [
            AccountSendState(
                account=account,
                daily_limit=rng.randint(
                    self._config.send_behavior.daily_limit_min,
                    self._config.send_behavior.daily_limit_max,
                ),
                hourly_limit=rng.randint(
                    self._config.send_behavior.hourly_limit_min,
                    self._config.send_behavior.hourly_limit_max,
                ),
            )
            for account in accounts
        ]

        for state in states:
            self._logger.info(
                "Акаунт %s: денний ліміт %s, годинний ліміт %s.",
                state.account.name,
                state.daily_limit,
                state.hourly_limit,
            )

        results: list[SendResult] = []
        while pending_pool:
            self._refresh_send_state_windows(states)
            eligible_states = [
                state
                for state in states
                if self._is_state_available(state)
                and self._has_eligible_comments_for_account(pending_pool, state.account)
            ]

            if not eligible_states:
                wait_seconds = self._seconds_until_next_available_slot(states)
                if wait_seconds is None:
                    raise TikTokClientError(
                        "Усі вибрані акаунти вичерпали денний ліміт або не підходять для залишку коментарів."
                    )

                self._logger.info(
                    "Усі акаунти зараз на паузі. Чекаю %s с перед наступною пачкою.",
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                continue

            state = rng.choice(eligible_states)
            batch = self._take_random_batch_for_account(pending_pool, state, rng)
            self._logger.info(
                "Акаунт %s: старт пачки %s, коментарів у пачці %s, залишилось у черзі %s.",
                state.account.name,
                state.batch_index + 1,
                len(batch),
                len(pending_pool),
            )

            client = TikTokPlaywrightClient(self._config, self._logger, state.account)
            batch_results = client.send_comments(batch)
            results.extend(batch_results)

            successful_count = sum(1 for result in batch_results if result.success)
            attempts_count = len(batch_results)
            state.sent_today += attempts_count
            state.sent_this_hour += attempts_count
            state.batch_index += 1
            state.last_batch_at_monotonic = time.monotonic()

            self._logger.info(
                "Акаунт %s: пачку %s завершено, успішно %s з %s.",
                state.account.name,
                state.batch_index,
                successful_count,
                attempts_count,
            )

            if pending_pool and state.daily_remaining > 0:
                pause_seconds = rng.randint(
                    self._config.send_behavior.batch_pause_min_seconds,
                    self._config.send_behavior.batch_pause_max_seconds,
                )
                state.next_available_at_monotonic = time.monotonic() + pause_seconds
                self._logger.info(
                    "Акаунт %s: пауза між пачками %s с.",
                    state.account.name,
                    pause_seconds,
                )

        self._logger.info(
            "Надсилання завершено. Успішно %s з %s коментарів.",
            sum(1 for result in results if result.success),
            len(results),
        )
        return results

    def _prepare_accounts(
        self,
        account_paths: list[Path] | None,
        *,
        action_label: str,
    ) -> list[TikTokAccountConfig]:
        accounts = self._account_repository.load_accounts(account_paths)
        prepared_accounts: list[TikTokAccountConfig] = []
        for index, account in enumerate(accounts, start=1):
            self._logger.info(
                "Підготовка акаунта %s (%s/%s) для %s.",
                account.name,
                index,
                len(accounts),
                action_label,
            )
            client = TikTokPlaywrightClient(self._config, self._logger, account)
            username = client.ensure_session_ready()
            if username and account.tiktok_username != username:
                account = replace(account, tiktok_username=username)

            if not account.tiktok_username:
                self._logger.warning(
                    "Акаунт %s не має tiktok_username у конфігу. "
                    "Для точного фільтра відповідей краще його додати.",
                    account.name,
                )

            prepared_accounts.append(account)
        return prepared_accounts

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
                f"Для коментаря #{comment.order} потрібен один із акаунтів: {allowed}, "
                "але їх не вибрано в цьому запуску."
            )

    def _refresh_send_state_windows(self, states: list[AccountSendState]) -> None:
        now = datetime.now()
        day_key = now.strftime("%Y-%m-%d")
        hour_key = now.strftime("%Y-%m-%d %H")
        for state in states:
            state.refresh_windows(day_key=day_key, hour_key=hour_key)

    def _is_state_available(self, state: AccountSendState) -> bool:
        if state.daily_remaining <= 0:
            return False
        if state.hourly_remaining <= 0:
            return False
        return time.monotonic() >= state.next_available_at_monotonic

    def _has_eligible_comments_for_account(
        self,
        pending_pool: list[OutgoingComment],
        account: TikTokAccountConfig,
    ) -> bool:
        return any(self._comment_matches_account(comment, account) for comment in pending_pool)

    def _comment_matches_account(self, comment: OutgoingComment, account: TikTokAccountConfig) -> bool:
        if not comment.allowed_account_names:
            return True
        return account.name in comment.allowed_account_names

    def _take_random_batch_for_account(
        self,
        pending_pool: list[OutgoingComment],
        state: AccountSendState,
        rng: random.Random,
    ) -> list[OutgoingComment]:
        eligible_comments = [
            comment for comment in pending_pool if self._comment_matches_account(comment, state.account)
        ]
        max_batch_size = min(
            len(eligible_comments),
            state.daily_remaining,
            state.hourly_remaining,
            rng.randint(
                self._config.send_behavior.batch_size_min,
                self._config.send_behavior.batch_size_max,
            ),
        )
        if max_batch_size <= 0:
            return []

        batch_source = rng.sample(eligible_comments, k=max_batch_size)
        batch: list[OutgoingComment] = []
        for comment in batch_source:
            pending_pool.remove(comment)
            delay_seconds = rng.choice(self._config.send_behavior.comment_delay_choices)
            batch.append(replace(comment, delay_seconds=delay_seconds))
        return batch

    def _seconds_until_next_available_slot(self, states: list[AccountSendState]) -> int | None:
        now = datetime.now()
        next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        seconds_until_next_hour = max(int((next_hour - now).total_seconds()), 1)
        monotonic_now = time.monotonic()

        waits: list[int] = []
        for state in states:
            if state.daily_remaining <= 0:
                continue

            if state.hourly_remaining <= 0:
                waits.append(seconds_until_next_hour)
                continue

            if state.next_available_at_monotonic > monotonic_now:
                waits.append(max(int(state.next_available_at_monotonic - monotonic_now), 1))
                continue

        if waits:
            return min(waits)

        if any(state.daily_remaining > 0 for state in states):
            return seconds_until_next_hour

        return None
