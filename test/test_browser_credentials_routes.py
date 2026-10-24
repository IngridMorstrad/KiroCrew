"""The owner's credential routes (``handlers/browser_credentials.py``): who may call
them, and that no response or audit row ever carries a cookie value."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.browser_harness import credentials
from kiro_crew.browser_harness.supervisor import HarnessSupervisor
from kiro_crew.dashboard import handlers
from kiro_crew.dashboard.handlers import _shared_request_body, _shared_session_memory
from kiro_crew.dashboard.handlers import browser_credentials as routes

_OWNER = "the-owner"
SECRET = "s3cr3t-cookie-value-77aa"
EXPORT = json.dumps(
    [{"name": "sid", "value": SECRET, "domain": ".example.com", "expires": time.time() + 3600}]
)


class _State:
    owner_id = _OWNER


class _Req:
    def __init__(self, sup: HarnessSupervisor, *, internal: bool = False, user: str = _OWNER):
        self.path = "/api/browser/cookies"
        self._claims: dict[str, Any] = {"user": user, "app": ""}
        if internal:
            self._claims["internal_auth"] = True
        self.app: dict[str, Any] = {"state": _State(), "browser_harness_supervisor": sup}

    def get(self, key: str, default: Any = None) -> Any:
        return self._claims.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self._claims

    def __getitem__(self, key: str) -> Any:
        return self._claims[key]


class _Sel:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.rows.append(kw)


class _Sup(HarnessSupervisor):
    """No browser: records the refresh and forget calls the routes make."""

    def __init__(self, home: Path) -> None:
        super().__init__(home, config_reader=lambda: {})
        self.calls: list[str] = []

    async def refresh_credentials(self) -> dict[str, Any]:
        self.calls.append("refresh")
        return self._credentials_snapshot()

    async def forget_imported(self, cookies: list[dict[str, Any]]) -> int:
        self.calls.append(f"forget:{len(cookies)}")
        return len(cookies)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[_Sup, _Sel, dict[str, Any]]:
    sel = _Sel()
    body: dict[str, Any] = {"content": EXPORT}
    monkeypatch.setattr(handlers, "sel", lambda: sel)
    monkeypatch.setattr(routes, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(_shared_session_memory, "_is_restricted_session", lambda s, r: False)

    async def read_body(request: Any, max_bytes: Any = None, **_kw: Any) -> Any:
        assert max_bytes == routes._MAX_BODY_BYTES
        return dict(body), None

    monkeypatch.setattr(_shared_request_body, "read_bounded_json", read_body)
    return _Sup(tmp_path), sel, body


def _json(resp: Any) -> dict[str, Any]:
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_import_stores_applies_and_reports_without_the_value(env: Any) -> None:
    sup, sel, _body = env
    resp = await routes.api_browser_cookies_import(_Req(sup))
    assert resp.status == 200
    payload = _json(resp)
    assert payload["summary"]["cookie_count"] == 1
    assert payload["summary"]["domains"] == ["example.com"]
    assert sup.calls == ["refresh"]
    assert credentials.load_imported(sup.home)[0]["value"] == SECRET
    status = _json(await routes.api_browser_cookies_status(_Req(sup)))
    assert status["present"] is True
    for text in (resp.body.decode(), json.dumps(status), json.dumps(sel.rows)):
        assert SECRET not in text
    assert "cookies=1 domains=example.com" in sel.rows[1]["resources"]


@pytest.mark.asyncio
async def test_a_bad_import_is_a_400_and_stores_nothing(env: Any) -> None:
    sup, _sel, body = env
    body["content"] = "{nope"
    resp = await routes.api_browser_cookies_import(_Req(sup))
    assert (resp.status, _json(resp)["code"]) == (400, "invalid_cookies")
    body["content"] = 7
    assert (await routes.api_browser_cookies_import(_Req(sup))).status == 400
    assert not credentials.has_imported(sup.home) and sup.calls == []


@pytest.mark.asyncio
async def test_clear_deletes_and_expires_only_the_import(env: Any) -> None:
    sup, _sel, _body = env
    await routes.api_browser_cookies_import(_Req(sup))
    resp = await routes.api_browser_cookies_clear(_Req(sup))
    assert _json(resp) == {"ok": True, "present": False, "expired": 1}
    assert sup.calls == ["refresh", "forget:1"]
    assert not credentials.has_imported(sup.home)


@pytest.mark.asyncio
async def test_refresh_returns_the_credentials_status(env: Any) -> None:
    sup, _sel, _body = env
    payload = _json(await routes.api_browser_credentials_refresh(_Req(sup)))
    assert payload["ok"] is True and payload["credentials"]["provider"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [
        routes.api_browser_cookies_status,
        routes.api_browser_cookies_import,
        routes.api_browser_cookies_clear,
        routes.api_browser_credentials_refresh,
    ],
)
async def test_the_agent_and_non_owners_are_refused(env: Any, handler: Any) -> None:
    sup, sel, _body = env
    resp = await handler(_Req(sup, internal=True))
    assert (resp.status, _json(resp)["code"]) == (403, "internal_caller")
    assert (await handler(_Req(sup, user="someone-else"))).status == 403
    assert sel.rows[-1]["outcome"] == "denied"
    assert sup.calls == [] and not credentials.has_imported(sup.home)


@pytest.mark.asyncio
async def test_a_restricted_session_is_refused(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    sup, _sel, _body = env
    monkeypatch.setattr(_shared_session_memory, "_is_restricted_session", lambda s, r: True)
    resp = await routes.api_browser_cookies_import(_Req(sup))
    assert (resp.status, _json(resp)["code"]) == (403, "restricted_session")
    assert not credentials.has_imported(sup.home)


def test_the_routes_are_registered() -> None:
    from aiohttp import web

    from kiro_crew.dashboard import server

    app = web.Application()
    server._register_mcp_routes(app)
    table = {
        (r.method, r.resource.canonical): r.handler.__name__
        for r in app.router.routes()
        if r.resource is not None
    }
    assert table[("GET", "/api/browser/cookies")] == "api_browser_cookies_status"
    assert table[("POST", "/api/browser/cookies")] == "api_browser_cookies_import"
    assert table[("DELETE", "/api/browser/cookies")] == "api_browser_cookies_clear"
    assert table[("POST", "/api/browser/credentials/refresh")] == "api_browser_credentials_refresh"
