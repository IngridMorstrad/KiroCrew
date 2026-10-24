"""Request-side helpers of fair admission: the trusted actor, keys, digests, bodies.

Pure functions over an aiohttp request (or anything with ``get`` and
``headers``); ``admission_gate`` re-exports them for its callers.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from kiro_crew.shared_crew.contracts import ACTOR_REQUEST_KEY, ActorContext, Principal
from kiro_crew.shared_crew.work_store import new_work_id

#: Header a client sends per logical send, reused on retry.
IDEMPOTENCY_HEADER = "Idempotency-Key"


def actor_of(request: Any) -> ActorContext | None:
    """The trusted actor the auth middleware published, or None.

    Read only from ``request[ACTOR_REQUEST_KEY]``; never from a header, body
    or query value.
    """
    getter = getattr(request, "get", None)
    if getter is None:
        return None
    actor = getter(ACTOR_REQUEST_KEY)
    return actor if isinstance(actor, ActorContext) else None


def principal_of(request: Any) -> Principal:
    """The initiator principal of *request*; Unknown when none was established."""
    actor = actor_of(request)
    return actor.principal if actor is not None else Principal.unknown()


def idempotency_key_of(request: Any) -> str:
    """The client's ``Idempotency-Key`` header, or a fresh server key when absent."""
    headers = getattr(request, "headers", None)
    raw = headers.get(IDEMPOTENCY_HEADER, "") if headers is not None else ""
    return raw.strip() if isinstance(raw, str) and raw.strip() else "srv-" + new_work_id()


def request_digest(*parts: object) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(repr(part).encode("utf-8", "replace"))
        h.update(b"\x1f")
    return h.hexdigest()


def coded_json_response(
    body: Mapping[str, Any], *, status: int, headers: Mapping[str, str] | None = None
) -> Any:
    """An :func:`error_body` as an aiohttp JSON response, its ``code`` written out.

    Every branch spells ``error`` and ``code`` in the literal, so the
    repository's error-code contract can see the code at each response site.
    """
    from aiohttp import web

    error, code = str(body["error"]), str(body["code"])
    hdrs = dict(headers or {})
    capacity, current = body.get("capacity"), body.get("current")
    if capacity is not None and current is not None:
        return web.json_response(
            {"error": error, "code": code, "capacity": capacity, "current": current},
            status=status,
            headers=hdrs,
        )
    if capacity is not None:
        return web.json_response(
            {"error": error, "code": code, "capacity": capacity}, status=status, headers=hdrs
        )
    if current is not None:
        return web.json_response(
            {"error": error, "code": code, "current": current}, status=status, headers=hdrs
        )
    return web.json_response({"error": error, "code": code}, status=status, headers=hdrs)


__all__ = [
    "IDEMPOTENCY_HEADER",
    "actor_of",
    "coded_json_response",
    "idempotency_key_of",
    "principal_of",
    "request_digest",
]
