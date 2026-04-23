from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BrowserProviderConfig:
    name: str = "playwright_local"
    profile_id: str | None = None
    api_url: str | None = None
    api_token: str | None = None
    api_token_env: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    require_auth: bool = True
    headless: bool = False
    launch_args: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "BrowserProviderConfig":
        if not payload:
            return cls()

        launch_args = payload.get("launch_args") or ()
        if not isinstance(launch_args, (list, tuple)):
            launch_args = ()

        return cls(
            name=str(payload.get("name", "playwright_local")).strip() or "playwright_local",
            profile_id=str(payload["profile_id"]).strip() if payload.get("profile_id") else None,
            api_url=str(payload["api_url"]).strip() if payload.get("api_url") else None,
            api_token=str(payload["api_token"]).strip() if payload.get("api_token") else None,
            api_token_env=str(payload["api_token_env"]).strip() if payload.get("api_token_env") else None,
            api_key=str(payload["api_key"]).strip() if payload.get("api_key") else None,
            api_key_env=str(payload["api_key_env"]).strip() if payload.get("api_key_env") else None,
            require_auth=bool(payload.get("require_auth", True)),
            headless=bool(payload.get("headless", False)),
            launch_args=tuple(str(item).strip() for item in launch_args if str(item).strip()),
        )

    def resolved_api_token(self) -> str | None:
        return self._resolve_secret(self.api_token, self.api_token_env)

    def resolved_api_key(self) -> str | None:
        return self._resolve_secret(self.api_key, self.api_key_env)

    @staticmethod
    def _resolve_secret(raw_value: str | None, env_name: str | None) -> str | None:
        if raw_value:
            return raw_value
        if env_name:
            resolved = os.getenv(env_name, "").strip()
            return resolved or None
        return None


@dataclass(frozen=True)
class TikTokAccountConfig:
    name: str
    storage_state_path: Path
    user_data_dir: Path
    tiktok_username: str | None = None
    browser_type: str = "chromium"
    browser_channel: str | None = None
    headless: bool = False
    slow_mo_ms: int = 150
    login_url: str = "https://www.tiktok.com/login"
    bootstrap_login_if_missing: bool = True
    login_username: str | None = None
    login_password: str | None = None
    login_totp_secret: str | None = None
    browser_provider: BrowserProviderConfig = field(default_factory=BrowserProviderConfig)

    @classmethod
    def from_dict(cls, payload: dict[str, Any], project_root: Path) -> "TikTokAccountConfig":
        name = str(payload.get("name", "main"))
        storage_state_raw = payload.get("storage_state_path", "data/accounts/main_storage_state.json")
        storage_state_path = Path(storage_state_raw)
        if not storage_state_path.is_absolute():
            storage_state_path = project_root / storage_state_path

        user_data_raw = payload.get("user_data_dir", f"data/accounts/{name}_user_data")
        user_data_dir = Path(user_data_raw)
        if not user_data_dir.is_absolute():
            user_data_dir = project_root / user_data_dir

        return cls(
            name=name,
            storage_state_path=storage_state_path,
            user_data_dir=user_data_dir,
            tiktok_username=str(payload["tiktok_username"]).strip()
            if payload.get("tiktok_username")
            else None,
            browser_type=str(payload.get("browser_type", "chromium")),
            browser_channel=payload.get("browser_channel"),
            headless=bool(payload.get("headless", False)),
            slow_mo_ms=int(payload.get("slow_mo_ms", 150)),
            login_url=str(payload.get("login_url", "https://www.tiktok.com/login")),
            bootstrap_login_if_missing=bool(payload.get("bootstrap_login_if_missing", True)),
            login_username=str(payload["login_username"]).strip() if payload.get("login_username") else None,
            login_password=str(payload["login_password"]).strip() if payload.get("login_password") else None,
            login_totp_secret=str(payload["login_totp_secret"]).strip()
            if payload.get("login_totp_secret")
            else None,
            browser_provider=BrowserProviderConfig.from_dict(payload.get("browser_provider")),
        )


@dataclass(frozen=True)
class ScrapedComment:
    video_url: str
    comment_id: str
    author_username: str
    author_display_name: str
    text: str
    likes: int | None = None
    published_at: str | None = None
    reply_author_usernames: tuple[str, ...] = ()
    has_account_reply: bool = False
    eligible_account_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutgoingComment:
    order: int
    video_url: str
    text: str
    delay_seconds: int
    allowed_account_names: tuple[str, ...] = ()
    target_username: str | None = None
    text_variants: tuple[str, ...] = ()

    @property
    def available_texts(self) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in self.text_variants if str(item).strip())
        if normalized:
            return normalized
        return (self.text,)


@dataclass(frozen=True)
class SendResult:
    account_name: str
    outgoing_comment: OutgoingComment
    success: bool
    details: str
    status: str = "unknown"


@dataclass(frozen=True)
class PublishOutcome:
    success: bool
    status: str
    details: str


@dataclass(frozen=True)
class SendBehaviorConfig:
    daily_limit_min: int
    daily_limit_max: int
    hourly_limit_min: int
    hourly_limit_max: int
    batch_size_min: int
    batch_size_max: int
    batch_pause_min_seconds: int
    batch_pause_max_seconds: int
    comment_delay_choices: tuple[int, ...]


@dataclass(frozen=True)
class AccountHealthCheckResult:
    account_name: str
    provider_name: str
    success: bool
    details: str
    resolved_username: str | None = None
    api_url: str | None = None


@dataclass(frozen=True)
class RunAccountSummary:
    account_name: str
    provider_name: str
    health_status: str
    collected_comments: int = 0
    attempted_comments: int = 0
    successful_comments: int = 0
    failed_comments: int = 0
    statuses: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass
class AccountSendState:
    account: TikTokAccountConfig
    daily_limit: int
    hourly_limit: int
    sent_today: int = 0
    sent_this_hour: int = 0
    batch_index: int = 0
    last_batch_at_monotonic: float = 0.0
    next_available_at_monotonic: float = 0.0
    day_window_key: str = ""
    hour_window_key: str = ""

    def refresh_windows(self, *, day_key: str, hour_key: str) -> None:
        if self.day_window_key != day_key:
            self.day_window_key = day_key
            self.sent_today = 0

        if self.hour_window_key != hour_key:
            self.hour_window_key = hour_key
            self.sent_this_hour = 0

    @property
    def daily_remaining(self) -> int:
        return max(self.daily_limit - self.sent_today, 0)

    @property
    def hourly_remaining(self) -> int:
        return max(self.hourly_limit - self.sent_this_hour, 0)
