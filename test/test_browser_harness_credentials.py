"""Browser credentials: the edition seam, the owner's import, and how both reach
Kiro's Chromium (``browser_harness/credentials.py`` and the supervisor's start).

Every test is deterministic: the relay is a fake that records the CDP it is asked to
send, and no browser, daemon or network is involved.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import sandbox_leaves, sandbox_precreate
from kiro_crew.browser_harness import HARNESS_VERSION, credentials, supervisor
from kiro_crew.browser_harness.supervisor import HarnessSupervisor
from kiro_crew.platform import interfaces
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.defaults import DefaultBrowserCredentialProvider
from kiro_crew.security import paths_lists

#: A value no output except the browser-bound CDP params may ever carry.
SECRET = "s3cr3t-cookie-value-9f1c"
FUTURE = time.time() + 86_400


class FakeRelay:
    """Records every relay-owned CDP request; ``fail_batches`` refuses multi-cookie sends."""

    def __init__(self, *, fail_batches: bool = False, refuse: set[str] | None = None) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.fail_batches = fail_batches
        self.refuse = refuse or set()
        self.closed = False
        self.on_upstream_closed: Any = None

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        params = params or {}
        self.sent.append((method, params))
        cookies = params.get("cookies") or []
        if self.fail_batches and len(cookies) > 1:
            raise supervisor.CdpError(method, {"message": "Invalid cookie fields"})
        if any(c.get("name") in self.refuse for c in cookies):
            raise supervisor.CdpError(method, {"message": "Invalid cookie fields"})
        return {}

    async def start(self, upstream: Any) -> str:
        return "http://127.0.0.1:1/secret"

    async def close(self) -> None:
        self.closed = True

    async def close_agent_targets(self) -> None:
        return None

    def agent_targets(self) -> list[str]:
        return []

    def cookies_set(self) -> list[dict[str, Any]]:
        return [c for m, p in self.sent if m == "Storage.setCookies" for c in p["cookies"]]


class FakeProvider:
    def __init__(
        self,
        cookies: list[dict[str, Any]] | None = None,
        *,
        domains: tuple[str, ...] = ("example.com",),
        launch_args: list[str] | None = None,
        raises: Exception | None = None,
        defaults: dict[str, Any] | None = None,
    ) -> None:
        self._cookies = cookies if cookies is not None else [_cookie()]
        self._domains = domains
        self._launch_args = launch_args or []
        self._raises = raises
        self._defaults = (
            defaults if defaults is not None else {"enabled": True, "domains": list(domains)}
        )
        self.targets: list[Any] = []
        self.fp: str | None = None
        self.status_value: Any = None
        self.keep = False

    def name(self) -> str:
        return "Test sign-in"

    def domains(self) -> tuple[str, ...]:
        return self._domains

    def cookies(self, target: Any) -> list[dict[str, Any]]:
        self.targets.append(target)
        if self._raises is not None:
            raise self._raises
        return list(self._cookies)

    def launch_args(self) -> list[str]:
        return list(self._launch_args)

    def default_settings(self) -> dict[str, Any]:
        return dict(self._defaults)

    def fingerprint(self) -> str | None:
        return self.fp

    def keep_expiry(self) -> bool:
        return self.keep

    def status(self) -> Any:
        return self.status_value


async def _apply(
    relay: Any, provider: Any, home: Path, kind: str = "kiro_chromium", **kw: Any
) -> Any:
    """``credentials.apply`` with the settings the supervisor would resolve (no config)."""
    settings = kw.pop("settings", None) or credentials.effective_settings(None, provider)
    return await credentials.apply(
        relay, provider=provider, home=home, target=_target(kind), settings=settings, **kw
    )


def _cookie(**over: Any) -> dict[str, Any]:
    base = {
        "name": "session",
        "value": SECRET,
        "domain": ".example.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
        "expires": FUTURE,
    }
    base.update(over)
    return base


def _target(kind: str = "kiro_chromium", reason: str = "start") -> Any:
    return credentials.Target(kind=kind, headless=True, reason=reason)


# ── the seam ──


def test_the_default_context_composes_a_provider_that_supplies_nothing() -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    ctx = build_default_context(KiroCrewConfig())
    provider = ctx.browser_credentials
    assert isinstance(provider, DefaultBrowserCredentialProvider)
    assert provider.name() is None
    assert list(provider.domains()) == []
    assert list(provider.cookies(_target())) == []
    assert list(provider.launch_args()) == []
    assert interfaces.BrowserCredentialProvider is not None
    assert interfaces.BrowserCredentialTarget is credentials.Target


@pytest.mark.asyncio
async def test_no_provider_and_no_import_writes_nothing(tmp_path: Path) -> None:
    relay = FakeRelay()
    status = await credentials.apply(
        relay, provider=DefaultBrowserCredentialProvider(), home=tmp_path, target=_target()
    )
    assert relay.sent == []
    assert status.state == credentials.STATE_NONE
    assert status.as_dict()["provider"] is None and status.as_dict()["applied"] == 0


@pytest.mark.asyncio
async def test_a_provider_s_cookies_are_set_through_the_relay(tmp_path: Path) -> None:
    relay = FakeRelay()
    provider = FakeProvider([_cookie(), _cookie(name="pref", expires=None, httpOnly="false")])
    status = await _apply(relay, provider, tmp_path)
    assert [m for m, _ in relay.sent] == ["Storage.setCookies"]
    names = {c["name"] for c in relay.cookies_set()}
    assert names == {"session", "pref"}
    pref = next(c for c in relay.cookies_set() if c["name"] == "pref")
    assert "expires" not in pref and pref["httpOnly"] is False
    assert status.state == credentials.STATE_APPLIED
    snap = status.as_dict()
    assert snap["provider"] == "Test sign-in" and snap["applied"] == 2
    assert snap["sources"][0]["domains"] == ["example.com"]
    (seen,) = provider.targets
    assert (seen.kind, seen.headless, seen.reason) == ("kiro_chromium", True, "start")


@pytest.mark.asyncio
async def test_only_the_provider_s_declared_domains_are_set(tmp_path: Path) -> None:
    relay = FakeRelay()
    provider = FakeProvider(
        [
            _cookie(domain="example.com"),
            _cookie(name="sub", domain="app.example.com"),
            _cookie(name="other", domain="example.org"),
            _cookie(name="suffix", domain="notexample.com"),
            _cookie(name="parent", domain=".com"),
        ],
        domains=("Example.com", "com", "127.0.0.1", "*.wild.example", "host:443"),
    )
    status = await _apply(relay, provider, tmp_path)
    assert {c["name"] for c in relay.cookies_set()} == {"session", "sub"}
    assert status.sources[0].dropped == 3


def test_declared_domains_refuse_anything_broader_than_a_host() -> None:
    assert credentials.normalize_declared_domains(
        [".Example.com", "com", "localhost", "10.0.0.1", "*.x.com", "a.b:80", 7, "intra.example"]
    ) == ("example.com", "intra.example")


@pytest.mark.asyncio
async def test_the_person_s_own_chrome_is_never_filled(tmp_path: Path) -> None:
    credentials.save_imported(tmp_path, [credentials.normalize_cookie(_cookie())])
    for kind in ("user_chrome", "edge"):
        relay = FakeRelay()
        provider = FakeProvider()
        status = await _apply(relay, provider, tmp_path, kind)
        assert relay.sent == [] and provider.targets == []
        assert (status.state, status.reason) == (credentials.STATE_SKIPPED, "user_browser")


@pytest.mark.asyncio
async def test_one_bad_cookie_does_not_lose_the_batch(tmp_path: Path) -> None:
    relay = FakeRelay(fail_batches=True, refuse={"bad"})
    provider = FakeProvider([_cookie(), _cookie(name="bad"), _cookie(name="ok2")])
    status = await _apply(relay, provider, tmp_path)
    assert status.sources[0].applied == 2 and status.sources[0].dropped == 1
    assert status.state == credentials.STATE_APPLIED


@pytest.mark.asyncio
async def test_a_failing_provider_is_a_code_and_never_its_message(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    relay = FakeRelay()
    provider = FakeProvider(raises=RuntimeError(f"cookie jar said {SECRET}"))
    with caplog.at_level(logging.DEBUG):
        status = await _apply(relay, provider, tmp_path)
    assert relay.sent == []
    assert (status.state, status.reason) == (credentials.STATE_ERROR, "provider_failed")
    assert SECRET not in json.dumps(status.as_dict())
    assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_a_hung_provider_times_out_and_the_browser_still_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    gate = threading.Event()

    class Hung(FakeProvider):
        def cookies(self, target: Any) -> list[dict[str, Any]]:
            gate.wait()
            return []

    monkeypatch.setattr(credentials, "PROVIDER_TIMEOUT_SECS", 0.05)
    try:
        status = await _apply(FakeRelay(), Hung(), tmp_path)
    finally:
        gate.set()
    assert status.reason == "provider_timeout"


@pytest.mark.asyncio
async def test_a_provider_returning_too_many_or_a_bad_shape_is_refused(tmp_path: Path) -> None:
    many = FakeProvider([_cookie(name=f"c{i}") for i in range(credentials.MAX_COOKIES + 1)])
    status = await _apply(FakeRelay(), many, tmp_path)
    assert status.reason == "too_many_cookies"

    class Odd(FakeProvider):
        def cookies(self, target: Any) -> Any:
            return {"not": "a list"}

    status = await _apply(FakeRelay(), Odd(), tmp_path)
    assert status.reason == "provider_bad_shape"


# ── normalising ──


def test_a_cookie_is_normalised_to_cdp_s_shape() -> None:
    out = credentials.normalize_cookie(
        {
            "name": "a",
            "value": "v",
            "domain": ".Example.COM",
            "expirationDate": "1999999999",
            "secure": "TRUE",
            "sameSite": "no_restriction",
        }
    )
    assert out == {
        "name": "a",
        "value": "v",
        "domain": ".example.com",
        "path": "/",
        "secure": True,
        "httpOnly": False,
        "sameSite": "None",
        "expires": 1999999999.0,
    }
    # SameSite=None without Secure is refused by Chromium; it is set as Lax instead.
    lax = credentials.normalize_cookie({"name": "a", "domain": "x.com", "sameSite": "none"})
    assert lax["sameSite"] == "Lax" and "expires" not in lax


@pytest.mark.parametrize(
    "raw",
    [
        "not an object",
        {"domain": "x.com"},
        {"name": "a"},
        {"name": "a", "domain": "."},
        {"name": "a\n", "domain": "x.com"},
        {"name": "a", "domain": "x.com", "value": "v\x00"},
        {"name": "a", "domain": "x.com", "value": "v" * 5000},
        {"name": "a", "domain": "x.com", "secure": "yes"},
        {"name": "a", "domain": "x.com", "httpOnly": 2},
    ],
)
def test_a_malformed_cookie_is_refused_without_naming_its_value(raw: Any) -> None:
    with pytest.raises(credentials.CookieImportError) as err:
        credentials.normalize_cookie(raw)
    assert "v\x00" not in str(err.value) and "vvvv" not in str(err.value)


def test_launch_args_keep_only_the_negotiate_allowlist() -> None:
    kept, dropped = credentials.filter_launch_args(
        [
            "--auth-server-allowlist=*.intra.example,intranet",
            "--auth-server-allowlist=twice.example",
            "--remote-debugging-port=9222",
            "--disable-web-security",
            "--proxy-server=evil:1",
            "--auth-server-allowlist=bad value",
            "--auth-server-allowlist",
            7,
        ]
    )
    assert kept == ["--auth-server-allowlist=*.intra.example,intranet"]
    assert dropped == 7
    assert credentials.filter_launch_args("--auth-server-allowlist=x") == ([], 0)


# ── the owner's import ──


def test_the_three_import_shapes_parse_and_expired_cookies_drop() -> None:
    state = json.dumps({"cookies": [_cookie(), _cookie(name="old", expires=5)], "origins": []})
    assert [c["name"] for c in credentials.parse_cookie_import(state)] == ["session"]
    array = json.dumps([{"name": "a", "value": "1", "domain": "x.com", "expirationDate": FUTURE}])
    assert credentials.parse_cookie_import(array)[0]["expires"] == FUTURE
    netscape = (
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.x.com\tTRUE\t/\tTRUE\t0\tsid\tabc\n"
        "x.com\tFALSE\t/p\tFALSE\t0\tempty\t\n"
    )
    rows = credentials.parse_cookie_import(netscape)
    assert [(c["name"], c["httpOnly"], c["value"], c["path"]) for c in rows] == [
        ("sid", True, "abc", "/"),
        ("empty", False, "", "/p"),
    ]


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "empty"),
        ("x" * (credentials.MAX_IMPORT_BYTES + 1), "too large"),
        ("{bad json", "not valid JSON"),
        ('{"cookies": 1}', '"cookies" array'),
        ("[NaN]", "invalid JSON constant"),
        ("7", "neither JSON"),
        (json.dumps([_cookie(expires=5)]), "no unexpired"),
    ],
    ids=["empty", "too-large", "bad-json", "cookies-not-array", "nan", "scalar", "all-expired"],
)
def test_a_bad_import_is_refused_with_a_message(text: str, message: str) -> None:
    with pytest.raises(credentials.CookieImportError, match=message):
        credentials.parse_cookie_import(text)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_the_import_is_stored_owner_only_and_round_trips(tmp_path: Path) -> None:
    cookies = credentials.parse_cookie_import(json.dumps([_cookie()]))
    credentials.save_imported(tmp_path, cookies)
    path = credentials.import_path(tmp_path)
    assert path.parent.name == credentials.IMPORT_DIR
    assert path.parent.parent.name == supervisor.IPC_LEAF
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert credentials.load_imported(tmp_path) == cookies
    summary = credentials.summarize(cookies)
    assert summary == {"cookie_count": 1, "domains": ["example.com"], "earliest_expiry": FUTURE}
    assert SECRET not in json.dumps(summary)
    assert credentials.clear_imported(tmp_path) == cookies
    assert not path.exists() and credentials.load_imported(tmp_path) == []


@pytest.mark.asyncio
async def test_an_import_and_a_provider_are_both_applied_and_reported_apart(
    tmp_path: Path,
) -> None:
    credentials.save_imported(
        tmp_path, [credentials.normalize_cookie(_cookie(name="imp", domain="other.test"))]
    )
    relay = FakeRelay()
    status = await _apply(relay, FakeProvider(), tmp_path)
    assert {c["name"] for c in relay.cookies_set()} == {"session", "imp"}
    assert [s["source"] for s in status.as_dict()["sources"]] == ["provider", "imported"]
    assert status.imported is True


@pytest.mark.asyncio
async def test_clearing_expires_exactly_the_imported_cookies() -> None:
    relay = FakeRelay()
    gone = await credentials.expire(relay, [credentials.normalize_cookie(_cookie())])
    assert gone == 1
    (cookie,) = relay.cookies_set()
    assert cookie == {
        "name": "session",
        "value": "",
        "domain": ".example.com",
        "path": "/",
        "expires": 1,
    }


def test_the_import_lives_in_a_leaf_every_fence_already_hides(tmp_path: Path) -> None:
    """No new sandbox entry: the import sits inside the Harness IPC leaf."""
    leaf = credentials.import_path(tmp_path).relative_to(tmp_path).parts[0]
    assert leaf == supervisor.IPC_LEAF
    assert leaf in sandbox_leaves._CREW_HIDDEN_LEAVES
    assert leaf in sandbox_precreate._CREW_PRECREATE_HIDDEN_DIR_LEAVES
    assert leaf in paths_lists._CREW_SECRET_LEAVES


# ── agent-visible surfaces ──


def test_the_snapshot_walker_never_reads_document_cookie() -> None:
    walker = (Path(supervisor.__file__).parent / "snapshot_walker.js").read_text(encoding="utf-8")
    assert "document.cookie" not in walker
    assert "cookieStore" not in walker


def test_no_helper_the_agent_reaches_can_read_cookies() -> None:
    for name in supervisor.HELPERS:
        assert "cookie" not in name.lower()
    from kiro_crew.browser_harness import ops

    assert not any("cookie" in op.lower() for op in ops.OPS)


# ── the supervisor: the apply point, before the daemon, on every start ──


class _Proc:
    def __init__(self) -> None:
        self.returncode: int | None = None


def _supervisor_with_fakes(
    home: Path, monkeypatch: pytest.MonkeyPatch, provider: Any, kind: str = "kiro_chromium"
) -> tuple[HarnessSupervisor, list[str], list[FakeRelay], list[list[str]]]:
    events: list[str] = []
    relays: list[FakeRelay] = []
    argvs: list[list[str]] = []
    exe = home / "chrome"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        supervisor.install,
        "install_state",
        lambda _home: supervisor.install.InstallState(
            prefix=home,
            installed=True,
            version=HARNESS_VERSION,
            python=Path(sys.executable),
            chromium=supervisor.install.chromium.ChromiumState(
                version="x", platform="mac-arm64", present=True, executable=exe
            ),
        ),
    )

    def new_relay() -> FakeRelay:
        relay = FakeRelay()
        original = relay.send

        async def send(method: str, params: Any = None, session_id: Any = None) -> Any:
            events.append(method)
            return await original(method, params, session_id)

        relay.send = send  # type: ignore[method-assign]
        relays.append(relay)
        return relay

    monkeypatch.setattr(supervisor, "CdpRelay", new_relay)
    sup = HarnessSupervisor(home, config_reader=lambda: {}, credential_provider=lambda: provider)
    monkeypatch.setattr(sup, "runtime_dir", lambda: home / "run")
    choice = supervisor.discover.BrowserChoice(kind, exe, kind == "kiro_chromium", None)  # type: ignore[arg-type]
    monkeypatch.setattr(sup, "_choose", lambda state: choice)

    async def launch(self: HarnessSupervisor, argv: list[str], *_rest: Any) -> Any:
        argvs.append(argv)
        return _Proc(), "ws://127.0.0.1:1/x"

    monkeypatch.setattr(HarnessSupervisor, "_launch_chromium_pipe", launch)
    monkeypatch.setattr(HarnessSupervisor, "_launch_chromium_port", launch)

    async def user_ws(_choice: Any) -> str:
        return "ws://127.0.0.1:1/devtools/browser/x"

    async def start_daemon(python: Any, relay_url: str) -> Any:
        events.append("daemon")
        return _Proc()

    async def start_worker(handle: Any) -> Any:
        return _Proc()

    async def nothing(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(sup, "_user_browser_ws_url", user_ws)
    monkeypatch.setattr(sup, "_start_daemon", start_daemon)
    monkeypatch.setattr(sup, "_start_worker", start_worker)
    monkeypatch.setattr(sup, "_ipc_shutdown", nothing)
    monkeypatch.setattr(sup, "_shutdown_stray_daemon", nothing)
    monkeypatch.setattr(supervisor, "_stop_process", nothing)
    return sup, events, relays, argvs


@pytest.mark.asyncio
async def test_cookies_land_before_the_daemon_and_again_after_a_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider(launch_args=["--auth-server-allowlist=*.example.com", "--bad"])
    sup, events, relays, argvs = _supervisor_with_fakes(tmp_path, monkeypatch, provider)
    assert sup.status()["credentials"]["state"] == credentials.STATE_PENDING
    handle = await sup.ensure()
    assert events == ["Storage.setCookies", "daemon"]
    assert "--auth-server-allowlist=*.example.com" in argvs[0]
    assert "--bad" not in argvs[0]
    status = sup.status()["credentials"]
    assert status["state"] == credentials.STATE_APPLIED and status["launch_args_dropped"] == 1
    # The browser died: the next ensure() relaunches it and fills it again.
    handle.daemon.returncode = 1  # type: ignore[union-attr]
    await sup.ensure()
    assert events == ["Storage.setCookies", "daemon", "Storage.setCookies", "daemon"]
    assert len(relays) == 2 and relays[1].cookies_set()[0]["name"] == "session"
    assert [t.reason for t in provider.targets] == ["start", "start"]
    # An explicit refresh re-pulls into the live browser without a relaunch.
    refreshed = await sup.refresh_credentials()
    assert refreshed["state"] == credentials.STATE_APPLIED
    assert [t.reason for t in provider.targets][-1] == "refresh" and len(relays) == 2
    assert SECRET not in json.dumps(sup.status())


@pytest.mark.asyncio
async def test_a_refresh_during_a_start_does_not_wait_and_the_start_reapplies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    provider = FakeProvider()
    sup, events, _relays, _argvs = _supervisor_with_fakes(tmp_path, monkeypatch, provider)
    monkeypatch.setattr(supervisor, "CREDENTIAL_LOCK_WAIT_SECS", 0.01)
    release = asyncio.Event()
    original = sup._start_daemon

    async def slow_daemon(python: Any, relay_url: str) -> Any:
        await release.wait()
        return await original(python, relay_url)

    monkeypatch.setattr(sup, "_start_daemon", slow_daemon)
    start = asyncio.create_task(sup.ensure())
    while events != ["Storage.setCookies"]:
        await asyncio.sleep(0)
    # The start is parked until `release`, so a refresh that waited for it would
    # never return: the bound proves it does not.
    snap = await asyncio.wait_for(sup.refresh_credentials(), 5.0)
    assert snap["state"] == credentials.STATE_APPLIED
    assert await sup.forget_imported([]) == 0
    release.set()
    await start
    assert events == ["Storage.setCookies", "daemon", "Storage.setCookies"]
    assert [t.reason for t in provider.targets] == ["start", "refresh"]


@pytest.mark.asyncio
async def test_the_person_s_chrome_gets_no_cookies_from_the_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider()
    sup, events, relays, argvs = _supervisor_with_fakes(
        tmp_path, monkeypatch, provider, kind="user_chrome"
    )
    await sup.ensure()
    assert events == ["daemon"] and argvs == [] and provider.targets == []
    assert sup.status()["credentials"]["state"] == credentials.STATE_SKIPPED
    assert await sup.forget_imported([credentials.normalize_cookie(_cookie())]) == 0


@pytest.mark.asyncio
async def test_without_a_provider_the_start_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup, events, _relays, argvs = _supervisor_with_fakes(
        tmp_path, monkeypatch, DefaultBrowserCredentialProvider()
    )
    assert sup.status()["credentials"]["state"] == credentials.STATE_NONE
    await sup.ensure()
    assert events == ["daemon"]
    assert not any(a.startswith("--auth-") for a in argvs[0])
    assert sup.status()["credentials"]["provider"] is None


# ── opt-in, domains, session cookies, provider status ──


def test_settings_precedence_config_then_edition_then_off() -> None:
    edition = FakeProvider(defaults={"enabled": True, "domains": ["example.com"]})
    assert credentials.effective_settings(None, edition) == credentials.CredentialSettings(
        True, ("example.com",), "edition"
    )
    owner = credentials.effective_settings(
        {"enabled": False, "domains": ["app.example.com"]}, edition
    )
    assert (owner.enabled, owner.domains, owner.settings_from) == (
        False,
        ("app.example.com",),
        "config",
    )
    # An empty domains list is "unset": the edition's list applies.
    mixed = credentials.effective_settings({"enabled": True, "domains": []}, edition)
    assert mixed.domains == ("example.com",) and mixed.enabled is True
    silent = FakeProvider(defaults={})
    assert credentials.effective_settings(None, silent) == credentials.CredentialSettings()
    # Without a provider nothing can supply defaults, and a config "on" does nothing.
    none = credentials.effective_settings(
        {"enabled": True, "domains": ["x.example"]}, DefaultBrowserCredentialProvider()
    )
    assert none.enabled is True and none.domains == ("x.example",)


@pytest.mark.asyncio
async def test_a_disabled_provider_is_not_asked_for_cookies_or_switches(tmp_path: Path) -> None:
    relay = FakeRelay()
    provider = FakeProvider(defaults={}, launch_args=["--auth-server-allowlist=*.example.com"])
    status = await _apply(relay, provider, tmp_path)
    assert relay.sent == [] and provider.targets == []
    assert (status.state, status.reason) == (credentials.STATE_DISABLED, "disabled")
    assert status.as_dict()["provider"] == "Test sign-in"
    off = credentials.CredentialSettings()
    assert await credentials.provider_launch_args(provider, off) == ([], 0)


@pytest.mark.asyncio
async def test_only_domains_both_lists_allow_are_set(tmp_path: Path) -> None:
    relay = FakeRelay()
    provider = FakeProvider(
        [
            _cookie(),
            _cookie(name="app", domain="app.example.com"),
            _cookie(name="deep", domain="a.app.example.com"),
        ]
    )
    settings = credentials.CredentialSettings(True, ("app.example.com",), "config")
    status = await _apply(relay, provider, tmp_path, settings=settings)
    assert {c["name"] for c in relay.cookies_set()} == {"app", "deep"}
    assert status.sources[0].dropped == 1


@pytest.mark.asyncio
async def test_cookies_are_set_as_session_cookies_unless_the_provider_keeps_expiry(
    tmp_path: Path,
) -> None:
    relay = FakeRelay()
    provider = FakeProvider()
    status = await _apply(relay, provider, tmp_path)
    (cookie,) = relay.cookies_set()
    assert "expires" not in cookie
    assert status.earliest_expiry == pytest.approx(FUTURE)
    provider.keep = True
    relay = FakeRelay()
    await _apply(relay, provider, tmp_path)
    assert relay.cookies_set()[0]["expires"] == pytest.approx(FUTURE)


@pytest.mark.asyncio
async def test_the_provider_status_is_reduced_and_needs_refresh_is_a_state(tmp_path: Path) -> None:
    provider = FakeProvider()
    provider.status_value = {
        "needs_refresh": True,
        "message": "Run the sign-in tool\x1b[31m again" + "x" * 500,
        "earliest_expiry": float("nan"),
        "count": True,
        "secret": SECRET,
    }
    status = await _apply(FakeRelay(), provider, tmp_path)
    shown = status.as_dict()["provider_status"]
    assert set(shown) == {"needs_refresh", "message", "earliest_expiry", "count"}
    assert shown["needs_refresh"] is True and shown["earliest_expiry"] is None
    assert shown["count"] is None
    assert "\x1b" not in shown["message"] and len(shown["message"]) == 200
    assert status.state == credentials.STATE_NEEDS_REFRESH
    assert SECRET not in json.dumps(status.as_dict())


@pytest.mark.asyncio
async def test_a_missing_or_expired_source_is_needs_refresh_not_a_crash(tmp_path: Path) -> None:
    from kiro_crew.browser_harness.credential_files import CredentialSourceError

    missing = FakeProvider(raises=CredentialSourceError("source_missing"))
    status = await _apply(FakeRelay(), missing, tmp_path)
    assert (status.state, status.reason) == (credentials.STATE_NEEDS_REFRESH, "source_missing")
    stale = FakeProvider([_cookie(expires=10.0)])
    status = await _apply(FakeRelay(), stale, tmp_path)
    assert (status.state, status.reason) == (credentials.STATE_NEEDS_REFRESH, "source_expired")


# ── the supervisor: audit, and the re-pull before a navigation ──


class _Sel:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.rows.append(kw)


@pytest.mark.asyncio
async def test_each_injection_writes_one_audit_row_without_a_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiro_crew.sel as sel_module

    fake = _Sel()
    monkeypatch.setattr(sel_module, "sel", lambda: fake)
    sup, _events, _relays, _argvs = _supervisor_with_fakes(tmp_path, monkeypatch, FakeProvider())
    await sup.ensure()
    (row,) = fake.rows
    assert row["operation"] == "browser_harness.credentials_injected"
    assert row["resources"] == ("provider=Test sign-in reason=start provider=1 domains=example.com")
    assert SECRET not in json.dumps(fake.rows)


@pytest.mark.asyncio
async def test_a_changed_fingerprint_re_pulls_before_the_next_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider()
    provider.fp = "v1"
    sup, events, _relays, _argvs = _supervisor_with_fakes(tmp_path, monkeypatch, provider)
    await sup.ensure()
    assert await sup.refresh_credentials_if_source_changed() is False
    provider.fp = "v2"  # the person re-ran their sign-in tool
    assert await sup.refresh_credentials_if_source_changed() is True
    assert [t.reason for t in provider.targets] == ["start", "source_changed"]
    assert await sup.refresh_credentials_if_source_changed() is False
    provider.fp = None  # cannot tell: never re-pulled
    assert await sup.refresh_credentials_if_source_changed() is False
    assert events.count("Storage.setCookies") == 2


@pytest.mark.asyncio
async def test_the_op_route_asks_before_navigating_and_never_fails_the_op() -> None:
    from kiro_crew.dashboard.handlers import browser_ops

    assert {"navigate", "tabs"} <= browser_ops._NAVIGATING_OPS
    assert "click" not in browser_ops._NAVIGATING_OPS

    class Broken:
        async def refresh_credentials_if_source_changed(self) -> bool:
            raise RuntimeError(SECRET)

    await browser_ops._refresh_credentials(Broken())
    await browser_ops._refresh_credentials(object())  # a supervisor without the hook


# ── file hygiene helpers ──


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner and mode bits")
def test_read_source_file_refuses_what_a_provider_must_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from kiro_crew.browser_harness import credential_files as cf

    def code(path: Path, **kw: Any) -> str:
        with pytest.raises(cf.CredentialSourceError) as err:
            cf.read_source_file(path, **kw)
        assert str(tmp_path) not in str(err.value)
        return err.value.code

    good = tmp_path / "jar"
    good.write_bytes(b"data")
    os.chmod(good, 0o600)
    assert cf.read_source_file(good) == b"data"
    assert code(tmp_path / "absent") == cf.SOURCE_MISSING
    link = tmp_path / "link"
    link.symlink_to(good)
    assert code(link) == cf.SOURCE_SYMLINK
    assert code(tmp_path) == cf.SOURCE_NOT_REGULAR
    assert code(good, max_bytes=3) == cf.SOURCE_TOO_LARGE
    os.chmod(good, 0o640)
    assert code(good) == cf.SOURCE_PERMISSIONS
    os.chmod(good, 0o600)
    monkeypatch.setattr(cf.platform_compat, "effective_uid", lambda: os.getuid() + 1)
    assert code(good) == cf.SOURCE_NOT_OWNED


def test_source_fingerprint_moves_when_the_file_does(tmp_path: Path) -> None:
    import os

    from kiro_crew.browser_harness.credential_files import source_fingerprint

    path = tmp_path / "jar"
    assert source_fingerprint(path) is None
    path.write_bytes(b"one")
    first = source_fingerprint(path)
    path.write_bytes(b"two-longer")
    os.utime(path, ns=(1, 2_000_000_000))
    assert first is not None and source_fingerprint(path) != first


@pytest.mark.asyncio
async def test_status_reads_the_live_config_once_the_browser_it_filled_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A switch flipped while no browser runs must read back as flipped, not as the
    last apply's value (the Settings switch is bound to this ``enabled``)."""
    provider = FakeProvider()
    sup, _events, relays, _argvs = _supervisor_with_fakes(tmp_path, monkeypatch, provider)
    cfg: dict[str, Any] = {"credentials": {"enabled": False}}
    sup._config_reader = lambda: cfg
    await sup.ensure()
    assert sup.status()["credentials"]["state"] == credentials.STATE_DISABLED
    relays[-1].closed = True  # the browser went away
    cfg["credentials"] = {"enabled": True}
    snap = await sup.refresh_credentials()
    assert snap["enabled"] is True and snap["state"] == credentials.STATE_PENDING
    assert sup.status()["credentials"]["enabled"] is True


@pytest.mark.asyncio
async def test_a_provider_status_that_hangs_does_not_hold_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    gate = threading.Event()

    class Hangs(FakeProvider):
        def status(self) -> Any:
            gate.wait()
            return {"count": 1}

    monkeypatch.setattr(credentials, "PROVIDER_TIMEOUT_SECS", 0.05)
    try:
        assert await credentials._bounded_provider_status(Hangs()) is None
    finally:
        gate.set()
