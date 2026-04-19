from __future__ import annotations

import random
import time
from dataclasses import replace
from datetime import datetime, timedelta

from app.config import AppConfig
from app.models import AccountSendState, OutgoingComment, TikTokAccountConfig


class SendExecutionPolicy:
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def build_states(
        self,
        accounts: list[TikTokAccountConfig],
        rng: random.Random,
    ) -> list[AccountSendState]:
        return [
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

    def refresh_state_windows(self, states: list[AccountSendState]) -> None:
        now = datetime.now()
        day_key = now.strftime("%Y-%m-%d")
        hour_key = now.strftime("%Y-%m-%d %H")
        for state in states:
            state.refresh_windows(day_key=day_key, hour_key=hour_key)

    def select_eligible_states(
        self,
        states: list[AccountSendState],
        pending_pool: list[OutgoingComment],
    ) -> list[AccountSendState]:
        return [
            state
            for state in states
            if self.is_state_available(state)
            and self.has_eligible_comments_for_account(pending_pool, state.account)
        ]

    def is_state_available(self, state: AccountSendState) -> bool:
        if state.daily_remaining <= 0:
            return False
        if state.hourly_remaining <= 0:
            return False
        return time.monotonic() >= state.next_available_at_monotonic

    def has_eligible_comments_for_account(
        self,
        pending_pool: list[OutgoingComment],
        account: TikTokAccountConfig,
    ) -> bool:
        return any(self.comment_matches_account(comment, account) for comment in pending_pool)

    def comment_matches_account(
        self,
        comment: OutgoingComment,
        account: TikTokAccountConfig,
    ) -> bool:
        if not comment.allowed_account_names:
            return True
        return account.name in comment.allowed_account_names

    def take_batch_for_account(
        self,
        pending_pool: list[OutgoingComment],
        state: AccountSendState,
        rng: random.Random,
    ) -> list[OutgoingComment]:
        eligible_comments = [
            comment for comment in pending_pool if self.comment_matches_account(comment, state.account)
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

        selected = rng.sample(eligible_comments, k=max_batch_size)
        batch: list[OutgoingComment] = []
        for comment in selected:
            pending_pool.remove(comment)
            batch.append(
                replace(
                    comment,
                    text=self.resolve_comment_text(comment, rng),
                    delay_seconds=comment.delay_seconds
                    if comment.delay_seconds > 0
                    else self.resolve_delay_seconds(rng),
                )
            )
        return batch

    def resolve_delay_seconds(self, rng: random.Random) -> int:
        base_delay = rng.choice(self._config.send_behavior.comment_delay_choices)
        jitter = rng.choice((-1, 0, 1, 2))
        return max(base_delay + jitter, 2)

    @staticmethod
    def resolve_comment_text(comment: OutgoingComment, rng: random.Random) -> str:
        return rng.choice(comment.available_texts)

    def schedule_next_cooldown(
        self,
        state: AccountSendState,
        *,
        has_pending_comments: bool,
        rng: random.Random,
    ) -> int | None:
        if not has_pending_comments or state.daily_remaining <= 0:
            return None

        pause_seconds = rng.randint(
            self._config.send_behavior.batch_pause_min_seconds,
            self._config.send_behavior.batch_pause_max_seconds,
        )
        state.next_available_at_monotonic = time.monotonic() + pause_seconds
        return pause_seconds

    def seconds_until_next_available_slot(self, states: list[AccountSendState]) -> int | None:
        now = datetime.now()
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
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

        if waits:
            return min(waits)

        if any(state.daily_remaining > 0 for state in states):
            return seconds_until_next_hour
        return None
