"""Shared Crew access modes: identity class eligibility, setup paths, workload host.

Contract tests for ``shared_crew.access`` and the parts of the enterprise seam
and the capabilities payload that read it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
from typing import Iterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.shared_crew import access
from kiro_crew.shared_crew import capabilities as caps_mod
from kiro_crew.shared_crew.access import (
    IdentityClass,
    SetupPathState,
    access_modes_dict,
    detect_workload_host,
    identity_class_of,
    note_tailnet,
    register_setup_path_source,
    remote_control_eligibility,
    setup_paths_dict,
    shared_access_eligibility,
    workload_host_dict,
)
from kiro_crew.shared_crew.capabilities import aggregate_capabilities
from kiro_crew.shared_crew.contracts import (
    CapabilityState,
    Principal,
    PrincipalKind,
    Provenance,
    principal_id_for,
)
from kiro_crew.shared_crew.enterprise import (
    CAP_HEARTBEAT,
    CAP_WORKLOAD_IDENTITY,
    AccessReason,
    ContractCredentialVerifier,
    CredentialClaims,
    GrantGate,
    GrantState,
    IndividualGrant,
    PresentedCredential,
    VerificationKey,
    VerifierPolicy,
    resolve_enterprise,
)
from kiro_crew.shared_crew.routes_capabilities import CAPABILITIES_PATH, register

T0 = 1_800_000_000.0
ISSUER, AUDIENCE, PROVIDER, CREW = "https://issuer.example.test", "crew", "edition", "crew-1"


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    access._reset_setup_paths_for_tests()
    caps_mod._reset_capability_sources_for_tests()
    yield
    access._reset_setup_paths_for_tests()
    caps_mod._reset_capability_sources_for_tests()


def _person(provenance: Provenance, identity_class: str | None = None) -> Principal:
    extra = {} if identity_class is None else {"identity_class": identity_class}
    return Principal(
        id=principal_id_for(PROVIDER, ISSUER, f"s-{provenance.value}-{identity_class}"),
        kind=PrincipalKind.HUMAN,
        provider=PROVIDER,
        issuer=ISSUER,
        subject="s",
        provenance=provenance,
        extra=extra,
    )


ORG = _person(Provenance.ENTERPRISE, "organization_directory")
INDIVIDUAL = _person(Provenance.ENTERPRISE, "individual")
TAILNET = _person(Provenance.TAILSCALE_SERVE)


class _KeySet:
    def __init__(self, key: VerificationKey) -> None:
        self.key = key

    def keys_for(self, issuer: str):
        return (self.key,)


def _signed(identity_class: IdentityClass | None) -> PresentedCredential:
    fields = dict(
        issuer=ISSUER,
        subject="person-1",
        audiences=(AUDIENCE,),
        key_id="k1",
        issued_at=T0,
        expires_at=T0 + 600,
    )
    if identity_class is not None:
        fields["identity_class"] = identity_class
    claims = CredentialClaims(**fields)  # type: ignore[arg-type]
    signed = repr(dataclasses.astuple(claims)).encode()
    return PresentedCredential(claims, signed, hmac.new(b"s1", signed, hashlib.sha256).digest())


def _verifier() -> ContractCredentialVerifier:
    key = VerificationKey(
        key_id="k1",
        issuer=ISSUER,
        valid_from=T0 - 10,
        valid_until=None,
        verify=lambda data, sig: hmac.compare_digest(
            hmac.new(b"s1", data, hashlib.sha256).digest(), sig
        ),
    )
    return ContractCredentialVerifier(
        provider=PROVIDER, policy=VerifierPolicy(frozenset({ISSUER}), AUDIENCE), keys=_KeySet(key)
    )


class _Authority:
    def lookup(self, principal_id: str, crew_id: str):
        return IndividualGrant(principal_id, crew_id, GrantState.ACTIVE, 1)


# ── identity class ──


class TestIdentityClass:
    def test_credential_carries_its_class_onto_the_principal(self) -> None:
        result = _verifier().verify(_signed(IdentityClass.ORGANIZATION_DIRECTORY), T0)
        assert result.ok
        assert result.identity_class is IdentityClass.ORGANIZATION_DIRECTORY
        assert identity_class_of(result.principal) is IdentityClass.ORGANIZATION_DIRECTORY

    def test_a_credential_that_states_no_class_is_individual(self) -> None:
        result = _verifier().verify(_signed(None), T0)
        assert result.identity_class is IdentityClass.INDIVIDUAL
        assert identity_class_of(result.principal) is IdentityClass.INDIVIDUAL

    def test_tailnet_principal_is_tailnet(self) -> None:
        assert identity_class_of(TAILNET) is IdentityClass.TAILNET
        assert identity_class_of(_person(Provenance.TAILSCALE_DIRECT)) is IdentityClass.TAILNET

    def test_unknown_class_reads_as_individual(self) -> None:
        assert identity_class_of(_person(Provenance.ENTERPRISE, "galaxy")) is (
            IdentityClass.INDIVIDUAL
        )
        assert identity_class_of(_person(Provenance.ENTERPRISE)) is IdentityClass.INDIVIDUAL


# ── eligibility ──


class TestEligibility:
    def test_no_identity_class_decides_shared_access_here(self) -> None:
        for person in (INDIVIDUAL, ORG, TAILNET):
            verdict = shared_access_eligibility(person)
            assert not verdict.eligible
            assert verdict.reason.value == "external_decision_required"

    def test_personal_remote_control_admits_every_human_class(self) -> None:
        for p in (ORG, INDIVIDUAL, TAILNET):
            assert remote_control_eligibility(p).eligible

    def test_non_person_is_never_eligible(self) -> None:
        bot = Principal.system("cron")
        assert shared_access_eligibility(bot).reason.value == "not_a_person"
        assert remote_control_eligibility(bot).reason.value == "not_a_person"

    def test_grant_gate_asks_the_authority_for_every_identity_class(self) -> None:
        calls: list[str] = []

        class Counting(_Authority):
            def lookup(self, principal_id: str, crew_id: str):
                calls.append(principal_id)
                return super().lookup(principal_id, crew_id)

        gate = GrantGate(Counting(), crew_id=CREW)
        gate.decide(INDIVIDUAL, T0)
        assert gate.decide(ORG, T0).reason is AccessReason.GRANT_ACTIVE
        assert gate.decide(TAILNET, T0).reason is AccessReason.GRANT_ACTIVE
        assert INDIVIDUAL.id in calls

    def test_disabled_grant_gate_stays_not_configured_for_individual(self) -> None:
        assert GrantGate(None, crew_id=CREW).decide(INDIVIDUAL, T0).reason is (
            AccessReason.NOT_CONFIGURED
        )


# ── access modes ──


class TestAccessModes:
    def test_modes_keep_their_requirements_apart(self) -> None:
        modes = access_modes_dict(
            shared_state=CapabilityState.UNAVAILABLE, workload_state=CapabilityState.UNAVAILABLE
        )
        assert modes["shared_multi_user"]["requires"] == "cloud_account"
        assert modes["agent_workload_identity"]["requires"] == "cloud_account"
        assert modes["personal_remote_control"]["requires"] == "individual_login"
        assert "identity_classes" not in modes["shared_multi_user"]
        assert modes["shared_multi_user"]["eligibility"] == {
            "decided_by": "external_authority",
            "state": "unavailable",
            "reason": "not_configured",
        }
        assert "individual" in modes["personal_remote_control"]["identity_classes"]

    def test_remote_control_is_off_by_default(self) -> None:
        modes = access_modes_dict(
            shared_state=CapabilityState.AVAILABLE, workload_state=CapabilityState.AVAILABLE
        )
        assert modes["personal_remote_control"]["default_enabled"] is False
        assert modes["personal_remote_control"]["state"] == "disabled"


# ── workload host ──


class _Workload:
    TEST_ONLY = True

    def bind(self, crew_id: str):
        raise NotImplementedError

    def renew(self, current):
        raise NotImplementedError


class _Transport:
    TEST_ONLY = True

    def send(self, payload, credential) -> bool:
        return True


class _Edition:
    TEST_ONLY = True

    def enabled(self) -> bool:
        return True

    def credential_verifier(self):
        return None

    def grant_authority(self):
        return None

    def workload_identity(self):
        return _Workload()

    def crew_binding(self):
        return None

    def heartbeat_transport(self):
        return _Transport()

    def inference_budget(self):
        return None

    def deployment_context(self):
        return {}


class TestWorkloadHost:
    @pytest.mark.parametrize(
        "env, host",
        [
            (
                {
                    "KUBERNETES_SERVICE_HOST": "10.0.0.1",
                    "AWS_WEB_IDENTITY_TOKEN_FILE": "/var/run/token",
                    "AWS_ROLE_ARN": "role",
                },
                "eks",
            ),
            (
                {
                    "KUBERNETES_SERVICE_HOST": "10.0.0.1",
                    "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.23/v1/credentials",
                },
                "eks",
            ),
            ({"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, "kubernetes"),
            ({"ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4"}, "ecs"),
            ({}, "local"),
        ],
    )
    def test_detects_the_host(self, env, host) -> None:
        assert detect_workload_host(env) == host

    def test_only_eks_is_supported(self) -> None:
        assert workload_host_dict("eks") == {
            "supported_hosts": ["eks"],
            "host": "eks",
            "host_supported": True,
            "reason": "",
            "pod_boundary": {"scope": "any_process_in_pod", "per_process_isolation": False},
        }
        for host in ("kubernetes", "ecs", "local"):
            row = workload_host_dict(host)
            assert row["host_supported"] is False and row["reason"] == "host_not_supported"

    def test_unsupported_host_drops_the_provider_and_the_heartbeat(self) -> None:
        seams = resolve_enterprise(_Edition(), allow_test_providers=True, workload_host="ecs")
        assert seams.workload_identity is None and seams.heartbeat_transport is None
        caps = seams.capabilities_dict()
        assert caps[CAP_WORKLOAD_IDENTITY] == {
            "state": "unavailable",
            "reason": "host_not_supported",
        }
        assert caps[CAP_HEARTBEAT]["reason"] == "requires_workload_identity"

    def test_eks_keeps_the_provider(self) -> None:
        seams = resolve_enterprise(_Edition(), allow_test_providers=True, workload_host="eks")
        assert seams.workload_identity is not None
        assert seams.capabilities_dict()[CAP_WORKLOAD_IDENTITY]["state"] == "available"


# ── setup paths ──


class TestSetupPaths:
    def test_default_build_reports_every_path_unavailable(self) -> None:
        assert setup_paths_dict() == {
            "tailscale": {"state": "unavailable", "reason": "not_configured"},
            "kiro_login": {"state": "unavailable", "reason": "not_in_this_build"},
            "systems_manager": {"state": "unavailable", "reason": "not_in_this_build"},
        }

    @pytest.mark.parametrize(
        "enabled, host, state, reason",
        [
            (True, "crew.tailnet.ts.net", "available", ""),
            (True, "", "degraded", "tailnet_unreachable"),
            (False, "crew.tailnet.ts.net", "unavailable", "not_configured"),
        ],
    )
    def test_tailnet_path_follows_what_the_gateway_resolved(
        self, enabled, host, state, reason
    ) -> None:
        note_tailnet(enabled=enabled, host=host)
        assert setup_paths_dict()["tailscale"] == {"state": state, "reason": reason}

    def test_identity_trust_sets_the_tailnet_path_up_without_the_enabled_flag(self) -> None:
        """``tailscale serve`` set up by hand plus an allowlist is sharing."""
        note_tailnet(enabled=False, host="", identity_trust=True)
        assert setup_paths_dict()["tailscale"] == {"state": "available", "reason": ""}

    def test_a_registered_source_speaks_for_its_path(self) -> None:
        register_setup_path_source("kiro_login", lambda: SetupPathState(CapabilityState.AVAILABLE))
        assert setup_paths_dict()["kiro_login"] == {"state": "available", "reason": ""}
        with pytest.raises(ValueError):
            register_setup_path_source(
                "kiro_login", lambda: SetupPathState(CapabilityState.AVAILABLE)
            )
        with pytest.raises(ValueError):
            register_setup_path_source(
                "carrier_pigeon", lambda: SetupPathState(CapabilityState.AVAILABLE)
            )

    def test_a_failing_source_claims_nothing(self) -> None:
        def boom() -> SetupPathState:
            raise RuntimeError("down")

        register_setup_path_source("systems_manager", boom)
        assert setup_paths_dict()["systems_manager"] == {
            "state": "unavailable",
            "reason": "source_failed",
        }


# ── the capabilities payload ──


class TestPayload:
    def test_payload_carries_setup_paths_modes_and_workload_host(self) -> None:
        body = aggregate_capabilities().to_dict()
        assert set(body["setup_paths"]) == {"tailscale", "kiro_login", "systems_manager"}
        assert set(body["access_modes"]) == {
            "shared_multi_user",
            "personal_remote_control",
            "agent_workload_identity",
        }
        assert body["access_modes"]["shared_multi_user"]["state"] == "unavailable"
        assert body["workload_identity"]["supported_hosts"] == ["eks"]

    def test_shared_mode_is_available_once_a_setup_path_is(self) -> None:
        note_tailnet(enabled=True, host="crew.tailnet.ts.net")
        body = aggregate_capabilities().to_dict()
        assert body["access_modes"]["shared_multi_user"]["state"] == "available"

    @pytest.mark.asyncio
    async def test_route_serves_the_new_keys(self) -> None:
        app = web.Application()
        register(app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(CAPABILITIES_PATH)
            body = await resp.json()
        assert body["setup_paths"]["kiro_login"]["reason"] == "not_in_this_build"
        assert body["access_modes"]["personal_remote_control"]["requires"] == "individual_login"


# ── no permission scoping ──


def test_no_role_or_acl_exists_in_any_contract() -> None:
    """Every admitted collaborator has the same rights: no contract names a role or ACL."""
    import inspect

    from kiro_crew.shared_crew import contracts, enterprise

    banned = {"role", "roles", "acl", "acls", "permission", "permissions", "scope", "scopes"}
    for module in (contracts, enterprise, access):
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if dataclasses.is_dataclass(obj):
                assert not banned & {f.name for f in dataclasses.fields(obj)}, obj
    params = set(inspect.signature(GrantGate.decide).parameters)
    assert params == {"self", "principal", "now"}
