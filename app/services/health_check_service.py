from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from app.config import AppConfig
from app.integrations.tiktok_client import TikTokPlaywrightClient
from app.logging_config import get_account_logger
from app.models import AccountHealthCheckResult, TikTokAccountConfig
from app.repositories.account_repository import AccountRepository


class AccountHealthService:
    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        account_repository: AccountRepository,
    ) -> None:
        self._config = config
        self._logger = logger
        self._account_repository = account_repository

    def check_accounts(
        self,
        account_paths: list[Path] | None = None,
    ) -> tuple[list[TikTokAccountConfig], list[AccountHealthCheckResult]]:
        accounts = self._account_repository.load_accounts(account_paths)
        prepared_accounts: list[TikTokAccountConfig] = []
        results: list[AccountHealthCheckResult] = []

        for account in accounts:
            account_logger = get_account_logger(self._logger, account.name)
            client = TikTokPlaywrightClient(self._config, account_logger, account)
            result = client.health_check()
            results.append(result)

            if not result.success:
                account_logger.error("Health check failed: %s", result.details)
                continue

            account_logger.info("Health check passed. %s", result.details)
            if result.resolved_username and account.tiktok_username != result.resolved_username:
                account = replace(account, tiktok_username=result.resolved_username)
            prepared_accounts.append(account)

        return prepared_accounts, results
