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

RULES = {"listRule": "", "viewRule": "", "createRule": "", "updateRule": "", "deleteRule": ""}


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
    fixed: list[str] = []
    for c in pb.collections.get_full_list():
        # ponytail: empty rule = superuser-only; never relax to auth-only
        cur = {k: getattr(c, k.replace("Rule", "_rule"), None) for k in RULES}
        if any(v != "" for v in cur.values()):
            pb.collections.update(c.id, RULES)
            fixed.append(c.name)
    print(f"checked, locked: {fixed if fixed else 'already superuser-only'}")


if __name__ == "__main__":
    main()
