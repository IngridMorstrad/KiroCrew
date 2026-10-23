"""Takeover: the person's input lock on a live session, and the card's events as CDP input.

:class:`InputLock` is one lock per session. The dashboard owner claims it from the
card (``POST /api/browser/input/claim``); while it is held every agent ``browser``
op answers ``user_in_control`` at once instead of waiting, and the lock refuses a
second claimant. Esc or "Hand back" releases it.

:func:`dispatch_input` maps one card event, in frame pixels, to
``Input.dispatchMouseEvent`` / ``Input.dispatchKeyEvent`` on the relay's own
session for the target, dividing positions by the stream's frame-to-CSS scale on
the way (CDP takes viewport CSS pixels; the JPEG is smaller at every rung but the
top). Every field is bounded here, at the boundary, and nothing in this module
logs an event: keystrokes never enter a log or the transcript.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kiro_crew.browser_harness.screencast import Relay

#: The owner the card claims as. The only claimant the routes ever pass.
USER = "user"
#: What an agent op would claim as; :meth:`InputLock.claim` refuses it while held.
AGENT = "agent"

#: Error code an agent op returns while the lock is held.
USER_IN_CONTROL = "user_in_control"

#: Largest frame coordinate accepted; a 16k-wide frame is far past any ladder rung.
MAX_COORD = 16_384
#: Largest wheel delta per event.
MAX_DELTA = 16_384
MAX_CLICKS = 3
MAX_KEY_CHARS = 32
MAX_TEXT_CHARS = 64
#: Alt | Ctrl | Meta | Shift, the four CDP modifier bits.
MAX_MODIFIERS = 15

_BUTTONS = frozenset({"none", "left", "middle", "right", "back", "forward"})
_MOUSE_ACTIONS = frozenset({"click", "move", "down", "up"})
_KEY_ACTIONS = frozenset({"press", "down", "up"})


class InputEventError(ValueError):
    """A card event the mapping refuses; ``code`` is the route's error code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


class InputLock:
    """One session's input lock: held by at most one owner at a time."""

    def __init__(self) -> None:
        self._owner: str | None = None

    @property
    def owner(self) -> str | None:
        return self._owner

    def held(self) -> bool:
        return self._owner is not None

    def claim(self, owner: str) -> bool:
        """Take the lock for ``owner``. ``False`` when another owner holds it."""
        if not owner:
            return False
        if self._owner is None or self._owner == owner:
            self._owner = owner
            return True
        return False

    def release(self) -> None:
        self._owner = None


@dataclass(frozen=True)
class InputEvent:
    """A validated card event, ready to map onto CDP."""

    type: str
    action: str
    x: int = 0
    y: int = 0
    button: str = "none"
    clicks: int = 1
    dx: int = 0
    dy: int = 0
    key: str = ""
    code: str = ""
    text: str = ""
    modifiers: int = 0


def _int_field(event: dict[str, Any], name: str, *, lo: int, hi: int, default: int = 0) -> int:
    value = event.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputEventError("invalid_input_event", f"{name} must be a number")
    value = int(value)
    if value < lo or value > hi:
        raise InputEventError("invalid_input_event", f"{name} out of range")
    return value


def _str_field(event: dict[str, Any], name: str, *, max_len: int) -> str:
    value = event.get(name, "")
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > max_len:
        raise InputEventError("invalid_input_event", f"{name} must be a short string")
    return value


def parse_input_event(raw: Any) -> InputEvent:
    """Bound every field of a card event; refuse anything outside the contract."""
    if not isinstance(raw, dict):
        raise InputEventError("invalid_input_event", "event must be an object")
    kind = raw.get("type")
    if kind not in ("mouse", "wheel", "key"):
        raise InputEventError("invalid_input_event", "type must be mouse, wheel or key")
    modifiers = _int_field(raw, "modifiers", lo=0, hi=MAX_MODIFIERS)
    if kind == "key":
        action = raw.get("action", "press")
        if action not in _KEY_ACTIONS:
            raise InputEventError("invalid_input_event", "key action must be press, down or up")
        key = _str_field(raw, "key", max_len=MAX_KEY_CHARS)
        code = _str_field(raw, "code", max_len=MAX_KEY_CHARS)
        text = _str_field(raw, "text", max_len=MAX_TEXT_CHARS)
        if not key and not text:
            raise InputEventError("invalid_input_event", "key event needs key or text")
        return InputEvent(
            type="key", action=str(action), key=key, code=code, text=text, modifiers=modifiers
        )
    x = _int_field(raw, "x", lo=0, hi=MAX_COORD)
    y = _int_field(raw, "y", lo=0, hi=MAX_COORD)
    if kind == "wheel":
        return InputEvent(
            type="wheel",
            action="wheel",
            x=x,
            y=y,
            dx=_int_field(raw, "dx", lo=-MAX_DELTA, hi=MAX_DELTA),
            dy=_int_field(raw, "dy", lo=-MAX_DELTA, hi=MAX_DELTA),
            modifiers=modifiers,
        )
    button = raw.get("button", "none") or "none"
    if button not in _BUTTONS:
        raise InputEventError("invalid_input_event", "unknown mouse button")
    action = raw.get("action")
    if action is None:
        action = "move" if button == "none" else "click"
    if action not in _MOUSE_ACTIONS:
        raise InputEventError("invalid_input_event", "mouse action must be click, move, down or up")
    if action != "move" and button == "none":
        raise InputEventError("invalid_input_event", "a mouse press needs a button")
    # A move carries no click count (the card sends 0); a press or release carries 1-3.
    if action == "move":
        clicks = _int_field(raw, "clicks", lo=0, hi=MAX_CLICKS, default=0)
    else:
        clicks = _int_field(raw, "clicks", lo=1, hi=MAX_CLICKS, default=1)
    return InputEvent(
        type="mouse",
        action=str(action),
        x=x,
        y=y,
        button=str(button),
        clicks=clicks,
        modifiers=modifiers,
    )


