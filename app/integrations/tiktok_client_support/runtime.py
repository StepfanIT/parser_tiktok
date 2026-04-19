from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from playwright.sync_api import Browser, BrowserContext


class TikTokClientError(RuntimeError):
    """Raised when TikTok automation fails."""


class TikTokLoginRequiredError(TikTokClientError):
    """Raised when the user needs to refresh or create the login session."""


class TikTokVerificationRequiredError(TikTokClientError):
    """Raised when TikTok asks for a manual verification challenge."""


@dataclass
class ManagedBrowserSession:
    context: BrowserContext
    browser: Browser | None = None
    close_callback: Callable[[], None] | None = None
    persist_storage_state: bool = True
    reuse_existing_page: bool = False
