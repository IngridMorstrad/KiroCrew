"""Human sign-in relying party, external access decisions and channel actors.

A sign-in names a person and grants nothing. Shared access is the external
authority's decision; with none composed a signed-in identity is refused, and
a channel user acts only through the authority's explicit mapping.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
from typing import Any, Iterator, Optional

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.shared_crew import enterprise_gates
from kiro_crew.shared_crew.codec import ContractError
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    Principal,
    PrincipalKind,
    Provenance,
    Transport,
    principal_id_for,
)
from kiro_crew.shared_crew.enterprise import (
    AccessReason,
    CredentialClaims,
    EnterpriseSeams,
    GrantGate,
    GrantState,
    IndividualGrant,
    PresentedCredential,
    VerificationKey,
)
from kiro_crew.shared_crew.enterprise_gates import EnterpriseGates, make_enterprise_gate_middleware
from kiro_crew.shared_crew.enterprise_revocation import Removal, RevocationPolicy
from kiro_crew.shared_crew.enterprise_signin import (
    ExchangedIdToken,
    RelyingParty,
    RelyingPartyConfig,
    SignInRejection,
)

T0 = 1_800_000_000.0
ISSUER = "https://idp.example.test"
CLIENT = "crew-client-1"
REDIRECT = "https://crew.example.test/auth/callback"
CREW = "crew-1"


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    enterprise_gates._reset_for_tests()
    yield
    enterprise_gates._reset_for_tests()


# ── test signing ──


def _signer(secret: bytes):
    def verify(data: bytes, sig: bytes) -> bool:
        return hmac.compare_digest(hmac.new(secret, data, hashlib.sha256).digest(), sig)

    return verify


class Keys:
    TEST_ONLY = True

    def __init__(self) -> None:
        self.keys = [
            VerificationKey("k-old", ISSUER, T0 - 1000, T0 + 100, _signer(b"old")),
            VerificationKey("k-new", ISSUER, T0 + 50, None, _signer(b"new")),
        ]

    def keys_for(self, issuer: str):
        return tuple(k for k in self.keys if k.issuer == issuer)


def _token(
    *,
    key: str = "k-old",
    secret: bytes = b"old",
    audience: str = CLIENT,
    issuer: str = ISSUER,
    subject: str = "alice",
    iat: float = T0,
    exp: float = T0 + 600,
    key_location: str = "",
) -> PresentedCredential:
    claims = CredentialClaims(
        issuer=issuer,
        subject=subject,
        audiences=(audience,),
        key_id=key,
        issued_at=iat,
        expires_at=exp,
        key_location=key_location,
    )
    body = repr(claims).encode()
    return PresentedCredential(claims, body, hmac.new(secret, body, hashlib.sha256).digest())


class Exchange:
    TEST_ONLY = True

    def __init__(self) -> None:
        self.answer: Optional[ExchangedIdToken] = None
        self.raise_next = False
        self.calls: list[tuple[str, str]] = []

    def exchange(self, code: str, redirect_uri: str) -> ExchangedIdToken:
        self.calls.append((code, redirect_uri))
        if self.raise_next:
            raise RuntimeError("provider refused")
        assert self.answer is not None
        return self.answer


def _config(**kw: Any) -> RelyingPartyConfig:
    base: dict[str, Any] = dict(
        issuer=ISSUER,
        client_id=CLIENT,
        audience=CLIENT,
        redirect_uri=REDIRECT,
        key_set_ref="pinned-keys-1",
    )
    base.update(kw)
    return RelyingPartyConfig(**base)


def _rp() -> tuple[RelyingParty, Exchange]:
    counter = itertools.count()
    ex = Exchange()
    rp = RelyingParty(
        _config(), exchange=ex, keys=Keys(), provider="test-idp", token=lambda: f"t{next(counter)}"
    )
    return rp, ex


class TestRelyingPartyConfig:
    @pytest.mark.parametrize(
        "kw",
        [
            {"issuer": "http://idp.example.test"},
            {"redirect_uri": "http://crew.example.test/cb"},
            {"redirect_uri": "https://crew.example.test/cb#frag"},
            {"client_id": ""},
            {"audience": ""},
            {"key_set_ref": ""},
            {"scopes": ("profile",)},
            {"pending_seconds": 0},
        ],
    )
    def test_incomplete_or_unsafe_configuration_is_refused(self, kw: dict) -> None:
        with pytest.raises(ContractError):
            _config(**kw)

    def test_loopback_http_redirect_is_allowed_for_development(self) -> None:
        assert _config(redirect_uri="http://127.0.0.1:8080/cb").redirect_uri


class TestSignIn:
    def test_full_flow_names_a_person_and_grants_nothing(self) -> None:
        rp, ex = _rp()
        req = rp.begin(T0)
        assert req.state != req.nonce and req.expires_at == T0 + 600
        ex.answer = ExchangedIdToken(_token(), nonce=req.nonce, authorized_party=CLIENT)
        result = rp.complete(state=req.state, code="c1", redirect_uri=REDIRECT, now=T0 + 1)
        assert result.ok and result.principal is not None
        assert result.principal.kind is PrincipalKind.HUMAN
        assert result.principal.provenance is Provenance.ENTERPRISE
        assert result.grants_access is False
        assert ex.calls == [("c1", REDIRECT)]

    def test_state_is_single_use(self) -> None:
        rp, ex = _rp()
        req = rp.begin(T0)
        ex.answer = ExchangedIdToken(_token(), nonce=req.nonce)
        assert rp.complete(state=req.state, code="c", redirect_uri=REDIRECT, now=T0).ok
        again = rp.complete(state=req.state, code="c", redirect_uri=REDIRECT, now=T0)
        assert again.rejection is SignInRejection.UNKNOWN_STATE

    def test_unknown_expired_and_redirect_mismatch(self) -> None:
        rp, ex = _rp()
        assert rp.complete(state="nope", code="c", redirect_uri=REDIRECT, now=T0).rejection is (
            SignInRejection.UNKNOWN_STATE
        )
        req = rp.begin(T0)
        late = rp.complete(state=req.state, code="c", redirect_uri=REDIRECT, now=T0 + 601)
        assert late.rejection is SignInRejection.STATE_EXPIRED
        req = rp.begin(T0)
        other = rp.complete(state=req.state, code="c", redirect_uri=REDIRECT + "x", now=T0)
        assert other.rejection is SignInRejection.REDIRECT_MISMATCH
        assert ex.calls == []

    def test_nonce_and_authorized_party_bind_the_token(self) -> None:
        rp, ex = _rp()
        req = rp.begin(T0)
        ex.answer = ExchangedIdToken(_token(), nonce="other")
        assert rp.complete(state=req.state, code="c", redirect_uri=REDIRECT, now=T0).rejection is (
            SignInRejection.NONCE_MISMATCH
        )
        req = rp.begin(T0)
        ex.answer = ExchangedIdToken(_token(), nonce=req.nonce, authorized_party="someone-else")
        assert rp.complete(state=req.state, code="c", redirect_uri=REDIRECT, now=T0).rejection is (
            SignInRejection.WRONG_AUTHORIZED_PARTY
        )

    def test_exchange_failure_signs_no_one_in(self) -> None:
        rp, ex = _rp()
        req = rp.begin(T0)
        ex.raise_next = True
        assert rp.complete(state=req.state, code="c", redirect_uri=REDIRECT, now=T0).rejection is (
            SignInRejection.EXCHANGE_FAILED
        )

    @pytest.mark.parametrize(
        "token_kw, reason",
        [
            ({"audience": "crew-client-2"}, "wrong_audience"),
            ({"issuer": "https://evil.example.test"}, "unknown_issuer"),
            ({"secret": b"forged"}, "bad_signature"),
            ({"exp": T0 - 100, "iat": T0 - 700}, "expired"),
            ({"key_location": "https://evil.example.test/keys"}, "key_location_refused"),
            ({"key": "k-unknown"}, "unknown_key"),
        ],
    )
    def test_id_token_checks(self, token_kw: dict, reason: str) -> None:
        rp, ex = _rp()
        req = rp.begin(T0)
        ex.answer = ExchangedIdToken(_token(**token_kw), nonce=req.nonce)
        result = rp.complete(state=req.state, code="c", redirect_uri=REDIRECT, now=T0 + 30)
        assert result.rejection is SignInRejection.CREDENTIAL_REJECTED
        assert result.credential_reason == reason

    def test_key_rotation_window(self) -> None:
        rp, ex = _rp()
        # The new key is not valid yet at T0, valid from T0 + 50.
        req = rp.begin(T0)
        ex.answer = ExchangedIdToken(_token(key="k-new", secret=b"new"), nonce=req.nonce)
        early = rp.complete(state=req.state, code="c", redirect_uri=REDIRECT, now=T0 + 10)
        assert early.credential_reason == "key_not_valid"
        req = rp.begin(T0 + 60)
        ex.answer = ExchangedIdToken(
            _token(key="k-new", secret=b"new", iat=T0 + 60, exp=T0 + 600), nonce=req.nonce
        )
        assert rp.complete(state=req.state, code="c", redirect_uri=REDIRECT, now=T0 + 61).ok
        # The old key retired at T0 + 100.
        req = rp.begin(T0 + 200)
        ex.answer = ExchangedIdToken(_token(iat=T0 + 200, exp=T0 + 700), nonce=req.nonce)
        retired = rp.complete(state=req.state, code="c", redirect_uri=REDIRECT, now=T0 + 201)
        assert retired.credential_reason == "key_not_valid"

    def test_pending_requests_are_bounded(self) -> None:
        counter = itertools.count()
        rp = RelyingParty(
            _config(),
            exchange=Exchange(),
            keys=Keys(),
            provider="test-idp",
            max_pending=3,
            token=lambda: f"t{next(counter)}",
        )
        for _ in range(10):
            rp.begin(T0)
        assert rp.pending_count() == 3


# ── access decisions ──


def _person(provenance: Provenance, subject: str, cls: Optional[str] = None) -> Principal:
    return Principal(
        id=principal_id_for("edition", "issuer", subject),
        kind=PrincipalKind.HUMAN,
        provider="edition",
        issuer="issuer",
        subject=subject,
        provenance=provenance,
        extra={} if cls is None else {"identity_class": cls},
    )


SIGNED_IN = _person(Provenance.ENTERPRISE, "signed-in", "individual")
WARNED = _person(Provenance.ENTERPRISE, "warned", "organization_directory")
MEMBER = _person(Provenance.ENTERPRISE, "member", "individual")
TAILNET = _person(Provenance.TAILSCALE_SERVE, "tail")


class Authority:
    TEST_ONLY = True

    def lookup(self, principal_id: str, crew_id: str) -> Optional[IndividualGrant]:
        if principal_id == WARNED.id:
            return IndividualGrant(principal_id, crew_id, GrantState.WARNING_ONLY, 1, "", "unpaid")
        if principal_id in (MEMBER.id, TAILNET.id):
            return IndividualGrant(principal_id, crew_id, GrantState.ACTIVE, 1, "", "approved")
        return None


class Verifier:
    TEST_ONLY = True

    def verify(self, presented, now):  # pragma: no cover - never called here
        raise AssertionError


class TestExternalAccessDecision:
    def test_approved_decision_carries_the_authority_reason(self) -> None:
        decision = GrantGate(Authority(), crew_id=CREW).decide(MEMBER, T0)
        assert decision.allowed and decision.authority_reason == "approved"

    def test_warning_only_member_add_is_not_a_grant(self) -> None:
        decision = GrantGate(Authority(), crew_id=CREW).decide(WARNED, T0)
        assert decision.reason is AccessReason.WARNING_ONLY_NOT_A_GRANT
        assert not decision.allowed and decision.authority_reason == "unpaid"

    def test_an_individual_class_with_an_approved_decision_is_admitted(self) -> None:
        # Identity class is data: an approved decision admits whatever the class.
        assert GrantGate(Authority(), crew_id=CREW).decide(MEMBER, T0).allowed

    def test_login_alone_never_grants_access(self) -> None:
        gates = EnterpriseGates(EnterpriseSeams(credential_verifier=Verifier()))
        verdict = gates.check_shared_access(SIGNED_IN, T0)
        assert (verdict.allowed, verdict.reason) == (False, "access_decision_unavailable")
        # The network path keeps governing tailnet people when no authority exists.
        assert gates.check_shared_access(TAILNET, T0).allowed

    def test_default_build_reports_shared_eligibility_unavailable(self) -> None:
        from kiro_crew.shared_crew.capabilities import aggregate_capabilities

        body = aggregate_capabilities(enterprise=EnterpriseSeams()).to_dict()
        eligibility = body["access_modes"]["shared_multi_user"]["eligibility"]
        assert eligibility == {
            "decided_by": "external_authority",
            "state": "unavailable",
            "reason": "not_configured",
        }
        assert "identity_classes" not in body["access_modes"]["shared_multi_user"]
        assert body["access_modes"]["personal_remote_control"]["default_enabled"] is False


# ── middleware: Unknown callers, channel actors, revocation ──


def _app(gates: EnterpriseGates, principal: Optional[Principal], user: str = "") -> web.Application:
    @web.middleware
    async def publish(request: web.Request, handler: Any) -> web.StreamResponse:
        if user:
            request["user"] = user
        if principal is not None:
            request[ACTOR_REQUEST_KEY] = ActorContext(
                principal=principal, request_id="r1", transport=Transport.HTTP
            )
        return await handler(request)

    async def ok(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app = web.Application(
        middlewares=[publish, make_enterprise_gate_middleware(lambda: gates, clock=lambda: T0)]
    )
    app.router.add_get("/api/sessions", ok)
    app.router.add_post("/api/approvals/{id}/{action}", ok)
    return app


async def _call(app: web.Application, method: str, path: str) -> tuple[int, dict]:
    async with TestClient(TestServer(app)) as client:
        resp = await client.request(method, path)
        return resp.status, await resp.json()


class Mapping_:
    TEST_ONLY = True

    def principal_for(self, channel: str, channel_user_id: str) -> Optional[Principal]:
        return MEMBER if (channel, channel_user_id) == ("chat", "U-member") else None


class TestChannelActors:
    @pytest.mark.asyncio
    async def test_channel_minted_link_opened_as_unknown_is_refused_under_team_policy(
        self,
    ) -> None:
        """A dashboard link a channel minted authenticates a token subject; reached
        through a proxy it resolves to Unknown, which the authority cannot decide."""
        gates = EnterpriseGates(EnterpriseSeams(grant_authority=Authority()))
        status, body = await _call(
            _app(gates, Principal.unknown(), user="U-channel-user"), "GET", "/api/sessions"
        )
        assert (status, body["reason"]) == (403, "unattributed_actor")

    @pytest.mark.asyncio
    async def test_without_team_policy_the_link_behaves_as_before(self) -> None:
        gates = EnterpriseGates(EnterpriseSeams())
        status, _ = await _call(
            _app(gates, Principal.unknown(), user="U-channel-user"), "GET", "/api/sessions"
        )
        assert status == 200

    @pytest.mark.asyncio
    async def test_unauthenticated_unknown_is_left_to_dashboard_auth(self) -> None:
        gates = EnterpriseGates(EnterpriseSeams(grant_authority=Authority()))
        assert (await _call(_app(gates, Principal.unknown()), "GET", "/api/sessions"))[0] == 200

    def test_channel_user_acts_only_through_the_explicit_mapping(self) -> None:
        unmapped = EnterpriseGates(EnterpriseSeams(grant_authority=Authority()))
        verdict, person = unmapped.check_channel_actor("chat", "U-member", T0)
        assert (verdict.reason, person) == ("channel_actor_unmapped", None)

        gates = EnterpriseGates(
            EnterpriseSeams(grant_authority=Authority(), channel_actors=Mapping_())
        )
        verdict, person = gates.check_channel_actor("chat", "U-member", T0)
        assert verdict.allowed and person == MEMBER
        verdict, person = gates.check_channel_actor("chat", "U-stranger", T0)
        assert not verdict.allowed and person is None

    def test_no_team_policy_leaves_the_channel_allowlist_alone(self) -> None:
        verdict, _ = EnterpriseGates(EnterpriseSeams()).check_channel_actor("chat", "U-1", T0)
        assert verdict.allowed and verdict.reason == "not_configured"


class Feed:
    TEST_ONLY = True

    def __init__(self) -> None:
        self.log: list[Removal] = []

    def policy(self) -> RevocationPolicy:
        return RevocationPolicy(max_propagation_seconds=30.0, poll_interval_seconds=5.0)

    def removals_since(self, cursor: int):
        return tuple(self.log[cursor:]), len(self.log)


class TestRevocationInTheMiddleware:
    @pytest.mark.asyncio
    async def test_removed_member_is_refused_on_requests_and_approvals(self) -> None:
        feed = Feed()
        gates = EnterpriseGates(
            EnterpriseSeams(grant_authority=Authority(), revocation_feed=feed), now=T0
        )
        assert (await _call(_app(gates, MEMBER, "m"), "GET", "/api/sessions"))[0] == 200
        assert gates.revocation is not None
        gates.revocation.apply(Removal(MEMBER.id, "local", 2, T0), T0)
        for method, path in (("GET", "/api/sessions"), ("POST", "/api/approvals/a1/approve")):
            status, body = await _call(_app(gates, MEMBER, "m"), method, path)
            assert (status, body["reason"]) == (403, "member_removed")
        assert (await _call(_app(gates, TAILNET, "t"), "GET", "/api/sessions"))[0] == 200
