"""The trigger behind a session no person started: a schedule, a webhook or the Crew.

Unattended work has no human sender. A session a cron job, an inbound webhook or
the gateway itself opened is labelled with that trigger instead (``Scheduled:
<job name>``), and never with a person. The trigger is read from what the gateway
already records about the session:

* ``created_by`` of ``cron:<job id>``, which the chat routes stamp only for a
  caller the transport attests as that cron run (``cron_slot_creator``);
* a session key in the ``cron`` or ``hook`` namespace (``cron:<id>`` live,
  ``cron_<id>`` as a persisted file stem, ``cron-<id>`` as a dashboard tab);
* a slot origin of ``cron`` or ``system``.

A trigger is a display label. It is derived from records an agent's file tools
can edit, so it grants nothing and is never read by authorization.

``configured_by`` is the verified principal who configured the trigger. No
schedule or webhook in this build records one, so it is always ``None`` here;
the field exists so a build that records it can fill it without a new shape, and
the UI never fills the gap with the viewer or with the job's owner session.

Job names come from a resolver the gateway registers at start
(:func:`register_name_resolver`); it answers from the cron service's in-memory
snapshot, so building a label performs no I/O. Without one the label falls back
to the job id.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from kiro_crew.shared_crew.contracts import Principal

logger = logging.getLogger(__name__)

TRIGGER_SCHEDULED = "scheduled"
TRIGGER_WEBHOOK = "webhook"
TRIGGER_SYSTEM = "system"
TRIGGER_KINDS: tuple[str, ...] = (TRIGGER_SCHEDULED, TRIGGER_WEBHOOK, TRIGGER_SYSTEM)

#: The slot origins recorded by ``dashboard.state.SlotOrigin``.
ORIGIN_SYSTEM = "system"
ORIGIN_CRON = "cron"

_NAME_MAX = 128
_REF_MAX = 128
_CRON_JOB_ID = re.compile(r"\A[0-9a-f]{6,12}\Z")
_DASHBOARD_PREFIXES = ("dashboard:", "dashboard_")

NameResolver = Callable[[str], str]

_RESOLVERS: dict[str, NameResolver] = {}
_LOCK = threading.Lock()


@dataclass(frozen=True)
class SessionTrigger:
    """What started a session, for display."""

    kind: str
    ref: str = ""
    name: str = ""
    configured_by: Optional[Principal] = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "name": self.name,
            "configured_by": (
                None if self.configured_by is None else self.configured_by.to_public_dict()
            ),
        }


def register_name_resolver(kind: str, resolver: Optional[NameResolver]) -> None:
    """Set (or clear, with ``None``) the name lookup for one trigger kind."""
    if kind not in TRIGGER_KINDS:
        raise ValueError(f"unknown trigger kind {kind!r}")
    with _LOCK:
        if resolver is None:
            _RESOLVERS.pop(kind, None)
        else:
            _RESOLVERS[kind] = resolver


def _reset_for_tests() -> None:
    with _LOCK:
        _RESOLVERS.clear()


def _resolve_name(kind: str, ref: str) -> str:
    with _LOCK:
        resolver = _RESOLVERS.get(kind)
    if resolver is None or not ref:
        return ""
    try:
        name = resolver(ref)
    except Exception:  # noqa: BLE001 -- a failed lookup only loses the label
        logger.debug("trigger name lookup failed for %s %s", kind, ref, exc_info=True)
        return ""
    return name.strip()[:_NAME_MAX] if isinstance(name, str) else ""


def _strip_dashboard(key: str) -> str:
    for prefix in _DASHBOARD_PREFIXES:
        if key.startswith(prefix):
            return key[len(prefix) :]
    return key


def _cron_job_id(value: str) -> str:
    """The job id from a cron session, persisted stem or dashboard tab, else ``""``."""
    for sep in (":", "_", "-"):
        prefix = f"cron{sep}"
        if value.startswith(prefix):
            job_id = re.split(r"[:_]", value[len(prefix) :], maxsplit=1)[0]
            return job_id if _CRON_JOB_ID.match(job_id) else ""
    return ""


def _webhook(value: str) -> Optional[SessionTrigger]:
    if value.startswith("hook:"):
        parts = value.split(":")
        name = parts[1] if len(parts) > 1 else ""
        return SessionTrigger(TRIGGER_WEBHOOK, ref=name[:_REF_MAX], name=name[:_NAME_MAX])
    if value.startswith("hook_"):
        # The persisted stem folded every separator, so the name's end is unknown.
        return SessionTrigger(TRIGGER_WEBHOOK)
    return None


def trigger_of(
    *, key: str = "", created_by: str = "", origin: str = ""
) -> Optional[SessionTrigger]:
    """The trigger that started the session, or ``None`` for one a person (or nobody known) started."""
    created = str(created_by or "")
    job_id = _cron_job_id(created) if created.startswith("cron:") else ""
    bare = _strip_dashboard(str(key or ""))
    if not job_id:
        job_id = _cron_job_id(bare)
    if job_id or origin == ORIGIN_CRON:
        return SessionTrigger(
            TRIGGER_SCHEDULED, ref=job_id, name=_resolve_name(TRIGGER_SCHEDULED, job_id)
        )
    hook = _webhook(bare)
    if hook is not None:
        return hook
    if origin == ORIGIN_SYSTEM:
        return SessionTrigger(TRIGGER_SYSTEM)
    return None


__all__ = [
    "ORIGIN_CRON",
    "ORIGIN_SYSTEM",
    "SessionTrigger",
    "TRIGGER_KINDS",
    "TRIGGER_SCHEDULED",
    "TRIGGER_SYSTEM",
    "TRIGGER_WEBHOOK",
    "register_name_resolver",
    "trigger_of",
]
