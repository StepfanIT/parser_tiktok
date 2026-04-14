from __future__ import annotations

import logging
from pathlib import Path

from app.config import AppConfig
from app.integrations.tiktok_client import TikTokPlaywrightClient
from app.models import SendResult
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

    def collect_comments(
        self,
        video_url: str,
        *,
        account_path: Path | None = None,
        output_path: Path | None = None,
    ) -> Path:
        account = self._account_repository.load_account(account_path)
        client = TikTokPlaywrightClient(self._config, self._logger, account)
        comments = client.scrape_comments(video_url)
        return self._csv_repository.export_scraped_comments(comments, output_path)

    def send_comments(
        self,
        *,
        account_path: Path | None = None,
        csv_path: Path | None = None,
    ) -> list[SendResult]:
        account = self._account_repository.load_account(account_path)
        outgoing_comments = self._csv_repository.load_outgoing_comments(csv_path)
        client = TikTokPlaywrightClient(self._config, self._logger, account)
        return client.send_comments(outgoing_comments)

