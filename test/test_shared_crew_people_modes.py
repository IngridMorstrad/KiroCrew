"""People/setup modes, Unattributed filtering, trigger attribution and audit references.

Covers ``shared_crew.crew_mode`` (personal / network / console / invalid and the
startup refusal of an unusable team configuration), the ``unattributed`` session
filter with its older ``unknown`` spelling, the trigger label of unattended work
(``shared_crew.trigger``), and the workload identity, trigger and causal join an
audit record carries.
"""

from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.history import ConversationLog
from kiro_crew.shared_crew import access, audit, crew_mode, people, routes_identity, trigger
from kiro_crew.shared_crew.admission import AcceptedWork, OperationKind, OperationState
from kiro_crew.shared_crew.attribution import public_attribution_from_meta, trigger_of_public
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActionOutcome,
    ActionRecord,
    ActionType,
    ActorContext,
    AttributionAvailability,
    CapabilityState,
    CrewIdentity,
    Principal,
    Provenance,
    SessionAttribution,
    SessionQuery,
    Transport,
)
from kiro_crew.shared_crew.enterprise import (
    ENTERPRISE_CAPABILITY_KEYS,
    CrewBinding,
    EnterpriseCapability,
    EnterpriseSeams,
    resolve_enterprise,
    synthetic_crew_principal,
)
from kiro_crew.shared_crew.enterprise_gates import EnterpriseGates, make_enterprise_gate_middleware
from kiro_crew.shared_crew.identity import local_operator_principal, tailnet_principal
from kiro_crew.shared_crew.session_query import query_sessions

ALICE = tailnet_principal("alice@example.com", "a", Provenance.TAILSCALE_SERVE)
OPERATOR = local_operator_principal("local-app")
AVAILABLE = EnterpriseCapability(CapabilityState.AVAILABLE)
NOT_CONFIGURED = EnterpriseCapability(CapabilityState.UNAVAILABLE, "not_configured")
REFUSED = EnterpriseCapability(CapabilityState.UNAVAILABLE, "provider_refused")
NO_PATHS: dict[str, dict[str, str]] = {
    "tailscale": {"state": "unavailable", "reason": "not_configured"},
    "kiro_login": {"state": "unavailable", "reason": "not_in_this_build"},
    "systems_manager": {"state": "unavailable", "reason": "not_in_this_build"},
}
TAILNET_ON = {**NO_PATHS, "tailscale": {"state": "available", "reason": ""}}


@pytest.fixture(autouse=True)
def _clean():
    access._reset_setup_paths_for_tests()
    trigger._reset_for_tests()
    people.reset_for_tests()
    yield
    access._reset_setup_paths_for_tests()
    trigger._reset_for_tests()
    people.reset_for_tests()


def _seams(caps: dict[str, EnterpriseCapability], **kw: Any) -> EnterpriseSeams:
    full = {key: NOT_CONFIGURED for key in ENTERPRISE_CAPABILITY_KEYS}
    full.update(caps)
    return EnterpriseSeams(capabilities=full, **kw)


CONSOLE = {"human_credentials": AVAILABLE, "individual_grants": AVAILABLE}


class _Binding:
    def __init__(self, ref: str) -> None:
        crew = CrewIdentity(crew_id="crew-1", workload_identity_ref=ref)
        self.binding = CrewBinding(crew, synthetic_crew_principal("crew-1", "issuer"))

    def current_binding(self) -> CrewBinding:
        return self.binding


# ── modes ──


