"""Provider-neutral enterprise contracts for Shared Crew.

Each concern here is a separate contract, because each has a separate owner and
a separate failure mode: a human credential, an individual grant, the Crew's
workload identity, the synthetic Crew identity and its Profile, the outbound
heartbeat and the inference budget.

A Crew is a machine identity, not a group of people. Its synthetic principal is
the Crew acting as itself; the people who may work in it are individual grants,
and several Crews may grant the same people, so member lists can overlap. An edition supplies the providers through
``PlatformContext.shared_crew_enterprise``; :func:`resolve_enterprise` checks
each one before anything uses it. The public build supplies none, so every
concern answers "not configured" and nothing reaches the network.

No production credential format is defined here. A provider parses its own wire
format into :class:`PresentedCredential` and supplies the keys; this module
only decides, the same way for every provider, what a valid credential is.

Every decision takes ``now`` from the caller, so behaviour is a function of its
inputs and tests drive time explicitly.
"""

from __future__ import annotations

import enum
import logging
import math
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

from kiro_crew.shared_crew.access import (
    IDENTITY_CLASS_KEY,
    REASON_HOST_NOT_SUPPORTED,
    IdentityClass,
    detect_workload_host,
    workload_host_supported,
)
from kiro_crew.shared_crew.codec import SCHEMA_VERSION, ContractError
from kiro_crew.shared_crew.contracts import (
    CapabilityState,
    CrewIdentity,
    Principal,
    PrincipalKind,
    Provenance,
    principal_id_for,
)

logger = logging.getLogger(__name__)

#: Where an edition keeps enterprise credentials and key material, relative to
#: the data home. It sits inside the secret vault directory, which the
#: sensitive-path gate refuses to every file tool and every sandbox mode hides,
#: so a tool or terminal command cannot read what a provider stores here.
ENTERPRISE_STATE_PARENT = ".vault"
ENTERPRISE_STATE_DIR_NAME = "shared-crew-enterprise"


def enterprise_state_dir(data_home: Path) -> Path:
    """The directory an edition provider may keep credentials in."""
    return data_home / ENTERPRISE_STATE_PARENT / ENTERPRISE_STATE_DIR_NAME


# ── capability keys and reasons ──

CAP_HUMAN_CREDENTIALS = "human_credentials"
CAP_INDIVIDUAL_GRANTS = "individual_grants"
CAP_WORKLOAD_IDENTITY = "workload_identity"
CAP_CREW_IDENTITY = "crew_identity"
CAP_HEARTBEAT = "heartbeat"
CAP_INFERENCE_BUDGET = "inference_budget"
CAP_DEPLOYMENT_CONTEXT = "deployment_context"
CAP_HUMAN_SIGN_IN = "human_sign_in"
CAP_REVOCATION = "revocation"
CAP_WORKLOAD_VERIFIER = "workload_verifier"
CAP_CREW_REGISTRATION = "crew_registration"
CAP_DIRECTORY = "directory"
CAP_CHANNEL_ACTORS = "channel_actors"

ENTERPRISE_CAPABILITY_KEYS: tuple[str, ...] = (
    CAP_HUMAN_CREDENTIALS,
    CAP_INDIVIDUAL_GRANTS,
    CAP_WORKLOAD_IDENTITY,
    CAP_CREW_IDENTITY,
    CAP_HEARTBEAT,
    CAP_INFERENCE_BUDGET,
    CAP_DEPLOYMENT_CONTEXT,
    CAP_HUMAN_SIGN_IN,
    CAP_REVOCATION,
    CAP_WORKLOAD_VERIFIER,
    CAP_CREW_REGISTRATION,
    CAP_DIRECTORY,
    CAP_CHANNEL_ACTORS,
)


class UnavailableReason(str, enum.Enum):
    """Why an enterprise capability is not available."""

    #: No provider is composed. The public build answers this for every key.
    NOT_CONFIGURED = "not_configured"
    #: The provider object does not satisfy the contract it was offered for.
    PROVIDER_REFUSED = "provider_refused"
    #: A provider that marks itself test-only reached a production resolution.
    TEST_PROVIDER_REFUSED = "test_provider_refused"
    #: Reading the provider raised.
    PROVIDER_FAILED = "provider_failed"
    #: The heartbeat has a transport but no workload identity to authenticate with.
    REQUIRES_WORKLOAD_IDENTITY = "requires_workload_identity"
    #: Workload identity runs only on a supported hosted container platform.
    HOST_NOT_SUPPORTED = REASON_HOST_NOT_SUPPORTED


# ── deployment context and cap composition ──

_REF_RE = re.compile(r"\A[A-Za-z0-9._:\-]{0,128}\Z")


@dataclass(frozen=True)
class DeploymentContext:
    """Opaque references to where this Crew runs and which policy governs it.

    References only: never a URL, a credential or a host name, so the record
    can travel to the browser and into a heartbeat. ``tenant_ref`` is carried
    now so a tenant cap can later compose above the principal and Crew caps
    without a new field.
    """

    deployment_ref: str = ""
    tenant_ref: str = ""
    resource_policy_ref: str = ""

    def __post_init__(self) -> None:
        for name in ("deployment_ref", "tenant_ref", "resource_policy_ref"):
            if not _REF_RE.match(getattr(self, name)):
                raise ContractError("malformed_record", name, "not an opaque reference")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, str]) -> DeploymentContext:
        return cls(
            deployment_ref=str(raw.get("deployment_ref", "")),
            tenant_ref=str(raw.get("tenant_ref", "")),
            resource_policy_ref=str(raw.get("resource_policy_ref", "")),
        )

    @property
    def is_empty(self) -> bool:
        return not (self.deployment_ref or self.tenant_ref or self.resource_policy_ref)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "deployment_ref": self.deployment_ref,
            "tenant_ref": self.tenant_ref,
            "resource_policy_ref": self.resource_policy_ref,
        }


class LimitScope(str, enum.Enum):
    """A scope a limit applies at, outermost first."""

    TENANT = "tenant"
    CREW = "crew"
    PRINCIPAL = "principal"


