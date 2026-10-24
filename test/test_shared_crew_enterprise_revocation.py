"""Revocation contract: removal commit -> enforcement within the recorded bound.

A test feed and a fake clock drive every case. The measured bound is the
delay from the authority's removal commit to the moment the Crew refuses the
person, at every enforcement point.
"""

from __future__ import annotations

import asyncio
from typing import Iterator, Optional

import pytest

from kiro_crew.shared_crew import enterprise_revocation as rev
from kiro_crew.shared_crew.admission import ReasonCode
from kiro_crew.shared_crew.codec import ContractError
from kiro_crew.shared_crew.enterprise_revocation import (
    ENFORCEMENT_POINTS,
    EnforcementPoint,
    Removal,
    RevocationEnforcer,
    RevocationFeedUnavailable,
    RevocationPolicy,
    RevocationReason,
    RunningWorkAction,
    RunningWorkPolicy,
)

T0 = 1_800_000_000.0
CREW = "crew-1"
ALICE = "p_alice"
BOB = "p_bob"


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Feed:
    TEST_ONLY = True

    def __init__(self, policy: RevocationPolicy) -> None:
        self._policy = policy
        self.log: list[Removal] = []
        self.down = False

    def policy(self) -> RevocationPolicy:
        return self._policy

    def commit(self, principal_id: str, at: float, revision: int = 1) -> Removal:
        removal = Removal(principal_id, CREW, revision, at)
        self.log.append(removal)
        return removal

    def removals_since(self, cursor: int) -> tuple[tuple[Removal, ...], int]:
        if self.down:
            raise RevocationFeedUnavailable("partitioned")
        return tuple(self.log[cursor:]), len(self.log)


POLICY = RevocationPolicy(max_propagation_seconds=30.0, poll_interval_seconds=10.0)


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    rev.install_enforcer(None)
    yield
    rev.install_enforcer(None)


def _enforcer(policy: RevocationPolicy = POLICY) -> tuple[Feed, RevocationEnforcer]:
    feed = Feed(policy)
    return feed, RevocationEnforcer(feed, crew_id=CREW, now=T0)


def _poll_until(enforcer: RevocationEnforcer, clock: Clock, stop: float, step: float) -> None:
    while clock.t < stop:
        clock.t += step
        if enforcer.sync_due(clock.t):
            enforcer.sync(clock.t)


class TestPolicy:
    def test_a_bound_is_required_and_bounded(self) -> None:
        for bound, poll in ((0.0, 0.0), (901.0, 10.0), (float("inf"), 1.0), (30.0, 30.0)):
            with pytest.raises(ContractError):
                RevocationPolicy(max_propagation_seconds=bound, poll_interval_seconds=poll)

    def test_running_work_has_no_default_decision(self) -> None:
        assert POLICY.running_work is None

    def test_a_feed_without_a_policy_is_refused(self) -> None:
        class NoPolicy(Feed):
            def policy(self):  # type: ignore[override]
                return None

        with pytest.raises(ContractError):
            RevocationEnforcer(NoPolicy(POLICY), crew_id=CREW, now=T0)


class TestDisabled:
    def test_no_feed_is_not_configured_everywhere(self) -> None:
        enforcer = RevocationEnforcer(None, crew_id=CREW, now=T0)
        for point in ENFORCEMENT_POINTS:
            assert enforcer.check(point, ALICE, T0).reason is RevocationReason.NOT_CONFIGURED
        assert enforcer.sync(T0) is False

    def test_queued_hook_is_a_no_op_with_nothing_installed(self) -> None:
        assert rev.queued_dispatch_recheck(ALICE, T0) is None
        rev.install_enforcer(RevocationEnforcer(None, crew_id=CREW, now=T0))
        assert rev.active_enforcer() is None

    def test_offline_token_is_not_membership_without_an_authority(self) -> None:
        enforcer = RevocationEnforcer(None, crew_id=CREW, now=T0)
        assert enforcer.is_current_member(ALICE, T0, id_token_valid=True) is False


