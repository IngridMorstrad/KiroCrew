"""Directory, client connection and Remote Control registration contracts.

**Directory.** A directory answer is either a listing (possibly empty) or an
error; an error is never shown as an empty list. Each Crew carries a stable
opaque id and one :class:`CrewConnectionState`. :func:`classify_connection_failure`
checks access loss (removed, sign-in expired) before reachability, so losing
access is never presented as ``offline``.

**Remote Control.** Registration, discovery, connecting and enabling remote
access are separate transitions. Everything starts off. ``Turn on Remote
Control`` is the one step that registers, and only when the person asks for it
(the proposed baseline); nothing registers, publishes or calls out on its own.
Disabling remote access keeps local execution. A Crew listed in a directory is
connected only by an explicit Connect.

Fixtures and contract tests only: no directory service, relay or registrar is
wired in the public build.
"""

from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional, Protocol, Union, runtime_checkable

from kiro_crew.shared_crew.codec import ContractError

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"\A[A-Za-z0-9._:\-]{1,128}\Z")


class CrewConnectionState(str, enum.Enum):
    AVAILABLE = "available"
    REVOKED = "revoked"
    OFFLINE = "offline"
    RELAY_UNAVAILABLE = "relay_unavailable"
    SIGN_IN_EXPIRED = "sign_in_expired"
    HOST_DISABLED = "host_disabled"
    VERSION_INCOMPATIBLE = "version_incompatible"


#: States that mean the person lost access, as opposed to the Crew being unreachable.
ACCESS_LOSS_STATES = frozenset({CrewConnectionState.REVOKED, CrewConnectionState.SIGN_IN_EXPIRED})

#: Refusal reasons (from the gates and the revocation enforcer) that mean access loss.
REVOKED_REASONS = frozenset(
    {
        "member_removed",
        "grant_revoked",
        "no_grant",
        "warning_only_not_a_grant",
        "authority_stale",
        "shared_access_refused",
    }
)


@dataclass(frozen=True)
class DirectoryEntry:
    crew_id: str
    display_name: str
    state: CrewConnectionState

    def __post_init__(self) -> None:
        if not _ID_RE.match(self.crew_id or ""):
            raise ContractError("malformed_record", "crew_id", "not a stable id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "crew_id": self.crew_id,
            "display_name": self.display_name,
            "state": self.state.value,
        }