def effective_limit(
    limits: Mapping[LimitScope, Optional[int]],
) -> tuple[Optional[int], Optional[LimitScope]]:
    """The binding limit across scopes and the scope that binds it.

    Limits compose by intersection: the effective value is the smallest one any
    scope sets, so a tenant cap added later can only tighten what the Crew and
    principal caps already allow. ``None`` means the scope sets no limit. On a
    tie the outermost scope is named, because that is the one an operator of
    the inner scope cannot raise.
    """
    best: Optional[int] = None
    binding: Optional[LimitScope] = None
    for scope in LimitScope:
        value = limits.get(scope)
        if value is None:
            continue
        if value < 0:
            raise ContractError("malformed_record", scope.value, "a limit cannot be negative")
        if best is None or value < best:
            best, binding = value, scope
    return best, binding


# ── human credential verification ──


@dataclass(frozen=True)
class CredentialClaims:
    """The claims a provider extracted from a presented credential.

    Times are epoch seconds. ``key_id`` names the signing key so a rotated key
    set can hold several keys at once. ``identity_class`` is how the issuer
    established the person, recorded as data; it decides nothing.
    ``key_location`` is any key-set location the credential itself names: keys
    come only from the provider's pinned configuration, so a credential that
    names one is refused.
    """

    issuer: str
    subject: str
    audiences: tuple[str, ...]
    key_id: str
    issued_at: float
    expires_at: float
    not_before: Optional[float] = None
    identity_class: IdentityClass = IdentityClass.INDIVIDUAL
    key_location: str = ""


@dataclass(frozen=True)
class PresentedCredential:
    """A credential the provider parsed, with the exact bytes its signature covers."""

    claims: CredentialClaims
    signed_bytes: bytes = field(repr=False)
    signature: bytes = field(repr=False)


@dataclass(frozen=True)
class VerificationKey:
    """One signing key in a provider's key set.

    ``valid_from`` / ``valid_until`` bound the key's rotation window: a key
    outside its window verifies nothing, so a retired key cannot be replayed.
    ``verify`` is the provider's signature check over ``(signed_bytes,
    signature)``; only a literal ``True`` counts as a pass.
    """

    key_id: str
    issuer: str
    valid_from: float
    valid_until: Optional[float]
    verify: Callable[[bytes, bytes], bool] = field(repr=False, compare=False)


@runtime_checkable
class KeySet(Protocol):
    def keys_for(self, issuer: str) -> tuple[VerificationKey, ...]: ...


#: Bounds a verifier policy may choose inside. A policy outside them is refused
#: at construction, so a misconfigured provider cannot accept a day-old credential.
MAX_CLOCK_SKEW_SECONDS = 300.0
MAX_CREDENTIAL_LIFETIME_SECONDS = 24 * 3600.0


@dataclass(frozen=True)
class VerifierPolicy:
    """What a credential must satisfy: who issued it, for whom, and for how long."""

    issuers: frozenset[str]
    audience: str
    max_clock_skew_seconds: float = 60.0
    max_lifetime_seconds: float = 3600.0

    def __post_init__(self) -> None:
        if not self.issuers or not all(self.issuers):
            raise ContractError("malformed_record", "issuers", "at least one issuer is required")
        if not self.audience:
            raise ContractError("malformed_record", "audience", "an audience is required")
        if not 0 <= self.max_clock_skew_seconds <= MAX_CLOCK_SKEW_SECONDS:
            raise ContractError("malformed_record", "max_clock_skew_seconds", "out of bounds")
        if not 0 < self.max_lifetime_seconds <= MAX_CREDENTIAL_LIFETIME_SECONDS:
            raise ContractError("malformed_record", "max_lifetime_seconds", "out of bounds")


class CredentialRejection(str, enum.Enum):
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"
    UNKNOWN_ISSUER = "unknown_issuer"
    WRONG_AUDIENCE = "wrong_audience"
    UNKNOWN_KEY = "unknown_key"
    KEY_NOT_VALID = "key_not_valid"
    BAD_SIGNATURE = "bad_signature"
    EXPIRED = "expired"
    NOT_YET_VALID = "not_yet_valid"
    LIFETIME_EXCEEDED = "lifetime_exceeded"
    #: The credential named where to fetch its own verification keys.
    KEY_LOCATION_REFUSED = "key_location_refused"


@dataclass(frozen=True)
class VerificationResult:
    principal: Optional[Principal] = None
    rejection: Optional[CredentialRejection] = None
    identity_class: Optional[IdentityClass] = None

    @property
    def ok(self) -> bool:
        return self.principal is not None and self.rejection is None

    @classmethod
    def reject(cls, reason: CredentialRejection) -> VerificationResult:
        return cls(rejection=reason)


def _finite(*values: Optional[float]) -> bool:
    return all(v is None or (isinstance(v, (int, float)) and math.isfinite(v)) for v in values)


