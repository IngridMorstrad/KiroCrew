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

#: Root slot keys still owed a move onto the configured conductor: every root not on
#: it when a migration starts, so a cancel before a root is visited still leaves it
#: owed, and every root a retryable refusal sent back.
_pending: set[str] = set()

#: ``{root slot key: refusal code}`` for roots a permanent refusal kept off the
#: target. They are not retried by the idle loop; a config change or a restart tries
#: them again. While any is here the recorded name is not advanced.
_failed: dict[str, str] = {}

#: Refusals that clear on their own once the root is idle again: a turn, attached
#: subagents (a conductor between turns), a binding that moved mid-switch, a
#: metadata write that failed.
RETRYABLE: frozenset[str] = frozenset(
    {"turn_in_flight", "slot_subagents_running", "session_rebound", "history_unavailable"}
)

#: One migration at a time: the config watcher, the idle retry loop and the start
#: pass all call :func:`migrate`, and two of them on one idle root would reset its
#: fresh conductor session twice.
_lock: asyncio.Lock | None = None


#: ``{configured name: why it cannot run Crew Mode, "" when it can}``, so the idle
#: loop does not re-read the config every tick. Cleared on every config change.
_verdicts: dict[str, str] = {}


def _migration_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


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


def failed() -> dict[str, str]:
    """Roots a permanent refusal kept off the configured conductor, with the code."""
    return dict(_failed)


def _notify(state: Any, title: str, body: str) -> None:
    try:
        state.notify("agent", title, body)
    except Exception:
        logger.debug("crew mode migration notice failed", exc_info=True)


def _usable(target: str) -> str:
    """``""`` when *target* can run Crew Mode, else why it cannot. BLOCKING (config read).

    It must name an agent template that resolves (not a typo), and not a crewmate:
    a Crew Mode root runs a conductor template, never a crewmate.
    """
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.config.loader_agents import resolve_agent_bindings
    from kiro_crew.validation_parts.input_bounds import is_registered_agent_name

    if not is_registered_agent_name(target):
        return "invalid_agent_name"
    try:
        cfg = KiroCrewConfig.load()
    except Exception:
        # The config watcher just loaded this config; a read failure here is
        # transient, and the switch itself re-validates the name.
        return ""
    if target in getattr(cfg, "agents", {}):
        return "agent_is_crewmate"
    try:
        bindings = resolve_agent_bindings(
            cfg, target, None, validate_memory_files=False, selection_kind="template"
        )
    except Exception:
        return "agent_unresolved"
    return "" if bindings.requested_resolved else "agent_not_found"


async def _move(state: Any, slot: Any, target: str) -> str:
    """Switch one idle root onto *target*.

    ``"ok"`` when it is there (or already was), ``"retry"`` when it is busy or a
    refusal that clears on its own came back, else the permanent refusal code.
    """
    from kiro_crew.dashboard.chat_handlers import (
        SlotAgentSwitchCaller,
        agent_switch_outcome,
        switch_slot_agent,
    )

    if slot.agent == target:
        return "ok"
    if _busy(slot):
        return "retry"
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
        return "ok"
    code = str(body.get("code") or body.get("error") or resp.status)
    if code in RETRYABLE:
        return "retry"
    _audit(slot.key, before, target, "error", code)
    logger.warning("crew mode migration: %s not moved onto %s (%s)", slot.key, target, code)
    return code or "error"


