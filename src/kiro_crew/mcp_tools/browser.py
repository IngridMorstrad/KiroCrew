"""The ``browser`` tool: run one operation in the agent's browser.

``schemas()`` is the ADVERTISEMENT half (name, model-facing description, the
JSON Schema a call is validated against) and ``HANDLERS`` maps the name to the
function that runs it. Both halves live here so the contract and the behavior
read together, and ``test_mcp_tool_registry`` fails if one arrives without the
other.

The browser itself is the Browser Harness the gateway supervises
(``browser_harness/``), reached over the gateway's own CDP relay. This tool is
its agent-facing SUBMIT side only: ``kirocrew-core`` is a separate stdio shim, so
the handler cannot reach the supervisor in-process. It POSTs one op to the
gateway route ``/api/browser/op`` over loopback with the internal-secret
handshake, the same shape ``mcp_computer`` uses, and the gateway runs it through
``browser_harness.ops``. The tool holds NO state: the ref map lives in the page,
the tab ledger in the relay, the input lock in the gateway.

Two gates run HERE, before anything is posted, and they fail in opposite
directions on purpose:

* **Governance** (``capabilities.browse``): a denial refuses OUTRIGHT. There is
  no other browser to fall back to, and there must not be one, or the control
  would not control anything.
* **The navigate gate**: only a public http(s) URL, or a loopback ``host:port``
  the OPERATOR listed in ``dashboard.browser_local_origins``, is ever opened.
  Everything else (loopback, private, link-local, the cloud metadata address, a
  parser-differential spelling) is refused, because those are where the user's
  own control planes live, this dashboard included. The same gate covers the
  URL a new tab opens with.

Availability is the gateway's to report, not this process's to probe: a host
with no Harness answers ``not_installed`` and the tool says so. The tool is
always advertised because kiro-cli caches ``tools/list`` for a session's life,
so an install made mid-session is picked up on the next call.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.browser_harness import budgets, url_gate
from kiro_crew.browser_harness.ops import OPS, SLOW_OPS
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.mcp_shared import DeferredTool
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

# The gateway route the handler submits to. Loopback + ``X-Internal-Secret``; the
# handler answers HTTP 200 ``{ok: true, result}`` / 200 ``{ok: false, code, error}``.
COMMAND_PATH = "/api/browser/op"
HANDBACK_PATH = "/api/browser/handback"
KEEPALIVE_PATH = "/api/session-keepalive"
#: Seconds between two session-keepalive pings while parked: the ACP tool-stall
#: watchdog (600 s of silence) and the gateway's staleness check both reset on it.
HOLD_KEEPALIVE_SECS = 30.0
# Derived from the gateway's own waits (``browser_harness.budgets``), with a
# margin, so this shim never gives up on an op the gateway is still running: a
# "did not answer" here while the click lands there is a click the model retries.
SLOW_COMMAND_TIMEOUT_SECS = budgets.SHIM_SLOW_TIMEOUT_S
FAST_COMMAND_TIMEOUT_SECS = budgets.SHIM_FAST_TIMEOUT_S
COMMAND_TIMEOUT_SECS = SLOW_COMMAND_TIMEOUT_SECS

# Re-exported for the registry tests and the prompt gate: the ops the gateway serves.
BROWSER_OPS: tuple[str, ...] = OPS

_RESULT_CAP = 20_000

_NOT_INSTALLED_TEXT = (
    "Error: browsing is not set up on this host. Ask the user to install it from "
    "Settings -> Browser (one click), then call the tool again."
)

_URL_GATE_TEXT = (
    "Error: the browser tool only opens public http(s) URLs, plus loopback origins "
    "the user listed under Settings -> Browser -> Local preview origins "
    "(dashboard.browser_local_origins, exact host:port such as localhost:5173). "
    "A localhost, loopback, private or link-local address is refused otherwise. Do "
    "not edit the setting yourself; if the user wants it opened, ask them to add "
    "the origin there (or run `kirocrew config set dashboard.browser_local_origins "
    "'[\"localhost:5173\"]'`, which replaces the whole list), or to open it in "
    "their own browser."
)


def _navigate_target_is_public(url: str) -> bool:
    """See :func:`kiro_crew.browser_harness.url_gate.target_is_public` (the one rule, shared
    with the gateway, which applies it again to the URL a navigation COMMITTED on)."""
    return url_gate.target_is_public(url)


def _allowed_local_origins() -> frozenset[str]:
    """The operator's ``dashboard.browser_local_origins``, read fresh each call.

    Already canonical ``host:port`` entries (``config.sections`` drops every
    other shape). Fail-CLOSED to empty: a config read error must not widen the
    navigate gate.
    """
    try:
        raw = getattr(KiroCrewConfig.load().dashboard, "browser_local_origins", [])
    except Exception:
        return frozenset()
    return frozenset(e for e in raw if isinstance(e, str)) if isinstance(raw, list) else frozenset()


def _navigate_target_is_allowed_local(url: str, allowed: frozenset[str]) -> bool:
    """See :func:`kiro_crew.browser_harness.url_gate.target_is_allowed_local`; the gateway's
    own port is read here, in the shim, and an unknowable port refuses every local target."""
    try:
        port: int | None = mcp_core._api_port()
    except Exception:
        port = None
    return url_gate.target_is_allowed_local(url, allowed, port)


def _url_allowed(url: Any) -> bool:
    """The navigate gate: public, or an operator-listed loopback origin."""
    if not isinstance(url, str):
        return False
    return _navigate_target_is_public(url) or _navigate_target_is_allowed_local(
        url, _allowed_local_origins()
    )


def _gated_url(op: str, op_args: dict[str, Any]) -> Any:
    """The URL an op would open, or None when it opens none.

    ``navigate`` always opens one; ``tabs`` opens one only on ``action: new``
    with a URL other than ``about:blank``.
    """
    if op == "navigate":
        return op_args.get("url", "")
    if op == "tabs" and op_args.get("action") == "new":
        url = op_args.get("url", "about:blank")
        return None if url == "about:blank" else url
    return None


def _args_are_scalar(op_args: dict[str, Any]) -> bool:
    """Reject non-scalar op arguments (e.g. an object-valued ``type.text``).

    Every browser op takes string/number/bool values (a url, a ref, text, a
    key) or a list of those; none takes a nested object. An object would reach
    the page and render as ``[object Object]``, so refuse it here rather than
    inserting garbage into the page.
    """
    for value in op_args.values():
        if value is None or isinstance(value, (str, int, float, bool)):
            continue
        if isinstance(value, list) and all(
            item is None or isinstance(item, (str, int, float, bool)) for item in value
        ):
            continue
        return False
    return True


def schemas() -> list[dict[str, Any]]:
    """Descriptor for the browser tool. Always advertised (see the module docstring)."""
    return [
        {
            "name": "browser",
            "description": (
                "Drive the agent's browser: run ONE operation on the page and the "
                "user watches it live in the chat. `op` is the action and `args` its "
                "parameters. navigate {url}; snapshot (the page as an outline with "
                "element refs; call it before click/type and again after the page "
                "changes); click {ref | x,y, button?, clicks?: 1|2}; type {ref?, text, "
                "submit?} (a password field is refused: the user signs in by taking "
                "over); press_key {key, modifiers?}; hover {ref | x,y}; drag {ref|x,y "
                "-> to_ref|to_x,to_y}; scroll {ref|x,y?, dx?, dy?}; select_option "
                "{ref, values}; wait_for {text | textGone | time (s) | load}; "
                "back; console {limit?}; screenshot (writes a PNG, returns its "
                "absolute path: show it with ![..](path)); tabs {action: list|switch"
                "|new, target_id?, url?} over the tabs you opened. Only public "
                "http(s) URLs open, plus loopback origins the user listed in "
                "Settings -> Browser. If the user has taken over the browser, an op "
                "waits until they hand it back (up to 10 minutes) and then runs; you "
                "do not need to poll or ask them to continue."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": list(BROWSER_OPS),
                        "description": "The browser operation to perform.",
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "Operation parameters (e.g. url, ref, text, key). "
                            "Optional; defaults to an empty object."
                        ),
                    },
                },
                "required": ["op"],
            },
        }
    ]


def _post_command(
    session_key: str, op: str, args: dict[str, Any], session_header: str, timeout_s: float
) -> tuple[int | None, dict]:
    """POST one op to the gateway, surfacing the HTTP status.

    Returns ``(status, body)`` -- ``status`` is ``None`` for a transport failure
    (no gateway listening, a timeout). ``session_key`` goes in the BODY, where
    the gateway keys the live card and the input lock by it. ``session_header``
    is the ``dashboard:``-namespaced key for the ``X-Session-Key`` header, sent
    together with the per-session token (``_session_token_header``) only when
    the caller resolved one: the header and token are a pair the peer check
    compares, and a fabricated header would be refused where a missing one is
    simply absent.
    """
    body = json.dumps({"session_key": session_key, "op": op, "args": args}).encode()
    req = urllib.request.Request(
        f"{mcp_core._api_base()}{COMMAND_PATH}",
        data=body,
        headers=_headers(session_header),
        method="POST",
    )
    return _send_json(req, timeout_s)


def _post_handback(
    session_key: str, session_header: str, timeout_s: float
) -> tuple[int | None, dict]:
    """POST the parked tool's hand-back poll; same auth and shape as :func:`_post_command`."""
    body = json.dumps({"session_key": session_key}).encode()
    req = urllib.request.Request(
        f"{mcp_core._api_base()}{HANDBACK_PATH}",
        data=body,
        headers=_headers(session_header),
        method="POST",
    )
    return _send_json(req, timeout_s)