def verify_credential(
    presented: PresentedCredential,
    *,
    policy: VerifierPolicy,
    keys: KeySet,
    provider: str,
    now: float,
) -> VerificationResult:
    """Decide a presented credential against *policy* and *keys*.

    Checks run cheapest first and stop at the first failure. The signature is
    checked before any time claim is trusted, so an attacker cannot learn which
    times would pass by varying an unsigned credential. A pass yields a human
    :class:`Principal` keyed by ``(provider, issuer, subject)``, never by email,
    carrying the credential's identity class.
    """
    c = presented.claims
    if (
        not c.issuer
        or not c.subject
        or not c.key_id
        or not c.audiences
        or not _finite(c.issued_at, c.expires_at, c.not_before, now)
        or c.expires_at <= c.issued_at
    ):
        return VerificationResult.reject(CredentialRejection.MALFORMED)
    if c.key_location:
        return VerificationResult.reject(CredentialRejection.KEY_LOCATION_REFUSED)
    if c.issuer not in policy.issuers:
        return VerificationResult.reject(CredentialRejection.UNKNOWN_ISSUER)
    if policy.audience not in c.audiences:
        return VerificationResult.reject(CredentialRejection.WRONG_AUDIENCE)
    try:
        candidates = keys.keys_for(c.issuer)
    except Exception:  # noqa: BLE001 -- a failing key set verifies nothing
        logger.warning("enterprise key set lookup failed", exc_info=True)
        return VerificationResult.reject(CredentialRejection.UNAVAILABLE)
    key = next((k for k in candidates if k.key_id == c.key_id and k.issuer == c.issuer), None)
    if key is None:
        return VerificationResult.reject(CredentialRejection.UNKNOWN_KEY)
    if now < key.valid_from or (key.valid_until is not None and now > key.valid_until):
        return VerificationResult.reject(CredentialRejection.KEY_NOT_VALID)
    try:
        signature_ok = key.verify(presented.signed_bytes, presented.signature) is True
    except Exception:  # noqa: BLE001 -- a raising check is a failed check
        signature_ok = False
    if not signature_ok:
        return VerificationResult.reject(CredentialRejection.BAD_SIGNATURE)
    skew = policy.max_clock_skew_seconds
    if c.expires_at - c.issued_at > policy.max_lifetime_seconds:
        return VerificationResult.reject(CredentialRejection.LIFETIME_EXCEEDED)
    if now > c.expires_at + skew:
        return VerificationResult.reject(CredentialRejection.EXPIRED)
    starts = c.not_before if c.not_before is not None else c.issued_at
    if now + skew < starts:
        return VerificationResult.reject(CredentialRejection.NOT_YET_VALID)
    identity_class = (
        c.identity_class
        if isinstance(c.identity_class, IdentityClass)
        else IdentityClass.INDIVIDUAL
    )
    principal = Principal(
        id=principal_id_for(provider, c.issuer, c.subject),
        kind=PrincipalKind.HUMAN,
        provider=provider,
        issuer=c.issuer,
        subject=c.subject,
        provenance=Provenance.ENTERPRISE,
        extra={IDENTITY_CLASS_KEY: identity_class.value},
    )
    return VerificationResult(principal=principal, identity_class=identity_class)


@runtime_checkable
class CredentialVerifier(Protocol):
    def verify(self, presented: PresentedCredential, now: float) -> VerificationResult: ...


class DisabledCredentialVerifier:
    """Rejects every credential: there is no authority to check it against."""

    def verify(self, presented: PresentedCredential, now: float) -> VerificationResult:
        return VerificationResult.reject(CredentialRejection.UNAVAILABLE)


class ContractCredentialVerifier:
    """:func:`verify_credential` bound to one provider's policy and key set."""

    def __init__(self, *, provider: str, policy: VerifierPolicy, keys: KeySet) -> None:
        if not provider:
            raise ContractError("malformed_record", "provider", "a provider name is required")
        self._provider = provider
        self._policy = policy
        self._keys = keys

    def verify(self, presented: PresentedCredential, now: float) -> VerificationResult:
        return verify_credential(
            presented, policy=self._policy, keys=self._keys, provider=self._provider, now=now
        )


# ── authoritative individual grants ──


