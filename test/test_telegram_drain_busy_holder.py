"""A Telegram drain that finds its session held by another turn (#23589).

The drain runs after a Telegram turn releases the session. If something else took the
lease in between -- a dashboard, cron or auto-nudge turn on the same key, or another
channel's turn under a unified ``dm_scope`` -- replaying the queued message only reaches
``_handle_busy``, which queues the same text again, and the pump dequeued it straight
back. Every lap flipped the receipt to "Now answering" without a turn starting.

Covered here: the drain leaves the queue untouched and announces nothing while the
session is held, it asks the holder's release to wake it, the release does wake it, and
the message is then answered once with one notice.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import _dispatcher

from kiro_crew import session_allocation as sa
from kiro_crew.config import KiroCrewConfig
from kiro_crew.messaging import queue_drain
from kiro_crew.messaging.queue_drain import reset_drains
from kiro_crew.session import SessionManager
from kiro_crew.telegram.transport import TelegramInboundMessage
from kiro_crew.telegram.transport_dispatch import _inbound_origin

_KEY = "unified:kirocrew"


@pytest.fixture(autouse=True)
def _clean_registry():
    """A drain registration is process-wide; a test must not inherit another's."""
    reset_drains()
    yield
    reset_drains()


def _msg(text: str) -> TelegramInboundMessage:
    return TelegramInboundMessage(
        channel_type="telegram",
        user_id="7",
        conversation_id="70",
        text=text,
        message_id=11,
        chat_type="private",
    )


async def _queue_one(d: Any, sess: Any, text: str) -> None:
    """Queue *text* through the production mid-turn writer, as a busy turn would."""
    sess._busy = True
    assert await d._enqueue_with_receipt(
        _KEY, 70, text, origin=_inbound_origin(_msg(text))
    ), "the fake session must accept a mid-turn enqueue"


def _now_answering(cli: Any) -> list[str]:
    return [txt for _mid, txt, _ in cli.edits if "Now answering" in txt]


class TestTheDrainLeavesAHeldSessionAlone:
    @pytest.mark.asyncio
    async def test_a_held_session_is_not_announced_and_keeps_its_queue(self) -> None:
        """The session is still held when the drain runs.

        Before the fix the pump flipped the receipt and replayed the message anyway --
        the first lap of the dequeue -> "Now answering" -> requeue loop. The bounded
        wait keeps a regressed loop from hanging the test.
        """
        d, cli, sess = _dispatcher({7}, dm_scope="unified")
        replayed: list[Any] = []

        async def _spy(msg: Any, **kw: Any) -> None:
            replayed.append(msg)

        await _queue_one(d, sess, "hello")
        d.handle_message = _spy  # type: ignore[method-assign]
        # Still busy: another turn holds the lease the Telegram turn just released.
        await asyncio.wait_for(d._drain_queue(_KEY), timeout=5)

        assert replayed == [], "no turn can start in a held session"
        assert _now_answering(cli) == [], "so nothing may claim one has"
        assert [e[1] for e in sess.queued] == ["hello"], "the message stays queued"
        assert sess.drain_requests == 1, "and the holder's release is asked to wake us"

    @pytest.mark.asyncio
    async def test_once_free_the_message_is_answered_once_with_one_notice(self) -> None:
        """The other half: leaving the queue alone must not strand the message."""
        d, cli, sess = _dispatcher({7}, dm_scope="unified")
        replayed: list[Any] = []

        async def _spy(msg: Any, **kw: Any) -> None:
            replayed.append(msg)

        await _queue_one(d, sess, "hello")
        d.handle_message = _spy  # type: ignore[method-assign]
        await asyncio.wait_for(d._drain_queue(_KEY), timeout=5)  # held: deferred
        sess._busy = False  # the holder released
        await asyncio.wait_for(d._drain_queue(_KEY), timeout=5)

        assert [m.text for m in replayed] == ["hello"], "answered exactly once"
        assert len(_now_answering(cli)) == 1, "with exactly one notice"
        assert sess.queued == []

    @pytest.mark.asyncio
    async def test_a_free_session_drains_without_asking_for_a_wake(self) -> None:
        """The guard against fixing the loop by never draining: the ordinary path."""
        d, cli, sess = _dispatcher({7}, dm_scope="unified")
        replayed: list[Any] = []

        async def _spy(msg: Any, **kw: Any) -> None:
            replayed.append(msg)

        await _queue_one(d, sess, "hello")
        sess._busy = False
        d.handle_message = _spy  # type: ignore[method-assign]
        await asyncio.wait_for(d._drain_queue(_KEY), timeout=5)

        assert [m.text for m in replayed] == ["hello"]
        assert sess.drain_requests == 0

    @pytest.mark.asyncio
    async def test_the_real_replay_path_does_not_loop_on_a_held_session(self) -> None:
        """End to end through the production ``handle_message`` and ``_handle_busy``.

        Before the fix the pump dequeued the message and replayed it into the held
        session; in production that replay requeues it and the pump repeats. Here the
        first such dequeue already fails the test, and the lap cap and bounded wait
        keep a regressed loop from hanging it.
        """
        d, cli, sess = _dispatcher({7}, dm_scope="unified")
        await _queue_one(d, sess, "hello")
        laps = 0
        real_dequeue = sess.dequeue

        def _counted(key: str) -> Any:
            nonlocal laps
            item = real_dequeue(key)
            if item is not None:
                laps += 1
            if laps > 50:
                sess._busy = False  # let a regressed loop end so the test can fail
            return item

        sess.dequeue = _counted  # type: ignore[method-assign]
        await asyncio.wait_for(d._drain_queue(_KEY), timeout=30)

        assert laps == 0, "nothing is dequeued while the session is held"
        assert _now_answering(cli) == []
        assert [e[1] for e in sess.queued] == ["hello"]


