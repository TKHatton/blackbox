"""Tiering tests: the three shelves, and the guarantee that moving loses nothing.

The Phase 1.5 failure mode this closes: tiering implemented as a copy without a
delete, so Firestore keeps growing anyway. The previous implementation printed a
line and returned zero.

The strongest test here is not that events move. It is that folding a case
produces byte-identical state before and after tiering, which is the property
the whole system rests on.
"""

from datetime import datetime, timedelta, timezone

import pytest

from blackbox.backends import InMemoryBackend
from blackbox.event_store import EventStore
from blackbox.fold import fold_events
from blackbox.recorder import Recorder
from blackbox.schema import EventType
from blackbox.shelves import (
    InMemoryColdShelf,
    InMemoryWarmShelf,
    from_row,
    parquet_to_rows,
    rows_to_parquet,
    to_row,
)
from blackbox.tiering import TieringManager, events_match


@pytest.fixture
def tiering(store):
    return TieringManager(
        project_id="blackbox-test",
        event_store=store,
        hot_ttl_days=7,
        cold_ttl_days=365,
        in_memory=True,
    )


def seed_case(store: EventStore, case_id: str = "CASE-TIER", n: int = 6):
    """A case with a mix of event types, including a suspend and a resume."""
    rec = Recorder(case_id=case_id, actor="intake_agent", store=store)
    root = rec.tool_call(
        tool_name="IntakeChannel.poll", parameters={"channel": "web_form"},
        intended_outcome="Collect complaints",
    )
    rec.set_cause(root)
    rec.thought("Reasoning about the case.", "call lookup_customer", 0.8, "intake")
    rec.tool_result(tool_name="lookup_customer", success=True, result={"name": "A"})
    rec.policy_check("gate_a", "approval_threshold", {"amount": 300}, "allow", "under")
    rec.memory_write(memory_key=f"wiki:case:{case_id}", content={"status": "open"}, reason="opened")
    return rec, root


def age_all_events(store: EventStore, days: int) -> None:
    """Backdate every event on the desk, so a tiering run has something to move."""
    backend = store._backend
    for row in list(backend.query(store._collection, filters=[])):
        aged = datetime.fromisoformat(row["timestamp"]) - timedelta(days=days)
        row["timestamp"] = aged.isoformat()
        backend._data[store._collection][row["event_id"]] = row


# ----------------------------------------------------------------------
# The row format round-trips
# ----------------------------------------------------------------------


def test_a_row_round_trips_through_the_shelf_format(store):
    """Verification compares what came back, so the format must be lossless."""
    seed_case(store)
    for event in store.list_events("CASE-TIER"):
        original = event.to_firestore_dict()
        assert from_row(to_row(original)) == original


def test_parquet_round_trips(store):
    """The Warehouse format has to read back a year from now."""
    seed_case(store)
    rows = [to_row(e.to_firestore_dict()) for e in store.list_events("CASE-TIER")]
    restored = parquet_to_rows(rows_to_parquet(rows))

    assert len(restored) == len(rows)
    assert {r["event_id"] for r in restored} == {r["event_id"] for r in rows}
    for original, back in zip(rows, sorted(restored, key=lambda r: r["event_id"])):
        assert from_row(back)["payload"] == from_row(original)["payload"]


def test_events_match_tolerates_representation_differences():
    """BigQuery returns a datetime where Firestore held a string."""
    when = datetime.now(timezone.utc)
    desk = {"event_id": "E1", "timestamp": when.isoformat(), "payload": {"a": 1},
            "actor": "x", "event_type": "THOUGHT", "caused_by": None, "labels": {},
            "trace_id": "t", "span_id": "s", "case_id": "C"}
    shelf = dict(desk, timestamp=when)
    assert events_match(desk, shelf)


def test_events_match_rejects_a_changed_payload():
    """A partial comparison would let a corrupted payload through."""
    base = {"event_id": "E1", "timestamp": "2026-01-01T00:00:00+00:00",
            "payload": {"a": 1}, "actor": "x", "event_type": "THOUGHT",
            "caused_by": None, "labels": {}, "trace_id": "t", "span_id": "s",
            "case_id": "C"}
    assert not events_match(base, dict(base, payload={"a": 2}))
    assert not events_match(base, dict(base, actor="someone_else"))
    assert not events_match(base, dict(base, caused_by="E0"))