class GrantState(str, enum.Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    #: A member was added with a warning (for example, eligibility not yet
    #: confirmed). It is recorded, and it is not a grant.
    WARNING_ONLY = "warning_only"


@dataclass(frozen=True)
class IndividualGrant:
    """The authority's answer for one person on one Crew.

    Individual users only: there is no group field, and a grant is never
    inferred from a session the person created or a message they wrote.
    ``revision`` is the authority's monotonic counter for this grant.
    ``decision_reason`` is the authority's own reason for its eligibility
    decision (an opaque token), carried through so the refusal or grant
    names who decided and why. Only ``active`` admits.
    """

    principal_id: str
    crew_id: str
    state: GrantState
    revision: int
    grant_ref: str = ""
    decision_reason: str = ""


class AuthorityUnavailable(RuntimeError):
    """The grant authority could not be asked."""


@runtime_checkable
class GrantAuthority(Protocol):
    def lookup(self, principal_id: str, crew_id: str) -> Optional[IndividualGrant]:
        """The current grant, ``None`` when there is none; raises AuthorityUnavailable."""
        ...


#: Upper bounds a cache policy may choose inside. Together they bound how long a
#: revoked grant can still admit new access while the authority is unreachable.
MAX_FRESH_SECONDS = 300.0
MAX_OUTAGE_GRACE_SECONDS = 900.0
DEFAULT_GRANT_CACHE_ENTRIES = 4096


@dataclass(frozen=True)
class GrantCachePolicy:
    """How long a confirmed grant is reused, and how long it survives an outage.

    Within ``fresh_seconds`` of the last confirmation an active grant is used
    without asking the authority. After that the authority is asked; if it is
    unreachable, the last confirmed active grant is honoured for a further
    ``outage_grace_seconds`` and then access is refused. A revoked or missing
    grant is never extended by an outage.
    """

    fresh_seconds: float = 60.0
    outage_grace_seconds: float = 240.0

    def __post_init__(self) -> None:
        if not 0 <= self.fresh_seconds <= MAX_FRESH_SECONDS:
            raise ContractError("malformed_record", "fresh_seconds", "out of bounds")
        if not 0 <= self.outage_grace_seconds <= MAX_OUTAGE_GRACE_SECONDS:
            raise ContractError("malformed_record", "outage_grace_seconds", "out of bounds")

    @property
    def revocation_bound_seconds(self) -> float:
        """Longest a revocation the authority recorded can go unnoticed here."""
        return self.fresh_seconds + self.outage_grace_seconds


class AccessReason(str, enum.Enum):
    GRANT_ACTIVE = "grant_active"
    #: Active grant honoured from cache because the authority is unreachable.
    GRANT_CACHED_OUTAGE = "grant_cached_outage"
    NO_GRANT = "no_grant"
    GRANT_REVOKED = "grant_revoked"
    NOT_INDIVIDUAL = "not_individual"
    #: The authority recorded a warning-only addition, which is not a grant.
    WARNING_ONLY_NOT_A_GRANT = "warning_only_not_a_grant"
    AUTHORITY_UNAVAILABLE = "authority_unavailable"
    NOT_CONFIGURED = "not_configured"


_ALLOW_REASONS = frozenset({AccessReason.GRANT_ACTIVE, AccessReason.GRANT_CACHED_OUTAGE})


@dataclass(frozen=True)
class AccessDecision:
    reason: AccessReason
    principal_id: str
    revision: Optional[int] = None
    from_cache: bool = False
    #: The authority's own reason token, when it gave one.
    authority_reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.reason in _ALLOW_REASONS


@dataclass
class _CachedGrant:
    grant: IndividualGrant
    confirmed_at: float


class GrantGate:
    """New-access decisions against the authority, with a bounded cache.

    The decision reads only the principal and the authority. Session
    attribution, message authorship, presence, heartbeat freshness and the
    principal's identity class are not inputs, so none of them can grant or
    refuse access: eligibility is the authority's decision. Call it at every
    start of new work, including queued work about to run.
    """

    def __init__(
        self,
        authority: Optional[GrantAuthority],
        *,
        crew_id: str,
        policy: GrantCachePolicy = GrantCachePolicy(),
        max_entries: int = DEFAULT_GRANT_CACHE_ENTRIES,
    ) -> None:
        if not crew_id:
            raise ContractError("malformed_record", "crew_id", "a crew id is required")
        self._authority = authority
        self._crew_id = crew_id
        self._policy = policy
        self._max_entries = max(1, max_entries)
        self._cache: OrderedDict[str, _CachedGrant] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def policy(self) -> GrantCachePolicy:
        return self._policy

    def _remember(self, grant: IndividualGrant, now: float) -> None:
        with self._lock:
            held = self._cache.get(grant.principal_id)
            if held is not None and held.grant.revision > grant.revision:
                return
            self._cache[grant.principal_id] = _CachedGrant(grant, now)
            self._cache.move_to_end(grant.principal_id)
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)

    def _cached(self, principal_id: str) -> Optional[_CachedGrant]:
        with self._lock:
            return self._cache.get(principal_id)

    def _forget(self, principal_id: str) -> None:
        with self._lock:
            self._cache.pop(principal_id, None)

    def apply_revocation(self, principal_id: str, revision: int, now: float) -> None:
        """Record a revocation pushed by the authority; it applies immediately."""
        self._remember(
            IndividualGrant(principal_id, self._crew_id, GrantState.REVOKED, revision), now
        )

    def decide(self, principal: Principal, now: float) -> AccessDecision:
        """Decide shared access for *principal* from the authority's answer.

        No authority is ``not_configured``, which grants nothing. A non-human
        principal is ``not_individual``. Every person, whatever their identity
        class, gets the authority's answer.
        """
        pid = principal.id
        if self._authority is None:
            return AccessDecision(AccessReason.NOT_CONFIGURED, pid)
        if principal.kind is not PrincipalKind.HUMAN:
            return AccessDecision(AccessReason.NOT_INDIVIDUAL, pid)
        held = self._cached(pid)
        age = None if held is None else now - held.confirmed_at
        fresh = age is not None and 0 <= age < self._policy.fresh_seconds
        if held is not None and fresh:
            return self._from_grant(held.grant, from_cache=True)
        try:
            answer = self._authority.lookup(pid, self._crew_id)
        except Exception:  # noqa: BLE001 -- any failure to ask is an outage
            logger.info("grant authority unavailable", exc_info=True)
            return self._outage(held, age, pid)
        if answer is None:
            self._forget(pid)
            return AccessDecision(AccessReason.NO_GRANT, pid)
        if answer.principal_id != pid or answer.crew_id != self._crew_id:
            logger.warning("grant authority answered for a different principal or crew")
            return self._outage(held, age, pid)
        self._remember(answer, now)
        current = self._cached(pid)
        return self._from_grant(current.grant if current else answer, from_cache=False)

    def _outage(
        self, held: Optional[_CachedGrant], age: Optional[float], pid: str
    ) -> AccessDecision:
        limit = self._policy.revocation_bound_seconds
        if (
            held is not None
            and held.grant.state is GrantState.ACTIVE
            and age is not None
            and 0 <= age <= limit
        ):
            return AccessDecision(
                AccessReason.GRANT_CACHED_OUTAGE, pid, held.grant.revision, from_cache=True
            )
        return AccessDecision(AccessReason.AUTHORITY_UNAVAILABLE, pid)

    @staticmethod
    def _from_grant(grant: IndividualGrant, *, from_cache: bool) -> AccessDecision:
        if grant.state is GrantState.ACTIVE:
            reason = AccessReason.GRANT_ACTIVE
        elif grant.state is GrantState.WARNING_ONLY:
            reason = AccessReason.WARNING_ONLY_NOT_A_GRANT
        else:
            reason = AccessReason.GRANT_REVOKED
        return AccessDecision(
            reason,
            grant.principal_id,
            grant.revision,
            from_cache=from_cache,
            authority_reason=grant.decision_reason if _REF_RE.match(grant.decision_reason) else "",
        )


# ── workload identity ──


class WorkloadState(str, enum.Enum):
    UNAVAILABLE = "unavailable"
    UNBOUND = "unbound"
    BOUND = "bound"
    EXPIRED = "expired"
    DISABLED = "disabled"


@dataclass(frozen=True)
class WorkloadCredentialRef:
    """A handle to the Crew's workload credential, never the credential itself.

    The provider resolves the handle when it signs an outbound call, so the
    material never enters a record, a log line or a browser payload.
    """

    ref: str
    crew_id: str
    expires_at: float


class WorkloadUnavailable(RuntimeError):
    """No workload identity can be obtained right now."""


class WorkloadDisabled(RuntimeError):
    """The workload identity was disabled; it is not renewed again."""


