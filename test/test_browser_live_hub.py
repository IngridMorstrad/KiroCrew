"""The live hub: turn and step state, the lock, the stream's lifecycle, owner-only delivery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from browser_live_helpers import SESSION, FakeRelay

from kiro_crew.browser_harness import live_hub as lh
from kiro_crew.browser_harness import screencast as sc
from kiro_crew.browser_harness.takeover import InputEventError

pytestmark = pytest.mark.asyncio


class FakeStream:
    """Stands in for ``LiveStream``: records calls, pushes a frame on demand."""

    def __init__(self, relay: Any, target_id: str, sink: Any, **kwargs: Any) -> None:
        self.relay = relay
        self.target_id = target_id
        self.sink = sink
        self.kwargs = kwargs
        self.session_id = SESSION
        self.visible = True
        self.calls: list[tuple[str, Any]] = []
        self.rung = 0
        self.inputs = 0

    async def start(self) -> None:
        self.calls.append(("start", None))

    async def stop(self) -> None:
        self.calls.append(("stop", None))

    async def set_visible(self, visible: bool) -> None:
        self.calls.append(("visible", visible))

    async def set_takeover(self, active: bool) -> None:
        self.calls.append(("takeover", active))

    def ack(self, seq: int) -> bool:
        self.calls.append(("ack", seq))
        return True

    def note_input(self) -> None:
        self.inputs += 1

    async def push(self, blob: bytes) -> None:
        await self.sink.frame({"seq": 1}, blob)


class Wire:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.frames: list[bytes] = []

    async def publish(self, name: str, data: Any) -> int:
        self.events.append((name, dict(data)))
        return 1

    async def send_frame(self, blob: bytes) -> int:
        self.frames.append(bytes(blob))
        return 1


async def _settled(hub: lh.LiveHub) -> None:
    """Wait for the hub's scheduled publishes, which are the signal a sync call leaves."""
    while hub._tasks:
        await asyncio.gather(*list(hub._tasks), return_exceptions=True)


@pytest.fixture
def hub() -> tuple[lh.LiveHub, Wire]:
    wire = Wire()
    h = lh.LiveHub(clock=lambda: 100.0)
    h.bind(wire.publish, wire.send_frame)
    return h, wire


async def test_steps_build_the_strip_and_each_change_publishes_one_event(hub) -> None:
    h, wire = hub
    h.turn_started("chat-1", "turn-9")
    h.set_state("chat-1", lh.STATE_WAITING_ALLOW, browser="user_chrome", headless=False)
    h.step("chat-1", "Open example.com")
    h.step("chat-1", "Click Sign in")
    await _settled(h)
    assert [n for n, _ in wire.events] == [lh.LIVE_EVENT] * 3
    last = wire.events[-1][1]
    assert last == {
        "session_key": "chat-1",
        "turn_id": "turn-9",
        "state": "waiting_allow",
        "url": "",
        "title": "",
        "browser": "user_chrome",
        "headless": False,
        "step": {"index": 1, "label": "Click Sign in"},
        "steps": [
            {"label": "Open example.com", "status": "done"},
            {"label": "Click Sign in", "status": "active"},
        ],
        "rung": 0,
        "fps": 0.0,
    }


async def test_nothing_is_published_before_the_card_has_a_state(hub) -> None:
    h, wire = hub
    h.turn_started("chat-1", "t")
    h.step("chat-1", "first")
    await _settled(h)
    assert wire.events == []
    assert h.in_control("chat-1") is False and h.in_control("nope") is False


async def test_start_stream_goes_live_and_frames_reach_the_owner_sender(hub) -> None:
    h, wire = hub
    relay = FakeRelay()
    h.turn_started("chat-1", "t1")
    stream = await h.start_stream(
        "chat-1",
        relay,
        "target-1",
        browser="kiro_chromium",
        headless=True,
        url="https://example.com",
        title="Example",
        stream_factory=FakeStream,
    )
    assert isinstance(stream, FakeStream) and stream.calls == [("start", None)]
    assert stream.kwargs["session_key"] == "chat-1" and stream.kwargs["config"] == h.config
    await _settled(h)
    ev = wire.events[-1][1]
    assert ev["state"] == "live" and ev["browser"] == "kiro_chromium" and ev["headless"] is True
    assert ev["url"] == "https://example.com" and ev["title"] == "Example"
    await stream.push(b"\x00\x00\x00\x02{}jpeg")
    assert wire.frames == [b"\x00\x00\x00\x02{}jpeg"]


