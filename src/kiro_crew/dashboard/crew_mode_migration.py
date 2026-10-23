"""One switch for every Crew Mode chat: move every Crew Mode root onto the configured
conductor when ``agent.crew_mode_agent`` changes, or when an update ships a new default.

``agent.crew_mode_agent`` is the one place that names the conductor
(``kiro_crew.crew_mode``). The name last applied is kept in
``<data home>/crew-mode-conductor.json``; when the effective name differs from it (the
key was edited, or an update changed the shipped default), every Crew Mode root, new
and old, is switched onto the new conductor through the ordinary agent switch
(``switch_slot_agent`` with the migration caller, the one switch the Crew Mode lock
lets through). A root that is busy is left for its next idle point and retried; a
turn is never cut. The work ledger is keyed on the session, not the agent, so the new
conductor reads the same items. Each switch writes one audit line
(``crew_mode.migrate``). With no file yet (the first start with this code) the
current name is recorded and nothing moves: the existing roots stay on the conductor
they run, and are Crew Mode there.

The same start pass converts existing conductor sessions on load: a live root that
runs a conductor but carries no Crew Mode stamp gets one (and its metadata line), and
every closed root found in saved session metadata is recorded as a known root
(``crew_mode.note_roots``), so its workers stay out of the person's lists and History.
Nothing is deleted, filed or moved by that pass.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The file under the data home that records the conductor last applied.
STATE_FILENAME = "crew-mode-conductor.json"

#: Seconds between retries of roots that were busy when a migration ran.
RETRY_SECS = 15.0

#: Root slot keys still owed a move onto the configured conductor.
_pending: set[str] = set()


def _state_path() -> Any:
    from kiro_crew.config.paths import data_home

    return data_home() / STATE_FILENAME


def read_applied() -> str | None:
    """The conductor last applied, or ``None`` when nothing was ever recorded. BLOCKING."""
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = raw.get("applied") if isinstance(raw, dict) else None
    return value if isinstance(value, str) and value else None


def write_applied(agent: str) -> None:
    """Record *agent* as the conductor every root has been moved onto. BLOCKING."""
    from kiro_crew.atomic_write import atomic_write

    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps({"applied": agent}) + "\n", mode=0o600)


def pending() -> frozenset[str]:
    """Roots still owed a move (busy when the migration ran)."""
    return frozenset(_pending)


def _roots(state: Any) -> list[Any]:
    from kiro_crew import crew_mode

    return [s for s in list(state._slots.values()) if crew_mode.slot_is_crew_mode_root(s)]


def _busy(slot: Any) -> bool:
    return bool(getattr(slot, "running", False) or getattr(slot, "turn_running", False))


def _audit(slot_key: str, before: str, after: str, outcome: str, error: str = "") -> None:
    from kiro_crew.sel import sel

    try:
        sel().log_api_access(
            caller="crew_mode_migration",
            operation="crew_mode.migrate",
            outcome=outcome,
            source="dashboard",
            resources=f"slot={slot_key} from={before} to={after}",
            error=error,
        )
    except Exception:
        logger.debug("crew mode migration audit failed", exc_info=True)


async def _move(state: Any, slot: Any, target: str) -> bool:
    """Switch one idle root onto *target*. ``True`` when it is there (or already was)."""
    from kiro_crew.dashboard.chat_handlers import (
        SlotAgentSwitchCaller,
        agent_switch_outcome,
        switch_slot_agent,
    )

    if slot.agent == target:
        return True
    if _busy(slot):
        return False
    before = str(slot.agent or "")
    resp = await switch_slot_agent(
        state, slot, slot.key, target, "template", SlotAgentSwitchCaller.for_crew_mode_migration()
    )
    ok, body = agent_switch_outcome(resp)
    if ok:
        _audit(slot.key, before, target, "ok")
        try:
            state.push_slots_update()
        except Exception:
            logger.debug("crew mode migration: push_slots_update failed", exc_info=True)
        return True
    code = str(body.get("code") or body.get("error") or resp.status)
    if code == "turn_in_flight":
        return False
    _audit(slot.key, before, target, "error", code)
    logger.warning("crew mode migration: %s not moved onto %s (%s)", slot.key, target, code)
    # A refusal other than a busy turn will not change by retrying; drop it so one
    # broken session cannot keep the migration open forever.
    return True


async def migrate(state: Any, target: str | None = None) -> dict[str, list[str]]:
    """Move every live Crew Mode root onto *target* (default: the configured one).

    Idle roots move now; busy ones join :func:`pending` for the retry loop. Returns
    ``{"moved": [...], "pending": [...]}``. The recorded name is advanced only once
    nothing is pending, so a restart before then migrates the rest.
    """
    from kiro_crew import crew_mode

    target = target or crew_mode.crew_mode_agent()
    moved: list[str] = []
    for slot in _roots(state):
        if slot.agent == target:
            _pending.discard(slot.key)
            continue
        before = slot.agent
        if await _move(state, slot, target):
            _pending.discard(slot.key)
            if slot.agent != before:
                moved.append(slot.key)
        else:
            _pending.add(slot.key)
    live = {s.key for s in list(state._slots.values())}
    _pending.intersection_update(live)
    if not _pending:
        await asyncio.to_thread(write_applied, target)
    return {"moved": moved, "pending": sorted(_pending)}


async def reconcile(state: Any) -> dict[str, list[str]] | None:
    """Migrate when the configured conductor differs from the one last applied.

    ``None`` when there was nothing to do. A first start (nothing recorded) records
    the current name and moves nothing.
    """
    from kiro_crew import crew_mode

    target = crew_mode.crew_mode_agent()
    applied = await asyncio.to_thread(read_applied)
    if applied is None:
        await asyncio.to_thread(write_applied, target)
        return None
    if applied == target and not _pending:
        return None
    return await migrate(state, target)


def _stamp_live_roots(state: Any) -> list[str]:
    """Stamp every live conductor root that has no Crew Mode stamp yet. In memory only.

    Returns the keys stamped; the caller persists their metadata line off the loop.
    """
    from kiro_crew import crew_mode

    stamped: list[str] = []
    for slot in _roots(state):
        if not getattr(slot, crew_mode.STAMP_FIELD, ""):
            slot.crew_mode_agent = str(slot.agent or "")
            stamped.append(slot.key)
    return stamped


def history_slot_key(key: str) -> str:
    """A saved session's stem (``dashboard_chat-3-1792...``) as its slot key (``chat-3-1792...``)."""
    out = key
    while True:
        for prefix in ("dashboard_", "dashboard:"):
            if out.startswith(prefix):
                out = out[len(prefix) :]
                break
        else:
            return out


