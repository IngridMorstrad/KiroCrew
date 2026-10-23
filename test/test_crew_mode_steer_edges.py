"""A person's steer is one ``steer`` event on the ledger that holds the worker's item,
whichever way it was typed (edge review R1: X1, E5, E7, E11).

One person-steer transaction (``crew_workers.person_steer``) serves the side panel's
steer route and the worker's own composer; it lands on a sub-conductor's ledger when
that is where the item lives, records into a unit even when the root has no live
session, and answers an outstanding question on the board.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import work_ledger_helpers
from work_ledger_helpers import CONDUCTOR, WORKER, _new_item

from kiro_crew import work_ledger as wl

_isolated_home = work_ledger_helpers._isolated_home

SUB = "chat-3-subconductor"
GRANDCHILD = "chat-4-grandchild"


def _bound(board: str = CONDUCTOR, worker: str = WORKER) -> str:
    wl.ensure_conductor(board, goal="g")
    item_id = wl.apply_conductor_action(
        board, "create", title="t", acceptance={"kind": "human_approval"}
    )["item"].item_id
    wl.apply_conductor_action(board, "bind", item_id=item_id, worker_session_key=worker)
    return item_id


@pytest.fixture
def _log(monkeypatch):
    from kiro_crew.crew_log import emit as crew_log_emit

    entries: list[tuple[str, dict[str, Any]]] = []

    def fake_record(unit: str, data: dict[str, Any], **_k: Any) -> bool:
        entries.append((unit, data))
        return True

    monkeypatch.setattr(crew_log_emit, "on_work_recorded", fake_record)
    monkeypatch.setattr(crew_log_emit, "enabled", lambda: True)
    return entries


class _Sessions:
    def __init__(self, live: dict[str, str]):
        self.live = live

    def get_provider(self, key: str):
        unit = self.live.get(key)
        return SimpleNamespace(session_id=unit) if unit else None


# ── E5: a unit to record into, root session or not ──────────────────────────


@pytest.mark.asyncio
async def test_E5_a_root_with_no_live_session_records_into_the_workers_unit(_log):
    from kiro_crew.dashboard.handlers import work_ledger_worker as wlw

    item_id = _new_item()
    state = SimpleNamespace(sessions=_Sessions({f"dashboard:{WORKER}": "unit-w"}))
    got, why = await wlw.record_person_steer_result(
        state, CONDUCTOR, item_id, "go", unit_keys=(WORKER,)
    )
    assert (got, why) == (item_id, "")
    [(unit, entry)] = _log
    assert unit == "unit-w" and entry["by"] == CONDUCTOR and entry["actor"] == "person"


@pytest.mark.asyncio
async def test_E5_no_live_session_at_all_falls_back_to_the_boards_newest_unit(_log, monkeypatch):
    from kiro_crew import session_ledger
    from kiro_crew.dashboard.handlers import work_ledger_worker as wlw

    item_id = _new_item()
    monkeypatch.setattr(session_ledger, "work_crew_log_units", lambda slot: ("u-old", "u-new"))
    got, _why = await wlw.record_person_steer_result(
        SimpleNamespace(sessions=_Sessions({})), CONDUCTOR, item_id, "go", unit_keys=(WORKER,)
    )
    assert got == item_id and _log[0][0] == "u-new"


@pytest.mark.asyncio
async def test_E5_with_no_unit_anywhere_the_reason_is_named_not_silent(_log, monkeypatch):
    from kiro_crew import session_ledger
    from kiro_crew.dashboard.handlers import crew_workers

    _bound()
    monkeypatch.setattr(session_ledger, "work_crew_log_units", lambda slot: ())
    out = await crew_workers.person_steer(
        SimpleNamespace(sessions=_Sessions({})), CONDUCTOR, WORKER, "go", rows=[]
    )
    assert out == {"ledger_event": None, "ledger_error": "crew_log_unit_unknown"}
    assert _log == []


# ── E7: the ledger that holds the item, a sub-conductor's included ───────────


@pytest.mark.asyncio
async def test_E7_a_grandchild_is_steered_on_its_sub_conductors_ledger(_log):
    from kiro_crew.dashboard.handlers import crew_workers

    item_id = _bound(SUB, GRANDCHILD)
    rows = [
        {"key": SUB, "crew_root": CONDUCTOR},
        {"key": GRANDCHILD, "crew_root": CONDUCTOR},
    ]
    state = SimpleNamespace(sessions=_Sessions({f"dashboard:{GRANDCHILD}": "unit-g"}))
    out = await crew_workers.person_steer(state, CONDUCTOR, GRANDCHILD, "rebase", rows=rows)
    assert out["ledger_event"] == item_id
    assert [e.kind for e in wl.read_events(SUB, item_id)][-1] == "steer"
    assert _log[0][1]["by"] == SUB


@pytest.mark.asyncio
async def test_E7_a_binding_outside_the_roots_tree_is_not_followed(_log):
    from kiro_crew.dashboard.handlers import crew_workers

    item_id = _bound("chat-9-elsewhere", GRANDCHILD)
    state = SimpleNamespace(sessions=_Sessions({f"dashboard:{GRANDCHILD}": "unit-g"}))
    out = await crew_workers.person_steer(state, CONDUCTOR, GRANDCHILD, "x", rows=[])
    assert out == {"ledger_event": None, "ledger_error": "no_item"}
    assert "steer" not in [e.kind for e in wl.read_events("chat-9-elsewhere", item_id)]


# ── E11: a steer answers an outstanding question ─────────────────────────────


def test_E11_a_steer_newer_than_the_question_answers_it():
    from kiro_crew.dashboard.handlers.work_ledger_board import _is_outstanding

    item = SimpleNamespace(status="question", last_report_at="")
    asked = {"kind": "report", "status": "question", "ts": "2026-10-23T10:00:00Z"}
    steer = {"kind": "steer", "ts": "2026-10-23T10:05:00Z"}
    old_steer = {"kind": "steer", "ts": "2026-10-23T09:00:00Z"}
    assert _is_outstanding(item, [asked]) is True  # type: ignore[arg-type]
    assert _is_outstanding(item, [asked, steer]) is False  # type: ignore[arg-type]
    assert _is_outstanding(item, [old_steer, asked]) is True  # type: ignore[arg-type]


# ── X1: the worker's own composer runs the same transaction ──────────────────


class _Req(dict):
    def __init__(self, state: Any) -> None:
        super().__init__()
        self.app = {"state": state}


@pytest.mark.asyncio
async def test_X1_a_composer_send_to_a_worker_records_the_steer_and_says_so(monkeypatch):
    from aiohttp import web

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.chat_api import chat_send
    from kiro_crew.dashboard.handlers import crew_workers

    calls: list[tuple[str, str, str]] = []

    async def fake_send(request):
        request[chat_send._CREW_PERSON_STEER] = (CONDUCTOR, WORKER, "use main")
        return web.json_response({"ok": True, "slot": WORKER})

    async def fake_steer(state, root, worker, message, **_k):
        calls.append((root, worker, message))
        return {"ledger_event": "it_1", "ledger_error": None}

    # The send path runs on the chat_handlers facade (``_facade_compose``).
    monkeypatch.setattr(chat_handlers, "_api_chat_send", fake_send)
    monkeypatch.setattr(crew_workers, "person_steer", fake_steer)
    resp = await chat_handlers.api_chat(_Req(SimpleNamespace()))  # type: ignore[arg-type]
    assert calls == [(CONDUCTOR, WORKER, "use main")]
    assert json.loads(resp.text) == {
        "ok": True,
        "slot": WORKER,
        "ledger_event": "it_1",
        "ledger_error": None,
    }


@pytest.mark.asyncio
async def test_X1_a_refused_send_records_nothing(monkeypatch):
    from aiohttp import web

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.chat_api import chat_send
    from kiro_crew.dashboard.handlers import crew_workers

    async def fake_send(request):
        request[chat_send._CREW_PERSON_STEER] = (CONDUCTOR, WORKER, "x")
        return web.json_response({"error": "busy"}, status=409)

    async def boom(*_a, **_k):
        raise AssertionError("a refused send must not be recorded")

    # The send path runs on the chat_handlers facade (``_facade_compose``).
    monkeypatch.setattr(chat_handlers, "_api_chat_send", fake_send)
    monkeypatch.setattr(crew_workers, "person_steer", boom)
    resp = await chat_handlers.api_chat(_Req(SimpleNamespace()))  # type: ignore[arg-type]
    assert resp.status == 409


def test_X1_only_the_owners_own_send_to_a_worker_is_a_steer(monkeypatch):
    from kiro_crew.dashboard.chat_api import chat_send
    from kiro_crew.dashboard.handlers import source_providers

    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda r: True)
    root = SimpleNamespace(
        key="r",
        agent="kirocrew-conductor",
        agent_kind="template",
        crew_mode_agent="",
        mode="",
        memory_mode="persistent",
        _created_by="",
        _saved_created_by="",
        _app="",
        instance_id="",
    )
    worker = SimpleNamespace(
        **{**vars(root), "key": "w", "agent": "kirocrew-worker", "_created_by": "r"}
    )
    state = SimpleNamespace(_slots={"r": root, "w": worker})
    assert chat_send._person_crew_worker_root(object(), state, worker, "") == "r"  # type: ignore[arg-type]
    assert chat_send._person_crew_worker_root(object(), state, root, "") == ""  # type: ignore[arg-type]
    assert chat_send._person_crew_worker_root(object(), state, worker, "an-app") == ""  # type: ignore[arg-type]
    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda r: False)
    assert chat_send._person_crew_worker_root(object(), state, worker, "") == ""  # type: ignore[arg-type]


def test_X1_the_composer_send_refuses_an_over_long_steer_before_queueing_it():
    from kiro_crew.dashboard.chat_api import chat_send

    source = open(chat_send.__file__, encoding="utf-8").read()
    body = source.split("async def _api_chat_send", 1)[1]
    guard = body.index("_person_crew_worker_root(request, state, slot, request_app)")
    assert body.index('"code": "message_too_long"', guard) < body.index("responsive_chat.eligible")
