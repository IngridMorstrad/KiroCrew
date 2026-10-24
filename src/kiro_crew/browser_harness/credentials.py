"""Browser credentials for Kiro's own Chromium: what the gateway puts in it, and how.

Some sites the agent is asked to open sit behind a company sign-in the agent cannot
do itself (a device-bound login, a hardware key, a Kerberos ticket). Kiro's Chromium
starts with an empty profile, so every such site is a login wall. Two sources can
fill it, and both are applied by the GATEWAY, never by the sandboxed agent:

1. **An edition's provider** (``PlatformContext.browser_credentials``,
   :class:`~kiro_crew.platform.interfaces.BrowserCredentialProvider`). The public
   build's Default supplies nothing, so the stock browser is unchanged. An edition
   that already holds the user's session for its own sites (its own credential
   files, which its sandbox policy keeps hidden) returns those cookies here, plus at
   most the launch switches in :data:`ALLOWED_LAUNCH_SWITCHES`. OPT-IN: used only
   while ``browser.credentials.enabled`` is on, and only for cookies on a domain in
   BOTH the provider's ``domains()`` and ``browser.credentials.domains``; an unset
   key takes the provider's ``default_settings()`` (:func:`effective_settings`).
2. **Cookies the owner imported** from the dashboard (``POST /api/browser/cookies``):
   a Playwright ``storageState``, a cookie-extension JSON array, or a Netscape
   ``cookies.txt`` (the parser below, carried over from the retired browser CLI's
   import). Stored owner-only in :data:`IMPORT_DIR` inside the Harness IPC leaf,
   which every sandbox mode already masks and pre-creates.

**When.** The supervisor applies both through the relay's own CDP connection
(``Storage.setCookies`` on the browser target, the default browser context the
daemon's tabs live in) after the browser is up and BEFORE the daemon starts, so no
agent op can run before the cookies land. Every relaunch goes through the same start,
so a crashed or restarted browser is re-filled. An explicit refresh
(:meth:`HarnessSupervisor.refresh_credentials`, ``POST /api/browser/credentials/refresh``)
re-pulls the provider into a live browser, and so does every agent ``navigate`` (or
new tab) whose provider ``fingerprint()`` changed since the last apply: a sign-in
tool the person re-ran is picked up by the next navigation. A cookie a refresh omits
stays until the browser closes.

**Session cookies.** Every cookie is set without ``expires`` unless the provider's
``keep_expiry()`` opts out, so nothing is written into Kiro's profile on disk and
all of it is gone when the browser closes. The import is re-applied from its file
at each start.

**Where never.** The user's own Chrome or Edge: those are the person's profile, and
writing a company session into it is not the gateway's call. A start on either
records ``skipped`` and applies nothing.

**Values never leave.** Nothing here returns, logs or raises a cookie value. The
status (:class:`CredentialStatus`) carries the provider's name, counts, domains and
fixed reason codes, plus the provider's own ``status()`` (``needs_refresh``, an
edition-authored message, earliest expiry, count); a provider exception and a CDP
error are reduced to a code, because their text is not ours to vouch for. A missing
or expired source is the ``needs_refresh`` state, never an error. The supervisor
writes one SEL row per apply that set anything: provider name, counts, domains. The value travels in exactly one
place: the ``Storage.setCookies`` params the relay writes to the browser.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.browser_harness.credential_files import CredentialSourceError, read_source_file
from kiro_crew.platform.interfaces import BrowserCredentialTarget as Target

logger = logging.getLogger(__name__)

#: Where the owner's import lives: a directory inside the Harness IPC leaf
#: (``supervisor.IPC_LEAF``, ``browser-harness-ipc``), which every sandbox mode
#: already masks and pre-creates. No new sandbox entry is needed, and none is
#: added: the import is gateway-only data exactly like the daemon's socket beside it.
IMPORT_DIR = "cookie-import"
IMPORT_FILE = "storage-state.json"

#: Raw import text above this is refused before parsing.
MAX_IMPORT_BYTES = 2 * 1024 * 1024
#: More cookies than this, from one source, is refused (a logged-in profile has a
#: few hundred).
MAX_COOKIES = 5000
#: A browser stores at most 4096 bytes of name plus value; anything longer is refused
#: here rather than silently truncated there.
MAX_COOKIE_BYTES = 4096
#: How long one provider call (``domains()``, ``cookies()``, ``launch_args()`` or
#: ``status()``) may take. It runs
#: in a worker thread; a provider that hangs past this is reported ``provider_timeout``
#: and the browser starts without its cookies.
PROVIDER_TIMEOUT_SECS = 15.0
#: How long ``fingerprint()`` may take. It runs before every agent navigation, so it
#: must be cheap (a ``stat``); past this the navigation goes ahead with what the
#: browser already holds.
FINGERPRINT_TIMEOUT_SECS = 2.0
#: Ceiling on the provider's status message, in characters.
STATUS_MESSAGE_CAP = 200

#: The launch switches a provider may add, by name. Each one is ``--name=value``.
#: Today only Negotiate's server allowlist: it is the one HTTP-auth setting current
#: Chromium still reads from the command line (``chrome_switches.h``
#: ``kAuthServerAllowlist``, mapped onto the ``AuthServerAllowlist`` pref); the
#: delegate allowlist, the GSSAPI library name and the scheme list are policy-only.
#: On Linux Chromium loads the system GSSAPI library (``libgssapi_krb5.so.2``) for
#: Negotiate; the network service restarts unsandboxed the first time a server
#: answers ``WWW-Authenticate: Negotiate``, so the first such request may fail and
#: the next one succeeds. Anything not listed here (a debugging port, a disabled
#: web-security, a proxy) is dropped and counted, never passed.
ALLOWED_LAUNCH_SWITCHES: frozenset[str] = frozenset({"--auth-server-allowlist"})
#: A switch value: host patterns separated by commas (``*.example.com,intranet``).
_SWITCH_VALUE = re.compile(r"[A-Za-z0-9.*,_:\-]{1,2048}")
#: A host name a provider may declare: two or more DNS labels, lowercase.
_DECLARED_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}")
#: Characters a cookie field may not carry (C0 controls and DEL).
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

SOURCE_PROVIDER = "provider"
SOURCE_IMPORTED = "imported"

STATE_NONE = "none"  # no provider and nothing imported
STATE_APPLIED = "applied"
STATE_SKIPPED = "skipped"  # the browser in use is the person's own
STATE_ERROR = "error"
STATE_PENDING = "pending"  # credentials exist; no Kiro Chromium has started yet
STATE_DISABLED = "disabled"  # a provider exists; browser.credentials.enabled is off
STATE_NEEDS_REFRESH = "needs_refresh"  # the provider's source is missing or expired


class CookieImportError(ValueError):
    """An import that cannot be accepted; the message is for the person, never a value."""


class _Relay(Protocol):
    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SourceReport:
    """What one source contributed to the last apply. Counts and codes only."""

    source: str
    offered: int = 0
    applied: int = 0
    dropped: int = 0
    domains: tuple[str, ...] = ()
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "offered": self.offered,
            "applied": self.applied,
            "dropped": self.dropped,
            "domains": list(self.domains),
            "error": self.error,
        }


@dataclass
class CredentialStatus:
    """The ``credentials`` block of ``GET /api/browser/status``. Holds no value.

    ``provider`` is the edition provider's display name (``None`` with the public
    Default), so Settings can say "credentials from <provider>". ``imported`` is
    whether the owner has an import on disk.
    """

    provider: str | None = None
    imported: bool = False
    state: str = STATE_NONE
    reason: str | None = None
    applied_at: float | None = None
    launch_args_dropped: int = 0
    sources: list[SourceReport] = field(default_factory=list)
    enabled: bool = False
    domains: tuple[str, ...] = ()
    settings_from: str = "default"
    earliest_expiry: float | None = None
    provider_status: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "imported": self.imported,
            "enabled": self.enabled,
            "domains": list(self.domains),
            "settings_from": self.settings_from,
            "state": self.state,
            "reason": self.reason,
            "applied_at": self.applied_at,
            "applied": sum(s.applied for s in self.sources),
            "earliest_expiry": self.earliest_expiry,
            "launch_args_dropped": self.launch_args_dropped,
            "provider_status": self.provider_status,
            "sources": [s.as_dict() for s in self.sources],
        }


# ── normalising one cookie to CDP's Network.CookieParam ──

_SAME_SITE = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",
    "unspecified": "Lax",
}
_FLAG_STRINGS = {"true": True, "1": True, "false": False, "0": False}


def _same_site(raw: Any) -> str:
    """Any browser/extension ``sameSite`` spelling to CDP's three; unknown is ``Lax``."""
    if isinstance(raw, str):
        mapped = _SAME_SITE.get(raw.strip().lower())
        if mapped is not None:
            return mapped
    return "Lax"


