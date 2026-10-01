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


def test_import_json_relations_resolve():
    ids = {c["id"] for c in _mirror()}
    for coll in _mirror():
        for field in coll["fields"]:
            if field["type"] == "relation":
                # `_pb_users_auth_` is PocketBase's built-in users collection.
                assert field["collectionId"] in ids | {"_pb_users_auth_"}, (
                    f"{coll['name']}.{field['name']} -> {field['collectionId']}"
                )
