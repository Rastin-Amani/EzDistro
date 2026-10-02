"""`pb_collections_import.json` is generated from `COLLECTIONS`, never hand-edited.

Manual import through the PocketBase Admin UI is a supported path, and the file
has silently drifted from the code before (research collections, WordPress-mirror
fields). These assertions fail the moment it drifts again.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from app.scripts.bootstrap_pb import export_collections_json

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIRROR = REPO_ROOT / "pb_collections_import.json"


def _mirror() -> list[dict[str, Any]]:
    return json.loads(MIRROR.read_text())


def test_import_json_matches_generator(tmp_path):
    target = tmp_path / "mirror.json"
    export_collections_json(str(target))
    assert json.loads(target.read_text()) == _mirror()


def _select_values(collection: str, field: str) -> list[str]:
    for coll in _mirror():
        if coll["name"] == collection:
            for spec in coll["fields"]:
                if spec["name"] == field:
                    return list(spec.get("values") or [])
    raise AssertionError(f"{collection}.{field} not found in the mirror")


def test_research_type_select_covers_every_written_value():
    """A run is created with researchType='import' for file imports.

    The live PocketBase collection rejects a value the code writes unless the
    select already lists it (`validation_invalid_value: import`), so the schema
    must include every research type the app can persist.
    """
    values = _select_values("research_runs", "researchType")
    for written in ("keywords", "site", "competitors", "mixed", "import"):
        assert written in values, f"research_runs.researchType is missing {written!r}"


def test_import_json_relations_resolve():
    ids = {c["id"] for c in _mirror()}
    for coll in _mirror():
        for field in coll["fields"]:
            if field["type"] == "relation":
                # `_pb_users_auth_` is PocketBase's built-in users collection.
                assert field["collectionId"] in ids | {"_pb_users_auth_"}, (
                    f"{coll['name']}.{field['name']} -> {field['collectionId']}"
                )
