"""Lock all PocketBase API rules to superuser-only (idempotent).

Usage:
    .venv/bin/python -m app.scripts.lock_pb_rules
Requires PB_URL / PB_ADMIN_EMAIL / PB_ADMIN_PASSWORD (env or .env).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pocketbase import PocketBase  # noqa: E402

from app.config import settings  # noqa: E402
from app.scripts.bootstrap_pb import LOCKED_RULES  # noqa: E402

# None = JSON null = "locked" (superuser-only). "" would mean PUBLIC (anyone).
RULES = LOCKED_RULES


def main() -> None:
    if not settings.pb_admin_email or not settings.pb_admin_password:
        print("ERROR: PB_ADMIN_EMAIL/PB_ADMIN_PASSWORD required", file=sys.stderr)
        sys.exit(1)
    pb = PocketBase(settings.pb_url, auto_snake_case=False)
    try:
        pb.collection("_superusers").auth_with_password(
            settings.pb_admin_email, settings.pb_admin_password
        )
    except Exception:
        pb.collection("_admins").auth_with_password(
            settings.pb_admin_email, settings.pb_admin_password
        )

    def unlocked() -> list[str]:
        """Collections whose rules are not all null (i.e. still reachable)."""
        out: list[str] = []
        for c in pb.collections.get_full_list():
            cur = {k: getattr(c, k.replace("Rule", "_rule"), None) for k in RULES}
            if any(v is not None for v in cur.values()):
                out.append(c.name)
        return out

    fixed: list[str] = []
    for name in unlocked():
        c = pb.collections.get_one(name)
        # ponytail: None = locked (superuser-only); never relax to a public
        # or auth-only rule.
        try:
            pb.collections.update(c.id, RULES)
            fixed.append(name)
        except Exception as exc:  # some system collections refuse updates
            print(f"WARN: could not update {name}: {exc}", file=sys.stderr)

    # Re-read instead of trusting the writes: a repair that half-applies and
    # still prints success is worse than no repair at all.
    still_open = unlocked()
    if fixed:
        print(f"locked: {fixed}")
    if still_open:
        print(f"ERROR: still NOT locked: {still_open}", file=sys.stderr)
        sys.exit(1)
    print("verified: every collection is superuser-only")


if __name__ == "__main__":
    main()
