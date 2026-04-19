from __future__ import annotations

import logging

from app.integrations.browser_providers.adspower import AdsPowerProvider
from app.integrations.browser_providers.base import BaseLocalApiProvider, BrowserProviderError
from app.integrations.browser_providers.dolphin_anty import DolphinAntyProvider
from app.models import BrowserProviderConfig


def build_provider_client(
    config: BrowserProviderConfig,
    logger: logging.Logger,
) -> BaseLocalApiProvider | None:
    provider_name = config.name.strip().lower()
    if provider_name in {"", "local", "playwright_local", "playwright"}:
        return None
    if provider_name == "dolphin_anty":
        return DolphinAntyProvider(config, logger)
    if provider_name == "adspower":
        return AdsPowerProvider(config, logger)

    raise BrowserProviderError(f"Unsupported browser provider: {config.name}")