def _expires(raw: Any) -> float | None:
    """A positive finite unix time, or ``None`` for a session cookie.

    Netscape ``cookies.txt`` writes ``0`` for a session cookie and exports often omit
    the field; NaN and infinities carry no expiry either.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _flag(raw: Any, name: str) -> bool:
    """A real boolean; ``"false"`` must not become ``True`` (``bool("false")`` is)."""
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        mapped = _FLAG_STRINGS.get(raw.strip().lower())
        if mapped is not None:
            return mapped
    raise CookieImportError(f"a cookie has an invalid {name} value; expected true or false")


def cookie_host(domain: str) -> str:
    """The host a cookie's ``domain`` names: lowercase, one leading dot removed."""
    host = domain.strip().lower()
    return host[1:] if host.startswith(".") else host


def normalize_cookie(raw: Any) -> dict[str, Any]:
    """One cookie object to CDP's ``Network.CookieParam`` shape, or :class:`CookieImportError`.

    Accepts Playwright's spelling (``expires``) and the extensions' (``expirationDate``).
    A cookie with no name or no domain cannot be placed and is refused, never guessed.
    The error text names the field, never the value.
    """
    if not isinstance(raw, Mapping):
        raise CookieImportError("each cookie must be a JSON object")
    name, domain = raw.get("name"), raw.get("domain")
    if not isinstance(name, str) or not name:
        raise CookieImportError("a cookie is missing its name")
    if not isinstance(domain, str) or not cookie_host(domain):
        raise CookieImportError("a cookie is missing its domain")
    value = raw.get("value")
    value = value if isinstance(value, str) else ""
    path = raw.get("path")
    path = path if isinstance(path, str) and path else "/"
    for label, text in (("name", name), ("value", value), ("domain", domain), ("path", path)):
        if _CONTROL.search(text):
            raise CookieImportError(f"a cookie's {label} contains a control character")
    if len(name.encode("utf-8", "surrogatepass")) + len(value.encode("utf-8", "surrogatepass")) > (
        MAX_COOKIE_BYTES
    ):
        raise CookieImportError(f"a cookie is longer than {MAX_COOKIE_BYTES} bytes")
    same_site = _same_site(raw.get("sameSite"))
    secure = _flag(raw.get("secure"), "secure")
    cookie: dict[str, Any] = {
        "name": name,
        "value": value,
        "domain": domain.strip().lower(),
        "path": path,
        "secure": secure,
        "httpOnly": _flag(raw.get("httpOnly"), "httpOnly"),
        # Chromium rejects SameSite=None without Secure; Lax is what it would
        # have treated the cookie as anyway.
        "sameSite": "Lax" if same_site == "None" and not secure else same_site,
    }
    expires = _expires(raw["expirationDate"] if "expirationDate" in raw else raw.get("expires"))
    if expires is not None:
        cookie["expires"] = expires
    return cookie