def _closed_roots(log: Any) -> list[str]:
    """Slot keys of saved sessions whose metadata says they are Crew Mode roots. BLOCKING."""
    from kiro_crew import crew_mode

    conductors = crew_mode.conductor_agents()
    found: list[str] = []
    for row in log.list_sessions():
        key = str(row.get("key") or "")
        if not key or (row.get("agent") not in conductors and not row.get(crew_mode.STAMP_FIELD)):
            continue
        try:
            meta = log.get_metadata(key)
        except Exception:
            continue
        if isinstance(meta, dict) and crew_mode.saved_row_is_root(meta):
            found.append(history_slot_key(key))
    return found


async def on_start(state: Any) -> None:
    """The start pass: convert conductor sessions, learn closed roots, then reconcile."""
    from kiro_crew import crew_mode
    from kiro_crew.dashboard.chat_utils import slot_history_key

    stamped = _stamp_live_roots(state)
    log = getattr(state, "conversation_log", None)
    if log is not None:
        for key in stamped:
            slot = state._slots.get(key)
            if slot is None:
                continue
            try:
                await asyncio.to_thread(
                    log.update_metadata,
                    slot_history_key(slot),
                    {crew_mode.STAMP_FIELD: str(slot.crew_mode_agent or "")},
                )
            except Exception:
                logger.debug("crew mode: could not persist the stamp for %s", key, exc_info=True)
        try:
            crew_mode.note_roots(await asyncio.to_thread(_closed_roots, log))
        except Exception:
            logger.debug("crew mode: closed roots not loaded", exc_info=True)
    if stamped:
        try:
            state.push_slots_update()
        except Exception:
            logger.debug("crew mode: push_slots_update failed", exc_info=True)
    await reconcile(state)


async def run(state: Any, *, start_delay: float = 2.0) -> None:
    """The gateway's background task: the start pass, then retry busy roots."""
    await asyncio.sleep(start_delay)
    try:
        await on_start(state)
    except Exception:
        logger.warning("crew mode start pass failed", exc_info=True)
    while True:
        await asyncio.sleep(RETRY_SECS)
        if not _pending:
            continue
        try:
            await migrate(state)
        except Exception:
            logger.warning("crew mode migration retry failed", exc_info=True)


async def on_config_change(state: Any, cfg: Any = None) -> None:
    """``agent.crew_mode_agent`` changed: move every root onto the new conductor.

    *cfg* is the new config the watcher loaded; it is remembered first so every
    reader (the lock, the slot list) agrees on the new name before a root moves.
    """
    from kiro_crew import crew_mode

    if cfg is not None:
        crew_mode.remember(cfg)
    try:
        await reconcile(state)
    except Exception:
        logger.warning("crew mode migration after a config change failed", exc_info=True)