class TestModes:
    def test_personal_when_no_team_path_and_nobody_else(self):
        mode, problem = crew_mode.resolve_mode(EnterpriseSeams(), NO_PATHS, others_seen=False)
        assert (mode, problem) == (crew_mode.MODE_PERSONAL, None)

    def test_network_when_a_path_is_on_or_someone_else_was_seen(self):
        assert (
            crew_mode.resolve_mode(EnterpriseSeams(), TAILNET_ON, others_seen=False)[0] == "network"
        )
        degraded = {**NO_PATHS, "tailscale": {"state": "degraded", "reason": "tailnet_unreachable"}}
        assert (
            crew_mode.resolve_mode(EnterpriseSeams(), degraded, others_seen=False)[0] == "network"
        )
        assert crew_mode.resolve_mode(EnterpriseSeams(), NO_PATHS, others_seen=True)[0] == "network"

    def test_relying_party_sign_in_with_grants_is_a_console_team(self):
        seams = _seams({"human_sign_in": AVAILABLE, "individual_grants": AVAILABLE})
        assert crew_mode.resolve_mode(seams, NO_PATHS, others_seen=False) == ("console", None)

    def test_console_when_sign_in_and_grants_are_backed(self):
        assert crew_mode.resolve_mode(_seams(CONSOLE), NO_PATHS, others_seen=True)[0] == "console"

    def test_grants_alone_for_tailnet_people_is_not_a_console_team(self):
        seams = _seams({"individual_grants": AVAILABLE})
        assert crew_mode.resolve_mode(seams, TAILNET_ON, others_seen=False) == ("network", None)

    @pytest.mark.parametrize(
        "caps,paths,reason,capability",
        [
            (
                {"human_credentials": AVAILABLE},
                NO_PATHS,
                "console_capability_missing",
                "individual_grants",
            ),
            (
                {"human_credentials": REFUSED, "individual_grants": AVAILABLE},
                NO_PATHS,
                "console_capability_missing",
                "human_credentials",
            ),
            (CONSOLE, TAILNET_ON, "conflicting_team_modes", ""),
            (
                {"human_sign_in": AVAILABLE},
                NO_PATHS,
                "console_capability_missing",
                "individual_grants",
            ),
            (
                {"human_sign_in": REFUSED, "individual_grants": AVAILABLE},
                NO_PATHS,
                "console_capability_missing",
                "human_sign_in",
            ),
        ],
    )
    def test_invalid_team_configuration_is_refused_not_personal(
        self, caps, paths, reason, capability
    ):
        mode, problem = crew_mode.resolve_mode(_seams(caps), paths, others_seen=False)
        assert mode == crew_mode.MODE_INVALID
        assert problem is not None and problem.reason == reason and problem.capability == capability

    def test_a_test_only_provider_in_production_is_an_unusable_team(self):
        class Leaked:
            TEST_ONLY = True

            def enabled(self) -> bool:
                return True

        seams = resolve_enterprise(Leaked())
        problem = crew_mode.team_config_problem(seams, NO_PATHS)
        assert problem is not None
        assert (problem.reason, problem.cause) == (
            "console_provider_unusable",
            "test_provider_refused",
        )

    def test_an_unreadable_tailnet_allowlist_refuses_team_mode(self):
        access.note_tailnet(
            enabled=True, host="crew.example.ts.net", identity_config_unreadable=True
        )
        assert (
            crew_mode.resolve_mode(EnterpriseSeams(), access.setup_paths_dict(), others_seen=False)[
                0
            ]
            == "invalid"
        )
        access.note_tailnet(enabled=True, host="crew.example.ts.net")
        assert (
            crew_mode.resolve_mode(EnterpriseSeams(), access.setup_paths_dict(), others_seen=False)[
                0
            ]
            == "network"
        )

    def test_startup_validation_logs_a_machine_readable_reason(self, caplog):
        with caplog.at_level(logging.ERROR, logger="kiro_crew.shared_crew.crew_mode"):
            problem = crew_mode.validate_at_startup(
                _seams({"human_credentials": AVAILABLE}), NO_PATHS
            )
        assert problem is not None
        assert "console_capability_missing" in caplog.text and "individual_grants" in caplog.text
        assert crew_mode.validate_at_startup(EnterpriseSeams(), NO_PATHS) is None

    def test_others_observed_counts_network_people_and_other_operators(self):
        assert crew_mode.others_observed(OPERATOR, [OPERATOR, Principal.unknown()]) is False
        assert crew_mode.others_observed(OPERATOR, [ALICE]) is True
        assert crew_mode.others_observed(OPERATOR, [local_operator_principal("other")]) is True
        assert crew_mode.others_observed(OPERATOR, [Principal.system("automation")]) is False