class DirectoryErrorCode(str, enum.Enum):
    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "directory_unavailable"
    SIGN_IN_EXPIRED = "sign_in_expired"
    RELAY_UNAVAILABLE = "relay_unavailable"
    VERSION_INCOMPATIBLE = "version_incompatible"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class DirectoryListing:
    entries: tuple[DirectoryEntry, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.entries

    def to_dict(self) -> dict[str, Any]:
        return {"status": "ok", "entries": [e.to_dict() for e in self.entries]}


@dataclass(frozen=True)
class DirectoryError:
    code: DirectoryErrorCode

    def to_dict(self) -> dict[str, Any]:
        return {"status": "error", "error": self.code.value}


DirectoryAnswer = Union[DirectoryListing, DirectoryError]


def parse_directory_answer(raw: object) -> DirectoryAnswer:
    """A wire answer as a listing or an error. Anything unclear is an error, never empty."""
    if not isinstance(raw, Mapping):
        return DirectoryError(DirectoryErrorCode.MALFORMED)
    status = raw.get("status")
    if status == "error":
        try:
            return DirectoryError(DirectoryErrorCode(raw.get("error")))
        except ValueError:
            return DirectoryError(DirectoryErrorCode.UNAVAILABLE)
    entries = raw.get("entries")
    if status != "ok" or not isinstance(entries, list):
        return DirectoryError(DirectoryErrorCode.MALFORMED)
    out: list[DirectoryEntry] = []
    seen: set[str] = set()
    try:
        for row in entries:
            if not isinstance(row, Mapping):
                raise ContractError("malformed_record", "entries", "not an object")
            entry = DirectoryEntry(
                crew_id=str(row.get("crew_id", "")),
                display_name=str(row.get("display_name", ""))[:256],
                state=CrewConnectionState(row.get("state")),
            )
            if entry.crew_id in seen:
                raise ContractError("malformed_record", "crew_id", "repeated")
            seen.add(entry.crew_id)
            out.append(entry)
    except (ContractError, ValueError):
        return DirectoryError(DirectoryErrorCode.MALFORMED)
    return DirectoryListing(tuple(out))


@runtime_checkable
class DirectoryProvider(Protocol):
    def list_crews(self) -> DirectoryAnswer: ...


def read_directory(provider: Optional[DirectoryProvider]) -> DirectoryAnswer:
    """The provider's answer; no provider or a raising one is an error, not an empty list."""
    if provider is None:
        return DirectoryError(DirectoryErrorCode.NOT_CONFIGURED)
    try:
        answer = provider.list_crews()
    except Exception:  # noqa: BLE001 -- a failed read is an error answer
        logger.info("directory read failed", exc_info=True)
        return DirectoryError(DirectoryErrorCode.UNAVAILABLE)
    if isinstance(answer, (DirectoryListing, DirectoryError)):
        return answer
    return DirectoryError(DirectoryErrorCode.MALFORMED)


def classify_connection_failure(
    *,
    http_status: Optional[int] = None,
    reason: str = "",
    protocol_compatible: bool = True,
    host_enabled: bool = True,
    relay_reachable: bool = True,
) -> CrewConnectionState:
    """The state a failed connection shows. Access loss is checked before reachability."""
    if not protocol_compatible:
        return CrewConnectionState.VERSION_INCOMPATIBLE
    if reason in REVOKED_REASONS or http_status == 403:
        return CrewConnectionState.REVOKED
    if reason == "sign_in_expired" or http_status == 401:
        return CrewConnectionState.SIGN_IN_EXPIRED
    if not host_enabled:
        return CrewConnectionState.HOST_DISABLED
    if not relay_reachable:
        return CrewConnectionState.RELAY_UNAVAILABLE
    return CrewConnectionState.OFFLINE


class ConnectOutcome(str, enum.Enum):
    CONNECTED = "connected"
    EXPLICIT_CONNECT_REQUIRED = "explicit_connect_required"
    NOT_AVAILABLE = "not_available"


def connect_to_listed_crew(entry: DirectoryEntry, *, explicit: bool) -> ConnectOutcome:
    """Whether a client may open a connection to a listed Crew now."""
    if not explicit:
        return ConnectOutcome.EXPLICIT_CONNECT_REQUIRED
    if entry.state is not CrewConnectionState.AVAILABLE:
        return ConnectOutcome.NOT_AVAILABLE
    return ConnectOutcome.CONNECTED


# ── personal Remote Control ──


@runtime_checkable
class RemoteRegistrar(Protocol):
    def register(self) -> bool:
        """Register this machine for the signed-in person; True on success."""
        ...


@dataclass(frozen=True)
class RemoteControlState:
    registered: bool = False
    discoverable: bool = False
    remote_access_enabled: bool = False
    #: Local execution never depends on remote access.
    local_execution: bool = True

    def to_dict(self) -> dict[str, bool]:
        return {
            "registered": self.registered,
            "discoverable": self.discoverable,
            "remote_access_enabled": self.remote_access_enabled,
            "local_execution": self.local_execution,
        }


class RemoteControlOutcome(str, enum.Enum):
    DONE = "done"
    NOT_CONFIGURED = "not_configured"
    EXPLICIT_ACTION_REQUIRED = "explicit_action_required"
    NOT_REGISTERED = "not_registered"
    REGISTRATION_FAILED = "registration_failed"


class RemoteControl:
    """Personal Remote Control as separate, explicit transitions. Starts off."""

    def __init__(self, registrar: Optional[RemoteRegistrar] = None) -> None:
        self._registrar = registrar
        self._state = RemoteControlState()

    @property
    def state(self) -> RemoteControlState:
        return self._state

    def turn_on(self, *, explicit: bool) -> RemoteControlOutcome:
        """``Turn on Remote Control``: registers, and does nothing else."""
        if not explicit:
            return RemoteControlOutcome.EXPLICIT_ACTION_REQUIRED
        if self._registrar is None:
            return RemoteControlOutcome.NOT_CONFIGURED
        try:
            ok = self._registrar.register() is True
        except Exception:  # noqa: BLE001 -- a failed registration registers nothing
            logger.info("remote control registration failed", exc_info=True)
            ok = False
        if not ok:
            return RemoteControlOutcome.REGISTRATION_FAILED
        self._state = replace(self._state, registered=True)
        return RemoteControlOutcome.DONE

    def publish_discovery(self, *, explicit: bool) -> RemoteControlOutcome:
        if not explicit:
            return RemoteControlOutcome.EXPLICIT_ACTION_REQUIRED
        if not self._state.registered:
            return RemoteControlOutcome.NOT_REGISTERED
        self._state = replace(self._state, discoverable=True)
        return RemoteControlOutcome.DONE

    def enable_remote_access(self, *, explicit: bool) -> RemoteControlOutcome:
        if not explicit:
            return RemoteControlOutcome.EXPLICIT_ACTION_REQUIRED
        if not self._state.registered:
            return RemoteControlOutcome.NOT_REGISTERED
        self._state = replace(self._state, remote_access_enabled=True)
        return RemoteControlOutcome.DONE

    def disable_remote_access(self) -> RemoteControlOutcome:
        self._state = replace(self._state, remote_access_enabled=False, local_execution=True)
        return RemoteControlOutcome.DONE

    def turn_off(self) -> RemoteControlOutcome:
        self._state = RemoteControlState()
        return RemoteControlOutcome.DONE


__all__ = [
    "ACCESS_LOSS_STATES",
    "REVOKED_REASONS",
    "ConnectOutcome",
    "CrewConnectionState",
    "DirectoryAnswer",
    "DirectoryEntry",
    "DirectoryError",
    "DirectoryErrorCode",
    "DirectoryListing",
    "DirectoryProvider",
    "RemoteControl",
    "RemoteControlOutcome",
    "RemoteControlState",
    "RemoteRegistrar",
    "classify_connection_failure",
    "connect_to_listed_crew",
    "parse_directory_answer",
    "read_directory",
]
