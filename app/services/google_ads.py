"""Google Ads OAuth + connection service.

The OAuth *client* credentials are application-level (env: GOOGLE_ADS_CLIENT_ID /
GOOGLE_ADS_CLIENT_SECRET / GOOGLE_ADS_REDIRECT_URI) — users never paste them. Each
user's refresh token is Fernet-encrypted in `google_ads_connections.refreshTokenEnc`
and never leaves the server: the browser only ever sees an account label.

Developer tokens no longer exist as an onboarding step (sunset 2026-09-09); access
level rides on the Google Cloud project, so no user-facing token field is offered.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from structlog import get_logger

from app.config import settings
from app.providers.base import AdsCustomer, PermanentError, TransientError
from app.providers.google_ads import GoogleAdsClient
from app.providers.http import raise_for_provider, with_retry
from app.repositories.research import GoogleAdsConnectionRepo, GoogleAdsCustomerRepo
from app.services.secrets import get_secrets_service

logger = get_logger(__name__)

GOOGLE_ADS_SCOPE = "https://www.googleapis.com/auth/adwords"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
STATE_TTL_SECONDS = 900

# Token-endpoint errors -> a sentence a human can act on.
_TOKEN_ERRORS = {
    "invalid_grant": "Google Ads access was revoked or expired. Reconnect Google Ads.",
    "invalid_client": "Google Ads client credentials are misconfigured. Contact the administrator.",
    "redirect_uri_mismatch": (
        "The redirect URI does not match the Google Cloud OAuth client. Contact the administrator."
    ),
    "access_denied": "You declined the Google Ads permission request.",
    "invalid_request": "Google rejected the OAuth request. Try connecting again.",
}


def client_configured() -> bool:
    return bool(settings.google_ads_configured)


def redirect_uri(request_base: str) -> str:
    """The configured redirect URI, falling back to one derived from the request."""
    if settings.google_ads_redirect_uri:
        return settings.google_ads_redirect_uri
    return request_base.rstrip("/") + "/projects/google-ads/callback"


# ---------------------------------------------------------------------------
# Signed OAuth state (CSRF + user/project binding + expiry)
# ---------------------------------------------------------------------------
def _state_key() -> bytes:
    key = settings.effective_secrets_key
    return key if isinstance(key, bytes) else str(key).encode()


def make_state(*, user_id: str, project_id: str = "") -> str:
    payload = {"u": user_id, "p": project_id, "t": int(time.time())}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hmac.new(_state_key(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def read_state(state: str) -> dict[str, Any]:
    raw, _, sig = (state or "").partition(".")
    if not raw or not sig:
        raise PermanentError("Google Ads connection request is malformed")
    expected = hmac.new(_state_key(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        raise PermanentError("Google Ads connection request could not be verified")
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except Exception as exc:  # noqa: BLE001 - any decode failure is the same answer
        raise PermanentError("Google Ads connection request is malformed") from exc
    if int(payload.get("t", 0)) + STATE_TTL_SECONDS < int(time.time()):
        raise PermanentError("Google Ads connection request expired. Start again.")
    return payload


def authorize_url(state: str, *, request_base: str = "") -> str:
    if not client_configured():
        raise PermanentError(
            "Google Ads is not configured on this server "
            "(GOOGLE_ADS_CLIENT_ID / SECRET / REDIRECT_URI are missing)."
        )
    params = {
        "client_id": settings.google_ads_client_id,
        "redirect_uri": redirect_uri(request_base),
        "response_type": "code",
        "scope": GOOGLE_ADS_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------
def _human_token_error(data: dict[str, Any], status: int) -> str:
    code = str(data.get("error") or "")
    return _TOKEN_ERRORS.get(code) or f"Google sign-in failed (HTTP {status})."


async def _post_token(payload: dict[str, str]) -> dict[str, Any]:
    async def call() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(TOKEN_URL, data=payload)
        if response.status_code >= 400:
            try:
                body = response.json()
            except Exception:  # noqa: BLE001
                body = {}
            message = _human_token_error(body, response.status_code)
            if response.status_code >= 500 or response.status_code == 429:
                raise TransientError(message, details={"oauth_error": body.get("error")})
            raise PermanentError(message, details={"oauth_error": body.get("error")})
        return dict(response.json())

    return await with_retry(call, attempts=2, what="google_ads token")


async def exchange_code(code: str, *, request_base: str = "") -> dict[str, Any]:
    if not code:
        raise PermanentError("Google did not return an authorization code.")
    return await _post_token(
        {
            "code": code,
            "client_id": settings.google_ads_client_id,
            "client_secret": settings.google_ads_client_secret,
            "redirect_uri": redirect_uri(request_base),
            "grant_type": "authorization_code",
        }
    )


async def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    return await _post_token(
        {
            "refresh_token": refresh_token,
            "client_id": settings.google_ads_client_id,
            "client_secret": settings.google_ads_client_secret,
            "grant_type": "refresh_token",
        }
    )


async def fetch_identity(access_token: str) -> dict[str, Any]:
    """Best-effort Google account label for the connection list (never required)."""

    async def call() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
        if response.status_code >= 400:
            raise_for_provider(response, what="google account identity")
        return dict(response.json())

    try:
        return await call()
    except Exception as exc:  # noqa: BLE001 - a label is optional, never fatal
        logger.warning("google_ads.identity_lookup_failed", error=str(exc))
        return {}


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------
async def connect(pb: Any, *, user_id: str, code: str, request_base: str = "") -> dict[str, Any]:
    """Exchange the callback code and store the connection (encrypted at rest)."""
    tokens = await exchange_code(code, request_base=request_base)
    refresh = str(tokens.get("refresh_token") or "")
    if not refresh:
        raise PermanentError(
            "Google did not return a refresh token. Remove EZDistro's access in your "
            "Google account and connect again."
        )
    identity = await fetch_identity(str(tokens.get("access_token") or ""))
    connection = GoogleAdsConnectionRepo(pb).upsert(
        user=user_id,
        google_account_id=str(identity.get("sub") or identity.get("email") or user_id),
        email=str(identity.get("email") or ""),
        display_name=str(identity.get("name") or identity.get("email") or "Google Ads"),
        refresh_token_enc=get_secrets_service().encrypt(refresh),
        token_metadata={
            "scope": tokens.get("scope", ""),
            "token_type": tokens.get("token_type", ""),
            "obtainedAt": int(time.time()),
        },
        created_by=user_id,
    )
    logger.info("google_ads.connected", connection_id=connection.get("id"), user_id=user_id)
    return connection


def connection_for_user(pb: Any, user_id: str) -> dict[str, Any] | None:
    return GoogleAdsConnectionRepo(pb).for_user(user_id)


def disconnect(pb: Any, connection_id: str) -> None:
    """Drop the stored credential. Cascade removes the cached customer list."""
    GoogleAdsConnectionRepo(pb).delete(connection_id)
    logger.info("google_ads.disconnected", connection_id=connection_id)


def client_for_connection(
    connection: dict[str, Any], *, timeout: float = 60.0, attempts: int = 3
) -> GoogleAdsClient:
    """Build a Google Ads client from a stored connection (decrypts the token)."""
    if not connection:
        raise PermanentError("Connect Google Ads before running research.")
    ciphertext = str(connection.get("refreshTokenEnc") or "")
    if not ciphertext:
        raise PermanentError("This Google Ads connection has no stored credential. Reconnect.")
    try:
        refresh_token = get_secrets_service().decrypt(ciphertext)
    except ValueError as exc:
        raise PermanentError(
            "The stored Google Ads credential cannot be decrypted (SECRETS_KEY changed). "
            "Reconnect Google Ads."
        ) from exc
    return GoogleAdsClient(
        client_id=settings.google_ads_client_id,
        client_secret=settings.google_ads_client_secret,
        refresh_token=refresh_token,
        developer_token=settings.google_ads_developer_token,
        api_version=settings.google_ads_api_version,
        login_customer_id=settings.google_ads_login_customer_id,
        timeout=timeout,
        attempts=attempts,
        concurrency=settings.research_google_ads_concurrency,
    )


def client_for_user(pb: Any, user_id: str, **kwargs: Any) -> GoogleAdsClient:
    connection = connection_for_user(pb, user_id)
    if not connection:
        raise PermanentError("Connect Google Ads before running research.")
    return client_for_connection(connection, **kwargs)


async def discover_customers(
    pb: Any,
    connection: dict[str, Any],
    *,
    project_id: str = "",
    customer_id: str = "",
) -> list[dict[str, Any]]:
    """Fetch accessible Google Ads customers and cache them locally.

    `customer_id` limits the query to one manager/customer (used for the fresh
    account listing after OAuth); otherwise every accessible customer is probed.
    """
    client = client_for_connection(connection)
    try:
        remote: list[AdsCustomer] = await client.list_customers(customer_id or None)
    finally:
        await client.aclose()

    repo = GoogleAdsCustomerRepo(pb)
    out: list[dict[str, Any]] = []
    for customer in remote:
        payload: dict[str, Any] = {
            "connection": connection["id"],
            "customer_id": customer.customer_id,
            "descriptive_name": customer.descriptive_name,
            "currency_code": customer.currency_code,
            "time_zone": customer.time_zone,
            "manager_customer_id": customer.manager_customer_id,
            "is_manager": customer.is_manager,
            "accessible": True,
        }
        if project_id:
            payload["project"] = project_id
        out.append(repo.upsert(**payload))
    GoogleAdsConnectionRepo(pb).mark_verified(connection["id"])
    logger.info(
        "google_ads.customers_discovered",
        connection_id=connection.get("id"),
        count=len(out),
    )
    return out


def attach_customer_to_project(
    pb: Any, *, project_id: str, customer_record_id: str
) -> dict[str, Any]:
    """Bind a discovered customer to a project (idempotent)."""
    repo = GoogleAdsCustomerRepo(pb)
    record = repo.get(customer_record_id)
    if record is None:
        raise PermanentError("That Google Ads customer was not found.")
    if record.get("project") == project_id:
        return record
    return repo.update(record["id"], {"project": project_id})
