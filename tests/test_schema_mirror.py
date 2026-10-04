"""`pb_collections_import.json` is generated from `COLLECTIONS`, never hand-edited.

Manual import through the PocketBase Admin UI is a supported path, and the file
has silently drifted from the code before (research collections, WordPress-mirror
fields). These assertions fail the moment it drifts again.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from app.scripts.bootstrap_pb import COLLECTIONS, _system_fields, export_collections_json

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


def test_schema_defines_integration_model_and_legacy_date_timestamps():
    integrations = next(c for c in COLLECTIONS if c["name"] == "integrations")
    assert any(field["name"] == "model" for field in integrations["fields"])
    system = {field["name"]: field for field in _system_fields()}
    # Legacy date fields: PocketBase rejects date -> autodate conversion via
    # import/PATCH, so the schema must stay `date` to match the live DB.
    assert system["created"]["type"] == "date"
    assert system["created"]["onCreate"] is True
    assert system["updated"]["type"] == "date"
    assert system["updated"]["onUpdate"] is True
    mirrored = next(c for c in _mirror() if c["name"] == "integrations")
    mirrored_fields = {field["name"]: field for field in mirrored["fields"]}
    assert "model" in mirrored_fields
    assert mirrored_fields["created"]["type"] == "date"
    assert mirrored_fields["updated"]["type"] == "date"
    # Import compatibility: live keeps every competitor page row, so the mirror
    # must not demand a UNIQUE index that existing duplicates would block.
    competitor = next(c for c in _mirror() if c["name"] == "competitor_pages")
    assert all("UNIQUE" not in idx for idx in competitor.get("indexes", []))


def test_backfill_uses_saved_model_values_only(capsys):
    from app.repositories.integrations import IntegrationRepo
    from app.scripts.bootstrap_pb import backfill_integration_models
    from tests.helpers import make_pb

    pb = make_pb()
    project = pb.collection("projects").create({"name": "P", "slug": "p"})
    pb.collection("project_settings").create(
        {
            "project": project["id"],
            "imageCoverProvider": "openai_compat",
            "imageCoverModel": "saved-image-model",
            "imageInteriorProvider": "bfl",
            "imageInteriorModel": "other-provider-model",
        }
    )
    repo = IntegrationRepo(pb)
    compatible = repo.create(
        project=project["id"],
        category="image",
        provider="openai_compat",
        display_name="Compatible image",
    )
    unmatched = repo.create(
        project=project["id"],
        category="image",
        provider="missing-provider",
        display_name="Unconfigured image",
    )

    backfill_integration_models(pb)

    assert repo.get(compatible["id"])["model"] == "saved-image-model"
    assert repo.get(compatible["id"])["configuration"]["model"] == "saved-image-model"
    assert repo.get(unmatched["id"])["model"] == ""
    assert "has no saved model" in capsys.readouterr().err


def test_targeted_schema_migration_adds_model_field(monkeypatch):
    from types import SimpleNamespace

    import app.scripts.bootstrap_pb as bootstrap

    schema = {
        "fields": [
            {"id": "date-created", "name": "created", "type": "date"},
            {"id": "date-updated", "name": "updated", "type": "date"},
        ]
    }
    patched = []

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return schema

    def patch(_url, *, json, **_kwargs):
        patched.append(json["fields"])
        schema["fields"] = json["fields"]
        return Response()

    monkeypatch.setattr(bootstrap.httpx, "get", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(bootstrap.httpx, "patch", patch)
    pb = SimpleNamespace(
        base_url="https://pb.example/",
        auth_store=SimpleNamespace(token="admin-token"),
    )

    bootstrap.ensure_integration_model_field(pb)

    model = next(field for field in patched[0] if field["name"] == "model")
    assert model["type"] == "text"
    assert model["id"] == bootstrap._field_id("text", "model")