class TestTheDrainIsNotReEntered:
    @pytest.mark.asyncio
    async def test_a_second_drain_of_the_same_channel_is_retained_not_run(self) -> None:
        """A turn tail reaching the drain while a release-woken drain of the same
        channel is still pumping must not start a second pump on the same queue."""
        rounds: list[int] = []
        inner_ran: list[bool] = []

        async def _inner_pump(foreign: set[str]) -> None:
            inner_ran.append(True)

        async def _outer_pump(foreign: set[str]) -> None:
            rounds.append(len(rounds))
            if len(rounds) == 1:
                await queue_drain.drain_until_quiet(
                    channel="telegram", session_key=_KEY, pump=_inner_pump
                )

        await queue_drain.drain_until_quiet(channel="telegram", session_key=_KEY, pump=_outer_pump)

        assert inner_ran == [], "the nested drain did not pump"
        assert rounds == [0, 1], "the running drain re-pumped once for it instead"
        assert _KEY not in queue_drain._ACTIVE, "and the marker is released at the end"


def _factory():
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.cwd = ""
        m.disown_work_dir = MagicMock()
        m.memory_mode = "persistent"
        m.is_process_alive = lambda: True
        m.context_usage_pct = lambda: 10.0
        m.context_usage_unknown = lambda: False
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: False
        m.runtime_info = lambda: (None, None)
        return m

    return factory


class TestTheHoldersReleaseWakesTheDrain:
    @pytest.mark.asyncio
    async def test_the_request_is_refused_when_no_lease_is_held(self) -> None:
        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory())
        try:
            await mgr.get_or_create(_KEY)
            key = mgr._fold_key(_KEY)
            mgr.release(key)
            assert mgr.request_drain_on_release(key) is False, "nothing would come back"
            assert mgr.request_drain_on_release("unified:nobody") is False
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_release_after_a_request_wakes_the_queues_channels_once(
        self, monkeypatch
    ) -> None:
        woke: list[tuple[str, list[str]]] = []

        async def _wake(*, waker, session_key, channels):
            woke.append((session_key, list(channels)))

        monkeypatch.setattr(sa, "wake_other_drains", _wake)
        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory())
        try:
            await mgr.get_or_create(_KEY)  # another turn holds the lease
            key = mgr._fold_key(_KEY)
            mgr._sessions[key].queue.append(
                ("ts-1", "hello", {"queued_owner": "7", "queued_channel": "telegram"})
            )
            assert mgr.request_drain_on_release(key) is True
            mgr.release(key)
            for _ in range(20):
                await asyncio.sleep(0)
            assert woke == [(key, ["telegram"])], "woken after the lease is free"

            await mgr.get_or_create(_KEY)
            mgr.release(key)
            for _ in range(20):
                await asyncio.sleep(0)
            assert len(woke) == 1, "a request is answered by one release, not every one"
        finally:
            await mgr.close_all()
