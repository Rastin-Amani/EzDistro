"""Umami product events: the on/off gate and the mutations that carry them."""

from __future__ import annotations

from app.api import projects as P
from app.config import settings
from app.utils import analytics_event
from tests.helpers import call_route, hx_events, make_pb, make_req, make_user


def test_analytics_event_is_a_no_op_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "umami_website_id", "")
    assert analytics_event("thing_done") == {}


def test_analytics_event_payload_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "umami_website_id", "website-id")
    assert analytics_event("thing_done", {"cloned": True}) == {
        "analytics-event": {"name": "thing_done", "data": {"cloned": True}}
    }


def test_project_creation_reports_project_created(monkeypatch):
    monkeypatch.setattr(settings, "umami_website_id", "website-id")
    req = make_req(make_pb(), make_user(), "")
    resp = call_route(P.create_project, req, name="Site", slug="site")
    assert hx_events(resp)["analytics-event"] == {
        "name": "project_created",
        "data": {"cloned": False},
    }
