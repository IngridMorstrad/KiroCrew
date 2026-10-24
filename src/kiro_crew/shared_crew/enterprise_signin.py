"""Relying-party contract for per-Crew human sign-in (draft, provider-neutral).

The proposed human path is a per-Crew OpenID Connect client: each Crew is its
own relying party with its own client id, and the identity provider issues an
ID token only to people it lets sign in to that Crew. This module is the
complete relying-party side of that contract, with no provider wired in:

* :class:`RelyingPartyConfig` pins the issuer, client id, audience, exact
  redirect URI and the key-set reference. A discovery URL alone is not a
  configuration, and keys come only from the pinned key set.
* :meth:`RelyingParty.begin` issues a single-use ``state`` and ``nonce`` that
  expire.
* :meth:`RelyingParty.complete` consumes the ``state`` once, checks the
  redirect, asks the provider's :class:`CodeExchange` for the ID token, binds
  the ``nonce``, then verifies the token with ``enterprise.verify_credential``
  (issuer, audience, key id, key rotation window, signature, lifetime, expiry).

A completed sign-in names a person. It grants nothing:
:attr:`SignInResult.grants_access` is always False, and access is the external
authority's decision (``enterprise.GrantGate``), kept current by the revocation
contract (``enterprise_revocation``).
"""

from __future__ import annotations

import enum
import hmac
import logging
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, runtime_checkable
from urllib.parse import urlsplit

from kiro_crew.shared_crew.codec import ContractError
from kiro_crew.shared_crew.contracts import Principal
from kiro_crew.shared_crew.enterprise import (
    KeySet,
    PresentedCredential,
    VerifierPolicy,
    verify_credential,
)

logger = logging.getLogger(__name__)

MAX_PENDING_SECONDS = 900.0
DEFAULT_PENDING_SECONDS = 600.0
DEFAULT_MAX_PENDING = 1024
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _https_url(value: str, name: str, *, allow_loopback_http: bool = False) -> None:
    parts = urlsplit(value)
    if parts.fragment or not parts.hostname:
        raise ContractError("malformed_record", name, "not an absolute URL")
    if parts.scheme == "https":
        return
    if allow_loopback_http and parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS:
        return
    raise ContractError("malformed_record", name, "must be https")


@dataclass(frozen=True)
class RelyingPartyConfig:
    """One Crew's pinned relying-party registration."""

    issuer: str
    client_id: str
    audience: str
    redirect_uri: str
    key_set_ref: str
    scopes: tuple[str, ...] = ("openid",)
    pending_seconds: float = DEFAULT_PENDING_SECONDS

    def __post_init__(self) -> None:
        _https_url(self.issuer, "issuer")
        _https_url(self.redirect_uri, "redirect_uri", allow_loopback_http=True)
        for name in ("client_id", "audience", "key_set_ref"):
            if not getattr(self, name):
                raise ContractError("malformed_record", name, "required")
        if "openid" not in self.scopes:
            raise ContractError("malformed_record", "scopes", "openid is required")
        if not 0 < self.pending_seconds <= MAX_PENDING_SECONDS:
            raise ContractError("malformed_record", "pending_seconds", "out of bounds")


@dataclass(frozen=True)
class AuthorizationRequest:
    """What the browser is sent to the provider with. Single use."""

    issuer: str
    client_id: str
    redirect_uri: str
    scopes: tuple[str, ...]
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    expires_at: float = 0.0


@dataclass(frozen=True)
class ExchangedIdToken:
    """The provider's answer to a code exchange, parsed by the provider."""

    presented: PresentedCredential
    nonce: str = field(repr=False)
    #: The ``azp`` claim when present; it must name this client.
    authorized_party: str = ""


class CodeExchangeFailed(RuntimeError):
    """The provider refused or could not complete the code exchange."""


@runtime_checkable
class CodeExchange(Protocol):
    def exchange(self, code: str, redirect_uri: str) -> ExchangedIdToken: ...


class SignInRejection(str, enum.Enum):
    UNKNOWN_STATE = "unknown_state"
    STATE_EXPIRED = "state_expired"
    REDIRECT_MISMATCH = "redirect_mismatch"
    EXCHANGE_FAILED = "exchange_failed"
    NONCE_MISMATCH = "nonce_mismatch"
    WRONG_AUTHORIZED_PARTY = "wrong_authorized_party"
    #: The ID token failed ``verify_credential``; ``credential_reason`` says why.
    CREDENTIAL_REJECTED = "credential_rejected"


