"""Domain tests for the image subsystem: plan validation, fingerprints,
filenames, prompts, alt-text rules, cost estimation, error classification,
placeholders, publish gating (incl. fa/en alt text)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.images import (
    ArticleImagePlan,
    PlannedImage,
    build_negative_prompt,
    build_visual_prompt,
    classify_error,
    dims_for_aspect,
    estimate_cost,
    figure_html,
    image_filename,
    megapixels_billed,
    placeholder_keys,
    resolve_image_placeholders,
    validate_alt_text,
    validate_plan,
    validate_publishable_images,
)
from app.providers.base import PermanentError, TransientError


def make_plan(**overrides):
    cover = {
        "role": "cover",
        "prompt": "dashboard warning light close-up",
        "alt_text": "چراغ هشدار موتور روی داشبورد",
        "section_key": "",
    }
    interior = {
        "role": "interior",
        "section_key": "section-1",
        "prompt": "mechanic inspecting engine bay",
        "alt_text": "مکانیک در حال بازدید موتور خودرو",
    }
    images = [PlannedImage.model_validate({**cover, **overrides.get("cover", {})})]
    if not overrides.get("no_interior"):
        images.append(PlannedImage.model_validate({**interior, **overrides.get("interior", {})}))
    return ArticleImagePlan.model_validate({"version": 1, "style": {}, "images": images})


def to_dict_raw(plan_dict: dict):
    """Raw LLM-shaped dict (section_key naming)."""
    return plan_dict


class TestPlanValidation:
    def test_valid_plan_passes(self):
        problems = validate_plan(make_plan(), max_interior=2)
        assert problems == []

    def test_missing_cover_rejected(self):
        bad = ArticleImagePlan.model_validate(
            {
                "version": 1,
                "style": {},
                "images": [
                    {"role": "interior", "section_key": "section-1", "prompt": "p"},
                ],
            }
        )
        problems = validate_plan(bad, max_interior=2)
        assert any("cover" in p for p in problems)

    def test_duplicate_section_keys_rejected(self):
        plan = make_plan()
        second = PlannedImage.model_validate(
            {"role": "interior", "section_key": "section-1", "prompt": "x"}
        )
        plan.images.append(second)
        problems = validate_plan(plan, max_interior=4)
        assert any("section-1" in p for p in problems)

    def test_interior_requires_section_key(self):
        with pytest.raises(ValidationError):
            PlannedImage.model_validate({"role": "interior", "section_key": "", "prompt": "p"})

    def test_interior_bad_key_format(self):
        with pytest.raises(ValidationError):
            PlannedImage.model_validate(
                {"role": "interior", "section_key": "section-x", "prompt": "p"}
            )

    def test_too_many_interiors_rejected(self):
        plan = make_plan()
        for i in range(2, 6):
            plan.images.append(
                PlannedImage.model_validate(
                    {"role": "interior", "section_key": f"section-{i}", "prompt": "p"}
                )
            )
        problems = validate_plan(plan, max_interior=4)
        assert any("interior" in p.lower() or "داخل" in p for p in problems)

    def test_long_prompt_rejected(self):
        plan = make_plan(cover={"role": "cover", "prompt": "x" * 5000})
        problems = validate_plan(plan, max_interior=2)
        assert problems

    def test_find_normalizes_cover(self):
        plan = make_plan()
        assert plan.find("cover", "cover") is not None
        assert plan.find("cover", "") is not None
        assert plan.find("interior", "section-1") is not None
        assert plan.find("interior", "section-99") is None

    def test_parse_llm_tolerant(self):
        raw = {
            "images": [
                {"role": "cover", "prompt": "c", "alt_text": "دltdk"},
                {"role": "interior", "section_key": "section-2", "prompt": "i"},
            ]
        }
        plan = ArticleImagePlan.parse_llm(raw, version=3, style={"tone": "مینیمال"})
        assert plan.version == 3
        assert len(plan.images) == 2

    def test_parse_llm_garbage_raises(self):
        with pytest.raises(ValueError):
            ArticleImagePlan.parse_llm({"nope": 1}, version=1, style=None)


class TestFingerprintAndFiles:
    def test_fingerprint_stable_and_sensitive(self):
        from app.domain.images import image_fingerprint

        base = dict(
            article_id="a1",
            role="cover",
            section_key="cover",
            plan_version=1,
            provider="gemini",
            model="m",
            prompt="p",
            style_config={"tone": "t"},
            width=1600,
            height=896,
        )
        f1 = image_fingerprint(**base)
        assert f1 == image_fingerprint(**base)
        assert f1 != image_fingerprint(**{**base, "prompt": "p2"})
        assert f1 != image_fingerprint(**{**base, "provider": "bfl"})
        assert len(f1) == 32

    def test_ascii_filename(self):
        name = image_filename(
            article_title="Check Engine Light Guide",
            article_id="a1b2c3d4e5",
            role="cover",
            section_key="cover",
            ext="webp",
        )
        assert name == "check-engine-light-guide-cover.webp"
        assert name.isascii()

    def test_interior_filename_includes_section(self):
        name = image_filename(
            article_title="Guide", article_id="a1b2c3d4e5", role="interior", section_key="section-2"
        )
        assert name == "guide-section-2.webp"

    def test_persian_title_falls_back_to_id(self):
        name = image_filename(
            article_title="چراغ چک خودرو",
            article_id="a1b2c3d4e5",
            role="interior",
            section_key="section-1",
        )
        assert name.isascii()
        assert name == "image-a1b2c3d4-interior.webp"

    def test_long_title_truncated(self):
        name = image_filename(
            article_title="word " * 80, article_id="a1b2c3d4e5", role="cover", section_key="cover"
        )
        assert len(name) <= 126


class TestPromptsAndDims:
    def test_visual_prompt_appends_style(self):
        prompt = build_visual_prompt("a lighthouse", {"tone": "cinematic", "palette": "cold"})
        assert prompt.startswith("a lighthouse")
        assert "cinematic" in prompt and "cold" in prompt

    def test_negative_prompt_defaults_no_text(self):
        neg = build_negative_prompt({}, "")
        assert "text" in neg and "watermark" in neg

    def test_negative_prompt_merges_style(self):
        neg = build_negative_prompt({"negative_prompt": "blurry"}, "low quality")
        assert "blurry" in neg and "low quality" in neg

    def test_dims_cover_meet_min_width(self):
        w, h = dims_for_aspect("16:9", role="cover", min_width=1200)
        assert w >= 1200
        assert w % 32 == 0 and h % 32 == 0
        assert abs(w / h - 16 / 9) < 0.05

    def test_dims_interior_4_3(self):
        w, h = dims_for_aspect("4:3", role="interior")
        assert (w, h) == (1280, 960)

    def test_dims_invalid_aspect_falls_back(self):
        w, h = dims_for_aspect("garbage", role="interior")
        assert (w, h) == (1280, 704)

    def test_megapixel_rounding(self):
        assert megapixels_billed(1920, 1080) == 3
        assert megapixels_billed(1024, 1024) == 2

    def test_estimate_cost_bfl(self):
        assert estimate_cost("bfl", 1920, 1080) == pytest.approx(3 * 0.015)

    def test_estimate_cost_bfl_usage_overrides(self):
        assert estimate_cost("bfl", 1920, 1080, {"billed_megapixels": 5}) == pytest.approx(0.075)

    def test_estimate_cost_gemini_unknown(self):
        assert estimate_cost("gemini", "gemini-3-pro-image", 1600, 896) is None


class TestErrorClassification:
    @pytest.mark.parametrize(
        "message,expected",
        [
            ("rate limit exceeded 429", "rate_limited"),
            ("request timeout after 30s", "timeout"),
            ("invalid api key 401", "authentication"),
            ("content policy violation", "content_policy"),
            ("failed to decode image", "decode_failure"),
            ("resize failed", "optimization_failure"),
            ("wordpress upload error", "wordpress_upload_failure"),
            ("connection refused", "provider_unavailable"),
            ("malformed response", "invalid_request"),
            ("weird thing happened", "unknown"),
        ],
    )
    def test_classify(self, message, expected):
        assert classify_error(message, retryable=False) == expected

    def test_retryable_categories(self):
        from app.domain.images import category_is_retryable

        assert category_is_retryable("rate_limited")
        assert category_is_retryable("timeout")
        assert not category_is_retryable("authentication")

    def test_provider_error_mapping(self):
        assert classify_error(str(TransientError("429 too many")), retryable=True) == "rate_limited"
        assert (
            classify_error(str(PermanentError("api key invalid")), retryable=False)
            == "authentication"
        )
        assert classify_error("malformed response body", retryable=True) == "invalid_response"


class TestPlaceholdersAndFigures:
    def test_placeholder_keys(self):
        html = "<p>a</p>{{IMAGE:cover}}<p>b</p>{{IMAGE:section-2}}"
        assert placeholder_keys(html) == ["cover", "section-2"]

    def test_resolve_replaces_known_removes_unknown(self):
        html = "{{IMAGE:cover}}{{IMAGE:section-1}}{{IMAGE:section-9}}"
        out = resolve_image_placeholders(
            html, {"cover": "<img>", "section-1": "<figure>x</figure>"}
        )
        assert "<img>" in out and "<figure>" in out and "section-9" not in out

    def test_figure_html_cls_and_lazy(self):
        html = figure_html(
            src="https://x/y.webp", alt="alt متنی", width=1600, height=896, caption="توضیح"
        )
        assert 'src="https://x/y.webp"' in html
        assert 'width="1600"' in html and 'height="896"' in html
        assert 'loading="lazy"' in html and 'decoding="async"' in html
        assert "<figcaption>توضیح</figcaption>" in html

    def test_figure_html_cover_eager(self):
        html = figure_html(
            src="u", alt="a", width=10, height=10, loading="eager", fetchpriority="high"
        )
        assert 'fetchpriority="high"' in html
        assert "figcaption" not in html

    def test_figures_survive_sanitize(self):
        from app.domain.sanitize import sanitize_html

        html = figure_html(src="https://x/y.webp", alt="alt", width=100, height=50, caption="کپشن")
        cleaned = sanitize_html(html)
        assert "<img" in cleaned and "figcaption" in cleaned
        assert 'alt="alt"' in cleaned


class TestAltTextRules:
    TITLE = "راهنمای کامل چراغ چک خودرو"
    PROMPT = "a close-up photo of a car dashboard warning light"

    def test_valid_fa_alt(self):
        problems = validate_alt_text(
            "چراغ هشدار موتور که روی داشبورد خودرو روشن شده است",
            article_title=self.TITLE,
            keyword="چراغ چک",
        )
        assert problems == []

    def test_valid_en_alt(self):
        problems = validate_alt_text(
            "A mechanic checking the engine bay of a modern car",
            article_title="Engine guide",
            keyword="engine",
        )
        assert problems == []

    def test_too_short(self):
        problems = validate_alt_text("کوتاه", article_title=self.TITLE)
        assert problems

    def test_too_long(self):
        problems = validate_alt_text("ت" * 250, article_title=self.TITLE)
        assert problems

    def test_title_repeat_rejected(self):
        problems = validate_alt_text(self.TITLE, article_title=self.TITLE)
        assert problems

    def test_prompt_leak_rejected(self):
        problems = validate_alt_text(self.PROMPT, article_title=self.TITLE, prompt=self.PROMPT)
        assert problems

    def test_keyword_stuffing_rejected(self):
        problems = validate_alt_text(
            "چراغ چک روشن شد، چراغ چک یعنی خطا، چراغ چک را جدی بگیرید",
            article_title=self.TITLE,
            keyword="چراغ چک",
        )
        assert problems


class TestPublishGate:
    def ready_image(self, role, section_key, **kw):
        return {
            "id": "img1",
            "role": role,
            "sectionKey": section_key,
            "status": "ready",
            "active": True,
            "optimizedFile": f"{role}.webp",
            "width": 1600,
            "height": 896,
            "altText": kw.get("alt", "توضیح جایگزین تصویر برای دسترس‌پذیری"),
            "caption": "",
        }

    def test_missing_cover_blocks(self):
        images = [self.ready_image("interior", "section-1")]
        ok, problems = validate_publishable_images(
            images, has_cover=False, allow_missing_cover=False
        )
        assert not ok and problems

    def test_missing_cover_allowed_with_override(self):
        ok, problems = validate_publishable_images([], has_cover=False, allow_missing_cover=True)
        assert ok or not any("cover" in p for p in problems)

    def test_interior_without_alt_flagged(self):
        images = [
            self.ready_image("cover", "cover"),
            self.ready_image("interior", "section-1", alt=""),
        ]
        ok, problems = validate_publishable_images(
            images, has_cover=True, allow_missing_cover=False
        )
        assert ok and problems  # warning, not blocker

    def test_not_ready_image_ignored(self):
        img = self.ready_image("cover", "cover")
        img["status"] = "failed"
        ok, problems = validate_publishable_images(
            [img], has_cover=False, allow_missing_cover=False
        )
        assert not ok  # no ready cover → blocked
