"""API-only gateways install Shared Crew enforcement before serving traffic."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.config.sections_shared_crew import effective_limits, shared_crew_config_from
from kiro_crew.dashboard import server
from kiro_crew.shared_crew import admission_gate, containment, enterprise_gates, turn_gate, wiring
from kiro_crew.shared_crew.enterprise import BudgetState, DeploymentContext, EnterpriseSeams


class _BeforeBind(Exception):
    pass


@asynccontextmanager
async def _headless_app(tmp_path, monkeypatch):
    # An ``async with`` helper rather than an async generator fixture: the pinned
    # pytest-asyncio does not run async generator fixtures.
    import test_dashboard_server_coverage as harness

    runners = []
    build_runner = server.build_hardened_runner

    def capture(app, **kwargs):
        runner = build_runner(app, **kwargs)
        setup = runner.setup

        async def setup_before_bind():
            await setup()
            raise _BeforeBind

        class BeforeBindRunner:
            async def setup(self):
                await setup_before_bind()

        runners.append(runner)
        return BeforeBindRunner()

    monkeypatch.setattr(server, "build_hardened_runner", capture)
    monkeypatch.setattr(wiring, "install", Mock())
    monkeypatch.setattr(admission_gate, "shutdown_cleanup", AsyncMock(return_value=0))
    monkeypatch.setattr(admission_gate, "_SERVICE", None)
    monkeypatch.setattr(containment, "_HARD_MODE_REPORT", None)
    limits = effective_limits(
        shared_crew_config_from({"admission_enabled": True, "hard_limits": True}),
        auto_crew_concurrency=2,
    )
    service = admission_gate.AdmissionService(
        None,
        limits=limits,
        containment_probe=lambda: containment.failed_report(hard_limits=True, detail="no bus"),
    )
    monkeypatch.setattr(service, "release_stale_slices", AsyncMock(return_value=0))

    async def current_service():
        monkeypatch.setattr(admission_gate, "_SERVICE", service)
        return service

    monkeypatch.setattr(admission_gate, "current_service", current_service)
    try:
        with pytest.raises(_BeforeBind):
            await harness._start_api(tmp_path, monkeypatch)
        assert len(runners) == 1
        yield runners[0].app
    finally:
        for runner in runners:
            await runner.cleanup()


@pytest.mark.asyncio
async def test_headless_installs_admission_and_hard_refusal_before_bind(tmp_path, monkeypatch):
    async with _headless_app(tmp_path, monkeypatch) as headless_app:
        assert await turn_gate.admission_on()
        assert containment.hard_mode_refusal() == containment.REASON_UNKNOWN
        wiring.install.assert_called_once_with(headless_app["state"])


@pytest.mark.asyncio
async def test_headless_installs_enterprise_gate_inside_auth(tmp_path, monkeypatch):
    async with _headless_app(tmp_path, monkeypatch) as headless_app:
        middlewares = list(headless_app.middlewares)
    gate = next((mw for mw in middlewares if mw.__name__ == "enterprise_gate_middleware"), None)
    assert gate is not None
    auth = next(mw for mw in middlewares if getattr(mw, "_is_token_auth", False))
    assert middlewares.index(auth) < middlewares.index(gate)


@pytest.mark.asyncio
async def test_installed_headless_gate_refuses_exhausted_budget(tmp_path, monkeypatch):
    class Budget:
        def state(self, deployment):
            return BudgetState.EXHAUSTED

    gates = enterprise_gates.EnterpriseGates(
        EnterpriseSeams(inference_budget=Budget(), deployment=DeploymentContext())
    )
    async with _headless_app(tmp_path, monkeypatch) as headless_app:
        monkeypatch.setattr(enterprise_gates, "resolve_enterprise", lambda: gates.seams)
        monkeypatch.setattr(enterprise_gates, "_CACHED", None)
        gate = next(
            (mw for mw in headless_app.middlewares if mw.__name__ == "enterprise_gate_middleware"),
            None,
        )
        assert gate is not None
        # The installed factory holds current_gates; its resolver default is patched
        # at that function's own binding rather than substituting the middleware.
        monkeypatch.setattr(enterprise_gates.current_gates, "__defaults__", (lambda: gates.seams,))
        handler = AsyncMock(return_value=web.json_response({"ok": True}))
        request = make_mocked_request("POST", "/api/spawn", app=headless_app)
        response = await gate(request, handler)
    assert response.status == 403
    handler.assert_not_awaited()