# ----------------------------------------------------------------------
# Copy, verify, then evict
# ----------------------------------------------------------------------


def test_recent_events_are_left_alone(store, tiering):
    """The Desk holds recent events. Only aged ones move."""
    seed_case(store)
    report = tiering.tier_to_filing_cabinet()

    assert report.examined == 0
    assert report.evicted == 0
    assert len(store.list_events("CASE-TIER")) == 5


def test_aged_events_move_and_the_desk_shrinks(store, tiering):
    """The named failure mode: a copy without a delete, so Firestore keeps growing."""
    seed_case(store)
    age_all_events(store, days=30)
    before = len(store.list_events("CASE-TIER"))

    report = tiering.tier_to_filing_cabinet()

    assert report.copied == before
    assert report.verified == before
    assert report.evicted == before
    assert report.clean

    # The Desk is now empty of this case, and the Filing Cabinet holds it.
    assert store.list_events("CASE-TIER") == []
    assert tiering.warm.count() == before


def test_the_desk_count_stays_flat_as_the_system_runs(store, tiering):
    """What good looks like: Firestore document count roughly constant."""
    for cycle in range(4):
        seed_case(store, case_id=f"CASE-CYCLE-{cycle}")
        age_all_events(store, days=30)
        tiering.tier_to_filing_cabinet()
        assert tiering.shelf_counts()["desk"] == 0, f"the desk grew on cycle {cycle}"

    assert tiering.shelf_counts()["filing_cabinet"] == 20


def test_an_event_that_does_not_verify_is_not_evicted(store, tiering):
    """A botched run must leave the desk too big, never lose an event."""
    seed_case(store)
    age_all_events(store, days=30)

    class LosesOne(InMemoryWarmShelf):
        def insert(self, rows):
            # Drop one row on the floor, as a flaky load would.
            return super().insert(rows[1:])

    tiering._warm = LosesOne()
    report = tiering.tier_to_filing_cabinet()

    assert not report.clean
    assert len(report.failed_verification) == 1
    assert report.failed_verification[0]["reason"] == "absent from the warm shelf"

    # The unverified event is still on the desk, not lost.
    survivors = {e.event_id for e in store.list_events("CASE-TIER")}
    assert report.failed_verification[0]["event_id"] in survivors


def test_a_corrupted_readback_is_not_evicted(store, tiering):
    """Present but different is worse than absent, and must also block eviction."""
    seed_case(store)
    age_all_events(store, days=30)

    class CorruptsOne(InMemoryWarmShelf):
        def insert(self, rows):
            rows = [dict(r) for r in rows]
            rows[0]["actor"] = "not_the_original_actor"
            return super().insert(rows)

    tiering._warm = CorruptsOne()
    report = tiering.tier_to_filing_cabinet()

    assert not report.clean
    assert "read back different" in report.failed_verification[0]["reason"]
    survivors = {e.event_id for e in store.list_events("CASE-TIER")}
    assert report.failed_verification[0]["event_id"] in survivors


def test_eviction_is_not_reachable_from_the_event_store(store):
    """Agents have no route to a delete, and never will."""
    for forbidden in ("evict", "delete", "delete_event", "remove", "purge"):
        assert not hasattr(store, forbidden), f"EventStore exposes {forbidden}"


# ----------------------------------------------------------------------
# The property everything rests on
# ----------------------------------------------------------------------


def test_folding_a_case_is_identical_before_and_after_tiering(store, tiering):
    """The strongest guarantee: moving shelves changes nothing about the record."""
    seed_case(store)
    # Age first, then snapshot. Backdating is how this test stands in for a week
    # passing, so the baseline has to be taken after it, not before.
    age_all_events(store, days=30)
    before_events = store.list_events("CASE-TIER")
    before_state = fold_events(before_events)

    tiering.tier_to_filing_cabinet()
    assert store.list_events("CASE-TIER") == [], "precondition: the desk is empty"

    after_events = tiering.list_case_events("CASE-TIER")
    after_state = fold_events(after_events)

    assert [e.event_id for e in after_events] == [e.event_id for e in before_events]
    assert after_state.current_status == before_state.current_status
    assert after_state.last_event_id == before_state.last_event_id
    for before, after in zip(before_events, after_events):
        assert after.to_firestore_dict() == before.to_firestore_dict()


