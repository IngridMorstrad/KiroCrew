"""The live hub: per-session state behind the chat's browser card.

One :class:`LiveHub` per gateway holds a :class:`LiveSession` per session key: the
turn it belongs to, the step strip, the card's state, the stream and the input
lock. The ``browser`` tool reports through :meth:`LiveHub.turn_started`,
:meth:`LiveHub.step` and :meth:`LiveHub.turn_ended`, asks
:meth:`LiveHub.in_control` before every op and holds on
:meth:`LiveHub.wait_for_release` while the person has the browser; the card's
routes (``dashboard/handlers/browser_live.py``) call the rest.

Two things leave the hub, both to OWNER websockets only, through the two senders
:meth:`LiveHub.bind` installs: the ``browser_live`` JSON event
(:func:`live_event`) on every state change, and the binary frame the stream
built. The hub imports nothing from the dashboard; the dashboard binds itself in.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.browser_harness.screencast import (
    LiveConfig,
    LiveStream,
    Relay,
    TickStats,
)
from kiro_crew.browser_harness.takeover import USER, InputLock, dispatch_input

logger = logging.getLogger(__name__)

#: Owner-websocket event name for a card state change.
LIVE_EVENT = "browser_live"

#: The card states, in the Wire contract's words.
STATE_WAITING_ALLOW = "waiting_allow"
STATE_LIVE = "live"
STATE_IN_CONTROL = "in_control"
STATE_DONE = "done"
STATE_LOGIN_NEEDED = "login_needed"
STATE_ERROR = "error"
STATES = frozenset(
    {
        STATE_WAITING_ALLOW,
        STATE_LIVE,
        STATE_IN_CONTROL,
        STATE_DONE,
        STATE_LOGIN_NEEDED,
        STATE_ERROR,
    }
)

#: Longest step label kept; the tool's labels are a few words.
MAX_STEP_LABEL = 120
#: Steps kept per turn; older ones fold into the count.
MAX_STEPS = 200

Publish = Callable[[str, Mapping[str, Any]], Awaitable[Any]]
SendFrame = Callable[[bytes], Awaitable[Any]]


def _set_event() -> asyncio.Event:
    ev = asyncio.Event()
    ev.set()
    return ev


@dataclass
class Step:
    label: str
    status: str = "active"  # done | active | pending


@dataclass
class LiveSession:
    """Everything the card shows for one session, plus the stream feeding it."""

    session_key: str
    turn_id: str = ""
    state: str = ""
    url: str = ""
    title: str = ""
    browser: str = ""
    headless: bool = False
    steps: list[Step] = field(default_factory=list)
    lock: InputLock = field(default_factory=InputLock)
    stream: LiveStream | None = None
    visible: bool = True
    rung: int = 0
    fps: float = 0.0
    started_at: float = 0.0
    ended_at: float = 0.0
    #: The agent tab this session's ops run on, and every tab it has owned. The op
    #: route binds before each op (``browser_ops._bind_target``), so two chats never
    #: drive one daemon tab and a takeover in one chat fences exactly that chat's tab.
    target_id: str = ""
    targets: set[str] = field(default_factory=set)
    #: The relay the last stream ran on, kept after the stream stops so the finished
    #: card's "Open in Chrome" can still activate the retained tab.
    last_relay: Relay | None = None
    #: The op fence (see :meth:`LiveHub.op_fence`): held by the op route for the
    #: whole of bind + op, taken by :meth:`LiveHub.claim` so a takeover lands
    #: between ops, never inside one.
    op_fence: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: The op running under the fence right now, so a claim can cancel it.
    op_task: asyncio.Task[Any] | None = None
    #: Set by :meth:`LiveHub.claim` before it cancels ``op_task``: the route reads it
    #: to answer ``user_in_control`` instead of propagating the cancellation.
    op_interrupted: bool = False
    #: Set while the person does NOT hold the input lock; cleared by a claim and set
    #: again by a release or the turn's end. :meth:`LiveHub.wait_for_release` awaits
    #: it, which is how an agent op holds until the hand-back instead of failing.
    released: asyncio.Event = field(default_factory=_set_event)

    @property
    def relay(self) -> Relay | None:
        return self.stream.relay if self.stream is not None else self.last_relay


def live_event(sess: LiveSession) -> dict[str, Any]:
    """The ``browser_live`` payload: exactly the Wire-contract fields."""
    active = next((i for i, s in enumerate(sess.steps) if s.status == "active"), None)
    step = (
        {"index": active, "label": sess.steps[active].label}
        if active is not None
        else {"index": len(sess.steps) - 1, "label": sess.steps[-1].label} if sess.steps else None
    )
    return {
        "session_key": sess.session_key,
        "turn_id": sess.turn_id,
        "state": sess.state,
        "url": sess.url,
        "title": sess.title,
        "browser": sess.browser,
        "headless": bool(sess.headless),
        "step": step,
        "steps": [{"label": s.label, "status": s.status} for s in sess.steps],
        "rung": int(sess.rung),
        "fps": float(sess.fps),
    }


class _StreamSink:
    """The stream's :class:`FrameSink`, bound to one session of the hub."""

    def __init__(self, hub: "LiveHub", sess: LiveSession) -> None:
        self._hub = hub
        self._sess = sess

    async def frame(self, header: Mapping[str, Any], blob: bytes) -> None:
        await self._hub._send_frame_blob(self._sess, blob)


