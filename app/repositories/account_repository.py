import json
from pathlib import Path

from app.config import AppConfig
from app.models import TikTokAccountConfig


class AccountRepository:
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def list_account_paths(self) -> list[Path]:
        if not self._config.accounts_dir.exists():
            return []

        paths = [
            path
            for path in sorted(self._config.accounts_dir.glob("*.json"))
            if path.is_file() and "storage_state" not in path.name.lower()
        ]
        return paths

    def load_account(self, account_path: Path | None = None) -> TikTokAccountConfig:
        target_path = account_path or self._config.default_account_path
        if not target_path.is_absolute():
            target_path = self._config.project_root / target_path

        if not target_path.exists():
            raise FileNotFoundError(
                f"Account config not found: {target_path}. "
                "Fill in data/accounts/main_account.json first."
            )

        with target_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)

        return TikTokAccountConfig.from_dict(payload, self._config.project_root)

    def load_accounts(self, account_paths: list[Path] | None = None) -> list[TikTokAccountConfig]:
        paths = account_paths or [self._config.default_account_path]
        return [self.load_account(path) for path in paths]