def _headers(session_header: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Secret": mcp_core._internal_secret(),
    }
    if session_header:
        headers["X-Session-Key"] = session_header
        headers.update(mcp_core._session_token_header())
    return headers


def _send_json(req: urllib.request.Request, timeout_s: float) -> tuple[int | None, dict]:
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- URL is the loopback gateway (_api_base()) + a fixed internal path; never agent-controlled  # noqa: E501
        with mcp_core._api_urlopen(req, timeout=timeout_s) as resp:
            return int(resp.status), json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return int(exc.code), json.loads(exc.read())
        except Exception:
            return int(exc.code), {}
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        return None, {}
    except Exception:
        return None, {}


def _redacted(text: str) -> str:
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _result_text(op: str, result: Any) -> str:
    """Concise success text, with untrusted page content redacted."""
    if not isinstance(result, dict):
        return f"Browser {op}: ok"
    if op == "screenshot":
        path = str(result.get("path", ""))
        return f"Browser screenshot saved: {path}\nShow it with ![screenshot]({path})"
    if op == "snapshot":
        head = f"Browser snapshot of {result.get('url', '')} -- {result.get('title', '')}".rstrip(
            " -"
        )
        outline = str(result.get("snapshot", ""))
        partial = f"\n(partial: {result['partial']})" if result.get("partial") else ""
        return _redacted(f"{head}{partial}\n{outline}")[:_RESULT_CAP]
    fields = {k: v for k, v in result.items() if k != "ok" and v is not None}
    if not fields:
        return f"Browser {op}: ok"
    return _redacted(f"Browser {op}: {json.dumps(fields, default=str)}")[:_RESULT_CAP]


