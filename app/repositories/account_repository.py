import json
import re
from pathlib import Path
from typing import Any

from app.config import AppConfig
from app.models import TikTokAccountConfig


class AccountRepository:
    DEFAULT_DOLPHIN_API_URL = "http://127.0.0.1:3001"
    DEFAULT_ADSPOWER_API_URL = "http://127.0.0.1:50325"

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def list_account_paths(self) -> list[Path]:
        if not self._config.accounts_dir.exists():
            return []

        candidates = [
            path
            for path in sorted(self._config.accounts_dir.rglob("*.json"))
            if path.is_file()
            and "storage_state" not in path.name.lower()
            and not path.name.lower().endswith(".local.json")
        ]
        return [path for path in candidates if self._looks_like_account_config(path)]

    def resolve_account_identifier(self, identifier: str) -> Path | None:
        raw_value = str(identifier or "").strip()
        if not raw_value:
            return None

        direct_path = Path(raw_value)
        if not direct_path.is_absolute():
            direct_path = self._config.project_root / direct_path
        if direct_path.exists() and self._looks_like_account_config(direct_path):
            return direct_path

        normalized = raw_value.lower()
        for path in self.list_account_paths():
            if str(path).lower() == normalized:
                return path
            if path.stem.lower() == normalized:
                return path
            if path.parent.name.lower() == normalized:
                return path

            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            account_name = str(payload.get("name") or "").strip().lower()
            if account_name and account_name == normalized:
                return path
        return None

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

    def suggest_account_name(self, slot_index: int) -> str:
        base_name = f"account_{slot_index:02d}"
        return self._allocate_unique_name(base_name)

    def create_account_config(
        self,
        *,
        account_name: str,
        provider_name: str = "playwright_local",
        profile_id: str | None = None,
        api_url: str | None = None,
        api_token: str | None = None,
        api_key: str | None = None,
        login_username: str | None = None,
        login_password: str | None = None,
        login_totp_secret: str | None = None,
    ) -> Path:
        normalized_account_name = self._allocate_unique_name(account_name)
        folder_name = self._allocate_unique_folder_name(normalized_account_name)
        account_dir = self._config.accounts_dir / folder_name
        account_dir.mkdir(parents=True, exist_ok=True)
        config_path = account_dir / "account.json"

        payload = {
            "name": normalized_account_name,
            "storage_state_path": self._as_relative_project_path(account_dir / "storage_state.json"),
            "user_data_dir": self._as_relative_project_path(account_dir / "user_data"),
            "tiktok_username": None,
            "browser_type": "chromium",
            "browser_channel": None,
            "headless": False,
            "slow_mo_ms": 150,
            "login_url": "https://www.tiktok.com/login",
            "bootstrap_login_if_missing": True,
            "login_username": login_username,
            "login_password": login_password,
            "login_totp_secret": login_totp_secret,
            "browser_provider": self._build_browser_provider_payload(
                provider_name=provider_name,
                profile_id=profile_id,
                api_url=api_url,
                api_token=api_token,
                api_key=api_key,
            ),
        }

        config_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return config_path

    def _allocate_unique_name(self, account_name: str) -> str:
        base_name = self._normalize_account_name(account_name or "account")
        existing_names = self._load_existing_account_names()
        candidate = base_name
        suffix = 2
        while candidate.lower() in existing_names:
            candidate = f"{base_name}_{suffix}"
            suffix += 1
        return candidate

    def _allocate_unique_folder_name(self, account_name: str) -> str:
        base_folder = self._slugify(account_name)
        candidate = base_folder
        suffix = 2
        while (self._config.accounts_dir / candidate).exists():
            candidate = f"{base_folder}_{suffix}"
            suffix += 1
        return candidate

    def _load_existing_account_names(self) -> set[str]:
        names: set[str] = set()
        for path in self.list_account_paths():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            raw_name = str(payload.get("name") or "").strip()
            if raw_name:
                names.add(raw_name.lower())
        return names

    def _looks_like_account_config(self, path: Path) -> bool:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        required_keys = {"name", "storage_state_path", "user_data_dir", "browser_provider"}
        return required_keys.issubset(payload.keys())

    def _build_browser_provider_payload(
        self,
        *,
        provider_name: str,
        profile_id: str | None,
        api_url: str | None,
        api_token: str | None,
        api_key: str | None,
    ) -> dict[str, Any]:
        provider = provider_name.strip().lower() or "playwright_local"
        if provider in {"playwright_local", "playwright", "local"}:
            return {
                "name": "playwright_local",
                "profile_id": None,
                "api_url": None,
                "api_token": None,
                "api_token_env": None,
                "api_key": None,
                "api_key_env": None,
                "require_auth": True,
                "headless": False,
                "launch_args": [],
            }

        if provider == "dolphin_anty":
            return {
                "name": "dolphin_anty",
                "profile_id": profile_id,
                "api_url": api_url or self.DEFAULT_DOLPHIN_API_URL,
                "api_token": api_token or None,
                "api_token_env": None if api_token else "DOLPHIN_ANTY_TOKEN",
                "api_key": None,
                "api_key_env": None,
                "require_auth": True,
                "headless": False,
                "launch_args": [],
            }

        if provider == "adspower":
            return {
                "name": "adspower",
                "profile_id": profile_id,
                "api_url": api_url or self.DEFAULT_ADSPOWER_API_URL,
                "api_token": None,
                "api_token_env": None,
                "api_key": api_key or None,
                "api_key_env": None if api_key else "ADSPOWER_API_KEY",
                "require_auth": True,
                "headless": False,
                "launch_args": [],
            }

        raise ValueError(f"Unsupported browser provider for auto-generation: {provider_name}")

    def _as_relative_project_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self._config.project_root))
        except ValueError:
            return str(path)

    @staticmethod
    def _normalize_account_name(account_name: str) -> str:
        cleaned = re.sub(r"\s+", "_", str(account_name).strip())
        cleaned = re.sub(r"[^\w.-]+", "_", cleaned)
        cleaned = cleaned.strip("._")
        return cleaned or "account"

    @classmethod
    def _slugify(cls, account_name: str) -> str:
        return cls._normalize_account_name(account_name).lower()
