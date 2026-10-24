"""Workload identity verification and logical Crew identity contracts."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import pytest

from kiro_crew.shared_crew import access
from kiro_crew.shared_crew.codec import ContractError
from kiro_crew.shared_crew.enterprise import VerificationKey, resolve_enterprise
from kiro_crew.shared_crew.enterprise_registration import (
    InMemoryCrewRegistry,
    LogicalCrewIdentity,
    RegistrationClaim,
    RegistrationOutcome,
    RegistrationState,
)
from kiro_crew.shared_crew.enterprise_workload import (
    PresentedSvid,
    SvidClaims,
    TrustAnchor,
    WorkloadRejection,
    WorkloadVerifierConfig,
    parse_spiffe_id,
    verify_workload,
)

T0 = 1_800_000_000.0
TRUSTED_ISSUER = "https://wl.trusted.example.test"
OTHER_ISSUER = "https://wl.other.example.test"
CREW_A = "spiffe://acme.example/ns/crew-a/sa/crew"
CREW_B = "spiffe://acme.example/ns/crew-b/sa/crew"
AUD_CREW_A = "crew:crew-a"
AUD_CREW_B = "crew:crew-b"
AUD_RELAY = "relay:crew-a"


def _check(secret: bytes):
    def verify(data: bytes, sig: bytes) -> bool:
        return hmac.compare_digest(hmac.new(secret, data, hashlib.sha256).digest(), sig)

    return verify


class Keys:
    def __init__(self, issuer: str, secret: bytes) -> None:
        self._keys = (
            VerificationKey("w1", issuer, T0 - 100, T0 + 1000, _check(secret)),
            VerificationKey("w2", issuer, T0 + 500, None, _check(secret + b"2")),
        )

    def keys_for(self, issuer: str):
        return tuple(k for k in self._keys if k.issuer == issuer)


TRUSTED = TrustAnchor("trusted-wl", TRUSTED_ISSUER, "acme.example", Keys(TRUSTED_ISSUER, b"t"))
OTHER = TrustAnchor("other-wl", OTHER_ISSUER, "other.example", Keys(OTHER_ISSUER, b"o"))


def _config(audience: str = AUD_CREW_A, anchors=(TRUSTED, OTHER)) -> WorkloadVerifierConfig:
    return WorkloadVerifierConfig(
        audience=audience, anchors=tuple(anchors), expected_ids=frozenset({CREW_A})
    )


def _svid(
    *,
    issuer: str = TRUSTED_ISSUER,
    secret: bytes = b"t",
    spiffe_id: str = CREW_A,
    audience: str = AUD_CREW_A,
    key: str = "w1",
    iat: float = T0,
    exp: float = T0 + 300,
    key_location: str = "",
) -> PresentedSvid:
    claims = SvidClaims(issuer, spiffe_id, (audience,), key, iat, exp, key_location)
    body = repr(claims).encode()
    return PresentedSvid(claims, body, hmac.new(secret, body, hashlib.sha256).digest())


class TestSpiffeId:
    @pytest.mark.parametrize(
        "value",
        [
            "spiffe://acme.example",
            "spiffe://ACME.example/x",
            "https://acme.example/x",
            "spiffe://acme.example/../x",
            "spiffe://acme.example/x?y",
        ],
    )
    def test_malformed_ids_do_not_parse(self, value: str) -> None:
        assert parse_spiffe_id(value) is None

    def test_well_formed_id(self) -> None:
        assert parse_spiffe_id(CREW_A) == ("acme.example", "/ns/crew-a/sa/crew")


class TestVerify:
    def test_valid_token_binds_provider_and_exact_id(self) -> None:
        result = verify_workload(_svid(), _config(), T0 + 1)
        assert result.ok and result.spiffe_id == CREW_A and result.provider_id == "trusted-wl"

    def test_same_claimed_id_under_an_untrusted_provider(self) -> None:
        result = verify_workload(
            _svid(issuer="https://wl.rogue.example.test", secret=b"r"), _config(), T0
        )
        assert result.rejection is WorkloadRejection.UNTRUSTED_PROVIDER

    def test_self_asserted_trust_domain_under_another_registered_provider(self) -> None:
        # The other anchor is registered, but for a different trust domain.
        result = verify_workload(_svid(issuer=OTHER_ISSUER, secret=b"o"), _config(), T0)
        assert result.rejection is WorkloadRejection.TRUST_DOMAIN_MISMATCH

    def test_unexpected_id_in_the_trusted_domain(self) -> None:
        assert verify_workload(_svid(spiffe_id=CREW_B), _config(), T0).rejection is (
            WorkloadRejection.UNEXPECTED_SPIFFE_ID
        )

    def test_cross_crew_audience_is_refused(self) -> None:
        result = verify_workload(_svid(audience=AUD_CREW_B), _config(), T0)
        assert result.rejection is WorkloadRejection.CREDENTIAL_REJECTED
        assert result.credential_reason == "wrong_audience"

    def test_cross_destination_replay_is_refused(self) -> None:
        relay_token = _svid(audience=AUD_RELAY)
        assert verify_workload(relay_token, _config(audience=AUD_RELAY), T0).ok
        replay = verify_workload(relay_token, _config(audience=AUD_CREW_A), T0)
        assert replay.credential_reason == "wrong_audience"

    def test_caller_supplied_key_location_is_refused(self) -> None:
        token = _svid(key_location="https://attacker.example.test/keys")
        assert verify_workload(token, _config(), T0).rejection is (
            WorkloadRejection.KEY_LOCATION_REFUSED
        )

    @pytest.mark.parametrize(
        "kw, now, reason",
        [
            ({"secret": b"forged"}, T0, "bad_signature"),
            ({"exp": T0 - 120, "iat": T0 - 400}, T0, "expired"),
            ({"key": "w2"}, T0, "key_not_valid"),
            ({"key": "w9"}, T0, "unknown_key"),
        ],
    )
    def test_signature_expiry_and_rotation(self, kw: dict, now: float, reason: str) -> None:
        result = verify_workload(_svid(**kw), _config(), now)
        assert result.credential_reason == reason

    def test_rotated_key_verifies_inside_its_window(self) -> None:
        token = _svid(key="w2", secret=b"t2", iat=T0 + 600, exp=T0 + 900)
        assert verify_workload(token, _config(), T0 + 601).ok

    def test_config_refuses_no_anchor_and_inexact_ids(self) -> None:
        with pytest.raises(ContractError):
            WorkloadVerifierConfig(
                audience=AUD_CREW_A, anchors=(), expected_ids=frozenset({CREW_A})
            )
        with pytest.raises(ContractError):
            WorkloadVerifierConfig(
                audience=AUD_CREW_A, anchors=(TRUSTED,), expected_ids=frozenset({"spiffe://acme"})
            )

    def test_anchor_needs_a_pinned_key_set(self) -> None:
        with pytest.raises(ContractError):
            TrustAnchor("p", TRUSTED_ISSUER, "acme.example", object())  # type: ignore[arg-type]


class TestBoundaryTruth:
    def test_pod_boundary_claims_no_per_process_isolation(self) -> None:
        row = access.workload_host_dict("eks")
        assert row["pod_boundary"] == {
            "scope": "any_process_in_pod",
            "per_process_isolation": False,
        }

    def test_supported_host_stays_eks_only(self) -> None:
        assert access.SUPPORTED_WORKLOAD_HOSTS == ("eks",)

    def test_no_static_key_fallback_exists(self) -> None:
        import inspect

        from kiro_crew.shared_crew import enterprise_workload

        names = {n.lower() for n, _ in inspect.getmembers(enterprise_workload)}
        assert not {n for n in names if "static" in n or "secret" in n or "fallback" in n}


# ── logical Crew identity and registration fencing ──

IDENTITY = LogicalCrewIdentity("crew-a", CREW_A, "profile-1")


def _registry() -> InMemoryCrewRegistry:
    reg = InMemoryCrewRegistry()
    reg.provision(IDENTITY)
    return reg


def _claim(deployment: str = "dep-1", replica: str = "hub", cred: str = "c1") -> RegistrationClaim:
    return RegistrationClaim("crew-a", deployment, replica, cred)


class TestRegistration:
    def test_states_are_explicit(self) -> None:
        reg = _registry()
        assert reg.current("crew-a").state is RegistrationState.UNREGISTERED  # type: ignore[union-attr]
        first = reg.register(_claim())
        assert first.outcome is RegistrationOutcome.REGISTERED
        assert first.registration.state is RegistrationState.IN_USE  # type: ignore[union-attr]
        released = reg.release("crew-a", "dep-1", first.fencing_token)
        assert released.registration.state is RegistrationState.RELEASED  # type: ignore[union-attr]

    def test_unprovisioned_identity_is_never_created_by_registering(self) -> None:
        reg = InMemoryCrewRegistry()
        assert reg.register(_claim()).outcome is RegistrationOutcome.UNKNOWN_IDENTITY
        assert reg.current("crew-a") is None

    def test_authorized_replica_joins_and_a_competing_deployment_is_in_use(self) -> None:
        reg = _registry()
        token = reg.register(_claim(replica="hub")).fencing_token
        joined = reg.register(_claim(replica="worker"), fencing_token=token)
        assert joined.outcome is RegistrationOutcome.JOINED_REPLICA
        assert joined.registration.replicas == {"hub", "worker"}  # type: ignore[union-attr]
        rival = reg.register(_claim(deployment="dep-rival"), fencing_token=token)
        assert rival.outcome is RegistrationOutcome.IDENTITY_IN_USE

    def test_old_deployment_is_fenced_after_release_and_re_registration(self) -> None:
        reg = _registry()
        old = reg.register(_claim()).fencing_token
        assert reg.release("crew-a", "dep-1", old).ok
        new = reg.register(_claim(deployment="dep-2"))
        assert new.outcome is RegistrationOutcome.REGISTERED and new.fencing_token == old + 1
        stale = reg.register(_claim(deployment="dep-2", replica="late"), fencing_token=old)
        assert stale.outcome is RegistrationOutcome.FENCED
        assert reg.release("crew-a", "dep-1", old).outcome is RegistrationOutcome.NOT_HOLDER

    def test_restart_upgrade_and_rotation_keep_quota_and_profile(self) -> None:
        reg = _registry()
        token = reg.register(_claim(cred="c1")).fencing_token
        before = reg.current("crew-a")
        # Restart or upgrade: the same deployment rejoins with its token.
        assert reg.register(_claim(replica="hub", cred="c2"), fencing_token=token).ok
        rotated = reg.rotate_credential("crew-a", "dep-1", token, "c3")
        assert rotated.outcome is RegistrationOutcome.CREDENTIAL_ROTATED
        after = reg.current("crew-a")
        assert before is not None and after is not None
        assert after.identity == before.identity
        assert after.identity.quota_key == "crew-a" and after.identity.profile_ref == "profile-1"
        assert after.fencing_token == token

    def test_rotation_needs_the_current_holder(self) -> None:
        reg = _registry()
        token = reg.register(_claim()).fencing_token
        assert reg.rotate_credential("crew-a", "dep-1", token + 7, "c9").outcome is (
            RegistrationOutcome.FENCED
        )

    def test_reference_registry_is_refused_in_production_resolution(self) -> None:
        class Edition:
            def enabled(self) -> bool:
                return True

            def crew_registry(self) -> Any:
                return InMemoryCrewRegistry()

            def deployment_context(self) -> dict:
                return {}

        caps = resolve_enterprise(Edition()).capabilities_dict()
        assert caps["crew_registration"] == {
            "state": "unavailable",
            "reason": "test_provider_refused",
        }
        allowed = resolve_enterprise(Edition(), allow_test_providers=True)
        assert allowed.capabilities_dict()["crew_registration"]["state"] == "available"

    def test_identity_holds_no_credential(self) -> None:
        import dataclasses

        fields = {f.name for f in dataclasses.fields(LogicalCrewIdentity)}
        assert fields == {"crew_id", "spiffe_id", "profile_ref"}
