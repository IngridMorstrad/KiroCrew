"""Shared Crew repair: The TypeScript capabilities mirror carries reasons and enterprise, and every reason code."""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.shared_crew.admission import (
    ReasonCode,
)

REPO = Path(__file__).resolve().parent.parent


# -- 5. the TypeScript mirror carries reasons and enterprise ------------------------------


def test_typescript_capabilities_mirror_names_reasons_and_enterprise():
    from kiro_crew.shared_crew.capabilities import REASON_NO_SOURCE, REASON_SOURCE_FAILED
    from kiro_crew.shared_crew.enterprise import ENTERPRISE_CAPABILITY_KEYS, UnavailableReason

    source = (REPO / "website" / "src" / "api" / "sharedCrew.ts").read_text(encoding="utf-8")
    block = source.split("export interface SharedCrewCapabilities", 1)[1].split("\n}\n", 1)[0]
    assert re.search(r"^\s+reasons: ", block, re.M)
    assert re.search(r"^\s+enterprise: ", block, re.M)

    def union(name: str) -> set[str]:
        body = source.split(f"export type {name} =", 1)[1].split("\n\n", 1)[0]
        return set(re.findall(r"'([a-z_]+)'", body))

    assert union("EnterpriseCapabilityKey") == set(ENTERPRISE_CAPABILITY_KEYS)
    assert union("EnterpriseUnavailableReason") == {r.value for r in UnavailableReason}
    from kiro_crew.shared_crew.access import FORWARDED_ATTRIBUTION_REASONS

    assert union("CapabilityReason") == {
        REASON_NO_SOURCE,
        REASON_SOURCE_FAILED,
        *FORWARDED_ATTRIBUTION_REASONS,
    }
    assert union("ReasonCode") == {r.value for r in ReasonCode}


def test_typescript_mirror_names_setup_paths_identity_classes_and_heartbeat_health():
    from kiro_crew.shared_crew.access import SETUP_PATHS, IdentityClass
    from kiro_crew.shared_crew.enterprise import HeartbeatState

    source = (REPO / "website" / "src" / "api" / "sharedCrew.ts").read_text(encoding="utf-8")

    def union(name: str) -> set[str]:
        body = source.split(f"export type {name} =", 1)[1].split("\n\n", 1)[0]
        return set(re.findall(r"'([a-z_]+)'", body))

    assert union("SetupPath") == set(SETUP_PATHS)
    assert union("IdentityClass") == {c.value for c in IdentityClass}
    assert union("HeartbeatHealth") == {s.value for s in HeartbeatState}
    block = source.split("export interface SharedCrewCapabilities", 1)[1].split("\n}\n", 1)[0]
    for key in ("setup_paths", "access_modes", "workload_identity"):
        assert re.search(rf"^\s+{key}\?: ", block, re.M)
