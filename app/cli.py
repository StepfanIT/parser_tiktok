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

        try:
            output_path = self._prompt_optional_path(
                prompt="Шлях для CSV-експорту",
                default=None,
                empty_hint="Enter = створити новий файл в exports",
            )
            account_paths = self._prompt_account_paths(action_label="збору")
            exported_path = self._service.collect_comments(
                video_url,
                account_paths=account_paths,
                output_path=output_path,
            )
            print(f"Коментарі збережено у: {exported_path}")
        except (FileNotFoundError, ValueError, TikTokClientError) as error:
            self._logger.exception("Помилка під час збирання коментарів.")
            print(f"Не вдалося зібрати коментарі: {error}")

        self._pause()

    def _handle_send_comments(self) -> None:
        print()
        try:
            csv_path = self._prompt_path(
                prompt="Шлях до CSV з коментарями",
                default=self._config.default_outgoing_comments_csv,
            )
            account_paths = self._prompt_account_paths(action_label="надсилання")
            results = self._service.send_comments(
                account_paths=account_paths,
                csv_path=csv_path,
            )
            success_count = sum(1 for result in results if result.success)
            print(f"Завершено: успішно {success_count} з {len(results)} коментарів.")
            for result in results:
                status = "OK" if result.success else "WARN"
                print(f"[{status}] {result.account_name} | #{result.outgoing_comment.order}: {result.details}")
        except TikTokVerificationRequiredError as error:
            self._logger.exception("TikTok просить ручну перевірку.")
            print(f"Потрібно пройти перевірку TikTok у браузері: {error}")
        except TikTokLoginRequiredError as error:
            self._logger.exception("Потрібно оновити логін або сесію.")
            print(f"Проблема з логіном або сесією: {error}")
        except (FileNotFoundError, ValueError, TikTokClientError) as error:
            self._logger.exception("Помилка під час надсилання коментарів.")
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
        print(f"Конфіг за замовч.: {self._config.default_account_path}")
        print(f"CSV для відправки: {self._config.default_outgoing_comments_csv}")
        print(f"Логи:              {self._config.logs_dir / 'app.log'}")
        print("=" * 52)

    def _prompt_path(self, prompt: str, default: Path) -> Path:
        raw_value = input(f"{prompt} [{default}]: ").strip()
        return Path(raw_value) if raw_value else default

    def _prompt_optional_path(
        self,
        prompt: str,
        default: Path | None,
        *,
        empty_hint: str,
    ) -> Path | None:
        suffix = f" [{default}]" if default is not None else f" ({empty_hint})"
        raw_value = input(f"{prompt}{suffix}: ").strip()
        if not raw_value:
            return default
        return Path(raw_value)

    def _prompt_account_paths(self, *, action_label: str) -> list[Path]:
        print()
        available_paths = self._service.list_available_account_paths()
        if available_paths:
            print("Доступні конфіги акаунтів:")
            for index, path in enumerate(available_paths, start=1):
                print(f"{index}. {path}")
        else:
            print("У папці data/accounts поки що не знайдено додаткових конфігів.")

        print()
        print(f"Режим акаунтів для {action_label}:")
        print("1. Один акаунт")
        print("2. Кілька акаунтів")
        mode = input("Оберіть режим [1-2]: ").strip() or "1"

        if mode == "1":
            return [self._prompt_path("Шлях до конфігу акаунта", self._config.default_account_path)]

        if mode != "2":
            print("Невідомий режим, беру один акаунт за замовчуванням.")
            return [self._prompt_path("Шлях до конфігу акаунта", self._config.default_account_path)]

        count = self._prompt_positive_int("Скільки акаунтів використати", default=max(2, min(len(available_paths), 3)))
        selected_paths: list[Path] = []
        for index in range(count):
            default = available_paths[index] if index < len(available_paths) else None
            if default is None:
                raw_value = input(f"Шлях до конфігу акаунта #{index + 1}: ").strip()
                if not raw_value:
                    raise ValueError("Для багатоакаунтного режиму шлях до кожного конфігу обов'язковий.")
                selected_paths.append(Path(raw_value))
                continue

            selected_paths.append(
                self._prompt_path(
                    prompt=f"Шлях до конфігу акаунта #{index + 1}",
                    default=default,
                )
            )

        unique_paths: list[Path] = []
        seen: set[str] = set()
        for path in selected_paths:
            key = str(path).lower()
            if key in seen:
                continue
            seen.add(key)
            unique_paths.append(path)
        return unique_paths

    @staticmethod
    def _prompt_positive_int(prompt: str, *, default: int) -> int:
        raw_value = input(f"{prompt} [{default}]: ").strip()
        if not raw_value:
            return default

        value = int(raw_value)
        if value <= 0:
            raise ValueError("Число має бути більше нуля.")
        return value