class TestConsoleLink:
    @pytest.mark.parametrize(
        "raw",
        [
            "https://console.example.com/crews/1",
            "https://console.example.com:443/x?y=1",
        ],
    )
    def test_accepts_https_to_a_named_host(self, raw):
        assert crew_mode.validate_console_link(raw) == raw

    @pytest.mark.parametrize(
        "raw",
        [
            "http://console.example.com/",
            "javascript:alert(1)",
            "https://user:pw@console.example.com/",
            "https://127.0.0.1/",
            "https://[::1]/",
            "https://localhost/",
            "https://crew.localhost/",
            "https://console.example.com:8443/",
            "https://console.example.com/a b",
            "https://console.example.com/\x00",
            "https://" + "a" * 600 + ".com/",
            "",
            None,
            42,
        ],
    )
    def test_refuses_anything_else(self, raw):
        assert crew_mode.validate_console_link(raw) == ""

    def test_link_comes_only_from_the_provider_and_is_validated(self):
        class Provider:
            def __init__(self, url: object) -> None:
                self.url = url

            def console_link(self) -> object:
                return self.url

        assert crew_mode.console_link(Provider("https://console.example.com/")) == (
            "available",
            "https://console.example.com/",
        )
        assert crew_mode.console_link(Provider("file:///etc/passwd")) == ("invalid", "")
        assert crew_mode.console_link(Provider(None)) == ("not_offered", "")
        assert crew_mode.console_link(object()) == ("not_offered", "")

    def test_mode_view_offers_a_link_only_in_console_mode(self):
        class Provider:
            def console_link(self) -> str:
                return "https://console.example.com/"

        view = crew_mode.mode_view(
            OPERATOR,
            [],
            seams=_seams(CONSOLE),
            setup_paths=NO_PATHS,
            crew_id="g1",
            provider=Provider(),
        )
        assert view["mode"] == "console" and view["console"] == {
            "state": "available",
            "url": "https://console.example.com/",
        }
        assert view["team_config"]["state"] == "valid"
        personal = crew_mode.mode_view(
            OPERATOR,
            [],
            seams=EnterpriseSeams(),
            setup_paths=NO_PATHS,
            crew_id="g1",
            provider=Provider(),
        )
        assert personal["mode"] == "personal" and personal["console"]["url"] == ""
        assert personal["crew_id"] == "g1"
        assert personal["systems_manager_attribution"] == "unavailable"
        assert personal["workload_identity_ref"] == "unavailable"


# ── the gate refuses shared access while the team setup is invalid ──


