"""Shared Crew session attribution: creator, message author, persistence.

Driven through the real slot, the real metadata codec, the real full save and
the real session catalog. ``_AttrSlot`` is a plain subclass of the real
``_ChatSlot``, which declares ``_attribution`` among its slots.
"""

from __future__ import annotations

import json

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import state as state_module
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.slot_persistence import metadata_codec as codec
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history import carry_unowned_metadata
from kiro_crew.shared_crew import attribution as attr
from kiro_crew.shared_crew.contracts import (
    MESSAGE_AUTHOR_META_KEY,
    SESSION_ATTRIBUTION_META_KEY,
    ActorContext,
    AttributionAvailability,
    Principal,
    Provenance,
    SessionAttribution,
    Transport,
)
from kiro_crew.shared_crew.identity import tailnet_principal


class _AttrSlot(_ChatSlot):
    """The real slot plus an instance dict, so ``_attribution`` can be held."""


def _actor(principal: Principal) -> ActorContext:
    return ActorContext(principal=principal, request_id="r-1", transport=Transport.HTTP)


ALICE = tailnet_principal("alice@example.com", "laptop.tail.ts.net", Provenance.TAILSCALE_SERVE)
BOB = tailnet_principal("bob@example.com", "phone.tail.ts.net", Provenance.TAILSCALE_SERVE)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(state_module, "_ChatSlot", _AttrSlot)
    return _make_state(tmp_path)


def _line_on_disk(state, slot) -> dict:
    path = state.conversation_log._path(slot_history_key(slot))
    with open(path, encoding="utf-8") as f:
        return json.loads(f.readline())


def _folds(slot) -> codec.SaveFolds:
    return codec.SaveFolds(memory_mode=slot.memory_mode, created_at=slot.created_at)


def test_creator_is_write_once_whoever_messages_next():
    slot = _AttrSlot("s-immutable")
    first = attr.stamp_creator(slot, _actor(ALICE), now=10.0)
    second = attr.stamp_creator(slot, _actor(BOB), now=20.0)
    assert first.creator == ALICE and second is first
    assert attr.slot_attribution(slot).creator.id == ALICE.id
    assert attr.slot_attribution(slot).created_at == 10.0


def test_an_unknown_creator_is_recorded_as_unattributed_not_as_the_viewer():
    slot = _AttrSlot("s-unknown")
    stamped = attr.stamp_creator(slot, _actor(Principal.unknown()))
    assert stamped.availability is AttributionAvailability.UNATTRIBUTED
    assert stamped.creator.is_unknown


def test_a_legacy_session_stays_legacy_and_is_never_written_back():
    slot = _AttrSlot("s-legacy")
    restored = attr.restore_from_metadata(slot, {"_type": "metadata", "title": "old"})
    assert restored.availability is AttributionAvailability.LEGACY_MISSING
    # A later message by Bob must not make Bob the creator of an old session.
    attr.stamp_creator(slot, _actor(BOB))
    attr.stamp_creator_if_fresh(slot, _actor(BOB), prior_user_turns=0)
    assert attr.slot_attribution(slot).availability is AttributionAvailability.LEGACY_MISSING
    assert attr.metadata_value(slot) is None
    assert SESSION_ATTRIBUTION_META_KEY not in codec.encode(slot, folds=_folds(slot))


def test_first_message_stamps_only_a_slot_with_no_prior_user_turns():
    fresh = _AttrSlot("s-fresh")
    assert attr.stamp_creator_if_fresh(fresh, _actor(ALICE), prior_user_turns=0).creator == ALICE
    started = _AttrSlot("s-started")
    assert attr.stamp_creator_if_fresh(started, _actor(BOB), prior_user_turns=2) is None
    assert attr.slot_attribution(started) is None


def test_the_real_slot_class_holds_the_creator():
    slot = _ChatSlot("s-plain")
    stamped = attr.stamp_creator(slot, _actor(ALICE))
    assert stamped.creator == ALICE
    assert attr.slot_attribution(slot) == stamped
    assert attr.public_attribution_of_slot(slot)["availability"] == "recorded"


def test_full_save_restart_readback_and_catalog_agree(state):
    slot = state.get_or_create_slot("attr-save")
    attr.stamp_creator(slot, _actor(ALICE), now=111.0)
    slot.append("user", "hello", "msg msg-u", meta=attr.stamp_message_author({}, _actor(ALICE)))
    assert _save_slot_to_history(state, slot, force=True)

    line = _line_on_disk(state, slot)
    stored = line[SESSION_ATTRIBUTION_META_KEY]
    assert stored["creator"]["id"] == ALICE.id
    assert stored["availability"] == "recorded"

    # Restart: a fresh slot hydrated from the same line reads the same creator.
    fresh = _AttrSlot("attr-save-restored")
    codec.apply(state, fresh, line, codec.Restore(name="attr-save-restored"))
    assert attr.slot_attribution(fresh) == SessionAttribution.from_dict(stored)

    # Bob saving the same session later does not move the creator on disk.
    attr.stamp_creator(slot, _actor(BOB))
    slot.append("user", "bob here", "msg msg-u", meta=attr.stamp_message_author({}, _actor(BOB)))
    assert _save_slot_to_history(state, slot, force=True)
    assert _line_on_disk(state, slot)[SESSION_ATTRIBUTION_META_KEY]["creator"]["id"] == ALICE.id

    stem = state.conversation_log._path(slot_history_key(slot)).stem
    rows = [r for r in state.conversation_log.list_sessions() if r["key"] == stem]
    assert len(rows) == 1
    public = rows[0]["attribution"]
    assert public["creator"]["id"] == ALICE.id
    # Browser-bound: no issuer or subject in the catalog row.
    assert "subject" not in public["creator"] and "issuer" not in public["creator"]