class TestEnforcement:
    def test_removal_is_refused_at_every_point(self) -> None:
        feed, enforcer = _enforcer()
        enforcer.note_member(ALICE)
        feed.commit(ALICE, T0)
        enforcer.sync(T0 + 1)
        for point in ENFORCEMENT_POINTS:
            assert enforcer.check(point, ALICE, T0 + 1).reason is RevocationReason.REVOKED
            assert enforcer.check(point, BOB, T0 + 1).allowed

    def test_removal_closes_live_connections(self) -> None:
        feed, enforcer = _enforcer()
        closed: list[str] = []
        for point in (
            EnforcementPoint.WEBSOCKET,
            EnforcementPoint.RELAY_CONNECTION,
            EnforcementPoint.COOKIE,
        ):
            enforcer.register_connection(point, ALICE, lambda p=point: closed.append(p.value))
        enforcer.register_connection(EnforcementPoint.WEBSOCKET, BOB, lambda: closed.append("bob"))
        enforcer.apply(feed.commit(ALICE, T0), T0 + 2)
        assert sorted(closed) == ["cookie", "relay_connection", "websocket"]
        assert enforcer.open_connections(ALICE) == 0 and enforcer.open_connections(BOB) == 1

    def test_valid_offline_token_is_not_proof_of_membership(self) -> None:
        feed, enforcer = _enforcer()
        enforcer.note_member(ALICE)
        assert enforcer.is_current_member(ALICE, T0, id_token_valid=True)
        enforcer.apply(feed.commit(ALICE, T0), T0)
        assert enforcer.is_current_member(ALICE, T0, id_token_valid=True) is False

    def test_older_revision_never_overrides_and_reinstate_needs_newer(self) -> None:
        feed, enforcer = _enforcer()
        enforcer.apply(feed.commit(ALICE, T0, revision=5), T0)
        enforcer.reinstate(ALICE, 4)
        assert not enforcer.check(EnforcementPoint.RECONNECT, ALICE, T0).allowed
        enforcer.reinstate(ALICE, 6)
        assert enforcer.check(EnforcementPoint.RECONNECT, ALICE, T0).allowed

    def test_removal_for_another_crew_is_ignored(self) -> None:
        _, enforcer = _enforcer()
        enforcer.apply(Removal(ALICE, "crew-2", 1, T0), T0)
        assert enforcer.check(EnforcementPoint.COOKIE, ALICE, T0).allowed

    def test_backwards_cursor_is_not_trusted(self) -> None:
        feed, enforcer = _enforcer()
        feed.commit(ALICE, T0)
        enforcer.sync(T0)

        def backwards(cursor: int):
            return (), 0

        feed.removals_since = backwards  # type: ignore[method-assign]
        assert enforcer.sync(T0 + 10) is False


