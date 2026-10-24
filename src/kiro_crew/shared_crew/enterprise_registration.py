"""Logical Crew identity and registration fencing contract.

A logical Crew is one durable identity: its ``crew_id``, its workload SPIFFE
ID and its Profile mapping. It survives restarts, upgrades and extra replicas,
and the rotating workload credential is not part of it, so rotating a
credential never resets a quota (keyed by ``crew_id``) or remaps a Profile.

Replicas of one logical deployment (the draft's Hub and worker pods) share the
identity. An independently launched Crew needs its own pre-provisioned
identity. Registration states are explicit:

``unregistered`` --register--> ``in_use`` --release--> ``released`` --register--> ``in_use``

* a claim from the deployment holding the identity, presenting the current
  fencing token, joins as a replica;
* a claim from a different logical deployment while the identity is in use is
  ``identity_in_use``;
* after release and re-registration the fencing token moves on, so a replica
  of the old deployment presenting the old token is ``fenced``;
* an identity nobody provisioned is ``unknown_identity``: registering never
  creates one.

:class:`InMemoryCrewRegistry` is a reference implementation for contract tests
and marks itself ``TEST_ONLY``; it is refused in production resolution. No
persistent store is defined here.
"""

from __future__ import annotations

import enum
import re
import threading
from dataclasses import dataclass, field, replace
from typing import Optional, Protocol, runtime_checkable

from kiro_crew.shared_crew.codec import ContractError
from kiro_crew.shared_crew.enterprise_workload import parse_spiffe_id

_REF_RE = re.compile(r"\A[A-Za-z0-9._:\-]{1,128}\Z")


def _ref(value: str, name: str) -> None:
    if not isinstance(value, str) or not _REF_RE.match(value):
        raise ContractError("malformed_record", name, "not an opaque reference")


@dataclass(frozen=True)
class LogicalCrewIdentity:
    """The durable identity of one logical Crew. No credential lives here."""

    crew_id: str
    spiffe_id: str
    profile_ref: str

    def __post_init__(self) -> None:
        _ref(self.crew_id, "crew_id")
        _ref(self.profile_ref, "profile_ref")
        if parse_spiffe_id(self.spiffe_id) is None:
            raise ContractError("malformed_record", "spiffe_id", "not a SPIFFE ID")

    @property
    def quota_key(self) -> str:
        """What quotas are keyed by: the logical Crew, never a credential."""
        return self.crew_id


class RegistrationState(str, enum.Enum):
    UNREGISTERED = "unregistered"
    IN_USE = "in_use"
    RELEASED = "released"


@dataclass(frozen=True)
class RegistrationClaim:
    crew_id: str
    deployment_ref: str
    replica_id: str
    credential_ref: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        for name in ("crew_id", "deployment_ref", "replica_id"):
            _ref(getattr(self, name), name)


@dataclass(frozen=True)
class Registration:
    identity: LogicalCrewIdentity
    state: RegistrationState
    deployment_ref: str = ""
    fencing_token: int = 0
    replicas: frozenset[str] = frozenset()
    credential_ref: str = field(default="", repr=False)


class RegistrationOutcome(str, enum.Enum):
    REGISTERED = "registered"
    JOINED_REPLICA = "joined_replica"
    IDENTITY_IN_USE = "identity_in_use"
    FENCED = "fenced"
    UNKNOWN_IDENTITY = "unknown_identity"
    RELEASED = "released"
    NOT_HOLDER = "not_holder"
    CREDENTIAL_ROTATED = "credential_rotated"


@dataclass(frozen=True)
class RegistrationResult:
    outcome: RegistrationOutcome
    registration: Optional[Registration] = None

    @property
    def ok(self) -> bool:
        return self.outcome in (
            RegistrationOutcome.REGISTERED,
            RegistrationOutcome.JOINED_REPLICA,
            RegistrationOutcome.RELEASED,
            RegistrationOutcome.CREDENTIAL_ROTATED,
        )

    @property
    def fencing_token(self) -> int:
        return self.registration.fencing_token if self.registration else 0


@runtime_checkable
class CrewRegistry(Protocol):
    def register(
        self, claim: RegistrationClaim, fencing_token: Optional[int] = None
    ) -> RegistrationResult: ...

    def release(
        self, crew_id: str, deployment_ref: str, fencing_token: int
    ) -> RegistrationResult: ...

    def rotate_credential(
        self, crew_id: str, deployment_ref: str, fencing_token: int, credential_ref: str
    ) -> RegistrationResult: ...

    def current(self, crew_id: str) -> Optional[Registration]: ...