@runtime_checkable
class WorkloadIdentityProvider(Protocol):
    def bind(self, crew_id: str) -> WorkloadCredentialRef: ...

    def renew(self, current: WorkloadCredentialRef) -> WorkloadCredentialRef: ...


DEFAULT_RENEW_BEFORE_SECONDS = 300.0


class WorkloadLease:
    """Binds, renews and disables the Crew's workload identity.

    There is no fallback credential: with no provider the lease is
    ``unavailable`` and :meth:`ensure` raises. Disablement is terminal for this
    lease, whether the provider reports it or the host requests it, and no
    renewal is attempted afterwards.
    """

    def __init__(
        self,
        provider: Optional[WorkloadIdentityProvider],
        *,
        crew_id: str,
        renew_before_seconds: float = DEFAULT_RENEW_BEFORE_SECONDS,
    ) -> None:
        self._provider = provider
        self._crew_id = crew_id
        self._renew_before = max(0.0, renew_before_seconds)
        self._current: Optional[WorkloadCredentialRef] = None
        self._disabled = False
        self._lock = threading.Lock()

    def state(self, now: float) -> WorkloadState:
        with self._lock:
            if self._provider is None:
                return WorkloadState.UNAVAILABLE
            if self._disabled:
                return WorkloadState.DISABLED
            if self._current is None:
                return WorkloadState.UNBOUND
            if now >= self._current.expires_at:
                return WorkloadState.EXPIRED
            return WorkloadState.BOUND

    def disable(self) -> None:
        with self._lock:
            self._disabled = True
            self._current = None

    def _accept(self, ref: object, now: float) -> WorkloadCredentialRef:
        if (
            not isinstance(ref, WorkloadCredentialRef)
            or not ref.ref
            or ref.crew_id != self._crew_id
            or not _finite(ref.expires_at)
            or ref.expires_at <= now
        ):
            raise WorkloadUnavailable("provider returned an unusable workload credential")
        return ref

    def ensure(self, now: float) -> WorkloadCredentialRef:
        """A current credential handle, binding or renewing as needed."""
        with self._lock:
            provider = self._provider
            if provider is None:
                raise WorkloadUnavailable("no workload identity provider")
            if self._disabled:
                raise WorkloadDisabled("workload identity is disabled")
            current = self._current
            if current is not None and now < current.expires_at - self._renew_before:
                return current
            try:
                if current is None:
                    fresh = self._accept(provider.bind(self._crew_id), now)
                else:
                    fresh = self._accept(provider.renew(current), now)
            except WorkloadDisabled:
                self._disabled = True
                self._current = None
                raise
            except WorkloadUnavailable:
                if current is not None and now < current.expires_at:
                    return current
                self._current = None
                raise
            except Exception as exc:  # noqa: BLE001 -- a provider fault is unavailability
                if current is not None and now < current.expires_at:
                    return current
                self._current = None
                raise WorkloadUnavailable("workload identity provider failed") from exc
            self._current = fresh
            return fresh


# ── synthetic Crew identity and Profile binding ──

CREW_WORKLOAD_PROVIDER = "crew-workload"


def synthetic_crew_principal(crew_id: str, issuer: str) -> Principal:
    """The Crew's own principal: a machine identity, never a person or a group.

    It is a ``system`` principal keyed under its own provider namespace, so no
    human credential can produce the same id, and it names no members.
    """
    if not crew_id or not issuer:
        raise ContractError("malformed_record", "crew_id", "crew id and issuer are required")
    return Principal(
        id=principal_id_for(CREW_WORKLOAD_PROVIDER, issuer, crew_id),
        kind=PrincipalKind.SYSTEM,
        provider=CREW_WORKLOAD_PROVIDER,
        issuer=issuer,
        subject=crew_id,
        provenance=Provenance.ENTERPRISE,
    )


@dataclass(frozen=True)
class CrewBinding:
    """The stable Crew, its workload, and the synthetic identity inference runs as.

    The Crew is a machine identity. Who may work in it is decided per person by
    the grant authority, and several Crews may grant overlapping sets of people.
    The Profile reference lives on the Crew, so chargeable inference is billed
    to the Crew's synthetic identity and never to the human who started a turn.
    """

    crew: CrewIdentity
    synthetic_principal: Principal

    def __post_init__(self) -> None:
        p = self.synthetic_principal
        if p.kind is not PrincipalKind.SYSTEM or p.provider != CREW_WORKLOAD_PROVIDER:
            raise ContractError(
                "malformed_record", "synthetic_principal", "must be a synthetic Crew identity"
            )
        if p.subject != self.crew.crew_id:
            raise ContractError("malformed_record", "synthetic_principal", "names another Crew")

    @property
    def inference_owner(self) -> Principal:
        return self.synthetic_principal


@runtime_checkable
class CrewBindingProvider(Protocol):
    def current_binding(self) -> Optional[CrewBinding]: ...


# ── outbound heartbeat ──


class HeartbeatState(str, enum.Enum):
    """The Crew's health, as the Console shows it. Exactly three values."""

    #: An acknowledged heartbeat arrived within the late threshold.
    HEALTHY = "healthy"
    #: The last acknowledged heartbeat is older than the late threshold.
    HEARTBEAT_LATE = "heartbeat_late"
    #: No heartbeat was ever acknowledged, or the last one is older than the
    #: disconnect threshold. A Crew that never connected is this, not a fourth value.
    NOT_CONNECTED = "not_connected"


#: The only keys a heartbeat may carry. Counts and states, never content: there
#: is no field a transcript, prompt, session title or principal could ride in.
HEARTBEAT_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "crew_id",
        "deployment_ref",
        "sent_at",
        "build",
        "capabilities",
        "active_runtimes",
        "queued_work",
    }
)
_HEARTBEAT_TEXT_RE = re.compile(r"\A[A-Za-z0-9._:+\-]{0,64}\Z")
_HEARTBEAT_STATES = frozenset(s.value for s in CapabilityState)


