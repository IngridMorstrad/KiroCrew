"""The owner's routes for what goes into Kiro's Chromium: imported cookies and a refresh.

``GET    /api/browser/cookies``               the stored import's summary (counts, domains)
``POST   /api/browser/cookies``               ``{"content": str}``: parse, store, apply live
``DELETE /api/browser/cookies``               delete the import and expire it in the live browser
``POST   /api/browser/credentials/refresh``   re-pull the edition provider and the import

All four are OWNER-only, REFUSE the internal secret (an agent must not plant a session
in the browser it drives, nor read which sites one unlocks), and refuse a restricted
(incognito / temporary) session, the same rule every other credential write follows.
No response, log line or SEL record carries a cookie value: the summary and the
``credentials`` status are counts, domains and reason codes. The parse and the store
live in ``browser_harness/credentials.py``; the apply is the supervisor's.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from kiro_crew.browser_harness import credentials
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.handlers.browser_harness_status import (
    _audit,
    _deny_non_owner,
    _supervisor,
)

OP_COOKIES_STATUS = "browser_harness.cookies_status"
OP_COOKIES_IMPORT = "browser_harness.cookies_import"
OP_COOKIES_CLEAR = "browser_harness.cookies_clear"
OP_CREDENTIALS_REFRESH = "browser_harness.credentials_refresh"

#: JSON escaping can double an export's tabs, quotes and newlines; the parser still
#: caps the content itself at ``credentials.MAX_IMPORT_BYTES``.
_MAX_BODY_BYTES = 2 * credentials.MAX_IMPORT_BYTES + 1024
#: How many imported domains one SEL record names before it counts the rest.
_AUDIT_DOMAINS = 20

_lock: asyncio.Lock | None = None


def _transaction() -> asyncio.Lock:
    """One lock for import, clear and refresh, so a clear cannot interleave an apply."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _gate(request: web.Request, operation: str) -> web.Response | None:
    denied = _deny_non_owner(request, operation)
    if denied is not None:
        return denied
    from kiro_crew.dashboard.handlers._shared import _is_restricted_session

    if _is_restricted_session(request.app["state"], request):
        _audit(request, operation, "denied", "restricted session")
        return web.json_response(
            {
                "error": "a temporary or incognito session cannot change browser credentials",
                "code": "restricted_session",
            },
            status=403,
        )
    return None


def _summary(home: Any) -> dict[str, Any] | None:
    cookies = credentials.load_imported(home)
    return credentials.summarize(cookies) if cookies else None


def _audited_domains(summary: dict[str, Any] | None) -> str:
    """At most :data:`_AUDIT_DOMAINS` printable domain names: the import is untrusted text."""
    if not summary:
        return ""
    names = [str(d) for d in summary.get("domains") or []]
    shown = ",".join("".join(ch for ch in n if ch.isprintable()) for n in names[:_AUDIT_DOMAINS])
    extra = len(names) - _AUDIT_DOMAINS
    return shown + (f",+{extra} more" if extra > 0 else "")


async def api_browser_cookies_status(request: web.Request) -> web.Response:
    """GET /api/browser/cookies -- ``{"present", "summary"}``; never a value."""
    denied = _gate(request, OP_COOKIES_STATUS)
    if denied is not None:
        return denied
    summary = await asyncio.to_thread(_summary, config_dir())
    return web.json_response({"present": summary is not None, "summary": summary})


async def api_browser_cookies_import(request: web.Request) -> web.Response:
    """POST /api/browser/cookies -- store an import and apply it to a live Kiro Chromium.

    200 ``{"ok": true, "summary", "credentials"}``; 400 ``invalid_cookies`` with a
    message for the person; 403 non-owner, internal caller or restricted session.
    A browser that is not running yet receives the import at its next start.
    """
    denied = _gate(request, OP_COOKIES_IMPORT)
    if denied is not None:
        return denied
    from kiro_crew.dashboard.handlers._shared import read_bounded_json

    body, error = await read_bounded_json(request, max_bytes=_MAX_BODY_BYTES)
    if error is not None:
        return error
    assert body is not None
    content = body.get("content")
    if not isinstance(content, str):
        return web.json_response(
            {"error": "content must be a string", "code": "invalid_content"}, status=400
        )
    try:
        cookies = await asyncio.to_thread(credentials.parse_cookie_import, content)
    except credentials.CookieImportError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_cookies"}, status=400)
    home = config_dir()
    async with _transaction():
        await asyncio.to_thread(credentials.save_imported, home, cookies)
        status = await _supervisor(request).refresh_credentials()
    summary = credentials.summarize(cookies)
    _audit(
        request,
        OP_COOKIES_IMPORT,
        "applied",
        detail=f"cookies={summary['cookie_count']} domains={_audited_domains(summary)}",
    )
    return web.json_response({"ok": True, "summary": summary, "credentials": status})


async def api_browser_cookies_clear(request: web.Request) -> web.Response:
    """DELETE /api/browser/cookies -- delete the import, expire it in the live browser.

    Cookies the edition provider supplies, and ones a takeover sign-in left, are not
    touched: only the import's own name/domain/path triples are expired.
    """
    denied = _gate(request, OP_COOKIES_CLEAR)
    if denied is not None:
        return denied
    async with _transaction():
        held = await asyncio.to_thread(credentials.clear_imported, config_dir())
        expired = await _supervisor(request).forget_imported(held) if held else 0
    _audit(request, OP_COOKIES_CLEAR, "applied", detail=f"cookies={len(held)}")
    return web.json_response({"ok": True, "present": False, "expired": expired})


async def api_browser_credentials_refresh(request: web.Request) -> web.Response:
    """POST /api/browser/credentials/refresh -- re-pull both sources into a live browser."""
    denied = _gate(request, OP_CREDENTIALS_REFRESH)
    if denied is not None:
        return denied
    async with _transaction():
        status = await _supervisor(request).refresh_credentials()
    return web.json_response({"ok": True, "credentials": status})


__all__ = [
    "api_browser_cookies_clear",
    "api_browser_cookies_import",
    "api_browser_cookies_status",
    "api_browser_credentials_refresh",
]
