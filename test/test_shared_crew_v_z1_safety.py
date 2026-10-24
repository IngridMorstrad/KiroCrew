"""Startup readiness and unavailable revocation authorities preserve their fences."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from test_shared_crew_enterprise_gates import OPERATOR, ORG, _Authority

from kiro_crew.config.sections_shared_crew import effective_limits, shared_crew_config_from
from kiro_crew.dashboard import server
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import containment
from kiro_crew.shared_crew import enterprise_gates as gates
from kiro_crew.shared_crew import enterprise_revocation as rev
from kiro_crew.shared_crew import routes_admission, wiring
from kiro_crew.shared_crew.admission import OperationKind
from kiro_crew.shared_crew.contracts import ACTOR_REQUEST_KEY, ActorContext, Transport
from kiro_crew.shared_crew.enterprise import DeploymentContext, EnterpriseSeams


def _service():
    limits = effective_limits(
        shared_crew_config_from({"admission_enabled": True, "hard_limits": True}),
        auto_crew_concurrency=2,
    )
    return ag.AdmissionService(
        None,
        limits=limits,
        containment_probe=lambda: containment.failed_report(hard_limits=True, detail="no bus"),
    )


@pytest.mark.asyncio
async def test_startup_publishes_hard_refusal_without_maintenance(monkeypatch):
    service = _service()
    monkeypatch.setattr(ag, "_SERVICE", service)
    monkeypatch.setattr(containment, "_HARD_MODE_REPORT", None)
    monkeypatch.setattr(ag, "current_service", AsyncMock(return_value=service))
    reconcile = AsyncMock()
    sweep = AsyncMock(return_value=0)
    monkeypatch.setattr(service, "ensure_ready", reconcile)
    monkeypatch.setattr(service, "release_stale_slices", sweep)
    report = await ag.startup_check()
    assert report.refusal_reason == containment.REASON_UNKNOWN
    assert containment.hard_mode_refusal() == containment.REASON_UNKNOWN
    reconcile.assert_not_awaited()
    sweep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", [False, True])
async def test_maintenance_runs_after_bind_and_cleanup_owns_it(monkeypatch, finish):
    service = _service()
    entered = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()
    swept = asyncio.Event()

    async def reconcile():
        entered.set()
        try:
            await asyncio.wait_for(release.wait(), timeout=30)
        finally:
            settled.set()

    monkeypatch.setattr(ag, "_SERVICE", service)
    monkeypatch.setattr(ag, "startup_check", AsyncMock())
    monkeypatch.setattr(wiring, "install", Mock())
    monkeypatch.setattr(service, "ensure_ready", reconcile)
    sweep = AsyncMock(side_effect=lambda: swept.set())
    monkeypatch.setattr(service, "release_stale_slices", sweep)
    shutdown = AsyncMock(return_value=0)
    monkeypatch.setattr(ag, "shutdown_cleanup", shutdown)
    app = web.Application()
    app["state"] = SimpleNamespace()
    start_maintenance = gates.install_lifecycle(app)
    runner = web.AppRunner(app)
    try:
        await runner.setup()
        assert not entered.is_set()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert runner.addresses[0][0] == "127.0.0.1"
        assert callable(start_maintenance)
        start_maintenance()
        await asyncio.wait_for(entered.wait(), timeout=30)
        start_maintenance()
        sweep.assert_not_awaited()
        if finish:
            release.set()
            await asyncio.wait_for(swept.wait(), timeout=30)
            sweep.assert_awaited_once()
    finally:
        await asyncio.wait_for(runner.cleanup(), timeout=30)
    assert settled.is_set()
    shutdown.assert_awaited_once()


@pytest.mark.parametrize("entrypoint", [server.start_dashboard, server.start_api_server])
def test_both_servers_start_maintenance_only_after_socket_accepts(entrypoint):
    source = inspect.getsource(entrypoint)
    bind = (
        "await site.start()"
        if entrypoint is server.start_dashboard
        else "await _start_site(site, port)"
    )
    registration = "start_shared_crew_maintenance = enterprise_gates.install_lifecycle(app)"
    assert source.index(registration) < source.index("await runner.setup()") < source.index(bind)
    assert source.index(bind) < source.index("start_shared_crew_maintenance()")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["admit", "capacity"])
async def test_first_admission_and_capacity_still_reconcile(monkeypatch, operation):
    service = _service()
    reconcile = AsyncMock()
    monkeypatch.setattr(service, "ensure_ready", reconcile)
    monkeypatch.setattr(ag, "current_service", AsyncMock(return_value=service))
    if operation == "admit":
        await service.admit(
            budget_key=ORG.id,
            session_key="session",
            operation=OperationKind.CHAT_TURN,
            idempotency_key="first-admission",
        )
    else:
        await routes_admission.api_capacity(
            make_mocked_request("GET", routes_admission.CAPACITY_PATH)
        )
    reconcile.assert_awaited_once()


class _RecoverableFeed:
    def __init__(self, failure):
        self.failure = failure
        self.healthy = False
        self.policy_reads = 0

    def policy(self):
        self.policy_reads += 1
        if self.healthy:
            return rev.RevocationPolicy(30, 10)
        if self.failure == "raises":
            raise rev.RevocationFeedUnavailable("unavailable")
        return rev.RevocationPolicy(30, 30)

    def removals_since(self, cursor):
        return (rev.Removal(ORG.id, "crew-test", 1, 100),), 1


@pytest.fixture
def failed_feed(monkeypatch, request):
    monkeypatch.setattr(gates, "_CACHED", None)
    monkeypatch.setattr(rev, "_ACTIVE", None)
    feed = _RecoverableFeed(request.param)
    seams = EnterpriseSeams(
        grant_authority=_Authority(),
        revocation_feed=feed,
        deployment=DeploymentContext(deployment_ref="crew-test"),
        channel_actors=SimpleNamespace(principal_for=lambda *_: ORG),
    )
    return feed, lambda: gates.current_gates(lambda: seams)


@pytest.mark.parametrize("failed_feed", ["raises", "invalid"], indirect=True)
@pytest.mark.parametrize("point", list(rev.EnforcementPoint))
def test_unbuildable_feed_refuses_shared_access(failed_feed, point):
    _, current = failed_feed
    gate = current()
    verdict = gate.check_shared_access(ORG, 100, point)
    assert not verdict.allowed
    assert verdict.reason == "authority_stale"
    assert gate.check_shared_access(OPERATOR, 100).allowed


@pytest.mark.parametrize("failed_feed", ["raises", "invalid"], indirect=True)
def test_unbuildable_feed_refuses_channel_and_queue(failed_feed):
    _, current = failed_feed
    gate = current()
    assert rev.queued_dispatch_recheck(ORG.id, 100) == "authority_stale"
    verdict, person = gate.check_channel_actor("test", "person", 100)
    assert not verdict.allowed and person is None
    assert verdict.reason == "authority_stale"


@pytest.mark.parametrize("failed_feed", ["raises", "invalid"], indirect=True)
def test_same_feed_recovers_without_gateway_restart(failed_feed):
    feed, current = failed_feed
    failed = current()
    feed.healthy = True
    recovered = current()
    assert recovered is not failed
    assert feed.policy_reads == 2
    assert recovered is current()
    assert recovered.revocation is rev.active_enforcer()
    assert recovered.revocation.sync(101)
    assert recovered.check_shared_access(ORG, 101).reason == "member_removed"
    assert rev.queued_dispatch_recheck(ORG.id, 101) == "member_removed"


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_feed", ["raises", "invalid"], indirect=True)
async def test_failed_feed_middleware_refuses_approval(failed_feed):
    _, current = failed_feed
    middleware = gates.make_enterprise_gate_middleware(
        current, clock=lambda: 100, team_check=lambda _: None
    )
    request = make_mocked_request("POST", "/api/chat/slots/session/approve")
    request[ACTOR_REQUEST_KEY] = ActorContext(
        principal=ORG, request_id="request", transport=Transport.HTTP
    )
    request["user"] = "person"
    handler = AsyncMock(return_value=web.Response())
    response = await middleware(request, handler)
    assert response.status == 403
    handler.assert_not_awaited()