async def migrate(
    state: Any, target: str | None = None, *, retry_failed: bool = True
) -> dict[str, list[str]]:
    """Move every live Crew Mode root onto *target* (default: the configured one).

    Idle roots move now; busy ones, and ones a refusal that clears on its own sent
    back, stay in :func:`pending` for the retry loop. A permanent refusal is
    audited, named in one dashboard notice and kept in :func:`failed`; the idle loop
    skips those (*retry_failed* False). Returns ``{"moved": [...], "pending":
    [...]}``. The recorded name is advanced only when every live root is on
    *target*, so a restart before then migrates the rest.
    """
    from kiro_crew import crew_mode

    async with _migration_lock():
        target = target or crew_mode.crew_mode_agent()
        roots = _roots(state)
        if retry_failed:
            _failed.clear()
        # Every root not on the target is owed the move BEFORE any await, so a
        # cancel part-way through still leaves the unvisited ones owed.
        _pending.update(s.key for s in roots if s.agent != target and s.key not in _failed)
        moved: list[str] = []
        newly_failed: dict[str, str] = {}
        for slot in roots:
            if slot.agent == target:
                _pending.discard(slot.key)
                _failed.pop(slot.key, None)
                continue
            if slot.key in _failed:
                continue
            before = slot.agent
            outcome = await _move(state, slot, target)
            if outcome == "ok":
                _pending.discard(slot.key)
                if slot.agent != before:
                    moved.append(slot.key)
            elif outcome == "retry":
                _pending.add(slot.key)
            else:
                _pending.discard(slot.key)
                _failed[slot.key] = outcome
                newly_failed[slot.key] = outcome
        live = {s.key for s in list(state._slots.values())}
        _pending.intersection_update(live)
        for key in [k for k in _failed if k not in live]:
            _failed.pop(key, None)
        if newly_failed:
            names = ", ".join(f"{k} ({c})" for k, c in sorted(newly_failed.items()))
            _notify(
                state,
                "A Crew Mode chat did not move",
                f"These chats stay on their conductor instead of {target}: {names}. "
                "Kiro Crew tries again after the next config change or restart.",
            )
        if not _pending and not _failed and all(s.agent == target for s in _roots(state)):
            await asyncio.to_thread(write_applied, target)
        return {"moved": moved, "pending": sorted(_pending)}


async def reconcile(state: Any, *, retry_failed: bool = True) -> dict[str, list[str]] | None:
    """Migrate when the configured conductor differs from the one last applied.

    ``None`` when there was nothing to do. A first start (nothing recorded) records
    the current name and moves nothing. A configured name that is not a usable
    agent template (a typo, a crewmate) moves nothing: it is logged, named in one
    notice, and every reader keeps the conductor last applied
    (:func:`kiro_crew.crew_mode.set_unusable`).
    """
    from kiro_crew import crew_mode

    configured = crew_mode.configured_crew_mode_agent()
    applied = await asyncio.to_thread(read_applied)
    if configured != applied:
        why = _verdicts.get(configured)
        if why is None:
            why = _verdicts[configured] = await asyncio.to_thread(_usable, configured)
        if why:
            fallback = applied or crew_mode.DEFAULT_CREW_MODE_AGENT
            first = crew_mode.crew_mode_agent() == configured
            crew_mode.set_unusable(configured, fallback)
            if first:
                logger.error(
                    "agent.crew_mode_agent names %r, which cannot run Crew Mode (%s); "
                    "Crew Mode chats stay on %r",
                    configured,
                    why,
                    fallback,
                )
                _audit("*", fallback, configured, "denied", why)
                _notify(
                    state,
                    "Crew Mode conductor not changed",
                    f"agent.crew_mode_agent names {configured}, which is not an agent "
                    f"template ({why}). Crew Mode chats stay on {fallback}.",
                )
            return None
    crew_mode.clear_unusable()
    target = crew_mode.crew_mode_agent()
    if applied is None:
        await asyncio.to_thread(write_applied, target)
        return None
    if applied == target and not _pending:
        return None
    return await migrate(state, target, retry_failed=retry_failed)


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
        try:
            # Roots still owed a move, and a recorded name that is not yet the
            # target (a migration cancelled before it visited every root).
            await reconcile(state, retry_failed=False)
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
    _verdicts.clear()
    try:
        await reconcile(state)
    except Exception:
        logger.warning("crew mode migration after a config change failed", exc_info=True)


def schedule_on_config_change(state: Any, cfg: Any = None) -> asyncio.Task[None]:
    """Start :func:`on_config_change` as a tracked background task and return it.

    The config watcher runs each applier under a 10 s bound and cancels it on
    timeout; a migration resets sessions and can take longer, so it runs off the
    applier, never under that bound. *cfg* is remembered first, synchronously.
    """
    from kiro_crew import crew_mode

    if cfg is not None:
        crew_mode.remember(cfg)
    task = asyncio.create_task(on_config_change(state), name="crew-mode-migration")
    tasks = getattr(state, "_background_tasks", None)
    if isinstance(tasks, set):
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    return task
