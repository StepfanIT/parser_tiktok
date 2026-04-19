from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from app.config import AppConfig
from app.integrations.tiktok_client import (
    TikTokClientError,
    TikTokLoginRequiredError,
    TikTokVerificationRequiredError,
)
from app.services.comment_service import TikTokCommentService

RESET = "\033[0m"
BOLD = "\033[1m"
BLUE = "\033[38;5;75m"
GREEN = "\033[38;5;120m"
YELLOW = "\033[38;5;221m"
RED = "\033[38;5;203m"
CYAN = "\033[38;5;81m"
MAGENTA = "\033[38;5;177m"
TEAL = "\033[38;5;44m"


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
        self._use_colors = self._detect_color_support()

    def run(self) -> None:
        self._print_startup_banner()
        while True:
            self._print_menu()
            choice = input("Select an option [1-4]: ").strip()
            if choice == "1":
                self._handle_collect_comments()
            elif choice == "2":
                self._handle_send_comments()
            elif choice == "3":
                self._handle_health_check()
            elif choice == "4":
                print(self._paint("Exiting.", GREEN))
                return
            else:
                print(self._paint("Unknown option. Please enter 1, 2, 3, or 4.", YELLOW))
                self._pause()

    def _handle_collect_comments(self) -> None:
        self._print_section("Collect Comments")
        try:
            collect_mode = self._prompt_collect_mode()
            if collect_mode == "1":
                video_urls = [self._prompt_required_video_url()]
            else:
                video_urls = self._prompt_video_urls_for_multi_collect()
                if not video_urls:
                    print(self._paint("At least one video URL is required.", RED))
                    self._pause()
                    return

            output_path = self._prompt_optional_path(
                prompt="Export CSV path",
                default=None,
                empty_hint="Press Enter to create a fresh file in exports",
            )
            account_paths = self._prompt_account_paths(action_label="collection")
            exported_path = self._service.collect_comments_for_videos(
                video_urls=video_urls,
                account_paths=account_paths,
                output_path=output_path,
            )
            print(self._paint(f"Comments saved to: {exported_path}", GREEN))
        except (FileNotFoundError, ValueError, TikTokClientError) as error:
            self._logger.exception("Comment collection failed.")
            print(self._paint(f"Could not collect comments: {error}", RED))

        self._pause()

    def _handle_send_comments(self) -> None:
        self._print_section("Send Comments")
        try:
            csv_path = self._prompt_path(
                prompt="Outgoing CSV path",
                default=self._config.default_outgoing_comments_csv,
            )
            account_paths = self._prompt_account_paths(action_label="sending")
            results = self._service.send_comments(
                account_paths=account_paths,
                csv_path=csv_path,
            )
            success_count = sum(1 for result in results if result.success)
            print(
                self._paint(
                    f"Done: {success_count}/{len(results)} comments sent successfully.",
                    GREEN,
                )
            )
            for result in results:
                status = result.status.upper()
                print(
                    f"[{status}] {result.account_name} | "
                    f"#{result.outgoing_comment.order}: {result.details}"
                )
        except TikTokVerificationRequiredError as error:
            self._logger.exception("TikTok requires manual verification.")
            print(self._paint(f"TikTok verification is required in the browser: {error}", YELLOW))
        except TikTokLoginRequiredError as error:
            self._logger.exception("TikTok session refresh is required.")
            print(self._paint(f"Login/session issue: {error}", RED))
        except (FileNotFoundError, ValueError, TikTokClientError) as error:
            self._logger.exception("Comment sending failed.")
            print(self._paint(f"Could not send comments: {error}", RED))

        self._pause()

    def _handle_health_check(self) -> None:
        self._print_section("Account Health Check")
        try:
            account_paths = self._prompt_account_paths(action_label="health check")
            results, report_path = self._service.run_health_check(account_paths=account_paths)
            success_count = sum(1 for result in results if result.success)
            print(
                self._paint(
                    f"Health check complete: {success_count}/{len(results)} accounts passed.",
                    GREEN if success_count == len(results) else YELLOW,
                )
            )
            for result in results:
                status = "OK" if result.success else "FAIL"
                print(f"[{status}] {result.account_name} | {result.provider_name} | {result.details}")
            print(self._paint(f"Report saved to: {report_path}", GREEN))
        except (FileNotFoundError, ValueError, TikTokClientError) as error:
            self._logger.exception("Health check failed.")
            print(self._paint(f"Health check error: {error}", RED))

        self._pause()

    @staticmethod
    def _pause() -> None:
        print()
        input("Press Enter to return to the menu... ")

    def _print_menu(self) -> None:
        print()
        print(self._paint("=" * 60, CYAN))
        print(self._paint("TikTok Parser Console", CYAN, bold=True))
        print(self._paint("=" * 60, CYAN))
        print(self._paint("1. Collect comments", BLUE, bold=True))
        print(self._paint("2. Send comments", GREEN, bold=True))
        print(self._paint("3. Account health check", MAGENTA, bold=True))
        print(self._paint("4. Exit", RED, bold=True))
        print()
        print(self._paint(f"Default account config: {self._config.default_account_path}", TEAL))
        print(self._paint(f"Outgoing CSV:          {self._config.default_outgoing_comments_csv}", TEAL))
        print(self._paint(f"Logs:                  {self._config.logs_dir / 'app.log'}", TEAL))
        print(self._paint("=" * 60, CYAN))

    def _print_startup_banner(self) -> None:
        if not self._use_colors:
            print(
                r"""
  _______ _ _    _______     _
 |__   __(_) |  |__   __|   | |
    | |   _| | __  | | ___  | | __
    | |  | | |/ /  | |/ _ \ | |/ /
    | |  | |   <   | | (_) ||   <
    |_|  |_|_|\_\  |_|\___(_)_|\_\

             __
            / /_
       ____/ __/
      / __  /_
      \__,_/\__|
            TikTok
             🎵
            """
            )
            return

        print(
            fr"""{CYAN}{BOLD}
  _______ _ _    _______     _
 |__   __(_) |  |__   __|   | |
    | |   _| | __  | | ___  | | __
    | |  | | |/ /  | |/ _ \ | |/ /
    | |  | |   <   | | (_) ||   <
    |_|  |_|_|\_\  |_|\___(_)_|\_\

             __
            / /_
       ____/ __/
      / __  /_
      \__,_/\__|
            TikTok
             🎵
            {RESET}"""
        )

    def _prompt_path(self, prompt: str, default: Path) -> Path:
        raw_value = input(f"{prompt} [{default}]: ").strip()
        return Path(raw_value) if raw_value else default

    def _prompt_collect_mode(self) -> str:
        print("Collection mode:")
        print("1. All selected accounts on one video")
        print("2. Each selected account on each listed video")
        mode = input("Select mode [1-2]: ").strip() or "1"
        if mode not in {"1", "2"}:
            print(self._paint("Unknown mode. Falling back to one video mode.", YELLOW))
            return "1"
        return mode

    def _prompt_required_video_url(self) -> str:
        video_url = input("TikTok video URL: ").strip()
        if video_url:
            return video_url
        raise ValueError("Video URL is required.")

    def _prompt_video_urls_for_multi_collect(self) -> list[str]:
        print("Paste video URLs (comma-separated or one per line).")
        print("Submit an empty line to finish.")
        chunks: list[str] = []
        while True:
            raw_value = input("Video URL(s): ").strip()
            if not raw_value:
                break
            chunks.append(raw_value)

        if not chunks:
            fallback = input("TikTok video URL: ").strip()
            return [fallback] if fallback else []

        merged = ",".join(chunks).replace("\n", ",")
        values = [item.strip() for item in merged.split(",")]
        unique_urls: list[str] = []
        seen: set[str] = set()
        for url in values:
            if not url:
                continue
            key = url.lower()
            if key in seen:
                continue
            seen.add(key)
            unique_urls.append(url)
        return unique_urls

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
        self._print_section(f"Accounts · {action_label.title()}")
        available_paths = self._service.list_available_account_paths()
        if available_paths:
            print(self._paint("Available account configs:", BLUE, bold=True))
            for index, path in enumerate(available_paths, start=1):
                print(f"{index}. {path}")
        else:
            print(self._paint("No account configs found yet in data/accounts.", YELLOW))

        print()
        print(f"Account mode for {action_label}:")
        print("1. Single account")
        print("2. Multiple accounts")
        mode = input("Select mode [1-2]: ").strip() or "1"

        if mode == "1":
            default = available_paths[0] if available_paths else None
            return [self._prompt_account_slot(slot_index=1, default_path=default)]

        if mode != "2":
            print(self._paint("Unknown mode. Falling back to a single account.", YELLOW))
            default = available_paths[0] if available_paths else None
            return [self._prompt_account_slot(slot_index=1, default_path=default)]

        count = self._prompt_positive_int(
            "How many accounts should be used",
            default=max(2, min(max(len(available_paths), 2), 3)),
        )
        selected_paths: list[Path] = []
        use_saved_choice = self._prompt_multi_account_source_choice(has_saved=bool(available_paths))
        if use_saved_choice == "1":
            selected_paths.extend(available_paths[:count])
            if len(selected_paths) < count:
                print(
                    self._paint(
                        "Not enough saved accounts. Missing slots will be created or chosen manually.",
                        YELLOW,
                    )
                )

        preset = self._prompt_multi_account_creation_preset() if len(selected_paths) < count else None
        prepared_paths: set[str] = set()
        for index in range(len(selected_paths), count):
            default = available_paths[index] if index < len(available_paths) else None
            selected_path = self._prompt_account_slot(
                slot_index=index + 1,
                default_path=default,
                creation_preset=preset,
            )
            selected_paths.append(selected_path)

        for index, selected_path in enumerate(selected_paths, start=1):
            path_key = str(selected_path).lower()
            if path_key in prepared_paths:
                continue
            self._ensure_account_session_for_slot(slot_index=index, account_path=selected_path)
            prepared_paths.add(path_key)

        unique_paths: list[Path] = []
        seen: set[str] = set()
        for path in selected_paths:
            key = str(path).lower()
            if key in seen:
                continue
            seen.add(key)
            unique_paths.append(path)
        print(
            self._paint(
                "A browser window will open for each selected account during the session check.",
                BLUE,
            )
        )
        return unique_paths

    @staticmethod
    def _prompt_positive_int(prompt: str, *, default: int) -> int:
        raw_value = input(f"{prompt} [{default}]: ").strip()
        if not raw_value:
            return default

        value = int(raw_value)
        if value <= 0:
            raise ValueError("The value must be greater than zero.")
        return value

    def _prompt_account_slot(
        self,
        *,
        slot_index: int,
        default_path: Path | None,
        creation_preset: dict[str, Any] | None = None,
    ) -> Path:
        if default_path is None:
            print(
                self._paint(
                    f"No existing config for account #{slot_index}. Creating a new account config now.",
                    YELLOW,
                )
            )
            return self._create_account_config_interactively(slot_index, preset=creation_preset)

        raw_value = input(
            f"Account #{slot_index} config [{default_path}] "
            f"(Enter = use, NEW = create another): "
        ).strip()
        if not raw_value:
            return default_path
        if raw_value.lower() in {"new", "create", "+"}:
            return self._create_account_config_interactively(slot_index, preset=creation_preset)
        return Path(raw_value)

    def _create_account_config_interactively(
        self,
        slot_index: int,
        *,
        preset: dict[str, Any] | None = None,
    ) -> Path:
        print()
        print(self._paint(f"Create account #{slot_index}", CYAN, bold=True))
        suggested_name = self._service.suggest_account_name(slot_index)
        account_name = input(f"Account name [{suggested_name}]: ").strip() or suggested_name

        provider_name, api_url, api_token, api_key = self._resolve_account_provider_settings(preset=preset)
        profile_id: str | None = None
        if provider_name != "playwright_local":
            profile_id = self._prompt_required_text("Profile ID")

        config_path = self._service.create_account_config(
            account_name=account_name,
            provider_name=provider_name,
            profile_id=profile_id,
            api_url=api_url,
            api_token=api_token,
            api_key=api_key,
        )
        print(self._paint(f"Created config: {config_path}", GREEN))
        self._print_provider_secret_hint(provider_name, config_path, api_token=api_token, api_key=api_key)
        return config_path

    def _prompt_multi_account_creation_preset(self) -> dict[str, Any]:
        print()
        print("Default setup for NEW accounts in this multi-account batch:")
        print("1. Local browser (Playwright)")
        print("2. Dolphin anti-detect")
        print("3. AdsPower anti-detect")
        preset_choice = input("Select preset [1-3]: ").strip() or "1"

        if preset_choice == "2":
            api_url = self._prompt_provider_api_url("dolphin_anty")
            api_token, _ = self._prompt_provider_secret("dolphin_anty")
            return {
                "provider_name": "dolphin_anty",
                "api_url": api_url,
                "api_token": api_token,
                "api_key": None,
            }

        if preset_choice == "3":
            api_url = self._prompt_provider_api_url("adspower")
            _, api_key = self._prompt_provider_secret("adspower")
            return {
                "provider_name": "adspower",
                "api_url": api_url,
                "api_token": None,
                "api_key": api_key,
            }

        return {
            "provider_name": "playwright_local",
            "api_url": None,
            "api_token": None,
            "api_key": None,
        }

    def _prompt_multi_account_source_choice(self, *, has_saved: bool) -> str:
        print()
        print("Source for multi-account slots:")
        print("1. Use saved accounts first (auto-login/session check)")
        print("2. Choose/create each slot manually")
        if not has_saved:
            print(self._paint("No saved accounts found. Switching to manual slot setup.", YELLOW))
            return "2"
        return input("Select source [1-2]: ").strip() or "1"

    def _resolve_account_provider_settings(
        self,
        *,
        preset: dict[str, Any] | None,
    ) -> tuple[str, str | None, str | None, str | None]:
        if preset is not None:
            provider_name = str(preset.get("provider_name") or "playwright_local")
            return (
                provider_name,
                preset.get("api_url"),
                preset.get("api_token"),
                preset.get("api_key"),
            )

        print("Account type:")
        print("1. Simple account")
        print("2. Anti-detect account")
        account_type = input("Select type [1-2]: ").strip() or "1"
        if account_type != "2":
            if account_type != "1":
                print(self._paint("Unknown type. Falling back to a simple account.", YELLOW))
            return "playwright_local", None, None, None

        provider_name = self._prompt_provider_name()
        api_url = self._prompt_provider_api_url(provider_name)
        api_token, api_key = self._prompt_provider_secret(provider_name)
        return provider_name, api_url, api_token, api_key

    def _ensure_account_session_for_slot(self, *, slot_index: int, account_path: Path) -> None:
        print(
            self._paint(
                f"Preparing account #{slot_index}: {account_path}",
                BLUE,
            )
        )
        result = self._service.ensure_account_session(account_path=account_path)
        if not result.success:
            raise TikTokClientError(
                f"Account #{slot_index} is not ready: {result.details}"
            )
        print(
            self._paint(
                f"Account #{slot_index} is ready ({result.provider_name}).",
                GREEN,
            )
        )

    def _prompt_provider_name(self) -> str:
        print("Anti-detect provider:")
        print("1. Dolphin")
        print("2. AdsPower")
        provider_choice = input("Select provider [1-2]: ").strip() or "1"
        if provider_choice == "2":
            return "adspower"
        return "dolphin_anty"

    def _prompt_provider_api_url(self, provider_name: str) -> str | None:
        if provider_name == "dolphin_anty":
            default_api_url = "http://127.0.0.1:3001"
            raw_value = input(f"Dolphin API URL [{default_api_url}]: ").strip()
            return raw_value or default_api_url

        default_api_url = "http://127.0.0.1:50325"
        raw_value = input(f"AdsPower API URL [{default_api_url}]: ").strip()
        return raw_value or default_api_url

    def _prompt_provider_secret(self, provider_name: str) -> tuple[str | None, str | None]:
        if provider_name == "dolphin_anty":
            raw_value = input("Dolphin token [Enter = use env DOLPHIN_ANTY_TOKEN]: ").strip()
            if not raw_value and not os.getenv("DOLPHIN_ANTY_TOKEN", "").strip():
                print(
                    self._paint(
                        "Token not provided. Set DOLPHIN_ANTY_TOKEN before running this account.",
                        YELLOW,
                    )
                )
            return raw_value or None, None

        raw_value = input("AdsPower API key [Enter = use env ADSPOWER_API_KEY]: ").strip()
        if not raw_value and not os.getenv("ADSPOWER_API_KEY", "").strip():
            print(
                self._paint(
                    "API key not provided. Set ADSPOWER_API_KEY before running this account.",
                    YELLOW,
                )
            )
        return None, raw_value or None

    def _print_provider_secret_hint(
        self,
        provider_name: str,
        config_path: Path,
        *,
        api_token: str | None,
        api_key: str | None,
    ) -> None:
        if provider_name == "dolphin_anty" and not api_token:
            print(
                self._paint(
                    f"Token placeholder: set DOLPHIN_ANTY_TOKEN or edit browser_provider.api_token in {config_path}.",
                    BLUE,
                )
            )
        elif provider_name == "adspower" and not api_key:
            print(
                self._paint(
                    f"Key placeholder: set ADSPOWER_API_KEY or edit browser_provider.api_key in {config_path}.",
                    BLUE,
                )
            )

    @staticmethod
    def _prompt_required_text(prompt: str) -> str:
        raw_value = input(f"{prompt}: ").strip()
        if raw_value:
            return raw_value
        raise ValueError(f"{prompt} is required.")

    @staticmethod
    def _print_section(title: str) -> None:
        print()
        print(f"{MAGENTA}{BOLD}{title}{RESET}")
        print(f"{CYAN}{'-' * max(len(title), 16)}{RESET}")

    def _paint(self, value: str, color: str, *, bold: bool = False) -> str:
        if not self._use_colors:
            return value
        prefix = f"{BOLD}{color}" if bold else color
        return f"{prefix}{value}{RESET}"

    @staticmethod
    def _detect_color_support() -> bool:
        if os.getenv("NO_COLOR"):
            return False
        return bool(getattr(sys.stdout, "isatty", lambda: False)())
