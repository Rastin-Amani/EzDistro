"""Explicit job state machine — every transition is validated here.

States (schema in docs/SCHEMA.md §7):

    pending ──claim──▶ running ──success──▶ completed
        ▲                │  │
        │                │  └──transient failure──▶ retrying ──(available_at)──▶ running
        │                └──permanent failure─────▶ failed ──manual retry──▶ retrying
        │                └──cancel requested──────▶ cancelled
        └── manual retry (retry_failed_job) ── from failed

Lease expiry (crash recovery): running with leaseExpiresAt < now is reclaimed
by another worker → running again (same state, new lease).
"""

from __future__ import annotations

# pending → running           (worker claim, atomic lease)
# retrying → running          (backoff elapsed, worker claim)
# running → completed         (success)
# running → failed            (permanent error, or retries exhausted)
# running → retrying          (transient error, attempts left)
# running → cancelled         (user cancellation acknowledged at a checkpoint)
# failed → retrying           (manual retry requested — retry_failed_job)
# running → running           (lease expiry re-claim by another worker)
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"running", "cancelled", "retrying"},
    "retrying": {"running", "cancelled"},
    "running": {"completed", "failed", "cancelled", "retrying", "running"},
    "failed": {"retrying", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}

TERMINAL = {"completed", "failed", "cancelled"}
ACTIVE = {"pending", "retrying", "running"}


class JobStateError(ValueError):
    pass


def validate_transition(current: str, target: str) -> None:
    if current == target and current != "running":
        return
    allowed = ALLOWED_TRANSITIONS.get(current)
    if allowed is None:
        raise JobStateError(f"unknown job state: {current!r}")
    if target not in allowed:
        raise JobStateError(f"illegal job transition: {current} → {target}")
