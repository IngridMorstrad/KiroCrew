"""Authority and revocation contract for Shared Crew human access.

A sign-in proves who a person is; it does not prove they are still a member.
Removing a member is committed by an external authority, and this module is
how the Crew learns of that commit and enforces it within a recorded bound:

* :class:`RevocationPolicy` names the maximum propagation delay from the
  authority's removal commit to enforcement here. It is required; there is no
  default bound.
* :class:`RevocationFeed` is the authority's removal stream, read by cursor.
  :meth:`RevocationEnforcer.apply` takes a pushed removal at once.
* :class:`RevocationEnforcer` refuses a removed person at every
  :class:`EnforcementPoint` (cookie, WebSocket, relay connection, reconnect,
  approval submission, queued-work dispatch), closes their registered live
  connections, and fails closed for known members once the feed has not been
  read within the bound, so a partition or an authority outage cannot stretch
  it.
* An unexpired ID token is never proof of current membership:
  :meth:`RevocationEnforcer.is_current_member` reads the enforcer, not the
  token.
* What happens to work already running when its initiator is removed is
  :attr:`RevocationPolicy.running_work`, which has no default. Undecided is
  reported as undecided. Nothing here releases capacity: a disconnected person
  is not evidence that the runtime stopped.

:func:`queued_dispatch_recheck` is the admission revalidation hook. With no
enforcer installed it answers ``None`` and changes nothing.

Every decision takes ``now`` from the caller, so tests drive time explicitly.
"""

from __future__ import annotations

import enum
import logging
import math
import threading
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, runtime_checkable

from kiro_crew.shared_crew.codec import ContractError

logger = logging.getLogger(__name__)


class EnforcementPoint(str, enum.Enum):
    """Where a removed person must be refused."""

    COOKIE = "cookie"
    WEBSOCKET = "websocket"
    RELAY_CONNECTION = "relay_connection"
    RECONNECT = "reconnect"
    APPROVAL_SUBMISSION = "approval_submission"
    QUEUED_DISPATCH = "queued_dispatch"


ENFORCEMENT_POINTS: tuple[EnforcementPoint, ...] = tuple(EnforcementPoint)

#: Points with a live connection the enforcer closes when it learns a removal.
CONNECTION_POINTS = frozenset(
    {EnforcementPoint.WEBSOCKET, EnforcementPoint.RELAY_CONNECTION, EnforcementPoint.COOKIE}
)


class RunningWorkPolicy(str, enum.Enum):
    """What happens to work already running when its initiator is removed."""

    CONTINUE = "continue"
    CANCEL = "cancel"


class RunningWorkAction(str, enum.Enum):
    CONTINUE = "continue"
    CANCEL = "cancel"
    #: No policy was chosen. The work is left as it is and reported undecided.
    UNDECIDED = "undecided"


#: The longest bound a policy may record. A bound is a promise; one longer than
#: this is refused at construction rather than quietly honoured.
MAX_PROPAGATION_SECONDS = 900.0


@dataclass(frozen=True)
class RevocationPolicy:
    """The recorded revocation bound and the running-work decision.

    ``max_propagation_seconds`` is required. ``poll_interval_seconds`` is how
    often the feed is read and must be shorter than the bound.
    ``running_work`` is ``None`` until someone decides it.
    """

    max_propagation_seconds: float
    poll_interval_seconds: float
    running_work: Optional[RunningWorkPolicy] = None

    def __post_init__(self) -> None:
        bound = self.max_propagation_seconds
        poll = self.poll_interval_seconds
        if not (isinstance(bound, (int, float)) and math.isfinite(bound)):
            raise ContractError("malformed_record", "max_propagation_seconds", "not a time")
        if not 0 < bound <= MAX_PROPAGATION_SECONDS:
            raise ContractError("malformed_record", "max_propagation_seconds", "out of bounds")
        if not (isinstance(poll, (int, float)) and math.isfinite(poll)) or not 0 < poll < bound:
            raise ContractError(
                "malformed_record", "poll_interval_seconds", "must be shorter than the bound"
            )


@dataclass(frozen=True)
class Removal:
    """The authority's commit that removed one person from one Crew."""

    principal_id: str
    crew_id: str
    revision: int
    committed_at: float


class RevocationFeedUnavailable(RuntimeError):
    """The authority's removal feed could not be read."""


@runtime_checkable
class RevocationFeed(Protocol):
    def policy(self) -> RevocationPolicy:
        """The bound and running-work decision the authority recorded."""
        ...

    def removals_since(self, cursor: int) -> tuple[tuple[Removal, ...], int]:
        """Removals after *cursor* and the new cursor; raises RevocationFeedUnavailable."""
        ...