#: CDP ``buttons`` bitmask for the button a move reports as held.
_BUTTONS_MASK = {"left": 1, "right": 2, "middle": 4, "back": 8, "forward": 16}


def _css(value: int, scale: float) -> float:
    """A frame-pixel coordinate as viewport CSS pixels, to a hundredth."""
    return round(value / scale, 2) if scale > 0 else float(value)


def cdp_calls(event: InputEvent, scale: float = 1.0) -> list[tuple[str, dict[str, Any]]]:
    """The CDP commands one validated event becomes, in order. Pure.

    *scale* is the stream's frame-pixels-per-CSS-pixel: the card measures in
    JPEG pixels, ``Input.dispatch*`` takes viewport CSS pixels, and a screencast
    at ``maxWidth`` below the viewport makes the two differ. Positions and wheel
    deltas are divided by it here, at the one place the event becomes CDP.
    """
    if event.type == "wheel":
        return [
            (
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": _css(event.x, scale),
                    "y": _css(event.y, scale),
                    "deltaX": _css(event.dx, scale),
                    "deltaY": _css(event.dy, scale),
                    "modifiers": event.modifiers,
                },
            )
        ]
    if event.type == "mouse":
        base = {"x": _css(event.x, scale), "y": _css(event.y, scale), "modifiers": event.modifiers}
        if event.action == "move":
            moved: dict[str, Any] = {"type": "mouseMoved", **base}
            if event.button != "none":
                # A move with a button held is a drag; Chrome needs both fields.
                moved["button"] = event.button
                moved["buttons"] = _BUTTONS_MASK.get(event.button, 0)
            return [("Input.dispatchMouseEvent", moved)]
        pressed = {"button": event.button, "clickCount": event.clicks, **base}
        if event.action == "down":
            return [("Input.dispatchMouseEvent", {"type": "mousePressed", **pressed})]
        if event.action == "up":
            return [("Input.dispatchMouseEvent", {"type": "mouseReleased", **pressed})]
        calls: list[tuple[str, dict[str, Any]]] = []
        for n in range(1, event.clicks + 1):
            p = {**pressed, "clickCount": n}
            calls.append(("Input.dispatchMouseEvent", {"type": "mousePressed", **p}))
            calls.append(("Input.dispatchMouseEvent", {"type": "mouseReleased", **p}))
        return calls
    base_key: dict[str, Any] = {"modifiers": event.modifiers}
    if event.key:
        base_key["key"] = event.key
    if event.code:
        base_key["code"] = event.code
    down: dict[str, Any] = {**base_key}
    if event.text:
        down["type"] = "keyDown"
        down["text"] = event.text
        down["unmodifiedText"] = event.text
    else:
        down["type"] = "rawKeyDown"
    up = {"type": "keyUp", **base_key}
    if event.action == "down":
        return [("Input.dispatchKeyEvent", down)]
    if event.action == "up":
        return [("Input.dispatchKeyEvent", up)]
    return [("Input.dispatchKeyEvent", down), ("Input.dispatchKeyEvent", up)]


async def dispatch_input(
    relay: Relay, session_id: str, event: dict[str, Any], *, scale: float = 1.0
) -> int:
    """Validate one card event and send it to the page. Returns the CDP calls made.

    *scale* is the stream's frame-to-CSS ratio (see :func:`cdp_calls`). Raises
    :class:`InputEventError` for a malformed event before anything is sent.
    """
    parsed = parse_input_event(event)
    calls = cdp_calls(parsed, scale)
    for method, params in calls:
        await relay.send(method, params, session_id)
    return len(calls)


__all__ = [
    "AGENT",
    "InputEvent",
    "InputEventError",
    "InputLock",
    "USER",
    "USER_IN_CONTROL",
    "cdp_calls",
    "dispatch_input",
    "parse_input_event",
]
