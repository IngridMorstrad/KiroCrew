"""Crew Mode workers stay out of the person's search and keep their stamps across a
peer (edge review R1: E8, E9)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew import crew_mode


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(crew_mode, "_known_roots", set())
    monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    yield


ROWS = [
    {"key": "dashboard_root", "agent": "kirocrew-conductor", "title": "root", "snippet": "x"},
    {
        "key": "dashboard_worker",
        "agent": "kirocrew-worker",
        "created_by": "root",
        "title": "worker",
        "snippet": "x",
    },
    {"key": "dashboard_plain", "agent": "kirocrew", "title": "plain", "snippet": "x"},
]


class _Log:
    def __init__(self):
        self.limits: list[int] = []

    def search_sessions(self, _q, limit, **_kw):
        self.limits.append(limit)
        return [dict(r) for r in ROWS]


class _Req:
    def __init__(self, state, limit="2"):
        self.app = {"state": state}
        self.match_info = {}
        self.headers = {}
        self.query = {"q": "mascot", "limit": limit}
        self._attrs = {"user": "local-app", "app": ""}

    def get(self, key, default=""):
        return self._attrs.get(key, default)

    def __contains__(self, key):
        return key in self._attrs

    def __getitem__(self, key):
        return self._attrs[key]


@pytest.mark.asyncio
async def test_E8_session_search_leaves_out_a_worker_and_keeps_the_page_full():
    from kiro_crew.dashboard.handlers import sessions as hs

    log = _Log()
    state = SimpleNamespace(conversation_log=log, _slots={})
    resp = await hs.api_sessions_search(_Req(state))
    keys = [r["key"] for r in json.loads(resp.body.decode())["sessions"]]
    assert keys == ["dashboard_root", "dashboard_plain"]
    # Ranked over a wider window, so the hidden worker did not shorten the page.
    assert log.limits == [8]


@pytest.mark.asyncio
async def test_E8_the_federated_local_search_leaves_out_a_worker_too(monkeypatch):
    from kiro_crew.dashboard import handlers_instances as hi

    monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )
    state = SimpleNamespace(
        conversation_log=_Log(),
        _slots={},
        instances_manager=None,
        instances_registry=SimpleNamespace(get=lambda i: None, list=lambda: []),
    )
    resp = await hi.api_instances_search_sessions(_Req(state, limit="50"))
    keys = [r["key"] for r in json.loads(resp.body.decode())["sessions"]]
    assert "dashboard_worker" not in keys and "dashboard_root" in keys


def test_E9_a_peer_row_keeps_its_crew_stamps_and_drops_a_forged_root_flag():
    from kiro_crew.dashboard.handlers_instances_peer import _clean_peer_slot

    worker = _clean_peer_slot({"key": "w", "crew_root": "r", "crew_mode_root": False})
    root = _clean_peer_slot({"key": "r", "crew_mode_root": True})
    forged = _clean_peer_slot({"key": "f", "crew_mode_root": "yes", "crew_root": 7})
    assert worker == {**worker, "crew_root": "r"} and "crew_mode_root" not in worker
    assert root is not None and root["crew_mode_root"] is True
    assert forged is not None and "crew_mode_root" not in forged and "crew_root" not in forged