class TestMeasuredBound:
    def test_polled_removal_is_enforced_within_the_bound(self) -> None:
        feed, enforcer = _enforcer()
        clock = Clock()
        enforcer.note_member(ALICE)
        committed = T0 + 3.0
        feed.commit(ALICE, committed)
        _poll_until(enforcer, clock, T0 + 60, step=1.0)
        measured = enforcer.measured_propagation()
        assert set(measured) == {p.value for p in ENFORCEMENT_POINTS}
        worst = max(measured.values())
        assert 0 < worst <= POLICY.poll_interval_seconds
        assert worst <= POLICY.max_propagation_seconds and enforcer.within_bound()

    def test_partition_fails_closed_for_known_members_within_the_bound(self) -> None:
        feed, enforcer = _enforcer()
        clock = Clock()
        enforcer.note_member(ALICE)
        enforcer.sync(clock.t)
        feed.down = True
        committed = clock.t + 0.5
        feed.commit(ALICE, committed)
        refused_at: Optional[float] = None
        while clock.t < T0 + 120:
            clock.t += 0.5
            if enforcer.sync_due(clock.t):
                enforcer.sync(clock.t)
            if not enforcer.check(EnforcementPoint.QUEUED_DISPATCH, ALICE, clock.t).allowed:
                refused_at = clock.t
                break
        assert refused_at is not None
        assert refused_at - committed <= POLICY.max_propagation_seconds
        assert enforcer.check(EnforcementPoint.COOKIE, ALICE, clock.t).reason is (
            RevocationReason.AUTHORITY_STALE
        )

    def test_a_person_never_admitted_is_not_refused_by_staleness_alone(self) -> None:
        feed, enforcer = _enforcer()
        feed.down = True
        assert enforcer.check(EnforcementPoint.COOKIE, BOB, T0 + 999).allowed

    def test_recovery_after_partition_resumes_allowing(self) -> None:
        feed, enforcer = _enforcer()
        enforcer.note_member(BOB)
        feed.down = True
        assert not enforcer.check(EnforcementPoint.COOKIE, BOB, T0 + 31).allowed
        feed.down = False
        assert enforcer.sync(T0 + 32)
        assert enforcer.check(EnforcementPoint.COOKIE, BOB, T0 + 32).allowed


class TestRunningWork:
    def test_undecided_policy_reports_undecided(self) -> None:
        feed, enforcer = _enforcer()
        enforcer.apply(feed.commit(ALICE, T0), T0)
        assert enforcer.running_work_action(ALICE) is RunningWorkAction.UNDECIDED
        assert enforcer.running_work_action(BOB) is RunningWorkAction.CONTINUE

    @pytest.mark.parametrize("choice", list(RunningWorkPolicy))
    def test_a_recorded_policy_is_followed(self, choice: RunningWorkPolicy) -> None:
        policy = RevocationPolicy(
            max_propagation_seconds=30.0, poll_interval_seconds=10.0, running_work=choice
        )
        feed, enforcer = _enforcer(policy)
        enforcer.apply(feed.commit(ALICE, T0), T0)
        assert enforcer.running_work_action(ALICE).value == choice.value


class TestQueuedDispatch:
    def test_hook_refuses_a_removed_initiator(self) -> None:
        feed, enforcer = _enforcer()
        rev.install_enforcer(enforcer)
        assert rev.queued_dispatch_recheck(ALICE, T0) is None
        enforcer.apply(feed.commit(ALICE, T0), T0)
        assert rev.queued_dispatch_recheck(ALICE, T0) == "member_removed"
        assert rev.queued_dispatch_recheck(BOB, T0) is None

    def test_admission_revalidation_refuses_removed_queued_work(self) -> None:
        """The real ``AdmissionService._revalidate`` asks the hook before dispatch."""
        from kiro_crew.shared_crew import admission_gate

        feed, enforcer = _enforcer()
        rev.install_enforcer(enforcer)
        enforcer.apply(feed.commit(ALICE, T0), T0)

        class _Work:
            budget_key = ALICE
            state = None

        class _Live:
            cancel_reason = None
            work = _Work()

        class _Report:
            refusal_reason = None

        service = admission_gate.AdmissionService.__new__(admission_gate.AdmissionService)
        service._clock = lambda: T0  # type: ignore[attr-defined]
        service.policy_version = lambda: ""  # type: ignore[method-assign]
        _Live.request = type("R", (), {"policy_version": ""})()  # type: ignore[attr-defined]

        async def report() -> _Report:
            return _Report()

        service.containment_report = report  # type: ignore[method-assign]
        refusal = asyncio.run(service._revalidate(_Live(), None))  # type: ignore[arg-type]
        assert refusal is ReasonCode.GRANT_REVOKED
        _Work.budget_key = BOB
        assert asyncio.run(service._revalidate(_Live(), None)) is None  # type: ignore[arg-type]