@dataclass(frozen=True)
class SignInResult:
    principal: Optional[Principal] = None
    rejection: Optional[SignInRejection] = None
    credential_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.principal is not None and self.rejection is None

    @property
    def grants_access(self) -> bool:
        """Always False: a sign-in names a person and decides nothing."""
        return False


@dataclass
class _Pending:
    request: AuthorizationRequest


class RelyingParty:
    """The relying-party state machine for one Crew."""

    def __init__(
        self,
        config: RelyingPartyConfig,
        *,
        exchange: CodeExchange,
        keys: KeySet,
        provider: str,
        max_pending: int = DEFAULT_MAX_PENDING,
        token: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        if not provider:
            raise ContractError("malformed_record", "provider", "a provider name is required")
        self._config = config
        self._exchange = exchange
        self._keys = keys
        self._provider = provider
        self._token = token
        self._max_pending = max(1, max_pending)
        self._pending: OrderedDict[str, _Pending] = OrderedDict()
        self._lock = threading.Lock()
        self._policy = VerifierPolicy(issuers=frozenset({config.issuer}), audience=config.audience)

    @property
    def config(self) -> RelyingPartyConfig:
        return self._config

    def begin(self, now: float) -> AuthorizationRequest:
        state, nonce = self._token(), self._token()
        if not state or not nonce or state == nonce:
            raise ContractError("malformed_record", "state", "state and nonce must differ")
        cfg = self._config
        request = AuthorizationRequest(
            issuer=cfg.issuer,
            client_id=cfg.client_id,
            redirect_uri=cfg.redirect_uri,
            scopes=cfg.scopes,
            state=state,
            nonce=nonce,
            expires_at=now + cfg.pending_seconds,
        )
        with self._lock:
            self._pending[state] = _Pending(request)
            while len(self._pending) > self._max_pending:
                self._pending.popitem(last=False)
        return request

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def complete(self, *, state: str, code: str, redirect_uri: str, now: float) -> SignInResult:
        with self._lock:
            # Popped before anything else, so a state is spent on its first use
            # whatever the outcome, and a replay reads unknown_state.
            pending = self._pending.pop(state, None) if state else None
        if pending is None:
            return SignInResult(rejection=SignInRejection.UNKNOWN_STATE)
        request = pending.request
        if now > request.expires_at:
            return SignInResult(rejection=SignInRejection.STATE_EXPIRED)
        if redirect_uri != request.redirect_uri:
            return SignInResult(rejection=SignInRejection.REDIRECT_MISMATCH)
        try:
            answer = self._exchange.exchange(code, request.redirect_uri)
            if not isinstance(answer, ExchangedIdToken):
                raise CodeExchangeFailed("exchange returned a non-token")
        except Exception:  # noqa: BLE001 -- any exchange failure signs no one in
            logger.info("sign-in code exchange failed", exc_info=True)
            return SignInResult(rejection=SignInRejection.EXCHANGE_FAILED)
        if not answer.nonce or not hmac.compare_digest(answer.nonce, request.nonce):
            return SignInResult(rejection=SignInRejection.NONCE_MISMATCH)
        if answer.authorized_party and answer.authorized_party != self._config.client_id:
            return SignInResult(rejection=SignInRejection.WRONG_AUTHORIZED_PARTY)
        verdict = verify_credential(
            answer.presented,
            policy=self._policy,
            keys=self._keys,
            provider=self._provider,
            now=now,
        )
        if not verdict.ok:
            reason = verdict.rejection.value if verdict.rejection else ""
            return SignInResult(
                rejection=SignInRejection.CREDENTIAL_REJECTED, credential_reason=reason
            )
        return SignInResult(principal=verdict.principal)


# ── channel actors ──


@runtime_checkable
class ChannelActorMapping(Protocol):
    """The authority's explicit mapping from a channel user to a person.

    A channel (for example a chat workspace) reaches the Crew without the web
    sign-in. When team access is enforced, a channel user acts as a person only
    through this mapping, and that person then passes the same access decision
    as anyone else. An unmapped channel user is no one.
    """

    def principal_for(self, channel: str, channel_user_id: str) -> Optional[Principal]: ...


__all__ = [
    "ChannelActorMapping",
    "AuthorizationRequest",
    "CodeExchange",
    "CodeExchangeFailed",
    "ExchangedIdToken",
    "RelyingParty",
    "RelyingPartyConfig",
    "SignInRejection",
    "SignInResult",
]
