"""Product profile + geo option lists (settings Localization & brand)."""

from __future__ import annotations

from app.domain.geo import country_options, locale_options
from app.domain.product_profile import (
    format_product_profile,
    normalize_profile,
    product_footer_html,
)


def test_country_options_include_known_codes_and_sort():
    options = country_options()
    codes = {code for code, _ in options}
    assert {"US", "DE", "IR"}.issubset(codes)
    assert options
    # sorted by label
    labels = [label for _, label in options]
    assert labels == sorted(labels)


def test_country_options_preserve_legacy_free_text():
    options = country_options("Iran")
    assert options[0] == ("Iran", "Iran")
    assert any(code == "IR" for code, _ in options)


def test_locale_options_include_common_tags():
    tags = {tag for tag, _ in locale_options()}
    assert {"en-US", "fa-IR", "de-DE"}.issubset(tags)
    assert locale_options("fa_IR")[0][0] == "fa_IR"  # legacy value kept


def test_normalize_profile_bounds_and_keeps_shape():
    profile = normalize_profile(
        {
            "brand": "Acme",
            "summary": "Sells widgets.",
            "products": [
                {"name": "Widget", "description": "A widget", "differentiators": ["fast"]}
            ],
            "value_props": ["reliable"],
            "keywords": ["widget"],
            "unexpected": "dropped",
        }
    )
    assert profile["brand"] == "Acme"
    assert profile["products"][0]["name"] == "Widget"
    assert "unexpected" not in profile


def test_format_product_profile_reads_wrapper_and_direct():
    stored = {"status": "ready", "profile": {"brand": "Acme", "summary": "Sells widgets."}}
    text = format_product_profile(stored)
    assert "BRAND / PRODUCT CONTEXT" in text
    assert "Acme" in text and "Sells widgets." in text
    assert format_product_profile({}) == ""
    # direct shape also accepted
    assert "Acme" in format_product_profile({"brand": "Acme"})


def test_footer_html_links_and_escapes():
    stored = {"profile": {"brand": "Acme", "summary": "Sells <b>widgets</b> & more."}}
    footer = product_footer_html(stored, "https://acme.test/?a=1&b=2")
    assert "About Acme" in footer
    assert "&lt;b&gt;" in footer
    assert 'href="https://acme.test/?a=1&amp;b=2"' in footer
    assert product_footer_html({}) == ""
