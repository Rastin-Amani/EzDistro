from app.services.secrets import SecretsService


def test_encrypt_decrypt_roundtrip():
    svc = SecretsService(b"0123456789abcdef0123456789abcdef")
    secret = "sk-proj-SUPER-SECRET-KEY-123456"
    encrypted = svc.encrypt(secret)
    assert encrypted != secret
    assert secret not in encrypted
    assert svc.decrypt(encrypted) == secret


def test_empty_value_passthrough():
    svc = SecretsService(b"0123456789abcdef0123456789abcdef")
    assert svc.encrypt("") == ""
    assert svc.decrypt("") == ""


def test_wrong_key_fails_loudly():
    import pytest

    svc = SecretsService(b"0123456789abcdef0123456789abcdef")
    other = SecretsService(b"fedcba9876543210fedcba9876543210")
    encrypted = svc.encrypt("value")
    with pytest.raises(ValueError):
        other.decrypt(encrypted)


def test_mask():
    svc = SecretsService(b"0123456789abcdef0123456789abcdef")
    assert svc.mask("") == ""
    assert svc.mask("short") == "•••••"
    assert svc.mask("sk-proj-ABCDEFGH1234") == "sk-p…1234"
    assert "ABCDEFGH" not in svc.mask("sk-proj-ABCDEFGH1234")
