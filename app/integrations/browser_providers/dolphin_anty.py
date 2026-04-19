from __future__ import annotations

from app.integrations.browser_providers.base import (
    BaseLocalApiProvider,
    BrowserLaunchResult,
    BrowserProviderError,
)


class DolphinAntyProvider(BaseLocalApiProvider):
    provider_name = "dolphin_anty"
    default_api_url = "http://127.0.0.1:3001"

    def health_check(self) -> str:
        token = self._config.resolved_api_token()
        if self._config.require_auth and not token:
            raise BrowserProviderError(
                "Dolphin Anty requires browser_provider.api_token or browser_provider.api_token_env."
            )

        if token:
            payload = self._request(
                "/v1.0/auth/login-with-token",
                method="POST",
                payload={"token": token},
            )
            if not payload.get("success"):
                raise BrowserProviderError("Dolphin Anty token authorization was rejected.")
            return "Dolphin Anty local API is reachable and token authorization succeeded."

        return "Dolphin Anty local API is configured without token auth."

    def launch_profile(self) -> BrowserLaunchResult:
        if self._config.require_auth:
            self.health_check()

        path = f"/v1.0/browser_profiles/{self.profile_id}/start?automation=1"
        if self._config.headless:
            path += "&headless=1"

        payload = self._request(path)
        if not payload.get("success"):
            raise BrowserProviderError("Dolphin Anty failed to start the requested profile.")

        automation = payload.get("automation") or {}
        debug_port = int(automation["port"])
        ws_endpoint = str(automation.get("wsEndpoint") or "").strip() or None
        return BrowserLaunchResult(
            cdp_endpoint=f"http://127.0.0.1:{debug_port}",
            profile_id=self.profile_id,
            debug_port=debug_port,
            ws_endpoint=ws_endpoint,
        )

    def stop_profile(self) -> None:
        self._request(f"/v1.0/browser_profiles/{self.profile_id}/stop")
