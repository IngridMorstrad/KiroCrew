"""The enterprise grant and budget gates have a production call site.

``enterprise_gates`` asks ``GrantGate`` for every shared-access request and
``BudgetGate`` for every request that starts model work. With the default seam
both are a no-op; with a provider composed, both are enforced.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.shared_crew import enterprise_gates
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    Principal,
    PrincipalKind,
    Provenance,
    Transport,
    principal_id_for,
)
from kiro_crew.shared_crew.enterprise import (
    BudgetState,
    DeploymentContext,
    EnterpriseSeams,
    GrantState,
    IndividualGrant,
    OperationKind,
    resolve_enterprise,
)
from kiro_crew.shared_crew.enterprise_gates import (
    EnterpriseGates,
    chargeable_operation,
    current_gates,
    make_enterprise_gate_middleware,
)

T0 = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    enterprise_gates._reset_for_tests()
    yield
    enterprise_gates._reset_for_tests()


def _person(provenance: Provenance, identity_class: Optional[str], subject: str) -> Principal:
    return Principal(
        id=principal_id_for("edition", "issuer", subject),
        kind=PrincipalKind.HUMAN,
        provider="edition",
        issuer="issuer",
        subject=subject,
        provenance=provenance,
        extra={} if identity_class is None else {"identity_class": identity_class},
    )


ORG = _person(Provenance.ENTERPRISE, "organization_directory", "org")
INDIVIDUAL = _person(Provenance.ENTERPRISE, "individual", "solo")
TAILNET = _person(Provenance.TAILSCALE_SERVE, None, "tail")
OPERATOR = _person(Provenance.LOCAL_OPERATOR, None, "operator")
REVOKED = _person(Provenance.ENTERPRISE, "organization_directory", "gone")


class _Authority:
    def lookup(self, principal_id: str, crew_id: str) -> Optional[IndividualGrant]:
        if principal_id == REVOKED.id:
            return IndividualGrant(principal_id, crew_id, GrantState.REVOKED, 2)
        if principal_id in {ORG.id, TAILNET.id}:
            return IndividualGrant(principal_id, crew_id, GrantState.ACTIVE, 1)
        return None


class _Budget:
    def __init__(self, state: BudgetState) -> None:
        self.value = state

    def state(self, deployment: DeploymentContext) -> BudgetState:
        return self.value


def _seams(
    *, grants: bool = True, budget: Optional[BudgetState] = BudgetState.AVAILABLE
) -> EnterpriseSeams:
    return EnterpriseSeams(
        grant_authority=_Authority() if grants else None,
        inference_budget=None if budget is None else _Budget(budget),
        deployment=DeploymentContext(deployment_ref="dep-1"),
    )


def _app(gates: EnterpriseGates, principal: Optional[Principal]) -> web.Application:
    @web.middleware
    async def publish(request: web.Request, handler: Any) -> web.StreamResponse:
        if principal is not None:
            request[ACTOR_REQUEST_KEY] = ActorContext(
                principal=principal, request_id="r1", transport=Transport.HTTP
            )
        return await handler(request)

    async def ok(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app = web.Application(
        middlewares=[publish, make_enterprise_gate_middleware(lambda: gates, clock=lambda: T0)]
    )
    app.router.add_post("/api/chat", ok)
    app.router.add_get("/api/sessions", ok)
    app.router.add_post("/api/chat/slots/{slot}/regenerate", ok)
    return app


async def _call(app: web.Application, method: str, path: str) -> tuple[int, dict]:
    async with TestClient(TestServer(app)) as client:
        resp = await client.request(method, path)
        return resp.status, await resp.json()


class TestDisabledDefault:
    def test_default_seam_composes_no_gate(self) -> None:
        gates = current_gates(lambda: resolve_enterprise())
        assert not gates.configured
        assert gates.check_shared_access(INDIVIDUAL, T0).reason == "not_configured"
        assert gates.check_inference(OperationKind.NEW_TURN).reason == "not_configured"
        assert gates.check_inference(OperationKind.NEW_TURN).enforced is False

    @pytest.mark.asyncio
    async def test_default_seam_lets_every_request_through(self) -> None:
        gates = EnterpriseGates(EnterpriseSeams())
        assert await _call(_app(gates, INDIVIDUAL), "POST", "/api/chat") == (200, {"ok": True})


class TestSharedAccess:
    @pytest.mark.asyncio
    async def test_organization_and_tailnet_people_with_a_grant_pass(self) -> None:
        gates = EnterpriseGates(_seams())
        for person in (ORG, TAILNET):
            assert (await _call(_app(gates, person), "GET", "/api/sessions"))[0] == 200

    @pytest.mark.asyncio
    async def test_individual_account_gets_the_authority_answer_not_a_local_rule(self) -> None:
        status, body = await _call(
            _app(EnterpriseGates(_seams()), INDIVIDUAL), "GET", "/api/sessions"
        )
        # This authority holds no grant for the person, so it is "no_grant":
        # the refusal names the authority's answer, never an identity class.
        assert status == 403
        assert body == {
            "error": "shared_access_refused",
            "code": "shared_access_refused",
            "reason": "no_grant",
        }

    @pytest.mark.asyncio
    async def test_revoked_grant_is_refused(self) -> None:
        status, body = await _call(_app(EnterpriseGates(_seams()), REVOKED), "GET", "/api/sessions")
        assert (status, body["reason"]) == (403, "grant_revoked")

    @pytest.mark.asyncio
    async def test_local_operator_and_automation_are_not_shared_access(self) -> None:
        gates = EnterpriseGates(_seams())
        for principal in (OPERATOR, Principal.system("cron"), None):
            assert (await _call(_app(gates, principal), "POST", "/api/chat"))[0] == 200

    def test_grant_cache_survives_between_requests(self) -> None:
        seams = _seams()
        assert current_gates(lambda: seams) is current_gates(lambda: seams)


class TestInference:
    @pytest.mark.asyncio
    async def test_exhausted_budget_refuses_new_work_but_not_reads(self) -> None:
        gates = EnterpriseGates(_seams(grants=False, budget=BudgetState.EXHAUSTED))
        status, body = await _call(_app(gates, OPERATOR), "POST", "/api/chat")
        assert status == 403
        assert body == {
            "error": "inference_refused",
            "code": "inference_refused",
            "reason": "inference_budget_exhausted",
        }
        assert (await _call(_app(gates, OPERATOR), "GET", "/api/sessions"))[0] == 200

    @pytest.mark.asyncio
    async def test_unknown_budget_refuses_a_retry(self) -> None:
        gates = EnterpriseGates(_seams(grants=False, budget=BudgetState.UNKNOWN))
        status, body = await _call(_app(gates, OPERATOR), "POST", "/api/chat/slots/s1/regenerate")
        assert (status, body["reason"]) == (403, "inference_budget_unknown")

    @pytest.mark.asyncio
    async def test_available_budget_allows(self) -> None:
        gates = EnterpriseGates(_seams(grants=False, budget=BudgetState.AVAILABLE))
        assert (await _call(_app(gates, OPERATOR), "POST", "/api/chat"))[0] == 200

    @pytest.mark.parametrize(
        "method, path, op",
        [
            ("POST", "/api/chat", OperationKind.NEW_TURN),
            ("POST", "/v1/chat/completions", OperationKind.NEW_TURN),
            ("POST", "/api/chat/slots/abc/regenerate", OperationKind.RETRY),
            ("POST", "/api/spawn", OperationKind.SPAWN),
            ("POST", "/api/crons/j1/run", OperationKind.AUTOMATION_RUN),
            ("GET", "/api/chat", None),
            ("POST", "/api/chat/slots/abc/approve", None),
            ("POST", "/api/chat/extra", None),
        ],
    )
    def test_chargeable_routes(self, method, path, op) -> None:
        assert chargeable_operation(method, path) is op


def test_gateway_installs_the_gate() -> None:
    from pathlib import Path

    server = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard" / "server.py"
    assert "enterprise_gates.install(app)" in server.read_text(encoding="utf-8")


# -- model work with no request: cron schedules and channel turns ---------------


def _composed(budget: BudgetState):
    gates = EnterpriseGates(_seams(grants=False, budget=budget))
    return lambda: gates


@pytest.mark.asyncio
async def test_a_scheduled_cron_fire_is_refused_when_the_budget_is_spent(monkeypatch):
    from types import SimpleNamespace

    from kiro_crew.shared_crew import admission_entry

    monkeypatch.setattr(enterprise_gates, "current_gates", _composed(BudgetState.EXHAUSTED))
    ran: list[int] = []

    async def dispatch() -> str:
        ran.append(1)
        return "ran"

    job = SimpleNamespace(id="j1", created_by_principal="", last_status="", last_error="")
    assert await admission_entry.admitted_cron_dispatch(job, dispatch()) is None
    assert ran == []
    assert job.last_status == "error"
    assert "inference_budget_exhausted" in job.last_error


@pytest.mark.asyncio
async def test_a_cron_fire_runs_while_the_budget_allows(monkeypatch):
    from types import SimpleNamespace

    from kiro_crew.shared_crew import admission_entry
    from kiro_crew.shared_crew import admission_gate as ag

    monkeypatch.setattr(enterprise_gates, "current_gates", _composed(BudgetState.AVAILABLE))

    async def _unadmitted(coro, **_kw):
        return await coro

    monkeypatch.setattr(ag, "admit_and_run", _unadmitted)

    async def dispatch() -> str:
        return "ran"

    job = SimpleNamespace(id="j2", created_by_principal="", last_status="", last_error="")
    assert await admission_entry.admitted_cron_dispatch(job, dispatch()) == "ran"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "budget,reason",
    [
        (BudgetState.EXHAUSTED, "inference_budget_exhausted"),
        (BudgetState.UNKNOWN, "inference_budget_unknown"),
    ],
)
async def test_a_channel_turn_is_refused_when_the_budget_refuses(monkeypatch, budget, reason):
    from kiro_crew.shared_crew import turn_gate
    from kiro_crew.shared_crew.admission import OperationKind as Op

    monkeypatch.setattr(enterprise_gates, "current_gates", _composed(budget))
    ran: list[int] = []
    told: list[object] = []

    async def work() -> str:
        ran.append(1)
        return "ran"

    async def refused(why: object) -> None:
        told.append(why)

    assert (
        await turn_gate.run_channel_turn(
            session_key="slack:1", text="hi", automation=False, run=work, refused=refused
        )
        is None
    )
    assert (
        await turn_gate.run_admitted(
            work(), session_key="agent:x", operation=Op.CHANNEL_TURN, refused=told.append
        )
        is None
    )
    assert ran == []
    assert told == [reason, reason]
    assert "inference budget" in turn_gate.capacity_note(reason)


@pytest.mark.asyncio
async def test_an_http_turn_is_not_asked_twice(monkeypatch):
    from kiro_crew.shared_crew import turn_gate

    asked: list[int] = []

    def gates():
        asked.append(1)
        return EnterpriseGates(_seams(grants=False, budget=BudgetState.EXHAUSTED))

    monkeypatch.setattr(enterprise_gates, "current_gates", gates)

    async def off() -> bool:
        return False

    monkeypatch.setattr(turn_gate, "admission_on", off)

    async def work() -> str:
        return "ran"

    # A side chat or thread reply arrives over HTTP: the middleware already asked.
    assert await turn_gate.run_admitted(work(), session_key="side:x") == "ran"
    assert asked == []
