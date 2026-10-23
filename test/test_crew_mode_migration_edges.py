"""Crew Mode migration edge cases (edge review R1: E1-E4, E6, E10).

One config moves every root when it is idle: a refusal that clears on its own is
retried, a permanent one never advances the recorded name, a cancel never leaves a
root half switched or unvisited, two runs never move one root twice, and a name
that cannot run Crew Mode moves nothing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import crew_mode


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    from kiro_crew.config import paths
    from kiro_crew.dashboard import crew_mode_migration as mig

    monkeypatch.setattr(crew_mode, "_known_roots", set())
    monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    monkeypatch.setattr(crew_mode, "_configured", crew_mode.DEFAULT_CREW_MODE_AGENT)
    monkeypatch.setattr(crew_mode, "_unusable", {})
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr(mig, "_pending", set())
    monkeypatch.setattr(mig, "_failed", {})
    monkeypatch.setattr(mig, "_verdicts", {})
    monkeypatch.setattr(mig, "_lock", None)
    monkeypatch.setattr(mig, "_usable", lambda name: "")
    yield


def _slot(key: str, agent: str = "kirocrew-conductor", **over: Any) -> SimpleNamespace:
    base = dict(
        key=key,
        agent=agent,
        agent_kind="template",
        crew_mode_agent="",
        mode="",
        memory_mode="persistent",
        _created_by="",
        _app="",
        instance_id="",
        running=False,
        turn_running=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


class _State:
    def __init__(self, *slots: Any) -> None:
        self._slots = {s.key: s for s in slots}
        self.notices: list[tuple[str, str, str]] = []
        self.conversation_log = None
        self._background_tasks: set[Any] = set()

    def push_slots_update(self) -> None:
        pass

    def notify(self, category: str, title: str, body: str) -> None:
        self.notices.append((category, title, body))


def _answer(code: str | None, status: int = 409):
    if code is None:
        return None
    return web.json_response({"error": code, "code": code}, status=status)


@pytest.fixture
def switch(monkeypatch):
    """The migration's switch. ``refuse[key]`` answers that code instead of moving."""
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard import crew_mode_migration as mig

    calls: list[tuple[str, str]] = []
    refuse: dict[str, tuple[str, int]] = {}
    gate: dict[str, asyncio.Event] = {}

    async def fake_switch(state, slot, name, agent, kind, caller):
        calls.append((name, agent))
        if name in gate:
            await gate[name].wait()
        if name in refuse:
            code, status = refuse[name]
            return _answer(code, status)
        slot.agent = agent
        slot.crew_mode_agent = agent
        return web.json_response({"ok": True, "agent": agent})

    monkeypatch.setattr(chat_handlers, "switch_slot_agent", fake_switch)
    monkeypatch.setattr(mig, "_audit", lambda *a, **k: None)
    return SimpleNamespace(calls=calls, refuse=refuse, gate=gate, mig=mig)


def _target(monkeypatch, name: str = "better-conductor") -> None:
    monkeypatch.setattr(crew_mode, "_configured", name)


@pytest.mark.asyncio
async def test_E1_attached_subagents_keep_the_root_owed_and_the_name_unadvanced(
    switch, monkeypatch
):
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch)
    root = _slot("r")
    state = _State(root)
    switch.refuse["r"] = ("slot_subagents_running", 409)
    out = await switch.mig.reconcile(state)
    assert out == {"moved": [], "pending": ["r"]}
    assert switch.mig.read_applied() == "kirocrew-conductor"
    # Idle again: the retry loop's reconcile moves it, then advances the name.
    del switch.refuse["r"]
    out = await switch.mig.reconcile(state, retry_failed=False)
    assert out == {"moved": ["r"], "pending": []}
    assert root.agent == "better-conductor"
    assert switch.mig.read_applied() == "better-conductor"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["session_rebound", "history_unavailable"])
async def test_E1_other_retryable_refusals_are_retried(switch, monkeypatch, code):
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch)
    state = _State(_slot("r"))
    switch.refuse["r"] = (code, 503 if code == "history_unavailable" else 409)
    out = await switch.mig.reconcile(state)
    assert out["pending"] == ["r"]
    assert switch.mig.failed() == {}


@pytest.mark.asyncio
async def test_E1_a_permanent_refusal_never_advances_the_name_and_tells_the_person(
    switch, monkeypatch
):
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch)
    ok = _slot("ok")
    bad = _slot("bad")
    state = _State(ok, bad)
    switch.refuse["bad"] = ("agent_choice_unavailable", 409)
    out = await switch.mig.reconcile(state)
    assert out == {"moved": ["ok"], "pending": []}
    assert switch.mig.failed() == {"bad": "agent_choice_unavailable"}
    assert switch.mig.read_applied() == "kirocrew-conductor"
    assert len(state.notices) == 1 and "bad (agent_choice_unavailable)" in state.notices[0][2]
    # The idle loop does not hammer a permanent refusal, nor repeat the notice.
    n = len(switch.calls)
    await switch.mig.reconcile(state, retry_failed=False)
    assert len(switch.calls) == n and len(state.notices) == 1
    # A config change (or restart) tries it again.
    del switch.refuse["bad"]
    await switch.mig.reconcile(state)
    assert bad.agent == "better-conductor"
    assert switch.mig.read_applied() == "better-conductor"


@pytest.mark.asyncio
async def test_E2_the_migration_runs_off_the_applier_and_is_never_cancelled_by_its_bound(
    switch, monkeypatch
):
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch)
    root = _slot("r")
    state = _State(root)
    switch.gate["r"] = asyncio.Event()
    task = switch.mig.schedule_on_config_change(state)
    assert task in state._background_tasks
    # The applier returned at once; the watcher's 10 s bound has nothing to cancel.
    await asyncio.sleep(0)
    assert not task.done()
    switch.gate["r"].set()
    await asyncio.wait_for(task, 2)
    assert root.agent == "better-conductor"
    assert switch.mig.read_applied() == "better-conductor"


