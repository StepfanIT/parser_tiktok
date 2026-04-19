from __future__ import annotations

from app.integrations.browser_providers.base import (
    BaseLocalApiProvider,
    BrowserLaunchResult,
    BrowserProviderError,
)


class AdsPowerProvider(BaseLocalApiProvider):
    provider_name = "adspower"
    default_api_url = "http://127.0.0.1:50325"

    def health_check(self) -> str:
        payload = self._request("/status", headers=self._authorization_headers())
        if payload.get("code") != 0:
            raise BrowserProviderError(
                f"AdsPower local API returned an unhealthy status: {payload!r}"
            )
        return "AdsPower local API is reachable."

    def launch_profile(self) -> BrowserLaunchResult:
        payload = self._request(
            "/api/v2/browser-profile/start",
            method="POST",
            payload={
                "profile_id": self.profile_id,
                "headless": "1" if self._config.headless else "0",
                "launch_args": list(self._config.launch_args),
            },
            headers=self._authorization_headers(),
        )
        if payload.get("code") != 0:
            raise BrowserProviderError(
                f"AdsPower failed to start the requested profile: {payload.get('msg') or payload!r}"
            )

        data = payload.get("data") or {}
        ws = data.get("ws") or {}
        cdp_endpoint = str(ws.get("puppeteer") or "").strip()
        if not cdp_endpoint:
            raise BrowserProviderError("AdsPower did not return a Puppeteer/CDP endpoint.")

        debug_port_raw = data.get("debug_port")
        debug_port = int(debug_port_raw) if str(debug_port_raw or "").isdigit() else None
        return BrowserLaunchResult(
            cdp_endpoint=cdp_endpoint,
            profile_id=self.profile_id,
            debug_port=debug_port,
            ws_endpoint=cdp_endpoint,
        )

    def stop_profile(self) -> None:
        payload = self._request(
            "/api/v2/browser-profile/stop",
            method="POST",
            payload={"profile_id": self.profile_id},
            headers=self._authorization_headers(),
        )
        if payload.get("code") not in (None, 0):
            raise BrowserProviderError(
                f"AdsPower failed to stop the requested profile: {payload.get('msg') or payload!r}"
            )

    def _authorization_headers(self) -> dict[str, str]:
        api_key = self._config.resolved_api_key()
        if self._config.require_auth and not api_key:
            raise BrowserProviderError(
                "AdsPower requires browser_provider.api_key or browser_provider.api_key_env."
            )

        if not api_key:
            return {}
        return {"Authorization": f"Bearer {api_key}"}
