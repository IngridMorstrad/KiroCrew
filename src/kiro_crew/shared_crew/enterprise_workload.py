"""Workload identity verification contract (draft, SPIFFE-shaped, provider-neutral).

A Crew proves it is itself with a short-lived, audience-scoped workload token
(a JWT-SVID in the draft) from a workload API on its pod. A receiver accepts
one only when every binding holds:

* the issuer is a **registered** :class:`TrustAnchor`. A trust-domain string
  the token asserts is not trust: the same SPIFFE ID under an issuer that was
  never registered is refused ``untrusted_provider``;
* the SPIFFE ID's trust domain is the one that anchor was registered for;
* the SPIFFE ID is exactly one this receiver expects (no prefix match);
* this receiver's own audience is in the token, so a token minted for another
  Crew or another destination cannot be replayed here;
* the key id, rotation window, signature, lifetime and expiry pass
  ``enterprise.verify_credential`` against the anchor's pinned key set;
* the token names no key location of its own: keys come from configuration.

There is no static-key or shared-secret path, and nothing here reads one. The
supported host stays ``access.SUPPORTED_WORKLOAD_HOSTS``; the pod boundary is
reported as ``access.POD_BOUNDARY`` (any process in the attested pod can obtain
the identity), never as per-process isolation.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Optional

from kiro_crew.shared_crew.codec import ContractError
from kiro_crew.shared_crew.enterprise import (
    CredentialClaims,
    KeySet,
    PresentedCredential,
    VerifierPolicy,
    verify_credential,
)

_TRUST_DOMAIN_RE = re.compile(r"\A[a-z0-9]([a-z0-9.\-]{0,253}[a-z0-9])?\Z")
_SPIFFE_RE = re.compile(r"\Aspiffe://([^/]+)((?:/[A-Za-z0-9._\-]+)+)\Z")
#: A ``.`` or ``..`` path segment, which a SPIFFE ID must not carry.
_DOT_SEGMENT_RE = re.compile(r"/\.{1,2}(?=/|\Z)")


def parse_spiffe_id(value: str) -> Optional[tuple[str, str]]:
    """``(trust_domain, path)`` for a well-formed SPIFFE ID, else ``None``."""
    m = _SPIFFE_RE.match(value or "")
    if not m or not _TRUST_DOMAIN_RE.match(m.group(1)):
        return None
    if _DOT_SEGMENT_RE.search(m.group(2)):
        return None
    return m.group(1), m.group(2)


@dataclass(frozen=True)
class TrustAnchor:
    """A registered workload identity provider and the trust domain it vouches for."""

    provider_id: str
    issuer: str
    trust_domain: str
    keys: KeySet = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.provider_id or not self.issuer:
            raise ContractError("malformed_record", "provider_id", "provider and issuer required")
        if not _TRUST_DOMAIN_RE.match(self.trust_domain):
            raise ContractError("malformed_record", "trust_domain", "not a trust domain")
        if not isinstance(self.keys, KeySet):
            raise ContractError("malformed_record", "keys", "a pinned key set is required")


@dataclass(frozen=True)
class WorkloadVerifierConfig:
    """What this receiver trusts and who it expects."""

    audience: str
    anchors: tuple[TrustAnchor, ...]
    expected_ids: frozenset[str]

    def __post_init__(self) -> None:
        if not self.audience:
            raise ContractError("malformed_record", "audience", "required")
        if not self.anchors:
            raise ContractError("malformed_record", "anchors", "at least one trust anchor")
        issuers = [a.issuer for a in self.anchors]
        if len(set(issuers)) != len(issuers):
            raise ContractError("malformed_record", "anchors", "one anchor per issuer")
        if not self.expected_ids or any(parse_spiffe_id(i) is None for i in self.expected_ids):
            raise ContractError("malformed_record", "expected_ids", "exact SPIFFE IDs required")

    def anchor_for(self, issuer: str) -> Optional[TrustAnchor]:
        return next((a for a in self.anchors if a.issuer == issuer), None)


@dataclass(frozen=True)
class SvidClaims:
    issuer: str
    spiffe_id: str
    audiences: tuple[str, ...]
    key_id: str
    issued_at: float
    expires_at: float
    key_location: str = ""


@dataclass(frozen=True)
class PresentedSvid:
    claims: SvidClaims
    signed_bytes: bytes = field(repr=False)
    signature: bytes = field(repr=False)


class WorkloadRejection(str, enum.Enum):
    MALFORMED_SPIFFE_ID = "malformed_spiffe_id"
    KEY_LOCATION_REFUSED = "key_location_refused"
    UNTRUSTED_PROVIDER = "untrusted_provider"
    TRUST_DOMAIN_MISMATCH = "trust_domain_mismatch"
    UNEXPECTED_SPIFFE_ID = "unexpected_spiffe_id"
    #: Any ``enterprise.CredentialRejection``; ``credential_reason`` names it.
    CREDENTIAL_REJECTED = "credential_rejected"


@dataclass(frozen=True)
class WorkloadVerification:
    spiffe_id: str = ""
    provider_id: str = ""
    rejection: Optional[WorkloadRejection] = None
    credential_reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.spiffe_id) and self.rejection is None


def verify_workload(
    presented: PresentedSvid, config: WorkloadVerifierConfig, now: float
) -> WorkloadVerification:
    """Decide one presented workload token against *config*."""
    c = presented.claims
    if c.key_location:
        return WorkloadVerification(rejection=WorkloadRejection.KEY_LOCATION_REFUSED)
    parsed = parse_spiffe_id(c.spiffe_id)
    if parsed is None:
        return WorkloadVerification(rejection=WorkloadRejection.MALFORMED_SPIFFE_ID)
    anchor = config.anchor_for(c.issuer)
    if anchor is None:
        return WorkloadVerification(rejection=WorkloadRejection.UNTRUSTED_PROVIDER)
    if parsed[0] != anchor.trust_domain:
        return WorkloadVerification(rejection=WorkloadRejection.TRUST_DOMAIN_MISMATCH)
    if c.spiffe_id not in config.expected_ids:
        return WorkloadVerification(rejection=WorkloadRejection.UNEXPECTED_SPIFFE_ID)
    verdict = verify_credential(
        PresentedCredential(
            claims=CredentialClaims(
                issuer=c.issuer,
                subject=c.spiffe_id,
                audiences=c.audiences,
                key_id=c.key_id,
                issued_at=c.issued_at,
                expires_at=c.expires_at,
            ),
            signed_bytes=presented.signed_bytes,
            signature=presented.signature,
        ),
        policy=VerifierPolicy(issuers=frozenset({anchor.issuer}), audience=config.audience),
        keys=anchor.keys,
        provider=anchor.provider_id,
        now=now,
    )
    if not verdict.ok:
        reason = verdict.rejection.value if verdict.rejection else ""
        return WorkloadVerification(
            rejection=WorkloadRejection.CREDENTIAL_REJECTED, credential_reason=reason
        )
    return WorkloadVerification(spiffe_id=c.spiffe_id, provider_id=anchor.provider_id)


__all__ = [
    "PresentedSvid",
    "SvidClaims",
    "TrustAnchor",
    "WorkloadRejection",
    "WorkloadVerification",
    "WorkloadVerifierConfig",
    "parse_spiffe_id",
    "verify_workload",
]