def _error_text(code: str, detail: str) -> str:
    if code == "not_installed":
        return _NOT_INSTALLED_TEXT
    if code == "user_in_control":
        minutes = int(budgets.HOLD_TIMEOUT_S // 60)
        return (
            f"Error: user_in_control -- the user has held the browser for {minutes} minutes "
            "without handing it back. Do not retry in a loop; tell them you are waiting "
            "and end your turn. Continue when they say they are done."
        )
    detail = _redacted(detail or "the browser op failed")[:2000]
    return f"Error ({code}): {detail}" if code else f"Error: {detail}"


def _settle(op: str, status: int | None, payload: Any) -> str | None:
    """The tool's text for one gateway answer, or ``None`` when the answer is "hold".

    A ``hold`` answer (``user_in_control`` or ``interrupted`` with ``"hold": true``)
    means the person has the browser: the caller parks and polls for the hand-back
    instead of returning.
    """
    if status == 200 and isinstance(payload, dict) and payload.get("ok") is True:
        return _result_text(op, payload.get("result"))
    if status == 200 and isinstance(payload, dict) and payload.get("ok") is False:
        if payload.get("hold") is True:
            return None
        return _error_text(str(payload.get("code") or ""), str(payload.get("error") or ""))
    if status is None:
        return "Error: the gateway did not answer the browser op (is Kiro Crew running?)"
    code = payload.get("code") if isinstance(payload, dict) else None
    err = payload.get("error") if isinstance(payload, dict) else None
    return f"Error: browser op failed (HTTP {status}{f', {code}' if code else ''}): {err or 'no detail'}"


class _HandbackWait(DeferredTool):
    """The parked half of a ``browser`` op while the person is in control.

    The shim runs ONE worker thread for every session, and the ACP tool-stall
    watchdog ends a tool call that is silent for 600 s, so the ten-minute hold is
    not one blocking call: this object is handed to the dispatch loop, which runs
    :meth:`step` from its own timer every ``HANDBACK_POLL_S``. A step is one short
    poll of ``/api/browser/handback`` (the gateway itself waits at most
    ``HANDBACK_WAIT_S`` on the release signal) and, every ``HOLD_KEEPALIVE_SECS``, a
    session-keepalive ping. On the hand-back the op is posted again and runs; an op
    the takeover cut short (``interrupted``) is not re-posted, the agent is told to
    look first. Past ``HOLD_TIMEOUT_S`` the agent is told the person is still in
    control and to stop. A cancelled call simply stops stepping.
    """

    def __init__(
        self,
        *,
        op: str,
        op_args: dict[str, Any],
        body_key: str,
        session_header: str,
        timeout_s: float,
        interrupted: bool,
        interrupted_text: str,
    ) -> None:
        self.op = op
        self.op_args = op_args
        self.body_key = body_key
        self.session_header = session_header
        self.timeout_s = timeout_s
        self.interrupted = interrupted
        self.interrupted_text = interrupted_text
        now = mcp_core.time.monotonic()
        self.deadline = now + budgets.HOLD_TIMEOUT_S
        self.next_poll = now
        self.next_keepalive = now + HOLD_KEEPALIVE_SECS

    def due_at(self) -> float:
        return self.next_poll

    def step(self) -> str | None:
        now = mcp_core.time.monotonic()
        if now >= self.deadline:
            return _error_text("user_in_control", "")
        if now >= self.next_keepalive:
            self.next_keepalive = now + HOLD_KEEPALIVE_SECS
            try:
                mcp_core._post(KEEPALIVE_PATH, {}, timeout=5.0, session_key=self.session_header)
            except Exception:  # noqa: BLE001 - keepalive is best-effort
                pass
        self.next_poll = now + budgets.HANDBACK_POLL_S
        status, payload = _post_handback(
            self.body_key, self.session_header, budgets.HANDBACK_CALL_TIMEOUT_S
        )
        released = status == 200 and isinstance(payload, dict) and payload.get("released") is True
        if not released:
            return None
        if self.interrupted:
            return self.interrupted_text
        status, payload = _post_command(
            self.body_key, self.op, self.op_args, self.session_header, self.timeout_s
        )
        text = _settle(self.op, status, payload)
        if text is not None:
            return text
        # Taken over again before the op could run: keep waiting on the same deadline.
        self.interrupted = isinstance(payload, dict) and payload.get("code") == "interrupted"
        if self.interrupted:
            self.interrupted_text = _error_text(
                "interrupted", str(payload.get("error") or "")  # type: ignore[union-attr]
            )
        return None


def browser(name: str, args: dict[str, Any]) -> "str | DeferredTool":
    # Governance gate (capabilities.browse). A policy that denies web browsing
    # stops it here, outright: there is no other browser to fall back to.
    session_key = mcp_core._resolve_session_key()
    gov_denied = mcp_core._vet_browse_governance(session_key)
    if gov_denied:
        return f"Error: {gov_denied}"
    op = args.get("op")
    if not isinstance(op, str) or op not in BROWSER_OPS:
        return f"Error: op must be one of: {', '.join(BROWSER_OPS)}"
    op_args = args.get("args") or {}
    if not isinstance(op_args, dict):
        return "Error: args must be a JSON object"
    if not _args_are_scalar(op_args):
        return "Error: args values must be strings, numbers, or booleans (or lists of those)"

    # The navigate gate, applied to every URL an op would open (see _gated_url).
    gated = _gated_url(op, op_args)
    if gated is not None and not _url_allowed(gated):
        return _URL_GATE_TEXT

    # The caller's session key, resolved LENIENTLY like every other X-Session-Key
    # tool (artifacts, sessions, skills): on a default install the strict
    # resolver returns "" for the user's own main session too. The key names the
    # chat slot whose live card shows this op and whose input lock can refuse
    # it. A session with no key (a CLI run with no dashboard) still browses, with
    # no card to show it on; it gets a process-local key and no header.
    session_header = ""
    if session_key:
        header_err = mcp_core._session_key_header_error(session_key)
        if header_err:
            return f"Error: {header_err}"
        session_header = session_key
    body_key = session_key or f"anonymous:{os.getpid()}"

    timeout_s = SLOW_COMMAND_TIMEOUT_SECS if op in SLOW_OPS else FAST_COMMAND_TIMEOUT_SECS
    logger.debug("browser/tool: op=%s session=%s -> POST %s", op, body_key, COMMAND_PATH)
    status, payload = _post_command(body_key, op, op_args, session_header, timeout_s)
    text = _settle(op, status, payload)
    if text is not None:
        return text
    # The person holds the browser: park, poll for the hand-back, then run the op.
    interrupted = isinstance(payload, dict) and payload.get("code") == "interrupted"
    return _HandbackWait(
        op=op,
        op_args=op_args,
        body_key=body_key,
        session_header=session_header,
        timeout_s=timeout_s,
        interrupted=interrupted,
        interrupted_text=(
            _error_text("interrupted", str(payload.get("error") or "")) if interrupted else ""
        ),
    )


HANDLERS: dict[str, Callable[[str, dict[str, Any]], "str | DeferredTool"]] = {
    "browser": browser,
}