def test_E2_config_watch_schedules_the_migration_instead_of_awaiting_it():
    from kiro_crew.dashboard.server_runtime import config_watch

    source = open(config_watch.__file__, encoding="utf-8").read()
    assert "crew_mode_migration.schedule_on_config_change(" in source
    assert "await crew_mode_migration.on_config_change(" not in source


@pytest.mark.asyncio
async def test_E2_a_cancel_inside_the_reset_rolls_the_switch_back():
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

    slot = _ChatSlot("t1")
    prior_agent, prior_kind = slot.agent, slot.agent_kind
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.conversation_log = MagicMock()
    state.sessions = MagicMock()
    state.sessions.get_provider = MagicMock(return_value=None)
    caller = chat_handlers.SlotAgentSwitchCaller(app="", remote="t", owner=True)
    with (
        patch(
            "kiro_crew.dashboard.chat_handlers._reset_slot_session_or_warn",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ),
        patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", new=AsyncMock()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await chat_handlers.switch_slot_agent(state, slot, "t1", "kirocrew", "template", caller)
    assert slot.agent == prior_agent and slot.agent_kind == prior_kind
    state.conversation_log.update_metadata.assert_not_called()


@pytest.mark.asyncio
async def test_E3_a_cancel_before_the_second_root_leaves_it_owed_and_the_loop_moves_it(
    switch, monkeypatch
):
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch)
    r1, r2 = _slot("r1"), _slot("r2")
    state = _State(r1, r2)
    switch.gate["r1"] = asyncio.Event()
    task = asyncio.create_task(switch.mig.migrate(state))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert switch.mig.pending() == frozenset({"r1", "r2"})
    assert switch.mig.read_applied() == "kirocrew-conductor"
    switch.gate.clear()
    await switch.mig.reconcile(state, retry_failed=False)
    assert r1.agent == r2.agent == "better-conductor"
    assert switch.mig.read_applied() == "better-conductor"


@pytest.mark.asyncio
async def test_E3_the_idle_loop_retries_when_the_name_is_not_yet_applied(switch, monkeypatch):
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch)
    root = _slot("r")
    state = _State(root)
    monkeypatch.setattr(switch.mig, "RETRY_SECS", 0)

    async def stop_after(state):  # the start pass does nothing here
        return None

    monkeypatch.setattr(switch.mig, "on_start", stop_after)
    task = asyncio.create_task(switch.mig.run(state, start_delay=0))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if root.agent == "better-conductor":
            break
    task.cancel()
    assert root.agent == "better-conductor"


@pytest.mark.asyncio
async def test_E4_two_concurrent_migrations_switch_one_root_once(switch, monkeypatch):
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch)
    state = _State(_slot("r"))
    await asyncio.gather(switch.mig.reconcile(state), switch.mig.migrate(state))
    assert switch.calls == [("r", "better-conductor")]


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["agent_not_found", "agent_is_crewmate", "invalid_agent_name"])
async def test_E6_an_unusable_name_moves_nothing_and_keeps_the_applied_conductor(
    switch, monkeypatch, why
):
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch, "typo-conductr")
    monkeypatch.setattr(switch.mig, "_usable", lambda name: why)
    root = _slot("r")
    state = _State(root)
    assert await switch.mig.reconcile(state) is None
    assert switch.calls == [] and root.agent == "kirocrew-conductor"
    assert switch.mig.read_applied() == "kirocrew-conductor"
    assert len(state.notices) == 1 and "typo-conductr" in state.notices[0][2]
    # Every reader keeps the applied conductor, so new chats still turn Crew Mode on.
    assert crew_mode.crew_mode_agent() == "kirocrew-conductor"
    assert "typo-conductr" not in crew_mode.conductor_agents()
    # The idle loop does not repeat the notice.
    await switch.mig.reconcile(state, retry_failed=False)
    assert len(state.notices) == 1
    # A later usable name clears it and moves the roots.
    _target(monkeypatch, "better-conductor")
    monkeypatch.setattr(switch.mig, "_usable", lambda name: "")
    await switch.mig.on_config_change(state)
    assert root.agent == "better-conductor"
    assert crew_mode.crew_mode_agent() == "better-conductor"


def test_E6_usable_refuses_a_crewmate_and_an_unknown_template(monkeypatch):
    from kiro_crew.config import KiroCrewConfig, loader_agents
    from kiro_crew.dashboard import crew_mode_migration as mig

    monkeypatch.undo()  # the autouse fixture stubbed _usable
    cfg = SimpleNamespace(agents={"buddy": object()})
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(
        loader_agents,
        "resolve_agent_bindings",
        lambda c, name, p, **k: SimpleNamespace(requested_resolved=name == "real-conductor"),
    )
    assert mig._usable("buddy") == "agent_is_crewmate"
    assert mig._usable("nope-conductor") == "agent_not_found"
    assert mig._usable("real-conductor") == ""
    assert mig._usable("../x") == "invalid_agent_name"


def test_E10_a_migration_switch_counts_no_turned_on():
    from kiro_crew.dashboard.chat_api import agent_switch

    source = open(agent_switch.__file__, encoding="utf-8").read()
    i = source.index("crew_turned_on = (")
    block = source[i : source.index("\n        )\n", i)]
    assert "not caller.crew_mode_migration" in block
    assert "prior_crew_stamp" in block
