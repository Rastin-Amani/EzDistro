"""Retry horizons for work that must outlast a provider outage.

A third-party gateway blip is not an article defect. These are attempt counts
fed to the job engine, whose backoff is exponential with a 1-hour cap: past
~8 attempts one attempt is one hour, so an attempt budget is effectively a
number of hours.
"""

from __future__ import annotations

from typing import Any

# Image generation is idempotent and bills nothing when it fails. 48 attempts
# is ~2 days of hourly retries — long enough for a `503 no_healthy_sellers`
# outage, which has lasted 5+ hours on the live gateway.
IMAGE_MIN_ATTEMPTS = 48

# Publishing waits for the article's images (transient retry). It must outlast
# the image horizon above, not race it.
PUBLISH_ATTEMPTS = 60


def image_job_attempts(configured: Any) -> int:
    """Transient-error attempt budget for one image generation job."""
    try:
        value = int(configured or 0)
    except (TypeError, ValueError):
        value = 0
    return max(IMAGE_MIN_ATTEMPTS, value)