async def test_a_second_stream_replaces_the_first(hub) -> None:
    h, _wire = hub
    first = await h.start_stream(
        "chat-1", FakeRelay(), "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    second = await h.start_stream(
        "chat-1", FakeRelay(), "b", browser="edge", headless=False, stream_factory=FakeStream
    )
    assert first.calls == [("start", None), ("stop", None)]
    assert h.get("chat-1").stream is second


async def test_live_view_off_in_config_starts_no_stream(hub) -> None:
    h, wire = hub
    h.configure(SimpleNamespace(browser=SimpleNamespace(live=SimpleNamespace(enabled=False))))
    assert h.config == sc.LiveConfig(enabled=False)
    stream = await h.start_stream(
        "chat-1", FakeRelay(), "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    assert stream is None and h.get("chat-1").stream is None
    await _settled(h)
    # No card is announced: an event would mount a live card that can never show a
    # frame. The browser op itself is untouched (the hub still tracks the session).
    assert wire.events == []
    h.step("chat-1", "Open x")
    await _settled(h)
    assert wire.events == []


async def test_claim_takes_control_caps_the_stream_and_release_hands_back(hub) -> None:
    h, wire = hub
    relay = FakeRelay()
    stream = await h.start_stream(
        "chat-1", relay, "a", browser="user_chrome", headless=False, stream_factory=FakeStream
    )
    assert await h.claim("chat-1") is True
    assert h.in_control("chat-1") is True
    assert ("takeover", True) in stream.calls
    assert wire.events[-1][1]["state"] == "in_control"
    # An agent op asks the hub and gets the refusal code's condition.
    assert h.in_control("chat-1") is True
    sent = await h.input_event("chat-1", {"type": "mouse", "x": 1, "y": 1, "button": "left"})
    assert sent == 2 and stream.inputs == 1
    assert [s for _, _, s in relay.calls] == [SESSION, SESSION]
    await h.release("chat-1")
    assert h.in_control("chat-1") is False
    assert stream.calls[-1] == ("takeover", False)
    assert wire.events[-1][1]["state"] == "live"


async def test_input_is_refused_without_a_claim_or_without_a_stream(hub) -> None:
    h, _wire = hub
    with pytest.raises(LookupError, match="not_in_control"):
        await h.input_event("chat-1", {"type": "mouse", "x": 1, "y": 1})
    assert await h.claim("chat-1") is True  # the lock exists before the stream does
    with pytest.raises(LookupError, match="no_stream"):
        await h.input_event("chat-1", {"type": "mouse", "x": 1, "y": 1})
    await h.start_stream(
        "chat-1", FakeRelay(), "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    with pytest.raises(InputEventError):
        await h.input_event("chat-1", {"type": "key"})


async def test_a_stream_started_under_a_claim_is_capped_at_once(hub) -> None:
    h, wire = hub
    await h.claim("chat-1")
    stream = await h.start_stream(
        "chat-1", FakeRelay(), "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    assert stream.calls == [("start", None), ("takeover", True)]
    assert wire.events[-1][1]["state"] == "in_control"


async def test_turn_ended_stops_the_stream_opens_the_lock_and_folds_the_card(hub) -> None:
    h, wire = hub
    stream = await h.start_stream(
        "chat-1", FakeRelay(), "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    h.step("chat-1", "one")
    await h.claim("chat-1")
    h.turn_ended("chat-1")
    await _settled(h)
    assert ("stop", None) in stream.calls
    assert h.in_control("chat-1") is False and h.get("chat-1").stream is None
    ev = wire.events[-1][1]
    assert ev["state"] == "done" and ev["steps"] == [{"label": "one", "status": "done"}]
    assert ev["step"] == {"index": 0, "label": "one"}
    h.turn_ended("never-started")  # no record, no error


async def test_visibility_and_acks_reach_the_stream(hub) -> None:
    h, _wire = hub
    await h.set_visible("chat-1", False)  # before any stream: remembered
    stream = await h.start_stream(
        "chat-1", FakeRelay(), "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    assert stream.visible is False
    await h.set_visible("chat-1", True)
    assert ("visible", True) in stream.calls
    assert h.ack("chat-1", 4) is True and ("ack", 4) in stream.calls
    assert h.ack("other", 1) is False


async def test_open_in_browser_activates_and_brings_the_target_to_front(hub) -> None:
    h, _wire = hub
    relay = FakeRelay()
    await h.start_stream(
        "chat-1", relay, "tgt", browser="edge", headless=False, stream_factory=FakeStream
    )
    assert await h.open_in_browser("chat-1") is True
    assert relay.calls == [
        ("Target.activateTarget", {"targetId": "tgt"}, None),
        ("Page.bringToFront", {}, SESSION),
    ]
    assert await h.open_in_browser("nope") is False
    relay.fail.add("Target.activateTarget")
    assert await h.open_in_browser("chat-1") is False


async def test_tick_stats_update_rung_and_fps_and_publish_on_change(hub) -> None:
    h, wire = hub
    sess = h._session("chat-1")
    sess.state = lh.STATE_LIVE
    on_tick = h._tick_for(sess)
    await on_tick(sc.TickStats(rung=0, fps=0.0, frames=0, late_share=0.0, kb_per_frame=0.0))
    assert wire.events == []  # unchanged
    await on_tick(sc.TickStats(rung=2, fps=29.0, frames=29, late_share=0.1, kb_per_frame=40.0))
    assert wire.events[-1][1]["rung"] == 2 and wire.events[-1][1]["fps"] == 29.0


async def test_set_state_refuses_unknown_states_and_in_control_without_a_lock(hub) -> None:
    h, wire = hub
    with pytest.raises(ValueError):
        h.set_state("chat-1", "sleeping")
    h.set_state("chat-1", lh.STATE_IN_CONTROL)
    await _settled(h)
    assert wire.events[-1][1]["state"] == "live"


async def test_the_singleton_is_one_hub_and_resettable() -> None:
    lh.reset_live_hub()
    a = lh.get_live_hub()
    assert lh.get_live_hub() is a
    lh.reset_live_hub()
    assert lh.get_live_hub() is not a
    lh.reset_live_hub()


async def test_close_stops_every_stream(hub) -> None:
    h, _wire = hub
    s1 = await h.start_stream(
        "a", FakeRelay(), "x", browser="edge", headless=False, stream_factory=FakeStream
    )
    s2 = await h.start_stream(
        "b", FakeRelay(), "y", browser="edge", headless=False, stream_factory=FakeStream
    )
    await h.close()
    assert ("stop", None) in s1.calls and ("stop", None) in s2.calls


async def test_open_in_browser_still_focuses_the_tab_after_the_turn_ended(hub) -> None:
    """The finished card's "Open in Chrome": the stream is gone, the tab is not.

    ``turn_ended`` drops the stream; the retained target is activated through the
    relay's flat connection, which needs no screencast session.
    """
    h, _wire = hub
    relay = FakeRelay()
    h.bind_target("chat-1", "tgt")
    await h.start_stream(
        "chat-1", relay, "tgt", browser="user_chrome", headless=False, stream_factory=FakeStream
    )
    h.turn_ended("chat-1")
    await _settled(h)
    relay.calls.clear()
    assert h.get("chat-1").stream is None and h.get("chat-1").state == lh.STATE_DONE
    assert await h.open_in_browser("chat-1") is True
    assert relay.calls == [("Target.activateTarget", {"targetId": "tgt"}, None)]
    # A relay that has since closed (gateway restart, Disconnect) is not asked.
    relay.closed = True  # type: ignore[attr-defined]
    assert await h.open_in_browser("chat-1") is False


async def test_a_stream_whose_start_raised_is_not_installed(hub) -> None:
    """Otherwise every later op on that tab sees "a stream with the right target"
    and only refreshes url/title: no frames for the rest of the turn."""
    h, _wire = hub

    class Failing(FakeStream):
        async def start(self) -> None:
            raise RuntimeError("Target closed")

    with pytest.raises(RuntimeError):
        await h.start_stream(
            "chat-1", FakeRelay(), "tgt", browser="edge", headless=False, stream_factory=Failing
        )
    assert h.get("chat-1").stream is None
    # The next attempt on the same tab starts a stream.
    stream = await h.start_stream(
        "chat-1", FakeRelay(), "tgt", browser="edge", headless=False, stream_factory=FakeStream
    )
    assert stream is not None and h.get("chat-1").stream is stream


async def test_claim_does_not_publish_in_control_after_an_esc_that_landed_mid_claim(hub) -> None:
    """``claim`` awaits the stream's takeover cap; a release during that await has
    already said ``live``, and the claim must not overwrite it with ``in_control``."""
    h, wire = hub
    gate = asyncio.Event()

    class Slow(FakeStream):
        async def set_takeover(self, active: bool) -> None:
            self.calls.append(("takeover", active))
            if active:
                await gate.wait()

    await h.start_stream(
        "chat-1", FakeRelay(), "tgt", browser="edge", headless=False, stream_factory=Slow
    )
    h.turn_started("chat-1", "t1")
    await _settled(h)
    wire.events.clear()
    claim = asyncio.ensure_future(h.claim("chat-1"))
    await asyncio.sleep(0)
    await h.release("chat-1")  # Esc while the claim is inside set_takeover
    gate.set()
    assert await claim is True
    await _settled(h)
    assert h.get("chat-1").lock.held() is False
    states = [e["state"] for _, e in wire.events]
    assert states and states[-1] == lh.STATE_LIVE
    assert lh.STATE_IN_CONTROL not in states[-1:]


async def test_claim_waits_for_the_op_fence_and_cancels_the_op_inside_it(hub) -> None:
    """The route holds ``op_fence`` for bind + op; a claim cancels the op in flight,
    waits for the fence, and only then confirms control (``op_interrupted`` tells
    the route to answer ``user_in_control`` instead of propagating the cancel)."""
    h, _wire = hub
    h.turn_started("chat-1", "t1")
    fence = h.op_fence("chat-1")
    cancelled = asyncio.Event()

    async def op() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def route() -> bool:
        async with fence:
            task = asyncio.ensure_future(op())
            h.op_started("chat-1", task)
            try:
                await task
            except asyncio.CancelledError:
                return h.op_finished("chat-1")
            return False

    route_task = asyncio.ensure_future(route())
    await asyncio.sleep(0)
    assert fence.locked()
    claim = asyncio.ensure_future(h.claim("chat-1"))
    await asyncio.sleep(0)
    assert h.in_control("chat-1") is True, "the lock is taken first, so no new op starts"
    assert await route_task is True, "the route learns the cancel was the claim's"
    assert cancelled.is_set()
    assert await claim is True
    assert not fence.locked()
    assert h.get("chat-1").state == lh.STATE_IN_CONTROL


async def test_wait_for_release_returns_on_the_hand_back_and_on_the_bound(hub) -> None:
    """The hold's awaitable, on an injected clock: a release ends it with ``True``;
    the bound (the hub's sleep) ends it with ``False`` while the lock is held."""
    h, _wire = hub
    h.turn_started("chat-1", "t1")
    assert await h.wait_for_release("chat-1", 600) is True, "free at once when not held"
    await h.claim("chat-1")
    waiter = asyncio.ensure_future(h.wait_for_release("chat-1", 600))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not waiter.done()
    await h.release("chat-1")
    assert await waiter is True
    # The bound: a hub whose sleep returns at once.
    slept: list[float] = []

    async def instant(seconds: float) -> None:
        slept.append(seconds)

    quick = lh.LiveHub(clock=lambda: 0.0, sleep=instant)
    quick.turn_started("chat-2", "t2")
    await quick.claim("chat-2")
    assert await quick.wait_for_release("chat-2", 600) is False
    assert slept == [600]
    assert quick.in_control("chat-2") is True
    # ``turn_ended`` is a release too.
    quick.turn_ended("chat-2")
    assert await quick.wait_for_release("chat-2", 600) is True


async def test_claim_on_a_finished_card_is_refused(hub) -> None:
    """No "in control" on a done card: there is no stream and no agent to fence."""
    h, wire = hub
    h.turn_started("chat-1", "t1")
    h.turn_ended("chat-1")
    await _settled(h)
    wire.events.clear()
    with pytest.raises(LookupError, match="turn_over"):
        await h.claim("chat-1")
    assert h.in_control("chat-1") is False
    assert wire.events == []
    h.set_state("chat-1", lh.STATE_ERROR)
    with pytest.raises(LookupError, match="turn_over"):
        await h.claim("chat-1")


async def test_an_ack_naming_another_stream_releases_nothing(hub) -> None:
    """The header promises ``(stream, seq)``: a late ack for the stream a tab switch
    just replaced must not free the new stream's frame of the same number."""
    h, _wire = hub
    stream = await h.start_stream(
        "chat-1", FakeRelay(), "tgt", browser="edge", headless=False, stream_factory=FakeStream
    )
    assert stream is not None
    stream.stream_id = "new1"  # type: ignore[attr-defined]
    assert h.ack("chat-1", 7, "old0") is False
    assert stream.calls.count(("ack", 7)) == 0
    assert h.ack("chat-1", 7, "new1") is True
    assert h.ack("chat-1", 8) is True, "no stream named: the current one"
