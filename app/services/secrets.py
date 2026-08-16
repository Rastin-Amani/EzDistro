"""Secrets service — Fernet encryption for credentials at rest.

Never log ciphertext or plaintext; the UI only ever sees `masked` previews.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class SecretsService:
    def __init__(self, key: bytes | None = None) -> None:
        self._fernet = Fernet(self._normalize_key(key))

    @staticmethod
    def _normalize_key(key: bytes | None) -> bytes:
        raw = key if key is not None else settings.effective_secrets_key
        if raw is None or raw == b"":
            raise RuntimeError("SECRETS_KEY is not configured")
        # Fernet expects the 44-char urlsafe base64 encoding of a 32-byte key.
        # Accept either that form or a raw 32-byte key (encode it on the spot).
        if len(raw) == 44:
            return raw
        return base64.urlsafe_b64encode(raw)

    @staticmethod
    def mask(secret: str) -> str:
        """Human-safe preview: keep first 4 + last 4 chars; never the full value."""
        if not secret:
            return ""
        if len(secret) <= 10:
            return "•" * len(secret)
        return f"{secret[:4]}…{secret[-4:]}"

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            return ""
        try:
            return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("credential cannot be decrypted — SECRETS_KEY changed?") from exc


_secrets_service: SecretsService | None = None


def get_secrets_service() -> SecretsService:
    global _secrets_service
    if _secrets_service is None:
        _secrets_service = SecretsService()
    return _secrets_service