def validate_heartbeat_payload(payload: Mapping[str, Any]) -> None:
    """Refuse any payload outside the allowlist, so content cannot leak by accident."""
    extra = set(payload) - HEARTBEAT_PAYLOAD_KEYS
    if extra:
        raise ContractError("malformed_record", sorted(extra)[0], "not a heartbeat field")
    for key in ("crew_id", "deployment_ref", "build"):
        value = payload.get(key, "")
        if not isinstance(value, str) or not _HEARTBEAT_TEXT_RE.match(value):
            raise ContractError("malformed_record", key, "not an opaque token")
    for key in ("active_runtimes", "queued_work", "schema_version"):
        value = payload.get(key, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ContractError("malformed_record", key, "expected a non-negative integer")
    sent_at = payload.get("sent_at", 0.0)
    if not isinstance(sent_at, (int, float)) or isinstance(sent_at, bool) or not _finite(sent_at):
        raise ContractError("malformed_record", "sent_at", "expected a time")
    caps = payload.get("capabilities", {})
    if not isinstance(caps, Mapping):
        raise ContractError("malformed_record", "capabilities", "expected an object")
    for name, state in caps.items():
        if name not in ENTERPRISE_CAPABILITY_KEYS or state not in _HEARTBEAT_STATES:
            raise ContractError("malformed_record", "capabilities", "not a capability state")


def build_heartbeat_payload(
    *,
    crew_id: str,
    deployment: DeploymentContext,
    sent_at: float,
    build: str,
    capabilities: Mapping[str, CapabilityState],
    active_runtimes: int,
    queued_work: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "crew_id": crew_id,
        "deployment_ref": deployment.deployment_ref,
        "sent_at": sent_at,
        "build": build,
        "capabilities": {k: v.value for k, v in capabilities.items()},
        "active_runtimes": active_runtimes,
        "queued_work": queued_work,
    }
    validate_heartbeat_payload(payload)
    return payload


@runtime_checkable
class HeartbeatTransport(Protocol):
    def send(self, payload: Mapping[str, Any], credential: WorkloadCredentialRef) -> bool:
        """Deliver one heartbeat authenticated as *credential*; True only on an ack."""
        ...


#: Placeholder timings. The real interval, late and disconnect thresholds, and
#: where health is persisted are unresolved, so these live in memory only and
#: no store is built for them.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60.0
DEFAULT_LATE_AFTER_SECONDS = 180.0
DEFAULT_NOT_CONNECTED_AFTER_SECONDS = 900.0


class HeartbeatTracker:
    """not_connected -> healthy <-> heartbeat_late -> not_connected, in memory.

    ``last_success`` moves only on an acknowledged send; ``last_attempt``
    moves on every send tried, acknowledged or not, so a Crew that keeps
    trying and failing reads late, with its attempts visible. The state is
    telemetry for the Console and the UI. It is not an input to any access
    decision: a healthy Crew grants nothing and a late one revokes nothing.
    """

    def __init__(
        self,
        *,
        late_after_seconds: float = DEFAULT_LATE_AFTER_SECONDS,
        not_connected_after_seconds: Optional[float] = DEFAULT_NOT_CONNECTED_AFTER_SECONDS,
    ) -> None:
        self._late_after = max(0.0, late_after_seconds)
        self._gone_after = (
            None
            if not_connected_after_seconds is None
            else max(self._late_after, not_connected_after_seconds)
        )
        self._last_success: Optional[float] = None
        self._last_attempt: Optional[float] = None
        self._lock = threading.Lock()

    @property
    def last_success(self) -> Optional[float]:
        with self._lock:
            return self._last_success

    @property
    def last_attempt(self) -> Optional[float]:
        with self._lock:
            return self._last_attempt

    def record_attempt(self, now: float) -> None:
        with self._lock:
            if self._last_attempt is None or now > self._last_attempt:
                self._last_attempt = now

    def record_ack(self, now: float) -> None:
        with self._lock:
            if self._last_attempt is None or now > self._last_attempt:
                self._last_attempt = now
            if self._last_success is None or now > self._last_success:
                self._last_success = now

    def state(self, now: float) -> HeartbeatState:
        with self._lock:
            last = self._last_success
        if last is None:
            return HeartbeatState.NOT_CONNECTED
        age = now - last
        if 0 <= age <= self._late_after:
            return HeartbeatState.HEALTHY
        if self._gone_after is not None and age > self._gone_after:
            return HeartbeatState.NOT_CONNECTED
        return HeartbeatState.HEARTBEAT_LATE

    def snapshot(self, now: float) -> dict[str, Any]:
        with self._lock:
            success, attempt = self._last_success, self._last_attempt
        return {"state": self.state(now).value, "last_success": success, "last_attempt": attempt}


class HeartbeatOutcome(str, enum.Enum):
    SENT = "sent"
    NOT_CONFIGURED = "not_configured"
    WORKLOAD_UNAVAILABLE = "workload_unavailable"
    WORKLOAD_DISABLED = "workload_disabled"
    REFUSED_PAYLOAD = "refused_payload"
    NOT_ACKNOWLEDGED = "not_acknowledged"


class HeartbeatSender:
    """One authenticated send per :meth:`tick`; the tracker moves only on an ack."""

    def __init__(
        self,
        transport: Optional[HeartbeatTransport],
        lease: WorkloadLease,
        tracker: HeartbeatTracker,
    ) -> None:
        self._transport = transport
        self._lease = lease
        self._tracker = tracker

    def tick(self, payload: Mapping[str, Any], now: float) -> HeartbeatOutcome:
        if self._transport is None:
            return HeartbeatOutcome.NOT_CONFIGURED
        try:
            validate_heartbeat_payload(payload)
        except ContractError:
            return HeartbeatOutcome.REFUSED_PAYLOAD
        self._tracker.record_attempt(now)
        try:
            credential = self._lease.ensure(now)
        except WorkloadDisabled:
            return HeartbeatOutcome.WORKLOAD_DISABLED
        except WorkloadUnavailable:
            return HeartbeatOutcome.WORKLOAD_UNAVAILABLE
        try:
            acked = self._transport.send(payload, credential) is True
        except Exception:  # noqa: BLE001 -- a failed send is an unacknowledged one
            logger.info("heartbeat send failed", exc_info=True)
            acked = False
        if not acked:
            return HeartbeatOutcome.NOT_ACKNOWLEDGED
        self._tracker.record_ack(now)
        return HeartbeatOutcome.SENT


# ── inference budget ──


class BudgetState(str, enum.Enum):
    AVAILABLE = "available"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"
    #: No enterprise budget applies (the public build).
    NOT_ENFORCED = "not_enforced"


class OperationKind(str, enum.Enum):
    NEW_TURN = "new_turn"
    STEER = "steer"
    RETRY = "retry"
    SPAWN = "spawn"
    AUTOMATION_RUN = "automation_run"
    READ_SESSION = "read_session"
    READ_RESULT = "read_result"
    LIST = "list"
    EXPORT = "export"
    CANCEL = "cancel"


#: Operations that start new chargeable inference. Everything else reads what
#: exists or stops work, and stays permitted when the budget is spent.
CHARGEABLE_OPERATIONS = frozenset(
    {
        OperationKind.NEW_TURN,
        OperationKind.STEER,
        OperationKind.RETRY,
        OperationKind.SPAWN,
        OperationKind.AUTOMATION_RUN,
    }
)


class BudgetReason(str, enum.Enum):
    ALLOWED = "allowed"
    INFERENCE_BUDGET_EXHAUSTED = "inference_budget_exhausted"
    INFERENCE_BUDGET_UNKNOWN = "inference_budget_unknown"


@dataclass(frozen=True)
class BudgetDecision:
    reason: BudgetReason
    state: BudgetState

    @property
    def allowed(self) -> bool:
        return self.reason is BudgetReason.ALLOWED


@runtime_checkable
class InferenceBudgetProvider(Protocol):
    def state(self, deployment: DeploymentContext) -> BudgetState: ...


def budget_allows(operation: OperationKind, state: BudgetState) -> BudgetDecision:
    """Whether *operation* may proceed under *state*.

    An unknown budget refuses new chargeable inference, because spend that
    cannot be checked must not start; reads and cancellation never depend on it.
    """
    if operation not in CHARGEABLE_OPERATIONS:
        return BudgetDecision(BudgetReason.ALLOWED, state)
    if state in (BudgetState.AVAILABLE, BudgetState.NOT_ENFORCED):
        return BudgetDecision(BudgetReason.ALLOWED, state)
    if state is BudgetState.EXHAUSTED:
        return BudgetDecision(BudgetReason.INFERENCE_BUDGET_EXHAUSTED, state)
    return BudgetDecision(BudgetReason.INFERENCE_BUDGET_UNKNOWN, state)


class BudgetGate:
    def __init__(
        self, provider: Optional[InferenceBudgetProvider], deployment: DeploymentContext
    ) -> None:
        self._provider = provider
        self._deployment = deployment

    def current_state(self) -> BudgetState:
        if self._provider is None:
            return BudgetState.NOT_ENFORCED
        try:
            state = self._provider.state(self._deployment)
        except Exception:  # noqa: BLE001 -- an unreadable budget is unknown
            logger.info("inference budget lookup failed", exc_info=True)
            return BudgetState.UNKNOWN
        if not isinstance(state, BudgetState) or state is BudgetState.NOT_ENFORCED:
            return BudgetState.UNKNOWN
        return state

    def check(self, operation: OperationKind) -> BudgetDecision:
        if operation not in CHARGEABLE_OPERATIONS:
            return BudgetDecision(BudgetReason.ALLOWED, BudgetState.NOT_ENFORCED)
        return budget_allows(operation, self.current_state())


# ── resolution ──


@dataclass(frozen=True)
class EnterpriseCapability:
    state: CapabilityState
    reason: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"state": self.state.value, "reason": self.reason}


