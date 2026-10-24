"""Directory/client states, Remote Control transitions and three-state health."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.shared_crew.enterprise import (
    HeartbeatSender,
    HeartbeatState,
    HeartbeatTracker,
    WorkloadCredentialRef,
    WorkloadLease,
)
from kiro_crew.shared_crew.enterprise_directory import (
    ACCESS_LOSS_STATES,
    ConnectOutcome,
    CrewConnectionState,
    DirectoryEntry,
    DirectoryError,
    DirectoryErrorCode,
    DirectoryListing,
    RemoteControl,
    RemoteControlOutcome,
    classify_connection_failure,
    connect_to_listed_crew,
    parse_directory_answer,
    read_directory,
)

T0 = 1_800_000_000.0
REPO = Path(__file__).resolve().parent.parent

FIXTURE_EMPTY = {"status": "ok", "entries": []}
FIXTURE_ERROR = {"status": "error", "error": "directory_unavailable"}
FIXTURE_LISTING = {
    "status": "ok",
    "entries": [
        {"crew_id": "crew-a", "display_name": "Build Crew", "state": "available"},
        {"crew_id": "crew-b", "display_name": "Ops Crew", "state": "revoked"},
        {"crew_id": "crew-c", "display_name": "Old Crew", "state": "version_incompatible"},
    ],
}


class TestDirectory:
    def test_empty_and_error_are_different_answers(self) -> None:
        empty = parse_directory_answer(FIXTURE_EMPTY)
        error = parse_directory_answer(FIXTURE_ERROR)
        assert isinstance(empty, DirectoryListing) and empty.empty
        assert isinstance(error, DirectoryError) and error.code is DirectoryErrorCode.UNAVAILABLE
        assert empty.to_dict() == FIXTURE_EMPTY and error.to_dict() == FIXTURE_ERROR

    def test_listing_round_trips_with_stable_ids(self) -> None:
        listing = parse_directory_answer(FIXTURE_LISTING)
        assert isinstance(listing, DirectoryListing)
        assert [e.crew_id for e in listing.entries] == ["crew-a", "crew-b", "crew-c"]
        assert listing.to_dict() == FIXTURE_LISTING

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            {"status": "ok"},
            {"entries": []},
            {"status": "ok", "entries": [{"crew_id": "", "state": "available"}]},
            {"status": "ok", "entries": [{"crew_id": "a", "state": "sleepy"}]},
            {
                "status": "ok",
                "entries": [
                    {"crew_id": "a", "state": "available"},
                    {"crew_id": "a", "state": "offline"},
                ],
            },
        ],
    )
    def test_anything_unclear_is_an_error_never_empty(self, raw: Any) -> None:
        assert isinstance(parse_directory_answer(raw), DirectoryError)

    def test_no_provider_or_failing_provider_is_an_error(self) -> None:
        assert read_directory(None) == DirectoryError(DirectoryErrorCode.NOT_CONFIGURED)

        class Broken:
            def list_crews(self):
                raise RuntimeError("down")

        assert read_directory(Broken()) == DirectoryError(DirectoryErrorCode.UNAVAILABLE)

    @pytest.mark.parametrize(
        "kw, state",
        [
            ({"reason": "member_removed", "relay_reachable": False}, "revoked"),
            ({"http_status": 403, "relay_reachable": False}, "revoked"),
            ({"reason": "grant_revoked", "host_enabled": False}, "revoked"),
            ({"http_status": 401, "relay_reachable": False}, "sign_in_expired"),
            ({"host_enabled": False}, "host_disabled"),
            ({"relay_reachable": False}, "relay_unavailable"),
            ({"protocol_compatible": False}, "version_incompatible"),
            ({}, "offline"),
        ],
    )
    def test_access_loss_is_never_presented_as_offline(self, kw: dict, state: str) -> None:
        assert classify_connection_failure(**kw).value == state

    def test_access_loss_states(self) -> None:
        assert CrewConnectionState.OFFLINE not in ACCESS_LOSS_STATES
        assert ACCESS_LOSS_STATES == {
            CrewConnectionState.REVOKED,
            CrewConnectionState.SIGN_IN_EXPIRED,
        }

    def test_listed_crew_connects_only_on_explicit_connect(self) -> None:
        live = DirectoryEntry("crew-a", "Build Crew", CrewConnectionState.AVAILABLE)
        gone = DirectoryEntry("crew-b", "Ops Crew", CrewConnectionState.REVOKED)
        assert connect_to_listed_crew(live, explicit=False) is (
            ConnectOutcome.EXPLICIT_CONNECT_REQUIRED
        )
        assert connect_to_listed_crew(live, explicit=True) is ConnectOutcome.CONNECTED
        assert connect_to_listed_crew(gone, explicit=True) is ConnectOutcome.NOT_AVAILABLE

    def test_typescript_mirror_names_every_state_and_error(self) -> None:
        source = (REPO / "website" / "src" / "api" / "sharedCrew.ts").read_text(encoding="utf-8")

        def union(name: str) -> set[str]:
            body = source.split(f"export type {name} =", 1)[1].split("\n\n", 1)[0]
            return set(re.findall(r"'([a-z_]+)'", body))

        assert union("CrewConnectionState") == {s.value for s in CrewConnectionState}
        assert union("DirectoryErrorCode") == {c.value for c in DirectoryErrorCode}


class Registrar:
    TEST_ONLY = True

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls = 0

    def register(self) -> bool:
        self.calls += 1
        return self.ok


class TestRemoteControl:
    def test_starts_off_and_constructing_calls_nothing(self) -> None:
        reg = Registrar()
        rc = RemoteControl(reg)
        assert rc.state.to_dict() == {
            "registered": False,
            "discoverable": False,
            "remote_access_enabled": False,
            "local_execution": True,
        }
        assert reg.calls == 0

    def test_turn_on_registers_and_nothing_else(self) -> None:
        reg = Registrar()
        rc = RemoteControl(reg)
        assert rc.turn_on(explicit=False) is RemoteControlOutcome.EXPLICIT_ACTION_REQUIRED
        assert reg.calls == 0
        assert rc.turn_on(explicit=True) is RemoteControlOutcome.DONE
        assert reg.calls == 1
        assert rc.state.registered and not rc.state.discoverable
        assert not rc.state.remote_access_enabled

    def test_each_transition_is_separate_and_needs_registration(self) -> None:
        rc = RemoteControl(Registrar())
        assert rc.publish_discovery(explicit=True) is RemoteControlOutcome.NOT_REGISTERED
        assert rc.enable_remote_access(explicit=True) is RemoteControlOutcome.NOT_REGISTERED
        rc.turn_on(explicit=True)
        assert rc.publish_discovery(explicit=False) is (
            RemoteControlOutcome.EXPLICIT_ACTION_REQUIRED
        )
        assert rc.publish_discovery(explicit=True) is RemoteControlOutcome.DONE
        assert not rc.state.remote_access_enabled
        assert rc.enable_remote_access(explicit=True) is RemoteControlOutcome.DONE
        assert rc.state.remote_access_enabled

    def test_disabling_remote_access_keeps_local_execution(self) -> None:
        rc = RemoteControl(Registrar())
        rc.turn_on(explicit=True)
        rc.enable_remote_access(explicit=True)
        rc.disable_remote_access()
        assert rc.state.local_execution and not rc.state.remote_access_enabled
        rc.turn_off()
        assert rc.state.local_execution and not rc.state.registered

    def test_no_registrar_means_no_network_and_not_configured(self) -> None:
        rc = RemoteControl(None)
        assert rc.turn_on(explicit=True) is RemoteControlOutcome.NOT_CONFIGURED
        assert not rc.state.registered

    def test_failed_registration_registers_nothing(self) -> None:
        rc = RemoteControl(Registrar(ok=False))
        assert rc.turn_on(explicit=True) is RemoteControlOutcome.REGISTRATION_FAILED
        assert not rc.state.registered


class TestHealth:
    def test_exactly_three_values(self) -> None:
        assert {s.value for s in HeartbeatState} == {"healthy", "heartbeat_late", "not_connected"}

    def test_never_connected_is_not_connected(self) -> None:
        tracker = HeartbeatTracker()
        tracker.record_attempt(T0)
        assert tracker.state(T0 + 1) is HeartbeatState.NOT_CONNECTED
        assert tracker.snapshot(T0 + 1) == {
            "state": "not_connected",
            "last_success": None,
            "last_attempt": T0,
        }

    def test_late_then_not_connected_after_the_disconnect_threshold(self) -> None:
        tracker = HeartbeatTracker(late_after_seconds=60, not_connected_after_seconds=300)
        tracker.record_ack(T0)
        assert tracker.state(T0 + 60) is HeartbeatState.HEALTHY
        assert tracker.state(T0 + 61) is HeartbeatState.HEARTBEAT_LATE
        assert tracker.state(T0 + 301) is HeartbeatState.NOT_CONNECTED

    def test_success_and_attempt_are_tracked_apart(self) -> None:
        class Transport:
            def send(self, payload, credential) -> bool:
                return False

        class Workload:
            def bind(self, crew_id: str) -> WorkloadCredentialRef:
                return WorkloadCredentialRef("h", crew_id, T0 + 3600)

            def renew(self, current: WorkloadCredentialRef) -> WorkloadCredentialRef:
                return current

        tracker = HeartbeatTracker()
        tracker.record_ack(T0)
        sender = HeartbeatSender(Transport(), WorkloadLease(Workload(), crew_id="c"), tracker)
        payload = {"schema_version": 1, "crew_id": "c", "sent_at": T0 + 30}
        sender.tick(payload, T0 + 30)
        assert tracker.last_success == T0 and tracker.last_attempt == T0 + 30

    def test_health_has_no_path_into_an_access_decision(self) -> None:
        import inspect

        from kiro_crew.shared_crew import enterprise_gates
        from kiro_crew.shared_crew.enterprise import GrantGate

        assert "heartbeat" not in inspect.getsource(GrantGate.decide).lower()
        assert "Heartbeat" not in inspect.getsource(enterprise_gates)