class RevocationReason(str, enum.Enum):
    ALLOWED = "allowed"
    NOT_CONFIGURED = "not_configured"
    REVOKED = "member_removed"
    #: The feed was not read within the bound; known members are refused.
    AUTHORITY_STALE = "authority_stale"


@dataclass(frozen=True)
class RevocationDecision:
    reason: RevocationReason
    point: EnforcementPoint

    @property
    def allowed(self) -> bool:
        return self.reason in (RevocationReason.ALLOWED, RevocationReason.NOT_CONFIGURED)


@dataclass
class _Connection:
    point: EnforcementPoint
    principal_id: str
    close: Callable[[], None]


class RevocationEnforcer:
    """Applies the authority's removals at every enforcement point."""

    def __init__(self, feed: Optional[RevocationFeed], *, crew_id: str, now: float) -> None:
        if not crew_id:
            raise ContractError("malformed_record", "crew_id", "a crew id is required")
        policy = feed.policy() if feed is not None else None
        if feed is not None and not isinstance(policy, RevocationPolicy):
            raise ContractError("malformed_record", "policy", "the feed recorded no policy")
        self._feed = feed
        self._policy = policy
        self._crew_id = crew_id
        self._cursor = 0
        #: When this enforcer started. A feed never read counts as read at start,
        #: so a fresh enforcer gets one bound to make its first read.
        self._last_sync = now
        self._last_attempt: Optional[float] = None
        self._removed: dict[str, Removal] = {}
        self._members: set[str] = set()
        self._connections: dict[int, _Connection] = {}
        self._next_handle = 1
        self._enforced: dict[EnforcementPoint, float] = {}
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self._feed is not None

    @property
    def policy(self) -> Optional[RevocationPolicy]:
        return self._policy

    # ── learning removals ──

    def sync_due(self, now: float) -> bool:
        if self._policy is None:
            return False
        with self._lock:
            last = self._last_attempt if self._last_attempt is not None else self._last_sync
        return now - last >= self._policy.poll_interval_seconds or now < last

    def sync(self, now: float) -> bool:
        """Read the feed once; True when it answered. Does I/O: call off the loop."""
        if self._feed is None:
            return False
        with self._lock:
            self._last_attempt = now
            cursor = self._cursor
        try:
            removals, new_cursor = self._feed.removals_since(cursor)
        except Exception:  # noqa: BLE001 -- any failure to read is an outage
            logger.info("revocation feed unavailable", exc_info=True)
            return False
        if not isinstance(new_cursor, int) or new_cursor < cursor:
            logger.warning("revocation feed moved its cursor backwards; ignoring the answer")
            return False
        for removal in removals:
            self.apply(removal, now)
        with self._lock:
            self._cursor = new_cursor
            self._last_sync = max(self._last_sync, now)
        return True

    def apply(self, removal: Removal, now: float) -> None:
        """Enforce one removal now: refuse the person and close their connections."""
        if not isinstance(removal, Removal) or removal.crew_id != self._crew_id:
            return
        with self._lock:
            held = self._removed.get(removal.principal_id)
            if held is not None and held.revision >= removal.revision:
                return
            self._removed[removal.principal_id] = removal
            self._members.discard(removal.principal_id)
            doomed = [
                (h, c)
                for h, c in self._connections.items()
                if c.principal_id == removal.principal_id
            ]
            for handle, _ in doomed:
                del self._connections[handle]
            delay = max(0.0, now - removal.committed_at)
            for point in ENFORCEMENT_POINTS:
                self._enforced[point] = max(self._enforced.get(point, 0.0), delay)
        for _, conn in doomed:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 -- a failing close still unregisters
                logger.warning("closing a removed member's connection failed", exc_info=True)

    def reinstate(self, principal_id: str, revision: int) -> None:
        """The authority granted the person again at a newer *revision*."""
        with self._lock:
            held = self._removed.get(principal_id)
            if held is not None and revision > held.revision:
                del self._removed[principal_id]

    # ── deciding ──

    def note_member(self, principal_id: str) -> None:
        """Record that *principal_id* was admitted, so staleness applies to them."""
        if self._feed is None:
            return
        with self._lock:
            if principal_id not in self._removed:
                self._members.add(principal_id)

    def _stale(self, now: float) -> bool:
        assert self._policy is not None
        return now - self._last_sync > self._policy.max_propagation_seconds

    def check(self, point: EnforcementPoint, principal_id: str, now: float) -> RevocationDecision:
        if self._feed is None:
            return RevocationDecision(RevocationReason.NOT_CONFIGURED, point)
        with self._lock:
            if principal_id in self._removed:
                return RevocationDecision(RevocationReason.REVOKED, point)
            known = principal_id in self._members
            stale = self._stale(now)
        if known and stale:
            return RevocationDecision(RevocationReason.AUTHORITY_STALE, point)
        return RevocationDecision(RevocationReason.ALLOWED, point)

    def is_current_member(self, principal_id: str, now: float, *, id_token_valid: bool) -> bool:
        """Membership as the enforcer knows it now.

        *id_token_valid* is required and never enough: a valid offline token
        for a removed person, or one checked against a stale feed, is not a
        member.
        """
        if not id_token_valid or self._feed is None:
            return False
        return self.check(EnforcementPoint.RECONNECT, principal_id, now).allowed

    def running_work_action(self, principal_id: str) -> RunningWorkAction:
        """What to do with *principal_id*'s running work. Never releases capacity."""
        with self._lock:
            removed = principal_id in self._removed
        if not removed:
            return RunningWorkAction.CONTINUE
        if self._policy is None or self._policy.running_work is None:
            return RunningWorkAction.UNDECIDED
        return RunningWorkAction(self._policy.running_work.value)

    # ── live connections ──

    def register_connection(
        self, point: EnforcementPoint, principal_id: str, close: Callable[[], None]
    ) -> int:
        """Track a live connection so a removal closes it; returns its handle."""
        with self._lock:
            handle = self._next_handle
            self._next_handle += 1
            self._connections[handle] = _Connection(point, principal_id, close)
            return handle

    def unregister_connection(self, handle: int) -> None:
        with self._lock:
            self._connections.pop(handle, None)

    def open_connections(self, principal_id: str) -> int:
        with self._lock:
            return sum(1 for c in self._connections.values() if c.principal_id == principal_id)

    # ── measurement ──

    def measured_propagation(self) -> dict[str, float]:
        """The largest observed commit-to-enforcement delay per point, in seconds."""
        with self._lock:
            return {p.value: d for p, d in self._enforced.items()}

    def within_bound(self) -> bool:
        if self._policy is None:
            return False
        bound = self._policy.max_propagation_seconds
        return all(d <= bound for d in self.measured_propagation().values())