def normalize_declared_domains(domains: Iterable[Any]) -> tuple[str, ...]:
    """The provider's declared domains that are well-formed host names, sorted.

    A single label (``com``, ``localhost``), an IP literal, a wildcard or anything
    with a port is dropped: a declaration that broad would let a provider set
    cookies for hosts it does not serve.
    """
    out: set[str] = set()
    for raw in domains:
        if not isinstance(raw, str):
            continue
        host = cookie_host(raw)
        if _DECLARED_DOMAIN.fullmatch(host) and not host.replace(".", "").isdigit():
            out.add(host)
    return tuple(sorted(out))


def domain_allowed(domain: str, declared: Sequence[str]) -> bool:
    """Is *domain* one of *declared*, or a subdomain of one?"""
    host = cookie_host(domain)
    return any(host == d or host.endswith("." + d) for d in declared)


def _unexpired(cookies: Iterable[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    return [c for c in cookies if c.get("expires") is None or c["expires"] > now]


# ── the owner's import: parse, store, summarise ──


def _reject_json_constant(name: str) -> Any:
    raise CookieImportError(f"cookie import contains the invalid JSON constant {name}")


def _parse_netscape(text: str) -> list[dict[str, Any]]:
    """Netscape ``cookies.txt`` rows (seven tab-separated columns) to raw cookies.

    ``#HttpOnly_`` marks an httpOnly cookie; other ``#`` lines are comments. Columns
    are split from the line with only leading spaces removed, because an empty value
    is a TRAILING tab a full strip would eat.
    """
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        row = line.lstrip(" ")
        http_only = row.startswith("#HttpOnly_")
        if http_only:
            row = row[len("#HttpOnly_") :]
        elif row.startswith("#"):
            continue
        parts = row.split("\t")
        if len(parts) < 7:
            continue
        domain, _sub, path, secure, expires, name, value = parts[:7]
        rows.append(
            {
                "domain": domain,
                "path": path,
                "secure": secure.strip().upper() == "TRUE",
                "expires": expires,
                "name": name,
                "value": value,
                "httpOnly": http_only,
            }
        )
    return rows


def parse_cookie_import(text: str, *, now: float | None = None) -> list[dict[str, Any]]:
    """An import in any accepted shape to CDP cookies; expired ones dropped.

    Accepts a Playwright ``storageState`` object, a bare JSON array of cookie objects
    or a Netscape ``cookies.txt`` body. Raises :class:`CookieImportError` with a
    message for the person on any refusal.
    """
    if not isinstance(text, str):
        raise CookieImportError("cookie import must be text")
    if len(text.encode("utf-8", "surrogatepass")) > MAX_IMPORT_BYTES:
        raise CookieImportError("cookie import is too large (limit 2 MiB)")
    if not text.strip():
        raise CookieImportError("cookie import is empty")
    raw: Any
    if text.lstrip()[:1] in "{[":
        try:
            parsed = json.loads(text, parse_constant=_reject_json_constant)
        except RecursionError as exc:
            raise CookieImportError("cookie import is nested too deeply") from exc
        except CookieImportError:
            raise
        except ValueError as exc:
            raise CookieImportError("cookie import is not valid JSON") from exc
        if isinstance(parsed, dict):
            raw = parsed.get("cookies")
            if not isinstance(raw, list):
                raise CookieImportError('storageState JSON must carry a "cookies" array')
        elif isinstance(parsed, list):
            raw = parsed
        else:
            raise CookieImportError(
                "cookie import JSON must be a storageState object or an array of cookies"
            )
    else:
        raw = _parse_netscape(text)
        if not raw:
            raise CookieImportError(
                "cookie import is neither JSON nor a recognisable cookies.txt file"
            )
    if len(raw) > MAX_COOKIES:
        raise CookieImportError(f"too many cookies (limit {MAX_COOKIES})")
    cookies = _unexpired((normalize_cookie(c) for c in raw), time.time() if now is None else now)
    if not cookies:
        raise CookieImportError("no unexpired cookies to import")
    return cookies


def import_path(home: Path) -> Path:
    from kiro_crew.browser_harness.supervisor import IPC_LEAF

    return Path(home) / IPC_LEAF / IMPORT_DIR / IMPORT_FILE


def save_imported(home: Path, cookies: list[dict[str, Any]]) -> None:
    """Write the import owner-only (0600 / owner-only DACL), atomically."""
    path = import_path(home)
    platform_compat.make_owner_only_dir(path.parent.parent)
    platform_compat.make_owner_only_dir(path.parent)
    payload = json.dumps({"cookies": cookies, "origins": []}) + "\n"
    atomic_write(path, payload, mode=0o600, restrict_to_owner=True)


def load_imported(home: Path, *, now: float | None = None) -> list[dict[str, Any]]:
    """The stored import, re-validated and with expired cookies dropped; ``[]`` if none."""
    try:
        raw = read_source_file(import_path(home), max_bytes=MAX_IMPORT_BYTES)
    except CredentialSourceError as exc:
        if exc.code != "source_missing":
            logger.warning(
                "browser credentials: the stored cookie import is refused (%s)", exc.code
            )
        return []
    try:
        return parse_cookie_import(raw.decode("utf-8", errors="replace"), now=now)
    except CookieImportError:
        logger.warning("browser credentials: the stored cookie import is unreadable; ignored")
        return []


def clear_imported(home: Path) -> list[dict[str, Any]]:
    """Delete the import; return what it held so the live browser can drop it too."""
    held = load_imported(home)
    try:
        import_path(home).unlink()
    except FileNotFoundError:
        pass
    return held


def has_imported(home: Path) -> bool:
    return import_path(home).is_file()


def summarize(cookies: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Count, domains and earliest expiry. NEVER a value."""
    expiries = [float(c["expires"]) for c in cookies if c.get("expires") is not None]
    return {
        "cookie_count": len(cookies),
        "domains": sorted({cookie_host(str(c.get("domain", ""))) for c in cookies}),
        "earliest_expiry": min(expiries) if expiries else None,
    }


# ── the edition provider ──


def provider_name(provider: Any) -> str | None:
    """The provider's display name, or ``None`` (the Default, or a broken provider)."""
    try:
        name = provider.name()
    except Exception:  # noqa: BLE001 - a broken provider reads as no provider
        logger.warning("browser credentials: provider name() raised; treated as absent")
        return None
    # Printable only: it lands in Settings and in the SEL row's text.
    return _printable(name, 80)


@dataclass(frozen=True)
class CredentialSettings:
    """``browser.credentials``, resolved: is the provider used, and for which domains.

    ``settings_from`` says where each value came from: ``config`` (the owner set
    it), ``edition`` (the key is unset and the provider's ``default_settings()``
    supplied it) or ``default`` (neither: off, no domains).
    """

    enabled: bool = False
    domains: tuple[str, ...] = ()
    settings_from: str = "default"


def effective_settings(section: Any, provider: Any) -> CredentialSettings:
    """Resolve ``browser.credentials`` against the provider's edition defaults.

    *section* is the parsed ``browser.credentials`` (a mapping or a dataclass with
    ``enabled`` and ``domains``). A key the owner left unset (``None``) takes the
    provider's ``default_settings()`` value (an empty ``domains`` list counts as
    unset); with neither, the provider is OFF and
    no domain is allowed. Only a provider that exists can supply defaults.
    """
    if dataclasses.is_dataclass(section) and not isinstance(section, type):
        section = dataclasses.asdict(section)
    section = section if isinstance(section, Mapping) else {}
    enabled_raw, domains_raw = section.get("enabled"), section.get("domains")
    defaults: Mapping[str, Any] = {}
    if not domains_raw:
        domains_raw = None  # an empty list is "unset": the edition's default applies
    if provider_name(provider) is not None and (enabled_raw is None or domains_raw is None):
        try:
            got = provider.default_settings()
        except Exception:  # noqa: BLE001 - absent or broken defaults mean "off"
            got = None
        defaults = got if isinstance(got, Mapping) else {}
    origin = "default"
    if isinstance(enabled_raw, bool):
        enabled, origin = enabled_raw, "config"
    elif isinstance(defaults.get("enabled"), bool):
        enabled, origin = bool(defaults["enabled"]), "edition"
    else:
        enabled = False
    if isinstance(domains_raw, (list, tuple)):
        domains = normalize_declared_domains(domains_raw)
        origin = "config" if origin != "edition" or enabled_raw is not None else "edition"
    elif isinstance(defaults.get("domains"), (list, tuple)):
        domains = normalize_declared_domains(defaults["domains"])
        origin = origin if origin == "config" else "edition"
    else:
        domains = ()
    return CredentialSettings(enabled=enabled, domains=domains, settings_from=origin)


def filter_launch_args(raw: Any) -> tuple[list[str], int]:
    """The provider's switches that are on :data:`ALLOWED_LAUNCH_SWITCHES`; and how many were dropped."""
    if not isinstance(raw, (list, tuple)):
        return [], 0
    kept: list[str] = []
    seen: set[str] = set()
    dropped = 0
    for arg in raw:
        if not isinstance(arg, str) or "=" not in arg:
            dropped += 1
            continue
        switch, _, value = arg.partition("=")
        if switch not in ALLOWED_LAUNCH_SWITCHES or switch in seen:
            dropped += 1
            continue
        if not _SWITCH_VALUE.fullmatch(value):
            dropped += 1
            continue
        seen.add(switch)
        kept.append(f"{switch}={value}")
    return kept, dropped


async def provider_launch_args(
    provider: Any, settings: CredentialSettings
) -> tuple[list[str], int]:
    """Ask an ENABLED provider for launch switches (bounded, off the loop); filter them."""
    if provider_name(provider) is None or not settings.enabled:
        return [], 0
    try:
        raw = await asyncio.wait_for(asyncio.to_thread(provider.launch_args), PROVIDER_TIMEOUT_SECS)
    except Exception as exc:  # noqa: BLE001 - reduced to a code; the browser still starts
        logger.warning(
            "browser credentials: provider launch_args() failed (%s)", type(exc).__name__
        )
        return [], 0
    return filter_launch_args(raw)


async def provider_fingerprint(provider: Any) -> str | None:
    """The provider's ``fingerprint()``: a short string that changes when its source does.

    ``None`` when there is no provider, it has no fingerprint, or the call fails or
    passes :data:`FINGERPRINT_TIMEOUT_SECS`: that means "cannot tell", so nothing is
    re-pulled on a navigation.
    """
    if provider_name(provider) is None or not callable(getattr(provider, "fingerprint", None)):
        return None
    try:
        got = await asyncio.wait_for(
            asyncio.to_thread(provider.fingerprint), FINGERPRINT_TIMEOUT_SECS
        )
    except Exception:  # noqa: BLE001 - cannot tell is not a failure
        return None
    return str(got)[:256] if got is not None else None


def _printable(text: Any, cap: int) -> str | None:
    if not isinstance(text, str):
        return None
    clean = "".join(ch for ch in text if ch.isprintable()).strip()
    return clean[:cap] or None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


async def _bounded_provider_status(provider: Any) -> dict[str, Any] | None:
    """:func:`_provider_status` in a worker thread, bounded like every other call.

    It runs inside a browser start, under the supervisor's ensure lock, so a
    ``status()`` that hangs must not keep the browser from coming up: past
    :data:`PROVIDER_TIMEOUT_SECS` it reads as no status.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_provider_status, provider), PROVIDER_TIMEOUT_SECS
        )
    except asyncio.TimeoutError:
        logger.warning("browser credentials: provider status() timed out; none shown")
        return None


def _provider_status(provider: Any) -> dict[str, Any] | None:
    """The provider's own ``status()``, reduced to the four fields Settings may show.

    ``needs_refresh`` (bool), ``message`` (edition-authored, printable, at most
    :data:`STATUS_MESSAGE_CAP` characters), ``earliest_expiry`` and ``count``. Called
    in a worker thread by :func:`apply`; a raise or a non-mapping is ``None``.
    """
    method = getattr(provider, "status", None)
    if not callable(method):
        return None
    try:
        raw = method()
    except Exception:  # noqa: BLE001 - a broken status is no status
        return None
    if not isinstance(raw, Mapping):
        return None
    count = raw.get("count")
    return {
        "needs_refresh": raw.get("needs_refresh") is True,
        "message": _printable(raw.get("message"), STATUS_MESSAGE_CAP),
        "earliest_expiry": _number(raw.get("earliest_expiry")),
        "count": count if isinstance(count, int) and not isinstance(count, bool) else None,
    }


def _keeps_expiry(provider: Any) -> bool:
    method = getattr(provider, "keep_expiry", None)
    try:
        return callable(method) and method() is True
    except Exception:  # noqa: BLE001 - the safe default strips
        return False


def _earliest(cookies: Iterable[Mapping[str, Any]]) -> float | None:
    expiries = [float(c["expires"]) for c in cookies if c.get("expires") is not None]
    return min(expiries) if expiries else None


def _as_session(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop ``expires``: a session cookie lives in memory and dies with the browser."""
    return [{k: v for k, v in c.items() if k != "expires"} for c in cookies]


async def _provider_cookies(
    provider: Any, target: Any, settings: CredentialSettings
) -> tuple[list[dict[str, Any]], SourceReport]:
    name = provider_name(provider)
    if name is None:
        return [], SourceReport(SOURCE_PROVIDER)
    try:
        declared = normalize_declared_domains(
            await asyncio.wait_for(asyncio.to_thread(provider.domains), PROVIDER_TIMEOUT_SECS)
        )
        raw = await asyncio.wait_for(
            asyncio.to_thread(provider.cookies, target), PROVIDER_TIMEOUT_SECS
        )
    except asyncio.TimeoutError:
        return [], SourceReport(SOURCE_PROVIDER, error="provider_timeout")
    except CredentialSourceError as exc:
        # A missing or refused source file is a state the person can fix.
        return [], SourceReport(SOURCE_PROVIDER, error=exc.code)
    except Exception as exc:  # noqa: BLE001 - its text could carry a value; reduced to a code
        logger.warning("browser credentials: provider %r failed (%s)", name, type(exc).__name__)
        return [], SourceReport(SOURCE_PROVIDER, error="provider_failed")
    if not isinstance(raw, (list, tuple)):
        return [], SourceReport(SOURCE_PROVIDER, error="provider_bad_shape")
    raw = list(raw)[: MAX_COOKIES + 1]
    if len(raw) > MAX_COOKIES:
        return [], SourceReport(SOURCE_PROVIDER, offered=len(raw), error="too_many_cookies")
    kept: list[dict[str, Any]] = []
    dropped = 0
    now = time.time()
    for item in raw:
        try:
            cookie = normalize_cookie(item)
        except CookieImportError:
            dropped += 1
            continue
        # BOTH lists: what the edition says it serves AND what the owner allowed.
        if not (
            domain_allowed(cookie["domain"], declared)
            and domain_allowed(cookie["domain"], settings.domains)
        ):
            dropped += 1
            continue
        kept.append(cookie)
    fresh = _unexpired(kept, now)
    dropped += len(kept) - len(fresh)
    error = "source_expired" if raw and not fresh and len(kept) > 0 else None
    return fresh, SourceReport(
        SOURCE_PROVIDER,
        offered=len(raw),
        dropped=dropped,
        domains=tuple(sorted({cookie_host(c["domain"]) for c in fresh})),
        error=error,
    )


async def _set_cookies(relay: _Relay, cookies: list[dict[str, Any]]) -> tuple[int, int]:
    """``Storage.setCookies``; on a refused batch, one at a time. Returns (applied, failed)."""
    if not cookies:
        return 0, 0
    try:
        await relay.send("Storage.setCookies", {"cookies": cookies})
        return len(cookies), 0
    except Exception:  # noqa: BLE001 - one bad cookie refuses the batch; find it
        pass
    applied = failed = 0
    for cookie in cookies:
        try:
            await relay.send("Storage.setCookies", {"cookies": [cookie]})
            applied += 1
        except Exception:  # noqa: BLE001 - counted; the CDP text is not repeated
            failed += 1
    return applied, failed


#: Reason codes that mean "the person has to sign in again", not "something broke".
_NEEDS_REFRESH_CODES = frozenset({"source_missing", "source_expired"})


async def apply(
    relay: _Relay,
    *,
    provider: Any,
    home: Path,
    target: Target,
    settings: CredentialSettings | None = None,
    clock: Callable[[], float] = time.time,
) -> CredentialStatus:
    """Collect both sources and write them into the browser behind *relay*.

    The provider is used only when *settings* enable it, and only for cookies on
    a domain both it and the owner allow. Its cookies are set as SESSION cookies
    (``expires`` stripped) unless its ``keep_expiry()`` says otherwise, so nothing
    it supplies is written into Kiro's profile on disk; the owner's import is set
    the same way and re-applied from its file at every start.

    Never raises for a provider or a CDP failure: the browser is still useful without
    the cookies, so the outcome is a status, not an exception. Cancellation propagates.
    """
    settings = settings or CredentialSettings()
    name = provider_name(provider)
    status = CredentialStatus(
        provider=name,
        imported=await asyncio.to_thread(has_imported, home),
        enabled=settings.enabled,
        domains=settings.domains,
        settings_from=settings.settings_from,
    )
    if target.kind != "kiro_chromium":
        status.state, status.reason = STATE_SKIPPED, "user_browser"
        return status
    reports: list[SourceReport] = []
    earliest: list[float] = []
    if name is not None and settings.enabled:
        provider_cookies, report = await _provider_cookies(provider, target, settings)
        found = _earliest(provider_cookies)
        if found is not None:
            earliest.append(found)
        if not _keeps_expiry(provider):
            provider_cookies = _as_session(provider_cookies)
        applied, failed = await _set_cookies(relay, provider_cookies)
        reports.append(
            dataclasses.replace(report, applied=applied, dropped=report.dropped + failed)
        )
        status.provider_status = await _bounded_provider_status(provider)
    imported = await asyncio.to_thread(load_imported, home)
    if imported:
        found = _earliest(imported)
        if found is not None:
            earliest.append(found)
        domains = tuple(sorted({cookie_host(c["domain"]) for c in imported}))
        applied, failed = await _set_cookies(relay, _as_session(imported))
        reports.append(
            SourceReport(
                SOURCE_IMPORTED,
                offered=len(imported),
                applied=applied,
                dropped=failed,
                domains=domains,
            )
        )
    status.sources = reports
    status.earliest_expiry = min(earliest) if earliest else None
    if not reports:
        status.state = STATE_DISABLED if name is not None else STATE_NONE
        status.reason = "disabled" if name is not None else None
        return status
    errors = [r.error for r in reports if r.error]
    needs_refresh = bool(status.provider_status and status.provider_status["needs_refresh"])
    if needs_refresh or (errors and errors[0] in _NEEDS_REFRESH_CODES):
        status.state = STATE_NEEDS_REFRESH
    elif errors and not any(r.applied for r in reports):
        status.state = STATE_ERROR
    else:
        status.state = STATE_APPLIED
    status.reason = errors[0] if errors else None
    status.applied_at = clock()
    logger.info(
        "browser credentials: %s (%s)",
        status.state,
        ", ".join(f"{r.source} {r.applied}/{r.offered}" for r in reports),
    )
    return status


async def expire(relay: _Relay, cookies: Sequence[Mapping[str, Any]]) -> int:
    """Remove *cookies* from the browser: the same name/domain/path, already expired."""
    gone = [
        {
            "name": c["name"],
            "value": "",
            "domain": c["domain"],
            "path": c.get("path", "/"),
            "expires": 1,
        }
        for c in cookies
    ]
    applied, _failed = await _set_cookies(relay, gone)
    return applied


__all__ = [
    "ALLOWED_LAUNCH_SWITCHES",
    "CookieImportError",
    "CredentialStatus",
    "CredentialSettings",
    "IMPORT_DIR",
    "MAX_COOKIES",
    "SourceReport",
    "Target",
    "apply",
    "clear_imported",
    "domain_allowed",
    "effective_settings",
    "expire",
    "filter_launch_args",
    "has_imported",
    "load_imported",
    "normalize_cookie",
    "normalize_declared_domains",
    "parse_cookie_import",
    "provider_fingerprint",
    "provider_launch_args",
    "provider_name",
    "save_imported",
    "summarize",
]