@dataclass(frozen=True)
class EnterpriseSeams:
    """The checked enterprise providers; ``None`` where none is usable."""

    credential_verifier: Optional[CredentialVerifier] = None
    grant_authority: Optional[GrantAuthority] = None
    workload_identity: Optional[WorkloadIdentityProvider] = None
    crew_binding: Optional[CrewBindingProvider] = None
    heartbeat_transport: Optional[HeartbeatTransport] = None
    inference_budget: Optional[InferenceBudgetProvider] = None
    #: ``enterprise_signin.RelyingParty``
    sign_in: Optional[Any] = None
    #: ``enterprise_revocation.RevocationFeed``
    revocation_feed: Optional[Any] = None
    #: ``enterprise_workload.WorkloadVerifierConfig``
    workload_verifier: Optional[Any] = None
    #: ``enterprise_registration.CrewRegistry``
    crew_registry: Optional[Any] = None
    #: ``enterprise_directory.DirectoryProvider``
    directory: Optional[Any] = None
    #: ``enterprise_signin.ChannelActorMapping``
    channel_actors: Optional[Any] = None
    deployment: DeploymentContext = DeploymentContext()
    capabilities: Mapping[str, EnterpriseCapability] = field(default_factory=dict)

    def capabilities_dict(self) -> dict[str, dict[str, str]]:
        not_configured = EnterpriseCapability(
            CapabilityState.UNAVAILABLE, UnavailableReason.NOT_CONFIGURED.value
        )
        return {
            key: self.capabilities.get(key, not_configured).to_dict()
            for key in ENTERPRISE_CAPABILITY_KEYS
        }


def _is_test_only(obj: object) -> bool:
    return getattr(obj, "TEST_ONLY", False) is True


_SLOTS: tuple[tuple[str, str, type], ...] = (
    ("credential_verifier", CAP_HUMAN_CREDENTIALS, CredentialVerifier),
    ("grant_authority", CAP_INDIVIDUAL_GRANTS, GrantAuthority),
    ("workload_identity", CAP_WORKLOAD_IDENTITY, WorkloadIdentityProvider),
    ("crew_binding", CAP_CREW_IDENTITY, CrewBindingProvider),
    ("heartbeat_transport", CAP_HEARTBEAT, HeartbeatTransport),
    ("inference_budget", CAP_INFERENCE_BUDGET, InferenceBudgetProvider),
)