def test_reading_an_event_works_from_any_shelf(store, tiering):
    """The caller cannot tell which shelf answered, which is the point."""
    seed_case(store)
    age_all_events(store, days=30)
    target = store.list_events("CASE-TIER")[2]

    from_desk = tiering.read_event(target.event_id)
    assert from_desk is not None and from_desk.event_id == target.event_id

    tiering.tier_to_filing_cabinet()
    assert store.get_event(target.event_id) is None, "precondition: gone from the desk"

    from_cabinet = tiering.read_event(target.event_id)
    assert from_cabinet is not None
    assert from_cabinet.to_firestore_dict() == target.to_firestore_dict()


def test_an_unknown_event_is_none_everywhere(tiering):
    assert tiering.read_event("01ZZZZZZZZZZZZZZZZZZZZZZZZ") is None


def test_an_event_on_two_shelves_is_returned_once(store, tiering):
    """Mid-tiering an event exists in both places briefly."""
    seed_case(store)
    rows = [to_row(e.to_firestore_dict()) for e in store.list_events("CASE-TIER")]
    tiering.warm.insert(rows)  # copied, but not yet evicted

    events = tiering.list_case_events("CASE-TIER")
    ids = [e.event_id for e in events]
    assert len(ids) == len(set(ids)) == 5


# ----------------------------------------------------------------------
# The Warehouse
# ----------------------------------------------------------------------


def test_events_past_retention_reach_the_warehouse(store, tiering):
    """Shelf 3: Parquet, partitioned by date."""
    seed_case(store)
    age_all_events(store, days=800)
    tiering.tier_to_filing_cabinet()
    assert tiering.warm.count() == 5

    report = tiering.archive_to_warehouse()

    assert report.clean
    assert report.archived == 5
    assert report.partitions_written
    assert report.partitions_written[0].endswith(".parquet")
    assert tiering.warm.count() == 0, "the filing cabinet should have been cleared"


def test_a_case_still_folds_from_the_warehouse(store, tiering):
    """Transparent across all three shelves, including the coldest."""
    seed_case(store)
    age_all_events(store, days=800)
    before = store.list_events("CASE-TIER")
    before_state = fold_events(before)

    tiering.tier_to_filing_cabinet()
    tiering.archive_to_warehouse()

    counts = tiering.shelf_counts()
    assert counts["desk"] == 0
    assert counts["filing_cabinet"] == 0
    assert counts["warehouse"] == 5

    after = tiering.list_case_events("CASE-TIER")
    assert [e.event_id for e in after] == [e.event_id for e in before]
    assert fold_events(after).current_status == before_state.current_status


def test_the_warehouse_is_not_cleared_when_a_partition_does_not_verify(store, tiering):
    """Same discipline as the tier above: verify before removing."""
    seed_case(store)
    age_all_events(store, days=800)
    tiering.tier_to_filing_cabinet()

    class LosesRows(InMemoryColdShelf):
        def read_partition(self, path):
            return super().read_partition(path)[:-1]

    tiering._cold = LosesRows()
    report = tiering.archive_to_warehouse()

    assert not report.clean
    assert report.archived == 0
    assert tiering.warm.count() == 5, "the filing cabinet was cleared despite a failure"


def test_partitions_are_by_date(store, tiering):
    """Partitioned by date, so a six month query scans six months."""
    seed_case(store, case_id="CASE-A")
    age_all_events(store, days=800)
    tiering.tier_to_filing_cabinet()
    tiering.archive_to_warehouse()

    partitions = tiering.cold.list_partitions()
    assert partitions
    assert all(p.startswith("events/") for p in partitions)
    stamp = partitions[0].split("/")[1]
    datetime.strptime(stamp, "%Y-%m-%d")


def test_nothing_to_archive_is_not_an_error(tiering):
    report = tiering.archive_to_warehouse()
    assert report.examined == 0
    assert report.archived == 0
    assert report.clean
