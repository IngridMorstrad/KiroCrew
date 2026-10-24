"""The dashboard's Shared Crew admission sub-section: ``SharedCrewConfig`` and its parser.

``dashboard.shared_crew`` sizes the fair admission gate (``kiro_crew.shared_crew``
``admission_gate``) and the per-principal containment slices. Every value is
bounded at parse time, and ``effective_limits`` is the one place an ``auto``
value is resolved, so the capacity API reports exactly what the gate enforces.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.config.fields import _meta, _safe_bool, _safe_dict, _safe_int
from kiro_crew.config.sections_defaults import SectionReader

logger = logging.getLogger("kiro_crew.config.loader")

#: Bounds for every integer knob, ``(lo, hi)``. A value outside is clamped.
SHARED_CREW_BOUNDS: dict[str, tuple[int, int]] = {
    "principal_concurrency": (1, 256),
    "principal_child_limit": (1, 1024),
    "principal_queue_depth": (0, 1024),
    "crew_concurrency": (0, 1024),
    "crew_child_limit": (0, 4096),
    "crew_queue_depth": (1, 8192),
    "queue_expiry_seconds": (10, 86400),
    "root_deadline_seconds": (60, 86400),
    "child_deadline_seconds": (60, 86400),
    "max_request_bytes": (1024, 64 * 1024 * 1024),
    "session_creates_per_minute": (1, 10000),
    "principal_storage_mb": (1, 1024 * 1024),
    "principal_cpu_quota_percent": (0, 100000),
    "principal_memory_max_mb": (0, 16 * 1024 * 1024),
    "principal_tasks_max": (0, 4194304),
    "crew_cpu_quota_percent": (0, 100000),
    "typing_idle_seconds": (3, 60),
}


@dataclass
class SharedCrewConfig:
    """Fair admission and per-principal containment for a Crew several people use."""

    admission_enabled: bool = field(
        default=False,
        metadata=_meta(
            "Fair Admission",
            "Charge every turn, subagent, task, workflow and cron run to the person "
            "who started it, and admit work through per-person and Crew-wide limits "
            "with a fair queue. Off by default: a single-operator Crew keeps its "
            "current behaviour and reports queueing as unavailable.",
        ),
    )
    principal_concurrency: int = field(
        default=4,
        metadata=_meta(
            "Active Work Per Person",
            "Root executions (turns, tasks, workflows, cron runs) one person may run "
            "at once, across all tabs, devices and sessions.",
        ),
    )
    principal_child_limit: int = field(
        default=8,
        metadata=_meta(
            "Outstanding Subagents Per Person",
            "Subagents one person may have running or queued at once, across every "
            "session. A spawn past it is refused with principal_queue_full.",
        ),
    )
    principal_queue_depth: int = field(
        default=16,
        metadata=_meta(
            "Queue Depth Per Person",
            "Root executions one person may have waiting. A send past it is refused "
            "with principal_queue_full.",
        ),
    )
    crew_concurrency: int = field(
        default=0,
        metadata=_meta(
            "Active Work Crew-Wide",
            "Root executions the whole Crew runs at once. 0 uses the session start "
            "gate's automatic width for this host.",
        ),
    )
    crew_child_limit: int = field(
        default=0,
        metadata=_meta(
            "Outstanding Subagents Crew-Wide",
            "Subagents running or queued across the Crew. 0 means twice the "
            "Crew-wide active limit.",
        ),
    )
    crew_queue_depth: int = field(
        default=64,
        metadata=_meta(
            "Queue Depth Crew-Wide",
            "Root executions waiting across the Crew. A send past it is refused with "
            "crew_queue_full.",
        ),
    )
    queue_expiry_seconds: int = field(
        default=300,
        metadata=_meta(
            "Queue Expiry",
            "Seconds a queued execution waits before it expires unstarted.",
        ),
    )
    root_deadline_seconds: int = field(
        default=1800,
        metadata=_meta(
            "Turn Deadline",
            "Seconds a root execution may run before it is cancelled with run_deadline.",
        ),
    )
    child_deadline_seconds: int = field(
        default=600,
        metadata=_meta(
            "Subagent Deadline",
            "Seconds a subagent or tool execution admitted through the gate may run.",
        ),
    )
    max_request_bytes: int = field(
        default=4 * 1024 * 1024,
        metadata=_meta(
            "Request Size Limit",
            "Largest accepted execution request body, in bytes.",
        ),
    )
    session_creates_per_minute: int = field(
        default=30,
        metadata=_meta(
            "Session Creates Per Minute",
            "Sessions one person may create per minute.",
        ),
    )
    principal_storage_mb: int = field(
        default=2048,
        metadata=_meta(
            "Accounted Storage Per Person",
            "Megabytes of accepted request payload, uploads and logs charged to one "
            "person before new work is refused with storage_limit. Application "
            "accounting, not a filesystem quota.",
        ),
    )
    containment_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Per-Person Slices",
            "While fair admission is on, start each person's agent runtimes in that "
            "person's own systemd slice so their CPU, memory and process counts are "
            "bounded together. Linux with cgroup v2 delegation only.",
        ),
    )
    hard_limits: bool = field(
        default=False,
        metadata=_meta(
            "Require Enforced Limits",
            "Refuse to admit work when cgroup v2, a trusted systemd-run, a reachable "
            "user bus, or memory and pids delegation is missing, instead of running "
            "without per-person limits. Needs fair admission on: with fair admission "
            "off, no agent runtime starts. Off by default.",
        ),
    )
    principal_cpu_quota_percent: int = field(
        default=800,
        metadata=_meta(
            "CPU Per Person",
            "CPUQuota of a person's slice, in percent of one core. 0 emits none.",
        ),
    )
    principal_memory_max_mb: int = field(
        default=24576,
        metadata=_meta(
            "Memory Per Person",
            "MemoryMax of a person's slice, in megabytes. 0 emits none.",
        ),
    )
    principal_tasks_max: int = field(
        default=8192,
        metadata=_meta(
            "Processes Per Person",
            "TasksMax of a person's slice. 0 emits none.",
        ),
    )
    crew_cpu_quota_percent: int = field(
        default=2400,
        metadata=_meta(
            "CPU Crew-Wide",
            "CPUQuota of this Crew's agents slice, in percent of one core. 0 emits none.",
        ),
    )
    tailnet_direct: bool = field(
        default=True,
        metadata=_meta(
            "Direct Tailnet Link",
            "While Tailscale runs, also listen on this machine's own Tailscale "
            "addresses (100.x and fd7a:) on the dashboard port, and let in only "
            "people Tailscale names whose login is on dashboard.tailscale."
            "allowed_logins (only you when that list is empty), without a "
            "dashboard token. Never binds any other address. Off restores the "
            "loopback-only listener. A value that is not true or false reads as "
            "off. Takes effect on the next gateway start.",
            restart=True,
        ),
    )
    typing_idle_seconds: int = field(
        default=10,
        metadata=_meta(
            "Typing Lock Idle Window",
            "Seconds a collaborator may pause typing before the other collaborators' "
            "composers unlock. While someone types in a shared session, everyone "
            "else's composer is read-only until that person sends, clears the "
            "draft, or stays idle this long. 3 to 60.",
        ),
    )


#: The boolean knobs; every other field is an integer bounded by SHARED_CREW_BOUNDS.
SHARED_CREW_FLAGS = ("admission_enabled", "containment_enabled", "hard_limits")
#: Flags that widen what can reach this machine. A value that is not a boolean
#: reads as False, so a typo can only narrow exposure.
SHARED_CREW_EXPOSURE_FLAGS = ("tailnet_direct",)


def shared_crew_config_from(raw: object) -> SharedCrewConfig:
    """Build a bounded :class:`SharedCrewConfig` from a raw ``dashboard.shared_crew`` value.

    A non-object reads as every default. A wrong-typed key reads as its default,
    and an out-of-range integer is clamped into :data:`SHARED_CREW_BOUNDS`, so a
    hand edit can narrow or widen a limit but never disable one.
    """
    data = _safe_dict(raw)
    section = SectionReader(SharedCrewConfig, data)
    values: dict[str, Any] = {}
    for name in SHARED_CREW_FLAGS:
        values[name] = section.read(name, _safe_bool)
    for name in SHARED_CREW_EXPOSURE_FLAGS:
        if name not in data:
            values[name] = section.default(name)
            continue
        raw = data.get(name)
        if not isinstance(raw, bool):
            logger.warning(
                "config: 'dashboard.shared_crew.%s' is not true or false (got %s); "
                "reading it as false",
                name,
                type(raw).__name__,
            )
        values[name] = raw is True
    for name in SHARED_CREW_BOUNDS:
        lo, hi = SHARED_CREW_BOUNDS[name]
        values[name] = section.read(name, _safe_int, lo, hi)
    return SharedCrewConfig(**values)


@dataclass(frozen=True)
class EffectiveLimits:
    """The limits the admission gate enforces, with every ``auto`` resolved."""

    enabled: bool
    principal_active: int
    principal_children: int
    principal_queue: int
    crew_active: int
    crew_children: int
    crew_queue: int
    queue_expiry_seconds: int
    root_deadline_seconds: int
    child_deadline_seconds: int
    max_request_bytes: int
    session_creates_per_minute: int
    principal_storage_bytes: int
    containment_enabled: bool
    hard_limits: bool
    principal_cpu_quota_percent: int
    principal_memory_max_mb: int
    principal_tasks_max: int
    crew_cpu_quota_percent: int

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def effective_limits(cfg: SharedCrewConfig, *, auto_crew_concurrency: int) -> EffectiveLimits:
    """Resolve *cfg* against the host's automatic Crew width.

    ``crew_concurrency == 0`` takes *auto_crew_concurrency* (the session start
    gate's own automatic value), ``crew_child_limit == 0`` takes twice the
    resolved Crew width, and a per-person limit is never wider than the Crew's.
    """
    crew_active = cfg.crew_concurrency or max(1, int(auto_crew_concurrency))
    crew_children = cfg.crew_child_limit or 2 * crew_active
    return EffectiveLimits(
        enabled=cfg.admission_enabled,
        principal_active=min(cfg.principal_concurrency, crew_active),
        principal_children=min(cfg.principal_child_limit, crew_children),
        principal_queue=min(cfg.principal_queue_depth, cfg.crew_queue_depth),
        crew_active=crew_active,
        crew_children=crew_children,
        crew_queue=cfg.crew_queue_depth,
        queue_expiry_seconds=cfg.queue_expiry_seconds,
        root_deadline_seconds=cfg.root_deadline_seconds,
        child_deadline_seconds=cfg.child_deadline_seconds,
        max_request_bytes=cfg.max_request_bytes,
        session_creates_per_minute=cfg.session_creates_per_minute,
        principal_storage_bytes=cfg.principal_storage_mb * 1024 * 1024,
        containment_enabled=cfg.containment_enabled,
        hard_limits=cfg.hard_limits,
        principal_cpu_quota_percent=cfg.principal_cpu_quota_percent,
        principal_memory_max_mb=cfg.principal_memory_max_mb,
        principal_tasks_max=cfg.principal_tasks_max,
        crew_cpu_quota_percent=cfg.crew_cpu_quota_percent,
    )


def load_shared_crew_config() -> SharedCrewConfig:
    """Read ``dashboard.shared_crew`` from ``config.json`` now. Synchronous file I/O."""
    import json

    from kiro_crew.config.loader import config_path, read_config_text

    try:
        raw = json.loads(read_config_text(config_path()))
    except FileNotFoundError:
        return SharedCrewConfig()
    if not isinstance(raw, dict):
        raise ValueError("config must be an object")
    dashboard = raw.get("dashboard", {})
    if not isinstance(dashboard, dict):
        raise ValueError("dashboard must be an object")
    section = dashboard.get("shared_crew", {})
    if not isinstance(section, dict):
        raise ValueError("dashboard.shared_crew must be an object")
    return shared_crew_config_from(section)


__all__ = [
    "EffectiveLimits",
    "SHARED_CREW_BOUNDS",
    "SHARED_CREW_EXPOSURE_FLAGS",
    "SHARED_CREW_FLAGS",
    "SharedCrewConfig",
    "effective_limits",
    "load_shared_crew_config",
    "shared_crew_config_from",
]