class LiveHub:
    """Per-gateway registry of live sessions. Construct one; bind the dashboard in."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sessions: dict[str, LiveSession] = {}
        self._publish: Publish | None = None
        self._send_frame: SendFrame | None = None
        self._config = LiveConfig()
        self._clock = clock
        #: The one sleep the hold's bound runs on (tests inject one that returns
        #: at once, or never).
        self._sleep = sleep
        self._tasks: set[asyncio.Task[Any]] = set()

    # ── wiring ─────────────────────────────────────────────────────────────

    def bind(self, publish: Publish | None, send_frame: SendFrame | None) -> None:
        """Install the owner-only senders (JSON events, binary frames)."""
        self._publish = publish
        self._send_frame = send_frame

    def configure(self, cfg: Any) -> None:
        """Read ``browser.live.*`` from a loaded config; applies to streams started after."""
        self._config = LiveConfig.from_config(cfg)

    @property
    def config(self) -> LiveConfig:
        return self._config

    def get(self, session_key: str) -> LiveSession | None:
        return self._sessions.get(session_key)

    def _session(self, session_key: str) -> LiveSession:
        sess = self._sessions.get(session_key)
        if sess is None:
            sess = LiveSession(session_key=session_key)
            self._sessions[session_key] = sess
        return sess

    # ── the tool's side ────────────────────────────────────────────────────

    def turn_started(self, session_key: str, turn_id: str) -> None:
        """A turn began browsing: reset the strip; the card attaches to this turn."""
        sess = self._session(session_key)
        sess.turn_id = str(turn_id)
        sess.steps = []
        sess.started_at = self._clock()
        sess.ended_at = 0.0
        if sess.state in ("", STATE_DONE):
            sess.state = ""
        self._emit(sess)

    def step(self, session_key: str, label: str) -> None:
        """The agent is on a new step: the previous active one is done."""
        sess = self._session(session_key)
        text = str(label or "")[:MAX_STEP_LABEL]
        for s in sess.steps:
            if s.status == "active":
                s.status = "done"
        sess.steps.append(Step(label=text))
        if len(sess.steps) > MAX_STEPS:
            del sess.steps[: len(sess.steps) - MAX_STEPS]
        self._emit(sess)

    def turn_ended(self, session_key: str) -> None:
        """The turn is over: the stream stops, the lock opens, the card folds to one line."""
        sess = self._sessions.get(session_key)
        if sess is None:
            return
        for s in sess.steps:
            if s.status == "active":
                s.status = "done"
        sess.lock.release()
        sess.released.set()
        sess.ended_at = self._clock()
        sess.state = STATE_DONE
        stream, sess.stream = sess.stream, None
        if stream is not None:
            self._spawn(stream.stop())
        self._emit(sess)

    def in_control(self, session_key: str) -> bool:
        """Whether the person holds the input lock; an agent op holds until they let go."""
        sess = self._sessions.get(session_key)
        return sess is not None and sess.lock.held()

    async def wait_for_release(self, session_key: str, timeout: float) -> bool:
        """Wait until the person hands the browser back, at most *timeout* seconds.

        ``True`` when the lock is free (at once if it already is), ``False`` when the
        bound passed with the person still in control. The op route holds an agent
        op on this instead of answering ``user_in_control`` at once, so the agent
        resumes by itself after the hand-back. Cancellable like any await; the bound
        runs on the hub's injected sleep, never on a blocking call.
        """
        sess = self._session(session_key)
        if not sess.lock.held():
            return True
        waiter = asyncio.ensure_future(sess.released.wait())
        timer = asyncio.ensure_future(self._sleep(timeout))
        try:
            await asyncio.wait({waiter, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (waiter, timer):
                if not t.done():
                    t.cancel()
        return not sess.lock.held()

    # ── the op fence (shared by the op route and the person's claim) ───────

    def op_fence(self, session_key: str) -> asyncio.Lock:
        """The per-session fence the op route holds from bind to the op's end.

        :meth:`claim` takes the same fence, so a takeover is confirmed only once the
        op in flight has drained (or been cancelled by the claim), and the route's
        ``in_control`` check at entry is then the whole story: no op can start
        while the person holds the lock, and none is still running when they get it.
        """
        return self._session(session_key).op_fence

    def op_started(self, session_key: str, task: asyncio.Task[Any]) -> None:
        """The route created the op's task under the fence; a claim may cancel it."""
        sess = self._session(session_key)
        sess.op_task = task
        sess.op_interrupted = False

    def op_finished(self, session_key: str) -> bool:
        """The route left the fence. Returns whether a claim interrupted the op."""
        sess = self._session(session_key)
        sess.op_task = None
        interrupted, sess.op_interrupted = sess.op_interrupted, False
        return interrupted

    # ── the tab binding (the op route's side) ──────────────────────────────

    def target_of(self, session_key: str) -> str:
        """The agent tab this session's ops run on, or ``""`` before the first bind."""
        sess = self._sessions.get(session_key)
        return sess.target_id if sess is not None else ""

    def targets_of(self, session_key: str) -> set[str]:
        """Every agent tab this session has owned (the ``tabs`` op's universe)."""
        sess = self._sessions.get(session_key)
        return set(sess.targets) if sess is not None else set()

    def bind_target(self, session_key: str, target_id: str) -> None:
        """Make *target_id* this session's current tab, and one of its own."""
        sess = self._session(session_key)
        sess.target_id = str(target_id)
        sess.targets.add(sess.target_id)

    def owner_of_target(self, target_id: str) -> str | None:
        """The session key that owns *target_id*, if any session does."""
        for key, sess in self._sessions.items():
            if target_id in sess.targets:
                return key
        return None

    # ── the supervisor's side ──────────────────────────────────────────────

    def set_state(
        self,
        session_key: str,
        state: str,
        *,
        url: str | None = None,
        title: str | None = None,
        browser: str | None = None,
        headless: bool | None = None,
    ) -> None:
        """A state the supervisor knows (waiting_allow, login_needed, error) or a page change."""
        if state not in STATES:
            raise ValueError(f"unknown live state {state!r}")
        sess = self._session(session_key)
        if state == STATE_IN_CONTROL and not sess.lock.held():
            state = STATE_LIVE
        sess.state = state
        self._update_page(sess, url=url, title=title, browser=browser, headless=headless)
        self._emit(sess)

    def page_changed(self, session_key: str, *, url: str = "", title: str = "") -> None:
        sess = self._session(session_key)
        self._update_page(sess, url=url, title=title)
        self._emit(sess)

    @staticmethod
    def _update_page(
        sess: LiveSession,
        *,
        url: str | None = None,
        title: str | None = None,
        browser: str | None = None,
        headless: bool | None = None,
    ) -> None:
        if url is not None:
            sess.url = str(url)[:2048]
        if title is not None:
            sess.title = str(title)[:300]
        if browser is not None:
            sess.browser = str(browser)
        if headless is not None:
            sess.headless = bool(headless)

    async def start_stream(
        self,
        session_key: str,
        relay: Relay,
        target_id: str,
        *,
        browser: str,
        headless: bool,
        url: str = "",
        title: str = "",
        stream_factory: Callable[..., LiveStream] | None = None,
    ) -> LiveStream | None:
        """Start the screencast of the agent's target for this session's card.

        Returns ``None`` when the live view is off in config. Replaces a stream that
        is already running (the agent moved to another tab).
        """
        sess = self._session(session_key)
        self._update_page(sess, url=url, title=title, browser=browser, headless=headless)
        if not self._config.enabled:
            # Live view off: no stream, no state, no event -- a card that could
            # only ever wait for a frame is not announced (see ``_emit_now``).
            return None
        old, sess.stream = sess.stream, None
        if old is not None:
            await old.stop()
        factory = stream_factory or LiveStream
        stream = factory(
            relay,
            target_id,
            _StreamSink(self, sess),
            session_key=session_key,
            config=self._config,
            on_tick=self._tick_for(sess),
        )
        stream.visible = sess.visible
        sess.last_relay = relay
        # Installed only once it runs: a stream whose ``start()`` raised (the target
        # closed mid-navigation) would otherwise sit on the session with the right
        # ``target_id`` and stop every later op from starting a working one.
        await stream.start()
        sess.stream = stream
        if sess.lock.held():
            await stream.set_takeover(True)
        sess.rung = stream.rung
        sess.state = STATE_IN_CONTROL if sess.lock.held() else STATE_LIVE
        self._emit(sess)
        return stream

    async def stop_stream(self, session_key: str) -> None:
        sess = self._sessions.get(session_key)
        if sess is None or sess.stream is None:
            return
        stream, sess.stream = sess.stream, None
        await stream.stop()

    def _tick_for(self, sess: LiveSession) -> Callable[[TickStats], Awaitable[None]]:
        async def _on_tick(stats: TickStats) -> None:
            changed = stats.rung != sess.rung or round(stats.fps) != round(sess.fps)
            sess.rung, sess.fps = stats.rung, stats.fps
            if changed:
                await self._emit_now(sess)

        return _on_tick

    # ── the card's side ────────────────────────────────────────────────────

    async def set_visible(self, session_key: str, visible: bool) -> None:
        sess = self._session(session_key)
        sess.visible = bool(visible)
        if sess.stream is not None:
            await sess.stream.set_visible(sess.visible)

    def ack(self, session_key: str, seq: int, stream: str | None = None) -> bool:
        """The card received frame ``seq`` of ``stream``.

        The frame header names its stream (``frame_header``) and ``seq`` restarts at 1
        with every stream, so an ack that names another stream (a late ack for the
        stream a tab switch just replaced) must not release the new stream's frame
        of the same number early. A caller that names no stream acks the current one.
        """
        sess = self._sessions.get(session_key)
        if sess is None or sess.stream is None:
            return False
        if stream is not None and stream != getattr(sess.stream, "stream_id", stream):
            return False
        return sess.stream.ack(int(seq))

    async def claim(self, session_key: str) -> bool:
        """The person takes input. ``False`` only when someone else holds it.

        The lock is taken first, so the op route refuses anything that arrives from
        now on; then the op in flight (if any) is cancelled and the fence awaited,
        so the takeover is confirmed only once the agent has stopped touching the
        tab. The state is published ``in_control`` only if the lock is still held
        after the awaits: an Esc that lands in between has already said ``live``.
        """
        sess = self._session(session_key)
        if sess.state in (STATE_DONE, STATE_ERROR):
            # A finished card has no browser to take: the stream is gone and the
            # agent is not driving anything to be fenced off.
            raise LookupError("turn_over")
        if not sess.lock.claim(USER):
            return False
        sess.released.clear()
        task = sess.op_task
        if task is not None and not task.done():
            sess.op_interrupted = True
            task.cancel()
        async with sess.op_fence:
            pass
        if not sess.lock.held():
            return True
        sess.state = STATE_IN_CONTROL
        if sess.stream is not None:
            await sess.stream.set_takeover(True)
        if sess.lock.held():
            await self._emit_now(sess)
        return True

    async def release(self, session_key: str) -> None:
        sess = self._sessions.get(session_key)
        if sess is None:
            return
        was_held = sess.lock.held()
        sess.lock.release()
        sess.released.set()
        if sess.stream is not None:
            await sess.stream.set_takeover(False)
        if sess.state == STATE_IN_CONTROL:
            sess.state = STATE_LIVE
        if was_held:
            await self._emit_now(sess)

    async def input_event(self, session_key: str, event: dict[str, Any]) -> int:
        """Dispatch one card event. Raises ``InputEventError`` or ``LookupError``."""
        sess = self._sessions.get(session_key)
        if sess is None or not sess.lock.held() or sess.lock.owner != USER:
            raise LookupError("not_in_control")
        stream = sess.stream
        if stream is None or not stream.session_id:
            raise LookupError("no_stream")
        scale = float(getattr(stream, "scale", 1.0) or 1.0)
        sent = await dispatch_input(stream.relay, stream.session_id, event, scale=scale)
        stream.note_input()
        return sent

    async def open_in_browser(self, session_key: str) -> bool:
        """Focus the real tab the card shows or showed.

        ``Target.activateTarget`` on the session's tab goes through the relay's flat
        connection and needs no screencast: a finished turn (``turn_ended`` dropped
        the stream, the tab stays open in the person's Chrome) is still focusable.
        ``Page.bringToFront`` is added while a stream session is attached.
        """
        sess = self._sessions.get(session_key)
        if sess is None:
            return False
        stream = sess.stream
        target = stream.target_id if stream is not None else sess.target_id
        relay = sess.relay
        if relay is None or not target or getattr(relay, "closed", False):
            return False
        try:
            await relay.send("Target.activateTarget", {"targetId": target})
            if stream is not None and stream.session_id:
                await relay.send("Page.bringToFront", {}, stream.session_id)
        except Exception:
            logger.debug("open-in-browser failed", exc_info=True)
            return False
        return True

    # ── delivery ───────────────────────────────────────────────────────────

    async def _send_frame_blob(self, sess: LiveSession, blob: bytes) -> None:
        if self._send_frame is None:
            return
        try:
            await self._send_frame(blob)
        except Exception:
            logger.debug("live frame send failed", exc_info=True)

    def _publishable(self, sess: LiveSession) -> bool:
        """A state to show, a sender to show it through, and the live view turned on.

        ``browser.live.enabled=false`` is "no card": with no event the dashboard never
        mounts one, while the browser ops keep running underneath.
        """
        return self._publish is not None and bool(sess.state) and self._config.enabled

    async def _emit_now(self, sess: LiveSession) -> None:
        publish = self._publish
        if publish is None or not self._publishable(sess):
            return
        try:
            await publish(LIVE_EVENT, live_event(sess))
        except Exception:
            logger.debug("browser_live publish failed", exc_info=True)

    def _emit(self, sess: LiveSession) -> None:
        """Publish from a sync caller: scheduled on the running loop, dropped without one."""
        if not self._publishable(sess):
            return
        self._spawn(self._emit_now(sess))

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        task: asyncio.Task[Any] = loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        """Stop every stream (gateway shutdown)."""
        for sess in list(self._sessions.values()):
            stream, sess.stream = sess.stream, None
            if stream is not None:
                try:
                    await stream.stop()
                except Exception:
                    logger.debug("live stream stop failed", exc_info=True)
        for task in list(self._tasks):
            task.cancel()


_HUB: LiveHub | None = None


def get_live_hub() -> LiveHub:
    """The gateway's one hub, created on first use."""
    global _HUB
    if _HUB is None:
        _HUB = LiveHub()
    return _HUB


def reset_live_hub() -> None:
    """Drop the singleton (tests)."""
    global _HUB
    _HUB = None


__all__ = [
    "LIVE_EVENT",
    "LiveHub",
    "LiveSession",
    "STATES",
    "STATE_DONE",
    "STATE_ERROR",
    "STATE_IN_CONTROL",
    "STATE_LIVE",
    "STATE_LOGIN_NEEDED",
    "STATE_WAITING_ALLOW",
    "Step",
    "get_live_hub",
    "live_event",
    "reset_live_hub",
]
