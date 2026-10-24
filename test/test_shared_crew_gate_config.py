"""Shared Crew repair: A raising containment probe fails closed in hard-limit mode; a turn gate call picks up admission enabled by config."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from kiro_crew.config.sections_shared_crew import effective_limits, shared_crew_config_from
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import containment as _containment
from kiro_crew.shared_crew import turn_gate
from kiro_crew.shared_crew.admission import (
    Liveness,
    OperationKind,
    ReasonCode,
)
from kiro_crew.shared_crew.containment import ContainmentReport, SliceManager
from kiro_crew.shared_crew.contracts import (
    DisplaySnapshot,
    EnforcementLevel,
    Principal,
    PrincipalKind,
    Provenance,
    principal_id_for,
)
from kiro_crew.shared_crew.work_store import WorkStore
from kiro_crew.taskq.store import TaskStore
from kiro_crew.testing.clock import ManualClock


def _person(login: str) -> Principal:
    return Principal(
        id=principal_id_for("tailscale", "tailnet", login),
        kind=PrincipalKind.HUMAN,
        provider="tailscale",
        issuer="tailnet",
        subject=login,
        provenance=Provenance.TAILSCALE_SERVE,
        display=DisplaySnapshot(name=login.split("@")[0], login=login),
    )


ALICE = _person("alice@example.test")


def _report(**kw) -> ContainmentReport:
    base = dict(
        platform="linux",
        available=True,
        reason="",
        cpu=True,
        memory=True,
        pids=True,
        level=EnforcementLevel.BEST_EFFORT,
        hard_ready=True,
        slices_enabled=True,
    )
    base.update(kw)
    return ContainmentReport(**base)


def _limits(**cfg):
    raw = {"admission_enabled": True, "principal_concurrency": 1, "crew_concurrency": 2}
    raw.update(cfg)
    return effective_limits(shared_crew_config_from(raw), auto_crew_concurrency=8)


def _service(tmp_path, *, probe=None, **cfg) -> ag.AdmissionService:
    clock = ManualClock(start=50_000.0)
    store = WorkStore(TaskStore(tmp_path / "t.db", clock=clock.time).open(), clock=clock.time)
    return ag.AdmissionService(
        store,
        limits=_limits(**cfg),
        clock=clock.time,
        containment_probe=probe or (lambda: _report()),
        slices=SliceManager(
            runner=lambda cmd: subprocess.CompletedProcess(cmd, 0, "", ""),
            systemctl="/usr/bin/systemctl",
        ),
        liveness=lambda ref: Liveness.STOPPED,
    )


# -- 7. a raising probe fails closed ---------------------------------------------------------


@pytest.mark.asyncio
async def test_raising_probe_fails_closed_in_hard_limit_mode(tmp_path, monkeypatch):
    def boom():
        raise OSError("bus gone")

    svc = _service(tmp_path, probe=boom, hard_limits=True)
    monkeypatch.setattr(ag, "_SERVICE", svc)
    monkeypatch.setattr(_containment, "_HARD_MODE_REPORT", None)
    report = await svc.containment_report(refresh=True)
    assert report.refusal_reason == _containment.REASON_UNKNOWN
    assert _containment.hard_mode_refusal() == _containment.REASON_UNKNOWN
    refused = await svc.admit(
        budget_key=ALICE.id,
        session_key="s",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="key-refused-1",
    )
    assert refused.rejected and refused.reason is ReasonCode.CONTAINMENT_UNAVAILABLE

    soft = _service(tmp_path / "soft", probe=boom)
    assert (await soft.containment_report(refresh=True)).refusal_reason is None


@pytest.mark.asyncio
async def test_startup_check_that_raises_publishes_a_refusal_in_hard_mode(monkeypatch):
    async def broken():
        raise RuntimeError("store exploded")

    monkeypatch.setattr(ag, "current_service", broken)
    monkeypatch.setattr(ag, "_configured_hard_limits", lambda: True)
    monkeypatch.setattr(_containment, "_HARD_MODE_REPORT", None)
    report = await ag.startup_check()
    assert report is not None and report.refusal_reason == _containment.REASON_UNKNOWN
    assert _containment.hard_mode_refusal() == _containment.REASON_UNKNOWN

    monkeypatch.setattr(ag, "_configured_hard_limits", lambda: False)
    monkeypatch.setattr(_containment, "_HARD_MODE_REPORT", None)
    soft = await ag.startup_check()
    assert soft is not None and soft.refusal_reason is None
    assert _containment.hard_mode_refusal() is None


# -- 8. enabling admission by config reaches the turn gate -------------------------------


@pytest.mark.asyncio
async def test_turn_gate_picks_up_admission_enabled_by_config(tmp_path, monkeypatch):
    off = effective_limits(shared_crew_config_from({}), auto_crew_concurrency=2)
    disabled = ag.AdmissionService(None, limits=off)
    store = WorkStore(TaskStore(tmp_path / "t.db").open())
    monkeypatch.setattr(ag, "_load_limits", lambda: _limits())
    monkeypatch.setattr(ag, "_open_default_store", lambda: store)
    monkeypatch.setattr(ag, "_SERVICE_LOOP", asyncio.get_running_loop())
    ag.install_service(disabled)
    monkeypatch.setattr(ag, "_last_config_at", -1e9)
    try:
        assert not turn_gate.admission_active()
        admission = await turn_gate.admit_turn(
            budget_key=ALICE.id, session_key="dashboard:cfg", text="hi"
        )
        assert admission is not None and admission.admitted
        assert turn_gate.admission_active()
    finally:
        ticker = ag._ticker
        if ticker is not None:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        ag.install_service(None)


@pytest.mark.asyncio
async def test_turn_gate_without_a_gateway_service_stays_off(monkeypatch):
    monkeypatch.setattr(ag, "_SERVICE", None)
    assert await turn_gate.admission_on() is False
    assert await turn_gate.admit_turn(budget_key=ALICE.id, session_key="s", text="x") is None
