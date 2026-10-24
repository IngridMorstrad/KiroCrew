"""Contract tests for the Shared Crew enterprise seams.

Every provider here is a test double defined in this file and marked
``TEST_ONLY``. Keys are HMAC test keys from the standard library; no test
reaches the network and every clock is an explicit argument.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest

from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.shared_crew_enterprise import DefaultSharedCrewEnterpriseProvider
from kiro_crew.shared_crew.codec import ContractError
from kiro_crew.shared_crew.contracts import (
    AttributionAvailability,
    CapabilityState,
    CrewIdentity,
    Principal,
    PrincipalKind,
    Provenance,
    SessionAttribution,
    principal_id_for,
)
from kiro_crew.shared_crew.enterprise import (
    CAP_DEPLOYMENT_CONTEXT,
    CAP_HEARTBEAT,
    CAP_HUMAN_CREDENTIALS,
    CAP_INDIVIDUAL_GRANTS,
    CAP_WORKLOAD_IDENTITY,
    CHARGEABLE_OPERATIONS,
    ENTERPRISE_CAPABILITY_KEYS,
    ENTERPRISE_STATE_PARENT,
    HEARTBEAT_PAYLOAD_KEYS,
    AccessReason,
    AuthorityUnavailable,
    BudgetGate,
    BudgetReason,
    BudgetState,
    ContractCredentialVerifier,
    CredentialClaims,
    CredentialRejection,
    CrewBinding,
    DeploymentContext,
    DisabledCredentialVerifier,
    GrantCachePolicy,
    GrantGate,
    GrantState,
    HeartbeatOutcome,
    HeartbeatSender,
    HeartbeatState,
    HeartbeatTracker,
    IndividualGrant,
    LimitScope,
    OperationKind,
    PresentedCredential,
    UnavailableReason,
    VerificationKey,
    VerifierPolicy,
    WorkloadCredentialRef,
    WorkloadDisabled,
    WorkloadLease,
    WorkloadState,
    WorkloadUnavailable,
    budget_allows,
    build_heartbeat_payload,
    effective_limit,
    enterprise_state_dir,
    resolve_enterprise,
    synthetic_crew_principal,
    validate_heartbeat_payload,
)

ISSUER = "https://issuer.test"
AUDIENCE = "crew-test-audience"
PROVIDER = "test-idp"
CREW = "crew_test_1"
T0 = 1_800_000_000.0


# ── test doubles ──


def _hmac_key(
    key_id: str,
    secret: bytes,
    *,
    issuer: str = ISSUER,
    valid_from: float = 0.0,
    valid_until: Optional[float] = None,
) -> VerificationKey:
    def check(signed: bytes, signature: bytes) -> bool:
        return hmac.compare_digest(hmac.new(secret, signed, hashlib.sha256).digest(), signature)

    return VerificationKey(key_id, issuer, valid_from, valid_until, check)


class _KeySet:
    TEST_ONLY = True

    def __init__(self, *keys: VerificationKey) -> None:
        self.keys = list(keys)

    def keys_for(self, issuer: str) -> tuple[VerificationKey, ...]:
        return tuple(k for k in self.keys if k.issuer == issuer)


def _present(secret: bytes, **overrides: Any) -> PresentedCredential:
    fields: dict[str, Any] = {
        "issuer": ISSUER,
        "subject": "user-123",
        "audiences": (AUDIENCE,),
        "key_id": "k1",
        "issued_at": T0,
        "expires_at": T0 + 600,
    }
    fields.update(overrides)
    claims = CredentialClaims(**fields)
    signed = repr(dataclasses.astuple(claims)).encode()
    return PresentedCredential(claims, signed, hmac.new(secret, signed, hashlib.sha256).digest())


def _human(subject: str = "user-123") -> Principal:
    return Principal(
        id=principal_id_for(PROVIDER, ISSUER, subject),
        kind=PrincipalKind.HUMAN,
        provider=PROVIDER,
        issuer=ISSUER,
        subject=subject,
        provenance=Provenance.ENTERPRISE,
        extra={"identity_class": "organization_directory"},
    )


class _Authority:
    TEST_ONLY = True

    def __init__(self) -> None:
        self.grants: dict[str, IndividualGrant] = {}
        self.down = False
        self.calls = 0

    def grant(self, principal: Principal, revision: int, state: GrantState = GrantState.ACTIVE):
        self.grants[principal.id] = IndividualGrant(principal.id, CREW, state, revision)

    def lookup(self, principal_id: str, crew_id: str) -> Optional[IndividualGrant]:
        self.calls += 1
        if self.down:
            raise AuthorityUnavailable("down")
        return self.grants.get(principal_id)


class _Workload:
    TEST_ONLY = True

    def __init__(self, lifetime: float = 900.0) -> None:
        self.lifetime = lifetime
        self.binds = 0
        self.renews = 0
        self.disable_on_renew = False
        self.unavailable = False
        self.now = T0

    def bind(self, crew_id: str) -> WorkloadCredentialRef:
        self.binds += 1
        if self.unavailable:
            raise WorkloadUnavailable("no identity")
        return WorkloadCredentialRef(f"ref-{self.binds}", crew_id, self.now + self.lifetime)

    def renew(self, current: WorkloadCredentialRef) -> WorkloadCredentialRef:
        self.renews += 1
        if self.disable_on_renew:
            raise WorkloadDisabled("disabled by authority")
        if self.unavailable:
            raise WorkloadUnavailable("no identity")
        return WorkloadCredentialRef(
            f"ref-r{self.renews}", current.crew_id, self.now + self.lifetime
        )


class _Transport:
    TEST_ONLY = True

    def __init__(self) -> None:
        self.sent: list[tuple[Mapping[str, Any], WorkloadCredentialRef]] = []
        self.ack = True

    def send(self, payload: Mapping[str, Any], credential: WorkloadCredentialRef) -> bool:
        self.sent.append((payload, credential))
        return self.ack


class _Budget:
    TEST_ONLY = True

    def __init__(self, state: BudgetState) -> None:
        self.value = state

    def state(self, deployment: DeploymentContext) -> BudgetState:
        return self.value


class _Binding:
    TEST_ONLY = True

    def current_binding(self) -> Optional[CrewBinding]:
        return CrewBinding(CrewIdentity(CREW), synthetic_crew_principal(CREW, ISSUER))


class _Enterprise:
    """A fully populated edition provider for resolution tests."""

    TEST_ONLY = True

    def __init__(self, **overrides: Any) -> None:
        self.parts: dict[str, Any] = {
            "credential_verifier": ContractCredentialVerifier(
                provider=PROVIDER,
                policy=VerifierPolicy(frozenset({ISSUER}), AUDIENCE),
                keys=_KeySet(_hmac_key("k1", b"s1")),
            ),
            "grant_authority": _Authority(),
            "workload_identity": _Workload(),
            "crew_binding": _Binding(),
            "heartbeat_transport": _Transport(),
            "inference_budget": _Budget(BudgetState.AVAILABLE),
        }
        self.parts.update(overrides)
        self.refs = {"deployment_ref": "dep-1", "tenant_ref": "ten-1"}

    def enabled(self) -> bool:
        return True

    def __getattr__(self, name: str) -> Any:
        parts = self.__dict__.get("parts", {})
        if name in parts:
            return lambda: parts[name]
        raise AttributeError(name)

    def deployment_context(self) -> Mapping[str, str]:
        return self.refs


# ── default build ──


class TestDefaultBuild:
    def test_platform_default_slot_is_disabled(self) -> None:
        ctx = build_default_context(cfg=None)  # type: ignore[arg-type]
        assert isinstance(ctx.shared_crew_enterprise, DefaultSharedCrewEnterpriseProvider)
        assert ctx.shared_crew_enterprise.enabled() is False

    def test_every_enterprise_capability_unavailable_with_reason(self) -> None:
        caps = resolve_enterprise(DefaultSharedCrewEnterpriseProvider()).capabilities_dict()
        assert set(caps) == set(ENTERPRISE_CAPABILITY_KEYS)
        for key, row in caps.items():
            assert row == {"state": "unavailable", "reason": "not_configured"}, key

    def test_installed_context_reports_unavailable(self) -> None:
        seams = resolve_enterprise()
        assert seams.grant_authority is None and seams.workload_identity is None
        assert all(r["state"] == "unavailable" for r in seams.capabilities_dict().values())

    def test_default_seams_build_disabled_gates(self) -> None:
        seams = resolve_enterprise(DefaultSharedCrewEnterpriseProvider())
        gate = GrantGate(seams.grant_authority, crew_id=CREW)
        assert gate.decide(_human(), T0).reason is AccessReason.NOT_CONFIGURED
        lease = WorkloadLease(seams.workload_identity, crew_id=CREW)
        assert lease.state(T0) is WorkloadState.UNAVAILABLE
        with pytest.raises(WorkloadUnavailable):
            lease.ensure(T0)
        assert DisabledCredentialVerifier().verify(_present(b"s1"), T0).rejection is (
            CredentialRejection.UNAVAILABLE
        )


# ── resolution guards ──


class TestResolution:
    def test_test_provider_refused_without_opt_in(self) -> None:
        caps = resolve_enterprise(_Enterprise()).capabilities_dict()
        assert {r["reason"] for r in caps.values()} == {"test_provider_refused"}
        assert {r["state"] for r in caps.values()} == {"unavailable"}

    def test_test_sub_provider_refused_inside_real_provider(self) -> None:
        class Real(_Enterprise):
            TEST_ONLY = False

        caps = resolve_enterprise(Real()).capabilities_dict()
        assert caps[CAP_INDIVIDUAL_GRANTS]["reason"] == "test_provider_refused"
        # The generic verifier is not a test double; its test key set is the
        # edition's concern, so it resolves.
        assert caps[CAP_HUMAN_CREDENTIALS]["state"] == "available"

    def test_full_provider_resolves_when_tests_opt_in(self) -> None:
        seams = resolve_enterprise(_Enterprise(), allow_test_providers=True, workload_host="eks")
        caps = seams.capabilities_dict()
        original = (
            "human_credentials",
            "individual_grants",
            "workload_identity",
            "crew_identity",
            "heartbeat",
            "inference_budget",
            "deployment_context",
        )
        assert {caps[k]["state"] for k in original} == {"available"}
        # An edition written before the newer accessors existed reads not configured.
        assert {caps[k]["reason"] for k in caps if k not in original} == {"not_configured"}
        assert seams.deployment == DeploymentContext(deployment_ref="dep-1", tenant_ref="ten-1")

    def test_object_not_meeting_contract_refused(self) -> None:
        caps = resolve_enterprise(
            _Enterprise(grant_authority=object()), allow_test_providers=True
        ).capabilities_dict()
        assert caps[CAP_INDIVIDUAL_GRANTS] == {"state": "unavailable", "reason": "provider_refused"}

    def test_heartbeat_without_workload_identity_is_unavailable(self) -> None:
        seams = resolve_enterprise(_Enterprise(workload_identity=None), allow_test_providers=True)
        assert seams.heartbeat_transport is None
        assert seams.capabilities_dict()[CAP_HEARTBEAT]["reason"] == (
            UnavailableReason.REQUIRES_WORKLOAD_IDENTITY.value
        )

    def test_raising_provider_reports_failed(self) -> None:
        class Broken(_Enterprise):
            def enabled(self) -> bool:
                raise RuntimeError("boom")

        caps = resolve_enterprise(Broken(), allow_test_providers=True).capabilities_dict()
        assert {r["reason"] for r in caps.values()} == {"provider_failed"}

    def test_url_in_deployment_reference_refused(self) -> None:
        provider = _Enterprise()
        provider.refs = {"deployment_ref": "https://host.example/x"}
        caps = resolve_enterprise(provider, allow_test_providers=True).capabilities_dict()
        assert caps[CAP_DEPLOYMENT_CONTEXT]["reason"] == "provider_refused"


# ── credentials ──


def _verifier(keys: _KeySet, **policy: Any) -> ContractCredentialVerifier:
    return ContractCredentialVerifier(
        provider=PROVIDER,
        policy=VerifierPolicy(frozenset({ISSUER}), AUDIENCE, **policy),
        keys=keys,
    )


class TestCredentialVerifier:
    def test_valid_credential_yields_enterprise_human(self) -> None:
        result = _verifier(_KeySet(_hmac_key("k1", b"s1"))).verify(_present(b"s1"), T0 + 10)
        assert result.ok and result.principal == _human()
        assert result.principal.provenance is Provenance.ENTERPRISE

    @pytest.mark.parametrize(
        ("overrides", "secret", "expected"),
        [
            ({}, b"forged", CredentialRejection.BAD_SIGNATURE),
            ({"issuer": "https://other.test"}, b"s1", CredentialRejection.UNKNOWN_ISSUER),
            ({"audiences": ("someone-else",)}, b"s1", CredentialRejection.WRONG_AUDIENCE),
            ({"key_id": "k9"}, b"s1", CredentialRejection.UNKNOWN_KEY),
            ({"subject": ""}, b"s1", CredentialRejection.MALFORMED),
            ({"expires_at": T0 - 1}, b"s1", CredentialRejection.MALFORMED),
            ({"expires_at": T0 + 7200}, b"s1", CredentialRejection.LIFETIME_EXCEEDED),
            ({"not_before": T0 + 500}, b"s1", CredentialRejection.NOT_YET_VALID),
        ],
        ids=[
            "forged",
            "wrong-issuer",
            "wrong-audience",
            "unknown-key",
            "no-subject",
            "inverted-times",
            "too-long",
            "not-before",
        ],
    )
    def test_rejections(self, overrides, secret, expected) -> None:
        result = _verifier(_KeySet(_hmac_key("k1", b"s1"))).verify(
            _present(secret, **overrides), T0 + 10
        )
        assert not result.ok and result.rejection is expected

    def test_expiry_honours_bounded_skew(self) -> None:
        verifier = _verifier(_KeySet(_hmac_key("k1", b"s1")), max_clock_skew_seconds=30)
        cred = _present(b"s1")
        assert verifier.verify(cred, T0 + 600 + 30).ok
        assert verifier.verify(cred, T0 + 600 + 31).rejection is CredentialRejection.EXPIRED

    def test_tampered_claims_fail_signature(self) -> None:
        good = _present(b"s1")
        forged = PresentedCredential(
            dataclasses.replace(good.claims, subject="admin"),
            repr(dataclasses.astuple(dataclasses.replace(good.claims, subject="admin"))).encode(),
            good.signature,
        )
        verifier = _verifier(_KeySet(_hmac_key("k1", b"s1")))
        assert verifier.verify(forged, T0).rejection is CredentialRejection.BAD_SIGNATURE

    def test_key_rotation_overlap_then_retirement(self) -> None:
        old = _hmac_key("k1", b"s1", valid_until=T0 + 100)
        new = _hmac_key("k2", b"s2", valid_from=T0 + 50)
        verifier = _verifier(_KeySet(old, new))
        old_cred = _present(b"s1", issued_at=T0 + 60, expires_at=T0 + 400)
        new_cred = _present(b"s2", key_id="k2", issued_at=T0 + 60, expires_at=T0 + 400)
        assert verifier.verify(old_cred, T0 + 60).ok
        assert verifier.verify(new_cred, T0 + 60).ok
        assert verifier.verify(old_cred, T0 + 101).rejection is CredentialRejection.KEY_NOT_VALID
        assert verifier.verify(new_cred, T0 + 101).ok
        assert verifier.verify(new_cred, T0 + 10).rejection is CredentialRejection.KEY_NOT_VALID

    def test_key_from_another_issuer_never_matches(self) -> None:
        stray = _hmac_key("k1", b"s1", issuer="https://other.test")
        keys = _KeySet(stray)
        keys.keys_for = lambda issuer: (stray,)  # type: ignore[method-assign]
        assert _verifier(keys).verify(_present(b"s1"), T0).rejection is (
            CredentialRejection.UNKNOWN_KEY
        )

    def test_truthy_non_bool_signature_result_fails(self) -> None:
        key = VerificationKey("k1", ISSUER, 0.0, None, lambda s, g: 1)  # type: ignore[arg-type]
        assert _verifier(_KeySet(key)).verify(_present(b"s1"), T0).rejection is (
            CredentialRejection.BAD_SIGNATURE
        )

    def test_failing_key_set_rejects_unavailable(self) -> None:
        keys = _KeySet()

        def boom(issuer: str) -> tuple[VerificationKey, ...]:
            raise OSError("key endpoint unreachable")

        keys.keys_for = boom  # type: ignore[method-assign]
        assert _verifier(keys).verify(_present(b"s1"), T0).rejection is (
            CredentialRejection.UNAVAILABLE
        )

    @pytest.mark.parametrize(
        "policy",
        [
            {"issuers": frozenset()},
            {"audience": ""},
            {"max_clock_skew_seconds": 3600},
            {"max_lifetime_seconds": 0},
        ],
    )
    def test_policy_bounds(self, policy) -> None:
        base: dict[str, Any] = {"issuers": frozenset({ISSUER}), "audience": AUDIENCE}
        base.update(policy)
        with pytest.raises(ContractError):
            VerifierPolicy(**base)


# ── grants ──


class TestGrantGate:
    def _gate(self, authority: _Authority, **policy: float) -> GrantGate:
        return GrantGate(authority, crew_id=CREW, policy=GrantCachePolicy(**policy))

    def test_active_grant_allows_and_is_cached(self) -> None:
        authority = _Authority()
        authority.grant(_human(), 1)
        gate = self._gate(authority, fresh_seconds=60)
        assert gate.decide(_human(), T0).reason is AccessReason.GRANT_ACTIVE
        assert gate.decide(_human(), T0 + 30).from_cache
        assert authority.calls == 1

    def test_revoked_grant_denies_new_access(self) -> None:
        authority = _Authority()
        authority.grant(_human(), 1)
        gate = self._gate(authority, fresh_seconds=60)
        assert gate.decide(_human(), T0).allowed
        authority.grant(_human(), 2, GrantState.REVOKED)
        decision = gate.decide(_human(), T0 + 61)
        assert decision.reason is AccessReason.GRANT_REVOKED and not decision.allowed

    def test_pushed_revocation_applies_immediately_and_beats_stale_answer(self) -> None:
        authority = _Authority()
        authority.grant(_human(), 1)
        gate = self._gate(authority, fresh_seconds=60)
        assert gate.decide(_human(), T0).allowed
        gate.apply_revocation(_human().id, 2, T0 + 1)
        assert gate.decide(_human(), T0 + 2).reason is AccessReason.GRANT_REVOKED
        # A lagging authority replica still answers revision 1 active.
        assert gate.decide(_human(), T0 + 100).reason is AccessReason.GRANT_REVOKED
        authority.grant(_human(), 3)
        assert gate.decide(_human(), T0 + 200).reason is AccessReason.GRANT_ACTIVE

    def test_historical_attribution_never_grants(self) -> None:
        creator = _human("creator")
        attribution = SessionAttribution(
            creator=creator, availability=AttributionAvailability.RECORDED, created_at=T0 - 3600
        )
        assert attribution.creator.id == creator.id
        gate = self._gate(_Authority())
        assert gate.decide(creator, T0).reason is AccessReason.NO_GRANT

    @pytest.mark.parametrize("kind", [PrincipalKind.MACHINE, PrincipalKind.SYSTEM])
    def test_non_individual_principals_denied(self, kind) -> None:
        authority = _Authority()
        principal = dataclasses.replace(_human(), kind=kind)
        authority.grant(principal, 1)
        assert self._gate(authority).decide(principal, T0).reason is AccessReason.NOT_INDIVIDUAL
        assert self._gate(authority).decide(Principal.unknown(), T0).reason is (
            AccessReason.NOT_INDIVIDUAL
        )

    def test_outage_honours_confirmed_grant_within_bound_only(self) -> None:
        authority = _Authority()
        authority.grant(_human(), 1)
        gate = self._gate(authority, fresh_seconds=60, outage_grace_seconds=240)
        assert gate.decide(_human(), T0).allowed
        authority.down = True
        held = gate.decide(_human(), T0 + 300)
        assert held.reason is AccessReason.GRANT_CACHED_OUTAGE and held.allowed
        assert gate.policy.revocation_bound_seconds == 300
        assert gate.decide(_human(), T0 + 301).reason is AccessReason.AUTHORITY_UNAVAILABLE

    def test_outage_with_no_prior_grant_denies(self) -> None:
        authority = _Authority()
        authority.down = True
        assert self._gate(authority).decide(_human(), T0).reason is (
            AccessReason.AUTHORITY_UNAVAILABLE
        )

    def test_outage_never_extends_a_revocation(self) -> None:
        authority = _Authority()
        authority.grant(_human(), 1, GrantState.REVOKED)
        gate = self._gate(authority, fresh_seconds=10)
        assert gate.decide(_human(), T0).reason is AccessReason.GRANT_REVOKED
        authority.down = True
        assert gate.decide(_human(), T0 + 20).reason is AccessReason.AUTHORITY_UNAVAILABLE

    def test_clock_moving_backwards_forces_a_fresh_check(self) -> None:
        authority = _Authority()
        authority.grant(_human(), 1)
        gate = self._gate(authority, fresh_seconds=60)
        gate.decide(_human(), T0)
        authority.grant(_human(), 2, GrantState.REVOKED)
        assert gate.decide(_human(), T0 - 5).reason is AccessReason.GRANT_REVOKED

    def test_answer_for_another_principal_is_not_trusted(self) -> None:
        other = _human("other")
        authority = _Authority()
        authority.grants[_human().id] = IndividualGrant(other.id, CREW, GrantState.ACTIVE, 1)
        assert self._gate(authority).decide(_human(), T0).reason is (
            AccessReason.AUTHORITY_UNAVAILABLE
        )

    def test_cache_is_bounded(self) -> None:
        authority = _Authority()
        gate = GrantGate(authority, crew_id=CREW, max_entries=2)
        for i in range(5):
            p = _human(f"u{i}")
            authority.grant(p, 1)
            gate.decide(p, T0)
        authority.down = True
        assert gate.decide(_human("u0"), T0 + 61).reason is AccessReason.AUTHORITY_UNAVAILABLE
        assert gate.decide(_human("u4"), T0 + 61).allowed

    def test_heartbeat_freshness_is_not_an_input(self) -> None:
        authority = _Authority()
        authority.down = True
        tracker = HeartbeatTracker()
        tracker.record_ack(T0)
        assert tracker.state(T0) is HeartbeatState.HEALTHY
        assert self._gate(authority).decide(_human(), T0).reason is (
            AccessReason.AUTHORITY_UNAVAILABLE
        )

    def test_policy_bounds(self) -> None:
        with pytest.raises(ContractError):
            GrantCachePolicy(fresh_seconds=3600)
        with pytest.raises(ContractError):
            GrantCachePolicy(outage_grace_seconds=-1)


# ── workload identity ──


class TestWorkloadLease:
    def test_bind_then_reuse_then_renew(self) -> None:
        provider = _Workload(lifetime=900)
        lease = WorkloadLease(provider, crew_id=CREW, renew_before_seconds=300)
        assert lease.state(T0) is WorkloadState.UNBOUND
        first = lease.ensure(T0)
        assert lease.state(T0) is WorkloadState.BOUND
        assert lease.ensure(T0 + 100) == first and provider.renews == 0
        provider.now = T0 + 650
        renewed = lease.ensure(T0 + 650)
        assert renewed != first and provider.renews == 1

    def test_disablement_stops_renewals(self) -> None:
        provider = _Workload()
        lease = WorkloadLease(provider, crew_id=CREW)
        lease.ensure(T0)
        provider.disable_on_renew = True
        provider.now = T0 + 700
        with pytest.raises(WorkloadDisabled):
            lease.ensure(T0 + 700)
        assert lease.state(T0 + 700) is WorkloadState.DISABLED
        renews = provider.renews
        provider.disable_on_renew = False
        for step in range(3):
            with pytest.raises(WorkloadDisabled):
                lease.ensure(T0 + 800 + step)
        assert provider.renews == renews and provider.binds == 1

    def test_host_disable_is_terminal(self) -> None:
        provider = _Workload()
        lease = WorkloadLease(provider, crew_id=CREW)
        lease.ensure(T0)
        lease.disable()
        with pytest.raises(WorkloadDisabled):
            lease.ensure(T0 + 1)
        assert provider.binds == 1 and provider.renews == 0

    def test_failed_renewal_keeps_unexpired_credential_then_expires(self) -> None:
        provider = _Workload(lifetime=900)
        lease = WorkloadLease(provider, crew_id=CREW, renew_before_seconds=300)
        first = lease.ensure(T0)
        provider.unavailable = True
        assert lease.ensure(T0 + 700) == first
        with pytest.raises(WorkloadUnavailable):
            lease.ensure(T0 + 900)
        assert lease.state(T0 + 900) is WorkloadState.UNBOUND

    def test_no_provider_has_no_fallback(self) -> None:
        lease = WorkloadLease(None, crew_id=CREW)
        with pytest.raises(WorkloadUnavailable):
            lease.ensure(T0)

    def test_credential_for_another_crew_refused(self) -> None:
        class Wrong(_Workload):
            def bind(self, crew_id: str) -> WorkloadCredentialRef:
                return WorkloadCredentialRef("r", "another_crew", T0 + 900)

        with pytest.raises(WorkloadUnavailable):
            WorkloadLease(Wrong(), crew_id=CREW).ensure(T0)


# ── synthetic Crew identity ──


class TestCrewBinding:
    def test_synthetic_identity_is_system_and_distinct_from_humans(self) -> None:
        crew = synthetic_crew_principal(CREW, ISSUER)
        assert crew.kind is PrincipalKind.SYSTEM
        human_same_subject = principal_id_for(PROVIDER, ISSUER, CREW)
        assert crew.id != human_same_subject

    def test_binding_refuses_a_human(self) -> None:
        with pytest.raises(ContractError):
            CrewBinding(CrewIdentity(CREW), _human())

    def test_binding_refuses_another_crews_identity(self) -> None:
        with pytest.raises(ContractError):
            CrewBinding(CrewIdentity(CREW), synthetic_crew_principal("other", ISSUER))

    def test_inference_owner_is_the_crew(self) -> None:
        binding = CrewBinding(
            CrewIdentity(CREW, profile_ref="profile-1"), synthetic_crew_principal(CREW, ISSUER)
        )
        assert binding.inference_owner.kind is PrincipalKind.SYSTEM
        assert "profile-1" not in repr(binding.inference_owner)


# ── heartbeat ──


def _payload(**overrides: Any) -> dict[str, Any]:
    payload = build_heartbeat_payload(
        crew_id=CREW,
        deployment=DeploymentContext(deployment_ref="dep-1"),
        sent_at=T0,
        build="0.9.0",
        capabilities={CAP_WORKLOAD_IDENTITY: CapabilityState.AVAILABLE},
        active_runtimes=2,
        queued_work=1,
    )
    payload.update(overrides)
    return payload


class TestHeartbeat:
    def test_state_machine(self) -> None:
        tracker = HeartbeatTracker(late_after_seconds=180)
        assert tracker.state(T0) is HeartbeatState.NOT_CONNECTED
        assert tracker.snapshot(T0) == {
            "state": "not_connected",
            "last_success": None,
            "last_attempt": None,
        }
        tracker.record_ack(T0)
        assert tracker.state(T0 + 180) is HeartbeatState.HEALTHY
        assert tracker.state(T0 + 181) is HeartbeatState.HEARTBEAT_LATE
        tracker.record_ack(T0 + 200)
        assert tracker.state(T0 + 210) is HeartbeatState.HEALTHY
        tracker.record_ack(T0 + 5)
        assert tracker.last_success == T0 + 200

    def test_payload_keys_are_exactly_the_allowlist(self) -> None:
        assert set(_payload()) == HEARTBEAT_PAYLOAD_KEYS

    @pytest.mark.parametrize(
        "extra",
        [
            {"transcript": "hello"},
            {"session_title": "x"},
            {"principal": "p_abc"},
            {"build": "a prompt with spaces"},
            {"crew_id": "x" * 65},
            {"capabilities": {"transcript": "available"}},
            {"capabilities": {CAP_HEARTBEAT: "chatty"}},
            {"active_runtimes": -1},
            {"queued_work": True},
        ],
    )
    def test_content_refused(self, extra) -> None:
        with pytest.raises(ContractError):
            validate_heartbeat_payload(_payload(**extra))

    def test_send_authenticates_with_workload_and_marks_healthy(self) -> None:
        transport, workload = _Transport(), _Workload()
        tracker = HeartbeatTracker()
        sender = HeartbeatSender(transport, WorkloadLease(workload, crew_id=CREW), tracker)
        assert sender.tick(_payload(), T0) is HeartbeatOutcome.SENT
        assert transport.sent[0][1].crew_id == CREW
        assert tracker.state(T0) is HeartbeatState.HEALTHY

    def test_no_send_without_workload_identity(self) -> None:
        transport, tracker = _Transport(), HeartbeatTracker()
        sender = HeartbeatSender(transport, WorkloadLease(None, crew_id=CREW), tracker)
        assert sender.tick(_payload(), T0) is HeartbeatOutcome.WORKLOAD_UNAVAILABLE
        assert transport.sent == [] and tracker.state(T0) is HeartbeatState.NOT_CONNECTED

    def test_disabled_workload_stops_heartbeat(self) -> None:
        transport, workload = _Transport(), _Workload()
        lease = WorkloadLease(workload, crew_id=CREW)
        sender = HeartbeatSender(transport, lease, HeartbeatTracker())
        lease.disable()
        assert sender.tick(_payload(), T0) is HeartbeatOutcome.WORKLOAD_DISABLED
        assert transport.sent == []

    def test_unacknowledged_send_leaves_state(self) -> None:
        transport = _Transport()
        transport.ack = False
        tracker = HeartbeatTracker()
        sender = HeartbeatSender(transport, WorkloadLease(_Workload(), crew_id=CREW), tracker)
        assert sender.tick(_payload(), T0) is HeartbeatOutcome.NOT_ACKNOWLEDGED
        assert tracker.state(T0) is HeartbeatState.NOT_CONNECTED

    def test_bad_payload_never_sent(self) -> None:
        transport = _Transport()
        sender = HeartbeatSender(
            transport, WorkloadLease(_Workload(), crew_id=CREW), HeartbeatTracker()
        )
        assert sender.tick(_payload(transcript="x"), T0) is HeartbeatOutcome.REFUSED_PAYLOAD
        assert transport.sent == []

    def test_not_configured_without_transport(self) -> None:
        sender = HeartbeatSender(None, WorkloadLease(None, crew_id=CREW), HeartbeatTracker())
        assert sender.tick(_payload(), T0) is HeartbeatOutcome.NOT_CONFIGURED


# ── inference budget ──


class TestInferenceBudget:
    @pytest.mark.parametrize("op", sorted(CHARGEABLE_OPERATIONS, key=lambda o: o.value))
    def test_exhausted_blocks_new_inference(self, op) -> None:
        gate = BudgetGate(_Budget(BudgetState.EXHAUSTED), DeploymentContext())
        assert gate.check(op).reason is BudgetReason.INFERENCE_BUDGET_EXHAUSTED

    @pytest.mark.parametrize(
        "op", sorted(set(OperationKind) - CHARGEABLE_OPERATIONS, key=lambda o: o.value)
    )
    def test_exhausted_allows_reads_and_cancel(self, op) -> None:
        gate = BudgetGate(_Budget(BudgetState.EXHAUSTED), DeploymentContext())
        assert gate.check(op).allowed

    @pytest.mark.parametrize("state", [BudgetState.EXHAUSTED, BudgetState.UNKNOWN])
    def test_decision_function_allows_reads_in_every_state(self, state) -> None:
        for op in set(OperationKind) - CHARGEABLE_OPERATIONS:
            assert budget_allows(op, state).allowed, op
        assert not budget_allows(OperationKind.NEW_TURN, state).allowed

    def test_unknown_or_failing_budget_refuses_new_inference(self) -> None:
        class Failing(_Budget):
            def state(self, deployment: DeploymentContext) -> BudgetState:
                raise OSError("unreachable")

        gate = BudgetGate(Failing(BudgetState.AVAILABLE), DeploymentContext())
        assert gate.check(OperationKind.NEW_TURN).reason is BudgetReason.INFERENCE_BUDGET_UNKNOWN
        assert gate.check(OperationKind.READ_RESULT).allowed

    def test_provider_cannot_claim_not_enforced(self) -> None:
        gate = BudgetGate(_Budget(BudgetState.NOT_ENFORCED), DeploymentContext())
        assert gate.current_state() is BudgetState.UNKNOWN

    def test_public_build_does_not_enforce(self) -> None:
        assert BudgetGate(None, DeploymentContext()).check(OperationKind.NEW_TURN).allowed
        assert budget_allows(OperationKind.NEW_TURN, BudgetState.AVAILABLE).allowed


# ── deployment context and caps ──


class TestDeploymentContext:
    def test_tenant_cap_composes_above_crew_and_principal(self) -> None:
        assert effective_limit({LimitScope.PRINCIPAL: 4, LimitScope.CREW: 8}) == (
            4,
            LimitScope.PRINCIPAL,
        )
        assert effective_limit(
            {LimitScope.TENANT: 2, LimitScope.CREW: 8, LimitScope.PRINCIPAL: 4}
        ) == (2, LimitScope.TENANT)
        assert effective_limit({LimitScope.TENANT: 4, LimitScope.PRINCIPAL: 4}) == (
            4,
            LimitScope.TENANT,
        )
        assert effective_limit({}) == (None, None)

    def test_negative_cap_refused(self) -> None:
        with pytest.raises(ContractError):
            effective_limit({LimitScope.CREW: -1})

    @pytest.mark.parametrize("ref", ["https://x.example", "a/b", "x" * 129, "has space"])
    def test_reference_must_be_opaque(self, ref) -> None:
        with pytest.raises(ContractError):
            DeploymentContext(deployment_ref=ref)


# ── credential and config isolation ──


class TestCredentialIsolation:
    def test_state_dir_sits_inside_the_fenced_vault(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.config.paths import data_home
        from kiro_crew.security.paths import is_sensitive_path

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
        home = data_home()
        target = enterprise_state_dir(home) / "credential.json"
        assert target.parent.parent == home / ENTERPRISE_STATE_PARENT
        assert is_sensitive_path(str(target))
        assert is_sensitive_path(str(target.parent))
        assert not is_sensitive_path(str(home / "notes" / "credential.json"))

    def test_sandbox_hides_the_vault_in_every_mode(self) -> None:
        from kiro_crew import sandbox_leaves, sandbox_tiers

        for tier in (
            sandbox_tiers._STRICT_DIRS,
            sandbox_tiers._STANDARD_DIRS,
            sandbox_tiers._CC_DIRS,
        ):
            assert f".kiro/crew/{ENTERPRISE_STATE_PARENT}" in tier
        assert ENTERPRISE_STATE_PARENT in sandbox_leaves._RELOCATED_CREW_HIDDEN_LEAVES

    def test_platform_slot_reads_no_config(self) -> None:
        source = Path(
            __import__("kiro_crew.platform.shared_crew_enterprise", fromlist=["x"]).__file__
        ).read_text(encoding="utf-8")
        assert "config.json" not in source.replace("``config.json``", "")
        assert "open(" not in source and "environ" not in source