def test_message_rows_keep_their_own_author_across_save(state):
    slot = state.get_or_create_slot("attr-authors")
    attr.stamp_creator(slot, _actor(ALICE))
    slot.append("user", "a", "msg msg-u", meta=attr.stamp_message_author({}, _actor(ALICE)))
    slot.append("user", "b", "msg msg-u", meta=attr.stamp_message_author({}, _actor(BOB)))
    assert _save_slot_to_history(state, slot, force=True)
    path = state.conversation_log._path(slot_history_key(slot))
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f.read().splitlines()[1:]]
    authors = [attr.message_author(r.get("meta")).id for r in rows if r.get("role") == "user"]
    assert authors == [ALICE.id, BOB.id]


def test_a_client_supplied_author_is_replaced_by_the_server():
    forged = {MESSAGE_AUTHOR_META_KEY: BOB.to_public_dict(), "sendId": "x"}
    attr.stamp_message_author(forged, _actor(ALICE))
    assert forged[MESSAGE_AUTHOR_META_KEY]["id"] == ALICE.id
    assert forged["sendId"] == "x"


def test_a_message_written_before_authors_reads_as_unknown():
    assert attr.message_author({"mid": "m-1"}).is_unknown
    assert attr.message_author(None).is_unknown


def test_a_slot_without_attribution_carries_the_stored_value_through_a_rebuild():
    """The key is owned by no writer, so a save that does not write it keeps it."""
    slot = _ChatSlot("s-carry")
    stored = SessionAttribution(
        creator=ALICE, availability=AttributionAvailability.RECORDED
    ).to_dict()
    rebuilt = codec.encode(slot, folds=_folds(slot))
    assert SESSION_ATTRIBUTION_META_KEY not in rebuilt
    carried = carry_unowned_metadata(
        rebuilt, {"_type": "metadata", SESSION_ATTRIBUTION_META_KEY: stored}, owned=set()
    )
    assert carried[SESSION_ATTRIBUTION_META_KEY] == stored


@pytest.mark.parametrize(
    "damaged",
    [
        "not-an-object",
        {"creator": {"id": "not a principal id", "kind": "human"}, "availability": "recorded"},
        {"creator": ALICE.to_dict(), "availability": "recorded", "schema_version": "x"},
        {"creator": ALICE.to_dict(), "availability": "recorded", "created_at": "noon"},
    ],
    ids=["scalar", "bad-principal-id", "bad-version", "bad-timestamp"],
)
def test_a_damaged_attribution_reads_without_raising_and_never_names_someone_else(damaged):
    slot = _AttrSlot("s-damaged")
    value = attr.restore_from_metadata(slot, {SESSION_ATTRIBUTION_META_KEY: damaged})
    assert value.creator.id in (ALICE.id, Principal.unknown().id)
    assert value.creator.id != BOB.id


def test_a_torn_metadata_line_lists_the_session_as_unknown(state):
    path = state.conversation_log._path("dashboard_torn")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"_type": "metadata", "attribution": {"creator": {"id": "p_', encoding="utf-8")
    rows = [r for r in state.conversation_log.list_sessions() if r["key"] == "dashboard_torn"]
    assert rows and rows[0]["attribution"]["availability"] == "legacy_missing"
    assert rows[0]["attribution"]["creator"]["kind"] == "unknown"


def test_unknown_fields_inside_attribution_survive_a_read_and_write():
    raw = SessionAttribution(creator=ALICE, availability=AttributionAvailability.RECORDED).to_dict()
    raw["future_field"] = {"kept": True}
    raw["creator"]["future_principal_field"] = 7
    slot = _AttrSlot("s-future")
    attr.restore_from_metadata(slot, {SESSION_ATTRIBUTION_META_KEY: raw})
    written = attr.metadata_value(slot)
    assert written["future_field"] == {"kept": True}
    assert written["creator"]["future_principal_field"] == 7


def test_a_display_rename_keeps_the_principal_id():
    renamed = tailnet_principal("Alice@Example.com", "laptop", Provenance.TAILSCALE_SERVE)
    assert renamed.id == ALICE.id
    assert renamed.display.name != ALICE.display.name
