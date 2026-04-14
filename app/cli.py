from __future__ import annotations

import logging
from pathlib import Path

from app.config import AppConfig
from app.integrations.tiktok_client import (
    TikTokClientError,
    TikTokLoginRequiredError,
    TikTokVerificationRequiredError,
)
from app.services.comment_service import TikTokCommentService


class TikTokCli:
    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        service: TikTokCommentService,
    ) -> None:
        self._config = config
        self._logger = logger
        self._service = service

    def run(self) -> None:
        while True:
            self._print_menu()
            choice = input("Оберіть опцію [1-3]: ").strip()
            if choice == "1":
                self._handle_collect_comments()
            elif choice == "2":
                self._handle_send_comments()
            elif choice == "3":
                print("Завершення роботи.")
                return
            else:
                print("Невідома опція. Введіть 1, 2 або 3.")
                self._pause()

    def _handle_collect_comments(self) -> None:
        print()
        video_url = input("Посилання на TikTok-відео: ").strip()
        if not video_url:
            print("Посилання на відео обов'язкове.")
            self._pause()
            return

        output_path = self._prompt_path(
            prompt="Шлях для CSV-експорту",
            default=self._config.exports_dir / "scraped_comments_latest.csv",
        )
        account_path = self._prompt_path(
            prompt="Шлях до конфігу акаунта",
            default=self._config.default_account_path,
        )

        try:
            exported_path = self._service.collect_comments(
                video_url,
                account_path=account_path,
                output_path=output_path,
            )
            print(f"Коментарі збережено у: {exported_path}")
        except (FileNotFoundError, ValueError, TikTokClientError) as error:
            self._logger.exception("Collect comments failed.")
            print(f"Не вдалося зібрати коментарі: {error}")

        self._pause()

    def _handle_send_comments(self) -> None:
        print()
        csv_path = self._prompt_path(
            prompt="Шлях до CSV з коментарями",
            default=self._config.default_outgoing_comments_csv,
        )
        account_path = self._prompt_path(
            prompt="Шлях до конфігу акаунта",
            default=self._config.default_account_path,
        )

        try:
            results = self._service.send_comments(
                account_path=account_path,
                csv_path=csv_path,
            )
            success_count = sum(1 for result in results if result.success)
            print(f"Завершено: успішно {success_count} з {len(results)} коментарів.")
            for result in results:
                status = "OK" if result.success else "WARN"
                print(f"[{status}] #{result.outgoing_comment.order}: {result.details}")
        except TikTokVerificationRequiredError as error:
            self._logger.exception("Verification challenge requires user action.")
            print(f"Потрібно пройти перевірку TikTok у браузері: {error}")
        except TikTokLoginRequiredError as error:
            self._logger.exception("Login flow required.")
            print(f"Проблема з логіном або сесією: {error}")
        except (FileNotFoundError, ValueError, TikTokClientError) as error:
            self._logger.exception("Send comments failed.")
            print(f"Не вдалося надіслати коментарі: {error}")

        self._pause()

    @staticmethod
    def _pause() -> None:
        print()
        input("Натисніть Enter, щоб повернутися в меню... ")

    def _print_menu(self) -> None:
        print()
        print("=" * 52)
        print("TikTok Parser MVP")
        print("=" * 52)
        print("1. Збір коментарів")
        print("2. Надіслати коментарі")
        print("3. Вихід")
        print()
        print(f"Конфіг акаунта:   {self._config.default_account_path}")
        print(f"CSV для відправки:{self._config.default_outgoing_comments_csv}")
        print(f"Логи:             {self._config.logs_dir / 'app.log'}")
        print("=" * 52)

    def _prompt_path(self, prompt: str, default: Path) -> Path:
        raw_value = input(f"{prompt} [{default}]: ").strip()
        return Path(raw_value) if raw_value else default