class UnavailableRevocationEnforcer(RevocationEnforcer):
    """Refuse access while a composed feed cannot supply a valid policy.

    This enforcer has no invented propagation bound. It stays installed until
    the gateway can build the authority's real enforcer on a later request.
    """

    @property
    def configured(self) -> bool:
        return True

    def check(self, point: EnforcementPoint, principal_id: str, now: float) -> RevocationDecision:
        return RevocationDecision(RevocationReason.AUTHORITY_STALE, point)


_ACTIVE: Optional[RevocationEnforcer] = None
_ACTIVE_LOCK = threading.Lock()


def install_enforcer(enforcer: Optional[RevocationEnforcer]) -> None:
    """Make *enforcer* the one :func:`queued_dispatch_recheck` asks (None clears it)."""
    global _ACTIVE
    with _ACTIVE_LOCK:
        _ACTIVE = enforcer if enforcer is not None and enforcer.configured else None


def active_enforcer() -> Optional[RevocationEnforcer]:
    with _ACTIVE_LOCK:
        return _ACTIVE


def queued_dispatch_recheck(principal_id: str, now: float) -> Optional[str]:
    """The admission revalidation hook: a refusal reason, or ``None`` to proceed.

    With no enforcer installed (the public default) this is ``None`` for
    everyone. Otherwise a removed person, or a known member checked while the
    feed is stale, is refused before their queued work starts. A person the
    owner removed from the Tailscale access set (``access_set``) is refused the
    same way, with or without an enforcer.
    """
    from kiro_crew.shared_crew import access_set

    if access_set.principal_removed(principal_id):
        return "access_removed"
    enforcer = active_enforcer()
    if enforcer is None or not principal_id:
        return None
    decision = enforcer.check(EnforcementPoint.QUEUED_DISPATCH, principal_id, now)
    return None if decision.allowed else decision.reason.value


__all__ = [
    "CONNECTION_POINTS",
    "ENFORCEMENT_POINTS",
    "MAX_PROPAGATION_SECONDS",
    "EnforcementPoint",
    "Removal",
    "RevocationDecision",
    "RevocationEnforcer",
    "RevocationFeed",
    "RevocationFeedUnavailable",
    "RevocationPolicy",
    "RevocationReason",
    "RunningWorkAction",
    "RunningWorkPolicy",
    "active_enforcer",
    "install_enforcer",
    "queued_dispatch_recheck",
]