def _gate_app(seams: EnterpriseSeams, principal: Principal, paths: dict) -> web.Application:
    @web.middleware
    async def publish(request: web.Request, handler: Any) -> web.StreamResponse:
        request[ACTOR_REQUEST_KEY] = ActorContext(
            principal=principal, request_id="r", transport=Transport.HTTP
        )
        return await handler(request)

    def check(s: EnterpriseSeams) -> Optional[str]:
        problem = crew_mode.team_config_problem(s, paths, network_identity_unreadable=False)
        return None if problem is None else problem.reason

    async def ok(_r: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    gates = EnterpriseGates(seams)
    app = web.Application(
        middlewares=[publish, make_enterprise_gate_middleware(lambda: gates, team_check=check)]
    )
    app.router.add_get("/api/sessions", ok)
    return app


@pytest.mark.asyncio
async def test_invalid_team_setup_refuses_a_tailnet_person_but_not_the_local_operator():
    seams = _seams(CONSOLE)
    async with TestClient(TestServer(_gate_app(seams, ALICE, TAILNET_ON))) as client:
        resp = await client.get("/api/sessions")
        assert resp.status == 403
        assert await resp.json() == {
            "error": "team_config_invalid",
            "code": "team_config_invalid",
            "reason": "conflicting_team_modes",
        }
    async with TestClient(TestServer(_gate_app(seams, OPERATOR, TAILNET_ON))) as client:
        assert (await client.get("/api/sessions")).status == 200
    async with TestClient(TestServer(_gate_app(EnterpriseSeams(), ALICE, TAILNET_ON))) as client:
        assert (await client.get("/api/sessions")).status == 200


# ── triggers ──


class TestTriggers:
    def test_cron_created_by_names_the_schedule(self):
        trigger.register_name_resolver(trigger.TRIGGER_SCHEDULED, {"abc12345": "Nightly"}.get)
        found = trigger.trigger_of(key="dashboard:tab", created_by="cron:abc12345")
        assert found is not None and (found.kind, found.ref, found.name) == (
            "scheduled",
            "abc12345",
            "Nightly",
        )
        assert found.configured_by is None

    @pytest.mark.parametrize(
        "key", ["cron:abc12345", "cron_abc12345", "cron:abc12345:run", "dashboard_cron_abc12345"]
    )
    def test_cron_namespace_keys_in_either_spelling(self, key):
        found = trigger.trigger_of(key=key)
        assert found is not None and found.kind == "scheduled" and found.ref == "abc12345"
        assert found.name == ""

    def test_webhook_and_system(self):
        hook = trigger.trigger_of(key="hook:github:1")
        assert hook is not None and (hook.kind, hook.name) == ("webhook", "github")
        folded = trigger.trigger_of(key="hook_github_1")
        assert folded is not None and (folded.kind, folded.name) == ("webhook", "")
        system = trigger.trigger_of(key="startup", origin="system")
        assert system is not None and system.kind == "system"

    @pytest.mark.parametrize(
        "key,created_by", [("dashboard:tab", ""), ("cron:NOTHEX!", ""), ("tab", "dashboard:other")]
    )
    def test_a_person_s_session_has_no_trigger(self, key, created_by):
        assert trigger.trigger_of(key=key, created_by=created_by) is None

    def test_a_failing_name_lookup_only_loses_the_name(self):
        def boom(_ref: str) -> str:
            raise RuntimeError("down")

        trigger.register_name_resolver(trigger.TRIGGER_SCHEDULED, boom)
        found = trigger.trigger_of(key="cron:abc12345")
        assert found is not None and found.name == ""

    def test_public_attribution_names_the_trigger_never_for_a_person(self):
        legacy = public_attribution_from_meta({"created_by": "cron:abc12345"}, key="dashboard:t")
        assert trigger_of_public(legacy) == {
            "kind": "scheduled",
            "ref": "abc12345",
            "name": "",
            "configured_by": None,
        }
        person = SessionAttribution(
            creator=ALICE, availability=AttributionAvailability.RECORDED, created_at=1.0
        )
        meta = {"attribution": person.to_dict(), "created_by": "cron:abc12345"}
        assert trigger_of_public(public_attribution_from_meta(meta, key="cron:abc12345")) is None


# ── Unattributed filter over real catalog rows ──


def _write(
    base, key: str, *, creator: Principal | None, mtime: float, extra: dict | None = None
) -> None:
    meta: dict[str, Any] = {"_type": "metadata", "title": key}
    if creator is not None:
        meta["attribution"] = SessionAttribution(
            creator=creator, availability=AttributionAvailability.RECORDED, created_at=mtime
        ).to_dict()
    meta.update(extra or {})
    path = base / f"{key}.jsonl"
    path.write_text(
        json.dumps(meta) + "\n" + json.dumps({"role": "user", "content": "x"}) + "\n",
        encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))


@pytest.fixture
def catalog(tmp_path):
    base = tmp_path / "sessions"
    base.mkdir()
    for i in range(3):
        _write(base, f"dashboard_a{i}", creator=ALICE, mtime=1000.0 + i)
    for i in range(4):
        _write(base, f"dashboard_l{i}", creator=None, mtime=2000.0 + i)
    _write(base, "dashboard_c0", creator=None, mtime=3000.0, extra={"created_by": "cron:abc12345"})
    _write(base, "cron_def67890", creator=None, mtime=3001.0)
    return ConversationLog(base)