class InMemoryCrewRegistry:
    """Reference implementation of :class:`CrewRegistry` for contract tests."""

    TEST_ONLY = True

    def __init__(self) -> None:
        self._rows: dict[str, Registration] = {}
        self._lock = threading.Lock()

    def provision(self, identity: LogicalCrewIdentity) -> None:
        with self._lock:
            if identity.crew_id in self._rows:
                raise ContractError("malformed_record", "crew_id", "already provisioned")
            self._rows[identity.crew_id] = Registration(identity, RegistrationState.UNREGISTERED)

    def current(self, crew_id: str) -> Optional[Registration]:
        with self._lock:
            return self._rows.get(crew_id)

    def register(
        self, claim: RegistrationClaim, fencing_token: Optional[int] = None
    ) -> RegistrationResult:
        with self._lock:
            row = self._rows.get(claim.crew_id)
            if row is None:
                return RegistrationResult(RegistrationOutcome.UNKNOWN_IDENTITY)
            if row.state is not RegistrationState.IN_USE:
                if fencing_token is not None and fencing_token != row.fencing_token:
                    return RegistrationResult(RegistrationOutcome.FENCED, row)
                fresh = replace(
                    row,
                    state=RegistrationState.IN_USE,
                    deployment_ref=claim.deployment_ref,
                    fencing_token=row.fencing_token + 1,
                    replicas=frozenset({claim.replica_id}),
                    credential_ref=claim.credential_ref,
                )
                self._rows[claim.crew_id] = fresh
                return RegistrationResult(RegistrationOutcome.REGISTERED, fresh)
            if claim.deployment_ref != row.deployment_ref:
                return RegistrationResult(RegistrationOutcome.IDENTITY_IN_USE, row)
            if fencing_token != row.fencing_token:
                return RegistrationResult(RegistrationOutcome.FENCED, row)
            joined = replace(row, replicas=row.replicas | {claim.replica_id})
            self._rows[claim.crew_id] = joined
            return RegistrationResult(RegistrationOutcome.JOINED_REPLICA, joined)

    def _holder(
        self, crew_id: str, deployment_ref: str, fencing_token: int
    ) -> tuple[Optional[Registration], Optional[RegistrationResult]]:
        row = self._rows.get(crew_id)
        if row is None:
            return None, RegistrationResult(RegistrationOutcome.UNKNOWN_IDENTITY)
        if row.state is not RegistrationState.IN_USE or row.deployment_ref != deployment_ref:
            return None, RegistrationResult(RegistrationOutcome.NOT_HOLDER, row)
        if fencing_token != row.fencing_token:
            return None, RegistrationResult(RegistrationOutcome.FENCED, row)
        return row, None

    def release(self, crew_id: str, deployment_ref: str, fencing_token: int) -> RegistrationResult:
        with self._lock:
            row, refusal = self._holder(crew_id, deployment_ref, fencing_token)
            if row is None:
                assert refusal is not None
                return refusal
            gone = replace(
                row,
                state=RegistrationState.RELEASED,
                deployment_ref="",
                replicas=frozenset(),
                credential_ref="",
            )
            self._rows[crew_id] = gone
            return RegistrationResult(RegistrationOutcome.RELEASED, gone)

    def rotate_credential(
        self, crew_id: str, deployment_ref: str, fencing_token: int, credential_ref: str
    ) -> RegistrationResult:
        """Swap the rotating credential; the identity, quota key and Profile stay."""
        with self._lock:
            row, refusal = self._holder(crew_id, deployment_ref, fencing_token)
            if row is None:
                assert refusal is not None
                return refusal
            rotated = replace(row, credential_ref=credential_ref)
            self._rows[crew_id] = rotated
            return RegistrationResult(RegistrationOutcome.CREDENTIAL_ROTATED, rotated)


__all__ = [
    "CrewRegistry",
    "InMemoryCrewRegistry",
    "LogicalCrewIdentity",
    "Registration",
    "RegistrationClaim",
    "RegistrationOutcome",
    "RegistrationResult",
    "RegistrationState",
]
