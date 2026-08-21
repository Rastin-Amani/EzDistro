import pytest

from app.domain.validation import UnsafeUrlError, normalize_base_url, validate_url


def test_normalize_base_url_prepends_https_to_scheme_less_urls():
    assert normalize_base_url("api.openai.com/v1") == "https://api.openai.com/v1"
    assert normalize_base_url("localhost:11434/v1") == "https://localhost:11434/v1"
    assert normalize_base_url("  api.example.com  ") == "https://api.example.com"


def test_normalize_base_url_preserves_explicit_schemes_and_empty():
    assert normalize_base_url("https://api.openai.com/v1") == "https://api.openai.com/v1"
    assert normalize_base_url("http://localhost:6333") == "http://localhost:6333"
    assert normalize_base_url("") == ""
    assert normalize_base_url(None) == ""


def test_rejects_bad_schemes():
    with pytest.raises(UnsafeUrlError):
        validate_url("file:///etc/passwd")
    with pytest.raises(UnsafeUrlError):
        validate_url("javascript:alert(1)")
    with pytest.raises(UnsafeUrlError):
        validate_url("ftp://example.com/x")
    with pytest.raises(UnsafeUrlError):
        validate_url("")


def test_rejects_private_hosts():
    for host in ("127.0.0.1", "localhost", "10.0.0.5", "192.168.1.1", "169.254.0.1"):
        with pytest.raises(UnsafeUrlError):
            validate_url(f"http://{host}:6333", allow_private=False)


def test_accepts_public_host_when_allowed():
    url = validate_url("http://example.com", allow_private=False)
    assert url == "http://example.com"


def test_allow_private_networks_flag():
    url = validate_url("http://localhost:6333", allow_private=True)
    assert url == "http://localhost:6333"
