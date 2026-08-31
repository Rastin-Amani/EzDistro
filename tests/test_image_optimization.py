"""ImageOptimizationService: format targets, downscale-only, corrupt input."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.providers.base import PermanentError
from app.services.images import ImageOptimizationService


def _png(width: int, height: int, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buf, format=fmt)
    return buf.getvalue()


def test_optimize_webp_downscales_and_strips():
    service = ImageOptimizationService(max_width=1000)
    out, mime, w, h, small = service.optimize(_png(2000, 1000), target_format="webp")
    assert mime == "image/webp"
    assert w == 1000 and h == 500  # downscaled proportionally
    img = Image.open(io.BytesIO(out))
    assert img.format == "WEBP"
    assert not img.info.get("exif")  # metadata stripped
    assert small is not None and len(small) > 0  # small variant generated


def test_optimize_never_upscales():
    service = ImageOptimizationService(max_width=4000)
    out, mime, w, h, small = service.optimize(_png(600, 400), target_format="webp")
    assert (w, h) == (600, 400)
    assert small is None  # below small_width → no variant


def test_optimize_jpeg_target_converts_rgba():
    service = ImageOptimizationService()
    buf = io.BytesIO()
    Image.new("RGBA", (300, 200), (1, 2, 3, 255)).save(buf, format="PNG")
    out, mime, w, h, small = service.optimize(buf.getvalue(), target_format="jpeg")
    assert mime == "image/jpeg"
    img = Image.open(io.BytesIO(out))
    assert img.mode == "RGB"  # alpha flattened for jpeg


def test_optimize_corrupt_input_raises():
    service = ImageOptimizationService()
    with pytest.raises(PermanentError):
        service.optimize(b"garbage-not-an-image", target_format="webp")
