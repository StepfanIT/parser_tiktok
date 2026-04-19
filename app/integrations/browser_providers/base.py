from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from app.models import BrowserProviderConfig


class BrowserProviderError(RuntimeError):
    """Raised when an anti-detect browser provider request fails."""


@dataclass(frozen=True)
class BrowserLaunchResult:
    cdp_endpoint: str
    profile_id: str
    debug_port: int | None = None
    ws_endpoint: str | None = None


class BaseLocalApiProvider:
    provider_name = "base"
    default_api_url = ""

    def __init__(
        self,
        config: BrowserProviderConfig,
        logger: logging.Logger,
    ) -> None:
        self._config = config
        self._logger = logger

    @property
    def api_url(self) -> str:
        return (self._config.api_url or self.default_api_url).rstrip("/")

    @property
    def profile_id(self) -> str:
        if not self._config.profile_id:
            raise BrowserProviderError(
                f"{self.provider_name} provider requires browser_provider.profile_id in the account config."
            )
        return self._config.profile_id

    def health_check(self) -> str:
        raise NotImplementedError

    def launch_profile(self) -> BrowserLaunchResult:
        raise NotImplementedError

    def stop_profile(self) -> None:
        raise NotImplementedError

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_seconds: int = 15,
    ) -> dict[str, Any]:
        url = urljoin(f"{self.api_url}/", path.lstrip("/"))
        request_headers = {"Accept": "application/json"}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)

        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(url=url, data=body, method=method, headers=request_headers)
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw_body = response.read().decode("utf-8", errors="replace")
        except HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            raise BrowserProviderError(
                f"{self.provider_name} API request failed with HTTP {error.code}: {details or error.reason}"
            ) from error
        except URLError as error:
            raise BrowserProviderError(
                f"{self.provider_name} API is unavailable at {url}: {error.reason}"
            ) from error

        if not raw_body.strip():
            return {}

        try:
            return json.loads(raw_body)
        except json.JSONDecodeError as error:
            raise BrowserProviderError(
                f"{self.provider_name} API returned non-JSON data for {url}: {raw_body[:200]}"
            ) from error
