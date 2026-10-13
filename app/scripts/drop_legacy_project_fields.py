"""One-off migration: drop the legacy project columns from the LIVE database.

`language`, `timezone` and `description` were removed from the code schema
(`app/scripts/bootstrap_pb.py`): the writing language now derives from the
target locale, and locale/market live in `project_settings`. PocketBase's
importer merges collections field-by-field and never drops a field, so an
existing database keeps the orphan columns until they are removed explicitly.

Idempotent — safe to re-run; prints "nothing to drop" once clean.

The write goes through the RAW admin API with camelCase field keys, never the
Python SDK's ``collections.update``: the SDK snake-cases field metadata on
read, so round-tripping ``fields`` silently drops keys like
``autogeneratePattern`` on the system ``id`` field (which then makes every
create fail with ``id: Cannot be blank``) — see ``ensure_select_values``.

Run:  .venv/bin/python -m app.scripts.drop_legacy_project_fields
"""

from __future__ import annotations

import sys

import httpx

from app.pb import get_admin_pb

COLLECTION = "projects"
LEGACY_FIELDS = ("language", "timezone", "description")


def drop_legacy_project_fields() -> list[str]:
    """Remove LEGACY_FIELDS from the live COLLECTION. Returns the dropped names."""
    pb = get_admin_pb()
    base = str(pb.base_url).rstrip("/")
    headers = {"Authorization": getattr(pb.auth_store, "token", "")}

    resp = httpx.get(f"{base}/api/collections/{COLLECTION}", headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"could not read {COLLECTION}: {resp.status_code} {resp.text[:200]}")
    fields = resp.json().get("fields") or []
    dropped = [f["name"] for f in fields if f.get("name") in LEGACY_FIELDS]
    if not dropped:
        print(f"{COLLECTION}: nothing to drop (already clean)")
        return []

    kept = [f for f in fields if f.get("name") not in LEGACY_FIELDS]
    patched = httpx.patch(
        f"{base}/api/collections/{COLLECTION}",
        headers=headers,
        json={"fields": kept},
    )
    if patched.status_code != 200:
        raise RuntimeError(
            f"could not update {COLLECTION}: {patched.status_code} {patched.text[:300]}"
        )
    print(f"{COLLECTION}: dropped {', '.join(dropped)}")
    return dropped


def main() -> int:
    try:
        drop_legacy_project_fields()
    except Exception as exc:  # noqa: BLE001 — surface a non-zero exit for ops
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