@pytest.mark.parametrize("spelling", ["unattributed", "unknown"])
def test_unattributed_filter_counts_and_pages_before_search(catalog, spelling):
    rows = catalog.list_sessions()
    query = SessionQuery.from_query({"creator": spelling, "limit": "3"})
    assert query.creator == "unattributed"
    page = query_sessions(rows, query, ALICE)
    assert page.total_matching == 4 and page.hidden_by_filter == 5
    assert {i["key"] for i in page.items} <= {f"dashboard_l{i}" for i in range(4)}
    assert page.next_cursor
    rest = query_sessions(
        rows, SessionQuery.from_query({"creator": spelling, "cursor": page.next_cursor}), ALICE
    )
    assert len(page.items) + len(rest.items) == 4
    searched = query_sessions(
        rows, SessionQuery.from_query({"creator": spelling, "q": "l1"}), ALICE
    )
    assert [i["key"] for i in searched.items] == ["dashboard_l1"]
    assert searched.total_matching + searched.hidden_by_filter == 1


def test_scheduled_sessions_list_under_all_with_their_trigger(catalog):
    page = query_sessions(
        catalog.list_sessions(), SessionQuery.from_query({"creator": "all"}), ALICE
    )
    triggers = {i["key"]: trigger_of_public(i["attribution"]) for i in page.items}
    assert triggers["dashboard_c0"]["ref"] == "abc12345"
    assert triggers["cron_def67890"]["ref"] == "def67890"
    assert triggers["dashboard_l0"] is None and triggers["dashboard_a0"] is None


def test_filters_change_visibility_only(catalog):
    rows = catalog.list_sessions()
    every = {
        i["key"]
        for i in query_sessions(rows, SessionQuery.from_query({"limit": "200"}), ALICE).items
    }
    union: set[str] = set()
    for creator in ("mine", "unattributed"):
        page = query_sessions(
            rows, SessionQuery.from_query({"creator": creator, "limit": "200"}), ALICE
        )
        union |= {i["key"] for i in page.items}
    assert union < every and every - union == {"dashboard_c0", "cron_def67890"}


# ── the mode route ──


async def _mode_client(log, principal: Principal, *, app_name: str = "") -> TestClient:
    @web.middleware
    async def fake_auth(request: web.Request, handler: Any) -> web.StreamResponse:
        request["is_dashboard_user"] = not app_name
        if app_name:
            request["app"] = app_name
        request[ACTOR_REQUEST_KEY] = ActorContext(
            principal=principal, request_id="r", transport=Transport.HTTP
        )
        return await handler(request)

    app = web.Application(middlewares=[fake_auth])
    app["state"] = SimpleNamespace(conversation_log=log)
    routes_identity.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_mode_route_reports_personal_then_network_once_someone_else_creates(tmp_path):
    base = tmp_path / "s"
    base.mkdir()
    log = ConversationLog(base)
    client = await _mode_client(log, OPERATOR)
    try:
        body = await (await client.get(routes_identity.MODE_PATH)).json()
        assert body["mode"] == "personal" and body["crew_id"]
        assert body["team_config"]["state"] == "not_configured"
        _write(base, "dashboard_a0", creator=ALICE, mtime=1.0)
        body = await (await client.get(routes_identity.MODE_PATH)).json()
        assert body["mode"] == "network"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mode_route_refuses_an_app(tmp_path):
    client = await _mode_client(ConversationLog(tmp_path), Principal.system("app:x"), app_name="x")
    try:
        assert (await client.get(routes_identity.MODE_PATH)).status == 403
    finally:
        await client.close()


# ── audit ──


def _actor(principal: Principal) -> ActorContext:
    return ActorContext(principal=principal, request_id="r", transport=Transport.HTTP)