_ABSENT = object()


def _extended_slots() -> tuple[tuple[str, str, type], ...]:
    """The slots whose contracts live in sibling modules, imported on first use."""
    from kiro_crew.shared_crew.enterprise_directory import DirectoryProvider
    from kiro_crew.shared_crew.enterprise_registration import CrewRegistry
    from kiro_crew.shared_crew.enterprise_revocation import RevocationFeed
    from kiro_crew.shared_crew.enterprise_signin import ChannelActorMapping, RelyingParty
    from kiro_crew.shared_crew.enterprise_workload import WorkloadVerifierConfig

    return (
        ("sign_in", CAP_HUMAN_SIGN_IN, RelyingParty),
        ("revocation_feed", CAP_REVOCATION, RevocationFeed),
        ("workload_verifier", CAP_WORKLOAD_VERIFIER, WorkloadVerifierConfig),
        ("crew_registry", CAP_CREW_REGISTRATION, CrewRegistry),
        ("directory", CAP_DIRECTORY, DirectoryProvider),
        ("channel_actors", CAP_CHANNEL_ACTORS, ChannelActorMapping),
    )


def _context_provider() -> Optional[object]:
    from kiro_crew.platform import safe_context_call
    from kiro_crew.platform.context import current_context

    return safe_context_call(
        lambda: current_context().shared_crew_enterprise,
        fallback=None,
        log_message="shared crew enterprise seam unreadable; reporting it unavailable",
    )


def _unavailable(reason: UnavailableReason) -> EnterpriseCapability:
    return EnterpriseCapability(CapabilityState.UNAVAILABLE, reason.value)


def resolve_enterprise(
    provider: Optional[object] = None,
    *,
    allow_test_providers: bool = False,
    workload_host: Optional[str] = None,
) -> EnterpriseSeams:
    """Check the composed enterprise providers and report what each can back.

    With no *provider* the platform slot is read. A provider, or any object it
    returns, that sets ``TEST_ONLY = True`` is refused unless the caller passes
    ``allow_test_providers=True``; only tests pass it, so a test double cannot
    make a production Crew look connected. A capability reads ``available``
    when a checked provider is composed. That says the Crew is configured for
    it, not that the remote service answered.

    A workload identity provider is used only on a supported host
    (``access.SUPPORTED_WORKLOAD_HOSTS``); on any other host it is dropped with
    ``host_not_supported`` and no other credential takes its place.
    *workload_host* defaults to the detected one.
    """
    source = _context_provider() if provider is None else provider
    caps: dict[str, EnterpriseCapability] = {}
    if source is None:
        return EnterpriseSeams()
    if _is_test_only(source) and not allow_test_providers:
        refused = _unavailable(UnavailableReason.TEST_PROVIDER_REFUSED)
        return EnterpriseSeams(capabilities={k: refused for k in ENTERPRISE_CAPABILITY_KEYS})
    try:
        enabled = source.enabled() is True  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 -- an unreadable seam is a disabled one
        logger.warning("shared crew enterprise provider failed", exc_info=True)
        failed = _unavailable(UnavailableReason.PROVIDER_FAILED)
        return EnterpriseSeams(capabilities={k: failed for k in ENTERPRISE_CAPABILITY_KEYS})
    if not enabled:
        return EnterpriseSeams()

    chosen: dict[str, Any] = {}
    for attr, key, protocol in _SLOTS + _extended_slots():
        try:
            # An edition written before a slot existed simply lacks the accessor;
            # that is "not configured", not a provider failure.
            accessor = getattr(source, attr, None)
            obj = _ABSENT if accessor is None else accessor()
        except Exception:  # noqa: BLE001
            logger.warning("shared crew enterprise %s failed", attr, exc_info=True)
            caps[key] = _unavailable(UnavailableReason.PROVIDER_FAILED)
            continue
        if obj is _ABSENT or obj is None:
            caps[key] = _unavailable(UnavailableReason.NOT_CONFIGURED)
        elif _is_test_only(obj) and not allow_test_providers:
            caps[key] = _unavailable(UnavailableReason.TEST_PROVIDER_REFUSED)
        elif not isinstance(obj, protocol):
            caps[key] = _unavailable(UnavailableReason.PROVIDER_REFUSED)
        else:
            chosen[attr] = obj
            caps[key] = EnterpriseCapability(CapabilityState.AVAILABLE)

    if "workload_identity" in chosen:
        host = detect_workload_host() if workload_host is None else workload_host
        if not workload_host_supported(host):
            del chosen["workload_identity"]
            caps[CAP_WORKLOAD_IDENTITY] = _unavailable(UnavailableReason.HOST_NOT_SUPPORTED)

    if "heartbeat_transport" in chosen and "workload_identity" not in chosen:
        # An unauthenticated heartbeat is never sent, so a transport alone backs nothing.
        del chosen["heartbeat_transport"]
        caps[CAP_HEARTBEAT] = _unavailable(UnavailableReason.REQUIRES_WORKLOAD_IDENTITY)

    deployment = DeploymentContext()
    try:
        deployment = DeploymentContext.from_mapping(source.deployment_context())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 -- a malformed reference set is no reference set
        logger.warning("shared crew deployment context refused", exc_info=True)
        caps[CAP_DEPLOYMENT_CONTEXT] = _unavailable(UnavailableReason.PROVIDER_REFUSED)
    else:
        caps[CAP_DEPLOYMENT_CONTEXT] = (
            _unavailable(UnavailableReason.NOT_CONFIGURED)
            if deployment.is_empty
            else EnterpriseCapability(CapabilityState.AVAILABLE)
        )
    return EnterpriseSeams(deployment=deployment, capabilities=caps, **chosen)
