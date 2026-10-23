"""Crew Mode workers never ask the person; the conductor does, and a worker's tool
approval is decided on its conductor's approval card (edge review R1: S2, S4, and
owner decisions A and B).

Every case names the rule it guards and fails when that rule is removed.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import crew_mode
from kiro_crew.dashboard.chat_handlers import api_chat_slot_approve
from kiro_crew.dashboard.state import SlotOrigin


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(crew_mode, "_known_roots", set())
    monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    yield


def _live(key: str, agent: str, created_by: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        agent=agent,
        agent_kind="template",
        crew_mode_agent="",
        mode="",
        memory_mode="persistent",
        _created_by=created_by,
        _saved_created_by="",
        _app="",
        instance_id="",
    )


# ── S2: a worker's ask_question goes to its conductor ────────────────────────


def test_S2_a_worker_question_card_is_refused_with_the_way_to_ask_its_conductor():
    from kiro_crew.dashboard.handlers import ask_question as aq

    state = SimpleNamespace(
        _slots={
            "root": _live("root", "kirocrew-conductor"),
            "w": _live("w", "kirocrew-worker", created_by="root"),
            "plain": _live("plain", "kirocrew"),
        }
    )
    refused = aq._crew_worker_refusal(state, "w", "dashboard:w")
    assert refused is not None and refused.status == 409
    body = json.loads(refused.text)
    assert body["code"] == "crew_worker_asks_conductor"
    assert "work_report" in body["error"] and "question" in body["error"]
    # The conductor and a plain chat still ask the person.
    assert aq._crew_worker_refusal(state, "root", "dashboard:root") is None
    assert aq._crew_worker_refusal(state, "plain", "dashboard:plain") is None


def test_S2_both_question_routes_refuse_a_worker_before_any_card_opens():
    from kiro_crew.dashboard.handlers import ask_question as aq

    source = open(aq.__file__, encoding="utf-8").read()
    mcp_open = source.split("async def api_agent_ask_open", 1)[1]
    assert mcp_open.index("_crew_worker_refusal(") < mcp_open.index("state.open_agent_ask(")
    browser = source.split("async def api_ask_question(", 1)[1].split("async def ", 1)[0]
    assert "_crew_worker_refusal(" in browser


def test_S2_the_tool_hands_the_worker_the_gateways_instruction(monkeypatch):
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools import control

    monkeypatch.setattr(
        mcp_core,
        "_post",
        lambda *a, **k: {
            "error": "Call work_report with status 'question'.",
            "code": "crew_worker_asks_conductor",
        },
    )
    out = control._ask_question_blocking("dashboard:w", [{"question": "q"}])
    assert out == "Call work_report with status 'question'."
    assert "plain text" not in out


# ── B / S4: the approval card ───────────────────────────────────────────────


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


@web.middleware
async def _owner(request: web.Request, handler):
    request["app"] = ""
    request["user"] = "local-app"
    return await handler(request)


@web.middleware
async def _app_token(request: web.Request, handler):
    request["app"] = "some-app"
    request["user"] = "local-app"
    return await handler(request)


def _server(state, middleware) -> TestServer:
    from kiro_crew.dashboard.handlers.crew_workers import api_crew_worker_approve

    app = web.Application(middlewares=[middleware])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    app.router.add_post(
        "/api/chat/slots/{slot}/crew-workers/{worker}/approve", api_crew_worker_approve
    )
    return TestServer(app)


def _crew(state, request_id: str = "rid-1", grantable: bool = True):
    root = state.get_or_create_slot("root", origin=SlotOrigin.USER)
    root.agent = "kirocrew-conductor"
    worker = state.get_or_create_slot("w", origin=SlotOrigin.USER)
    worker.agent = "kirocrew-worker"
    worker._created_by = "root"
    other = state.get_or_create_slot("other-root", origin=SlotOrigin.USER)
    other.agent = "kirocrew-conductor"
    fut = asyncio.get_running_loop().create_future()
    meta = {"request_id": request_id, **({"trust_grantable": "1"} if grantable else {})}
    row = worker.append("permission", "Shell", json.dumps(meta))
    worker.register_approval(request_id, fut, row)
    return worker, fut, worker.approval_instance(request_id)


@pytest.mark.asyncio
async def test_B_approve_resolves_the_workers_request_once_pinned_to_the_card(state):
    worker, fut, mid = _crew(state)
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post(
            "/api/chat/slots/root/crew-workers/w/approve",
            json={"action": "approve", "request_id": "rid-1", "request_mid": mid},
        )
        assert resp.status == 200, await resp.text()
    assert fut.done() and fut.result() == "approved"
    assert not worker._trust


@pytest.mark.asyncio
async def test_B_reject_denies_it_once(state):
    _worker, fut, mid = _crew(state)
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post(
            "/api/chat/slots/root/crew-workers/w/approve",
            json={"action": "reject", "request_id": "rid-1", "request_mid": mid},
        )
        assert resp.status == 200, await resp.text()
    assert fut.done() and fut.result() == "rejected_once"


@pytest.mark.asyncio
async def test_B_trust_this_worker_trusts_the_worker_slot_only(state):
    worker, fut, _mid = _crew(state)
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post(
            "/api/chat/slots/root/crew-workers/w/approve",
            json={"action": "trust", "request_id": "rid-1"},
        )
        assert resp.status == 200, await resp.text()
    assert fut.done() and fut.result() in ("approved", "trust")
    assert worker._trust is True
    assert not state._slots["root"]._trust


@pytest.mark.asyncio
async def test_B_a_stale_card_cannot_pick_a_request_that_replaced_it(state):
    _worker, fut, _mid = _crew(state)
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post(
            "/api/chat/slots/root/crew-workers/w/approve",
            json={"action": "approve", "request_id": "rid-1", "request_mid": "stale-mid"},
        )
        assert resp.status == 404
    assert not fut.done()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body", "status", "code"),
    [
        # Another root's card cannot reach this root's worker.
        (
            "/api/chat/slots/other-root/crew-workers/w/approve",
            {"action": "approve"},
            404,
            "not_a_worker",
        ),
        # Nothing wider than the card's three decisions.
        ("/api/chat/slots/root/crew-workers/w/approve", {"action": "yolo"}, 400, "invalid_action"),
        (
            "/api/chat/slots/root/crew-workers/w/approve",
            {"action": "trust_command"},
            400,
            "invalid_action",
        ),
    ],
)
async def test_B_the_card_route_refuses_another_root_and_wider_grants(
    state, path, body, status, code
):
    _worker, fut, _mid = _crew(state)
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post(path, json={"request_id": "rid-1", **body})
        assert resp.status == status
        assert (await resp.json())["code"] == code
    assert not fut.done()


@pytest.mark.asyncio
async def test_B_an_app_token_is_refused(state):
    _worker, fut, mid = _crew(state)
    async with TestClient(_server(state, _app_token)) as client:
        resp = await client.post(
            "/api/chat/slots/root/crew-workers/w/approve",
            json={"action": "approve", "request_id": "rid-1", "request_mid": mid},
        )
        assert resp.status in (403, 404)
    assert not fut.done()


def test_B_the_card_shows_the_request_redacted_and_cut():
    from kiro_crew.dashboard.handlers import crew_workers

    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    row = {
        "pending_approval": True,
        "pending_approval_info": {
            "origin": "native",
            "request_id": "rid",
            "request_mid": "mid",
            "tool": "Shell",
            "tool_input": f"echo {secret} " + "x" * 5000,
        },
    }
    card = crew_workers._approval_of(row)
    assert card is not None
    assert secret not in card["tool_input"]
    assert len(card["tool_input"]) == crew_workers.APPROVAL_INPUT_MAX_CHARS
    assert card["tool_input_cut"] == "1"
    assert crew_workers._approval_of({"pending_approval": False}) is None
