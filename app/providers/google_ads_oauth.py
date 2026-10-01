"""Google Ads OAuth client adapter (Connections tab category `google_ads`).

This is not a data provider: it is the OAuth *client* configuration used to
authorise a Google Ads account for Keyword Planner research. Keeping it in the
provider table means the OAuth client is configured per project through the
Connections tab exactly like every other connection, rather than through
application-level environment variables.
"""

from __future__ import annotations

from typing import Any

from app.providers.base import PermanentError


class GoogleAdsOAuthProvider:
    category = "google_ads"
    provider_name = "google_ads"
    PROVIDER_META: dict[str, Any] = {
        "description": "Google Ads OAuth client used for Keyword Planner research",
        "requires_api_key": True,
        "supports_model_listing": False,
        "configuration_fields": [
            "client_id",
            "redirect_uri",
            "api_version",
            "login_customer_id",
        ],
        "secret_fields": ["client_secret", "developer_token"],
    }

    def __init__(
        self,
        *,
        client_id: str = "",
        client_secret: str = "",
        redirect_uri: str = "",
        api_version: str = "v25",
        login_customer_id: str = "",
        developer_token: str = "",
        timeout: float = 45.0,
    ) -> None:
        if not client_id or not client_secret:
            raise PermanentError(
                "Google Ads needs a client ID and client secret. "
                "Add a Google Ads connection on the Connections tab."
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.api_version = api_version or "v25"
        self.login_customer_id = login_customer_id
        self.developer_token = developer_token
        self.timeout = timeout
        self.provider_name = type(self).provider_name

    async def ping(self) -> bool:
        """Report whether the credentials are present.

        Deliberately performs no network call: a live check would require a
        Google Ads customer and would make the Connections "Test" button
        dependent on Google's uptime.
        """
        return bool(self.client_id and self.client_secret)

    async def aclose(self) -> None:
        return None
