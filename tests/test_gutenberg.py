"""Gutenberg block conversion (publish-time formatting)."""

from __future__ import annotations

from app.domain.gutenberg import to_gutenberg_blocks


def test_wraps_core_blocks_and_keeps_text():
    out = to_gutenberg_blocks("<h2>Title</h2><p>Intro &amp; more</p><ul><li>a</li></ul>")
    assert '<!-- wp:heading {"level":2} -->' in out
    assert "<!-- wp:paragraph -->" in out
    assert "<!-- wp:list -->" in out
    assert "<p>Intro &amp; more</p>" in out
    assert "<li>a</li>" in out


def test_table_and_quote_get_theme_classes():
    out = to_gutenberg_blocks("<table><tr><td>x</td></tr></table><blockquote><p>q</p></blockquote>")
    assert "<!-- wp:table -->" in out and "wp-block-table" in out
    assert "<!-- wp:quote -->" in out and "wp-block-quote" in out


def test_captioned_and_bare_images_become_image_blocks():
    captioned = to_gutenberg_blocks(
        '<figure><img src="u" alt="a"><figcaption>cap</figcaption></figure>'
    )
    assert "<!-- wp:image -->" in captioned
    assert 'class="wp-block-image"' in captioned
    assert 'class="wp-element-caption"' in captioned

    bare = to_gutenberg_blocks('<img src="u" alt="a" decoding="async">')
    assert "<!-- wp:image -->" in bare
    assert '<img src="u" alt="a" decoding="async">' in bare


def test_related_heading_and_list_grouped():
    out = to_gutenberg_blocks('<h2>Related</h2><ul><li><a href="/x">x</a></li></ul>')
    assert "<!-- wp:group" in out
    assert "ezdistro-related" in out
    assert "<!-- wp:list -->" in out


def test_empty_input_is_empty():
    assert to_gutenberg_blocks("") == ""
