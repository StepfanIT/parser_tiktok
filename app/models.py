from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TikTokAccountConfig:
    name: str
    storage_state_path: Path
    user_data_dir: Path
    browser_type: str = "chromium"
    browser_channel: str | None = None
    headless: bool = False
    slow_mo_ms: int = 150
    login_url: str = "https://www.tiktok.com/login"
    bootstrap_login_if_missing: bool = True

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
            browser_type=str(payload.get("browser_type", "chromium")),
            browser_channel=payload.get("browser_channel"),
            headless=bool(payload.get("headless", False)),
            slow_mo_ms=int(payload.get("slow_mo_ms", 150)),
            login_url=str(payload.get("login_url", "https://www.tiktok.com/login")),
            bootstrap_login_if_missing=bool(payload.get("bootstrap_login_if_missing", True)),
        )


@dataclass(frozen=True)
class ScrapedComment:
    comment_id: str
    author_username: str
    author_display_name: str
    text: str
    likes: int | None = None
    published_at: str | None = None


@dataclass(frozen=True)
class OutgoingComment:
    order: int
    video_url: str
    text: str
    delay_seconds: int


@dataclass(frozen=True)
class SendResult:
    outgoing_comment: OutgoingComment
    success: bool
    details: str
