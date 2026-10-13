"""Project creation/settings defaults after the field cleanup.

Projects no longer carry `language`, `timezone` or `description` — the writing
language now derives from the target locale (US English by default) and the
locale/market live in project settings.
"""

from __future__ import annotations

from app.repositories.projects import ProjectRepo, ProjectSettingsRepo
from app.services.settings import ProjectConfig
from tests.helpers import make_pb, make_project


def test_new_project_has_no_legacy_fields():
    pb = make_pb()
    project = ProjectRepo(pb).create(name="P", slug="p")
    for gone in ("language", "timezone", "description"):
        assert gone not in project


def test_settings_default_to_us_english():
    pb = make_pb()
    project = ProjectRepo(pb).create(name="P", slug="p")  # no settings record yet
    settings = ProjectSettingsRepo(pb).get_for_project(project["id"])
    assert settings["targetLocale"] == "en-US"
    assert settings["targetCountry"] == "US"


def test_blank_locale_and_market_fall_back_to_default():
    pb = make_pb()
    project = ProjectRepo(pb).create(name="P", slug="p")
    ProjectSettingsRepo(pb).create(
        {"project": project["id"], "targetLocale": "", "targetCountry": ""}
    )
    settings = ProjectSettingsRepo(pb).get_for_project(project["id"])
    assert settings["targetLocale"] == "en-US"
    assert settings["targetCountry"] == "US"


def test_language_derives_from_target_locale():
    pb = make_pb()
    project = make_project(pb)
    settings = ProjectSettingsRepo(pb).get_for_project(project["id"])
    assert ProjectConfig.load(pb, project["id"]).language == "en"

    ProjectSettingsRepo(pb).update(settings["id"], {"targetLocale": "fa-IR"})
    assert ProjectConfig.load(pb, project["id"]).language == "fa"