class TestAudit:
    def test_every_record_names_the_crew_workload_or_unavailable(self, monkeypatch):
        monkeypatch.setattr(audit, "current_workload_identity_ref", lambda: "unavailable")
        record = audit.new_action_record(
            ActionType.MESSAGE,
            session_key="dashboard:t",
            initiator=_actor(ALICE),
            outcome=ActionOutcome.ACCEPTED,
        )
        meta = audit.security_event(record).metadata
        assert meta["workload_identity_ref"] == "unavailable"
        assert meta["initiator"]["id"] == ALICE.id and meta["trigger"] is None

    def test_the_workload_ref_comes_from_the_crew_binding(self):
        seams = EnterpriseSeams(crew_binding=_Binding("wl-ref-1"))
        assert crew_mode.crew_identity_refs(seams) == ("crew-1", "wl-ref-1")
        assert crew_mode.crew_identity_refs(EnterpriseSeams(crew_binding=_Binding(""))) == (
            "crew-1",
            "unavailable",
        )
        record = audit.new_action_record(
            ActionType.CANCEL,
            session_key="dashboard:t",
            initiator=_actor(ALICE),
            outcome=ActionOutcome.APPLIED,
            workload_ref="wl-ref-1",
        )
        assert audit.security_event(record).metadata["workload_identity_ref"] == "wl-ref-1"

    def test_unattended_work_names_its_trigger_never_a_person(self):
        record = audit.new_action_record(
            ActionType.MESSAGE,
            session_key="cron:abc12345",
            initiator=_actor(Principal.system("automation")),
            outcome=ActionOutcome.ACCEPTED,
            workload_ref="",
        )
        meta = audit.security_event(record).metadata
        assert meta["trigger"] == {"kind": "scheduled", "ref": "abc12345", "name": ""}
        assert meta["initiator"]["kind"] == "system"
        assert meta["workload_identity_ref"] == "unavailable"
        human = audit.new_action_record(
            ActionType.MESSAGE,
            session_key="cron:abc12345",
            initiator=_actor(ALICE),
            outcome=ActionOutcome.ACCEPTED,
            workload_ref="",
        )
        assert audit.security_event(human).metadata["trigger"] is None

    def test_causal_join_work_session_actor_survives_a_round_trip(self):
        work = AcceptedWork(
            work_id="w1",
            scope="root",
            budget_key=ALICE.id,
            session_key="dashboard:t",
            operation=OperationKind.CHAT_TURN,
            state=OperationState.QUEUED,
            revision=1,
            accepted_at=1.0,
            expires_at=100.0,
        )
        bob = tailnet_principal("bob@example.com", "b", Provenance.TAILSCALE_SERVE)
        cancel = audit.new_action_record(
            ActionType.CANCEL,
            session_key=work.session_key,
            initiator=_actor(bob),
            outcome=ActionOutcome.APPLIED,
            target=work.work_id,
            workload_ref="wl",
        )
        follow = audit.new_action_record(
            ActionType.CONTROL,
            session_key=work.session_key,
            initiator=_actor(bob),
            outcome=ActionOutcome.APPLIED,
            target=work.work_id,
            causal_action_id=cancel.action_id,
            workload_ref="wl",
        )
        back = ActionRecord.from_dict(json.loads(json.dumps(cancel.to_dict())))
        assert audit.joins_work(back, work)
        # The actor who cancelled stays distinct from the person the work is charged to.
        assert back.initiator.id == bob.id != work.budget_key
        assert back.extra["workload_identity_ref"] == "wl"
        assert follow.causal_action_id == back.action_id
        event = audit.security_event(back).metadata
        assert (event["target"], event["session_key"], event["initiator"]["id"]) == (
            "w1",
            "dashboard:t",
            bob.id,
        )
        other = audit.new_action_record(
            ActionType.CANCEL,
            session_key="dashboard:other",
            initiator=_actor(bob),
            outcome=ActionOutcome.APPLIED,
            target="w1",
        )
        assert not audit.joins_work(other, work)


def test_no_external_requested_by_attribution_is_emitted():
    """Human attribution stays in Crew records; nothing tells a downstream system who asked.

    External "Requested by <person>" output is suspended pending product
    reconciliation, so no source file under ``src/kiro_crew`` may build one.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
    # Case-sensitive: the outbound label is "Requested by <person>"; prose such as
    # "requested by me" in a filter description is not attribution output.
    pattern = re.compile(r"[\"']Requested by\b|requested_by_human|on_behalf_of_human")
    hits = [
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if pattern.search(p.read_text(encoding="utf-8", errors="replace"))
    ]
    assert hits == []
