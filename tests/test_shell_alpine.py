"""Shell contract: Alpine must have an `x-data` root on <body>.

Alpine only initializes the subtrees it finds an `x-data` (or `x-init`) root
in. Without one on <body>, every `@click="$store.ui.openModal(…)"` outside a
dedicated component (Add website, Add topic, Delete project) renders fine and
then does nothing — the button looks alive and silently swallows the click.
"""

from __future__ import annotations

from app.api import dashboard as D
from app.api import projects as P
from tests.helpers import make_member, make_pb, make_project, make_req, make_user


def _body_tag(html: str) -> str:
    return html.split("<body", 1)[1].split(">", 1)[0]


def _full_page(route) -> str:
    pb = make_pb()
    project = make_project(pb)
    make_member(pb, project["id"], "u1")
    req = make_req(pb, make_user(), project["id"])
    req.headers = {}  # full page, not the htmx partial
    return route(req).body.decode()


def test_body_has_an_alpine_scope():
    assert "x-data" in _body_tag(_full_page(D.dashboard))


def test_modal_triggers_share_the_alpine_scope():
    """A trigger without a scope is dead markup — the Add website bug."""
    html = _full_page(P.projects_list)
    assert "openModal(" in html
    assert "x-data" in _body_tag(html)


def test_mobile_dock_does_not_paint_the_indicator_bar():
    """The active chip is the state; DaisyUI's ::after bar doubled it."""
    html = _full_page(D.dashboard)
    assert "classList.toggle('dock-active'" not in html


def test_every_popover_trigger_has_a_popover_target():
    """`popovertarget` on a missing/non-popover element is a dead button.

    Both sides are written with the same Jinja expression, so the literal
    attribute values can be matched per file.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app" / "templates"
    problems: list[str] = []
    for path in root.rglob("*.html"):
        source = path.read_text()
        for target in re.findall(r'popovertarget="([^"]+)"', source):
            pattern = r'id="' + re.escape(target) + r'"[^>]*\bpopover\b'
            if not re.search(pattern, source):
                problems.append(f"{path.name}: {target}")
    assert not problems, f"popovertarget without a popover element: {problems}"
