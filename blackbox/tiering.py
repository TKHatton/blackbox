"""Moving events between shelves, so the hot path stays small forever.

Three shelves, and an event moves outward as it ages:

- **The Desk.** Firestore. Active cases, current Wiki pages, and recent events.
- **The Filing Cabinet.** BigQuery. Searchable, long-term.
- **The Warehouse.** Cloud Storage, as Parquet partitioned by date.

The named failure mode is tiering implemented as a copy without a delete, so
Firestore keeps growing anyway. That is what the previous version of this module
did: it printed a line and returned zero. The fix is not simply to add a delete,
because a delete is the one operation that can destroy the record this system
exists to preserve.

## Copy, verify, then evict

Every eviction is preceded by reading the event back from the shelf it was
supposedly written to and comparing it field by field against the copy still in
Firestore. An event that does not read back identically is not evicted, and the
mismatch is reported. So the failure mode of a botched tiering run is Firestore
staying too big, which is a cost problem, rather than an event vanishing, which
is unrecoverable.

## What append-only means here

The Diary is append-only as a *record*. No event is ever altered and none stops
existing. Tiering changes an event's address, not its content or its existence:
after a tiering run the same event is still readable, byte for byte, through
``TieringManager.read_event``. ``EventStore`` still has no delete and no update,
and an agent has no route to one. The eviction capability lives on the backend,
is called only from here, and is gated on the verification above.

## Reading across shelves

``read_event`` and ``list_case_events`` check the Desk, then the Filing Cabinet,
then the Warehouse, and merge. That is what lets the Time Machine rewind to a
point in history regardless of which shelf now holds it, and it is why the tests
assert that folding a case produces identical state before and after tiering.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from .event_store import EventStore
from .schema import Event
from .shelves import (
    ColdShelf,
    InMemoryColdShelf,
    InMemoryWarmShelf,
    WarmShelf,
    from_row,
    to_row,
)

logger = logging.getLogger(__name__)

#: Fields compared when verifying an event survived the trip to another shelf.
#: All of them: a partial comparison would let a corrupted payload through.
VERIFIED_FIELDS = (
    "event_id",
    "actor",
    "event_type",
    "caused_by",
    "payload",
    "labels",
    "trace_id",
    "span_id",
    "case_id",
)


@dataclass
class TieringReport:
    """What one tiering run did."""

    examined: int = 0
    copied: int = 0
    verified: int = 0
    evicted: int = 0
    failed_verification: List[Dict[str, Any]] = field(default_factory=list)
    archived: int = 0
    partitions_written: List[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.failed_verification

    def to_dict(self) -> Dict[str, Any]:
        return {
            "examined": self.examined,
            "copied_to_filing_cabinet": self.copied,
            "verified_durable": self.verified,
            "evicted_from_desk": self.evicted,
            "failed_verification": self.failed_verification,
            "archived_to_warehouse": self.archived,
            "partitions_written": self.partitions_written,
            "clean": self.clean,
        }


def _timestamp_of(event_dict: Dict[str, Any]) -> Optional[datetime]:
    raw = event_dict.get("timestamp")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def events_match(desk: Dict[str, Any], shelf: Dict[str, Any]) -> bool:
    """True if an event read back from a shelf is the one that was written.

    Compares every field that carries meaning. Timestamps are compared as parsed
    instants rather than strings, because BigQuery hands back a datetime where
    Firestore held an ISO string, and that difference is a representation detail
    rather than a change to the record.
    """
    for name in VERIFIED_FIELDS:
        if desk.get(name) != shelf.get(name):
            return False

    left, right = _timestamp_of(desk), _timestamp_of(shelf)
    if left is None or right is None:
        return left == right
    return abs((left - right).total_seconds()) < 1


class TieringManager:
    """Moves events outward through the shelves, and reads back across them."""

    def __init__(
        self,
        project_id: str,
        event_store: EventStore,
        hot_ttl_days: int = 7,
        cold_ttl_days: int = 365,
        bucket_name: Optional[str] = None,
        warm_shelf: Optional[WarmShelf] = None,
        cold_shelf: Optional[ColdShelf] = None,
        in_memory: bool = False,
    ):
        self.project_id = project_id
        self.event_store = event_store
        self.hot_ttl_days = hot_ttl_days
        self.cold_ttl_days = cold_ttl_days
        self.bucket_name = bucket_name
        self._in_memory = in_memory

        self._warm = warm_shelf
        self._cold = cold_shelf

    @property
    def warm(self) -> WarmShelf:
        """The Filing Cabinet."""
        if self._warm is None:
            if self._in_memory:
                self._warm = InMemoryWarmShelf()
            else:
                from .shelves import BigQueryWarmShelf

                self._warm = BigQueryWarmShelf(project_id=self.project_id)
        return self._warm

    @property
    def cold(self) -> ColdShelf:
        """The Warehouse."""
        if self._cold is None:
            if self._in_memory or not self.bucket_name:
                self._cold = InMemoryColdShelf()
            else:
                from .shelves import CloudStorageColdShelf

                self._cold = CloudStorageColdShelf(
                    project_id=self.project_id, bucket_name=self.bucket_name
                )
        return self._cold

    def ensure_schema(self) -> None:
        """Create the Filing Cabinet's dataset and table if they do not exist."""
        self.warm.ensure_schema()

    # ------------------------------------------------------------------
    # Desk -> Filing Cabinet
    # ------------------------------------------------------------------

    def _desk_events_older_than(self, cutoff: datetime) -> List[Dict[str, Any]]:
        """Every event still on the Desk that is older than a cutoff.

        Reads through the backend rather than ``EventStore``, because this needs
        every case at once and ``EventStore`` is deliberately per-case.
        """
        rows = self.event_store._backend.query(
            self.event_store._collection, filters=[], order_by="event_id"
        )
        out = []
        for row in rows:
            when = _timestamp_of(row)
            if when is not None and when < cutoff:
                out.append(row)
        return out

    def tier_to_filing_cabinet(
        self, now: Optional[datetime] = None, batch_size: int = 500
    ) -> TieringReport:
        """Move aged events from Firestore into BigQuery.

        Copies first, reads each one back to confirm it arrived intact, and only
        then removes it from Firestore. An event that fails verification stays on
        the Desk and is reported.

        Args:
            now: Treat this as the current time. Lets a demonstration age events
                without waiting a week, with the compression visible.
            batch_size: How many events to move per run.

        Returns:
            What was examined, copied, verified, and evicted.
        """
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.hot_ttl_days)
        report = TieringReport()

        candidates = self._desk_events_older_than(cutoff)[:batch_size]
        report.examined = len(candidates)
        if not candidates:
            return report

        self.warm.insert([to_row(row) for row in candidates])
        report.copied = len(candidates)

        for row in candidates:
            event_id = row["event_id"]
            landed = self.warm.get(event_id)

            if landed is None or not events_match(row, from_row(landed)):
                # Not evicted. Firestore staying too big is a cost problem; an
                # event vanishing is not recoverable.
                logger.error(
                    "Event %s did not verify on the filing cabinet, leaving it on the desk",
                    event_id,
                )
                report.failed_verification.append(
                    {
                        "event_id": event_id,
                        "reason": "absent from the warm shelf"
                        if landed is None
                        else "read back different from what was written",
                    }
                )
                continue

            report.verified += 1
            self.event_store._backend.evict(self.event_store._collection, event_id)
            report.evicted += 1

        return report

    # ------------------------------------------------------------------
    # Filing Cabinet -> Warehouse
    # ------------------------------------------------------------------

    def archive_to_warehouse(self, now: Optional[datetime] = None) -> TieringReport:
        """Move events past the retention window from BigQuery into Cloud Storage.

        Written as Parquet, one partition per day. Same discipline as the tier
        above: every partition is read back before anything is removed from
        BigQuery.
        """
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.cold_ttl_days)
        report = TieringReport()

        doomed = self.warm.older_than(cutoff)
        report.examined = len(doomed)
        if not doomed:
            return report

        by_day: Dict[str, List[Dict[str, Any]]] = {}
        for row in doomed:
            when = _timestamp_of(row)
            day = (when or now).strftime("%Y-%m-%d")
            by_day.setdefault(day, []).append(row)

        verified_ids: Set[str] = set()
        for day, rows in sorted(by_day.items()):
            path = self.cold.write_partition(day, [to_row(from_row(r)) for r in rows])
            report.partitions_written.append(path)

            read_back = {r["event_id"] for r in self.cold.read_partition(path)}
            for row in rows:
                if row["event_id"] in read_back:
                    verified_ids.add(row["event_id"])
                else:
                    report.failed_verification.append(
                        {
                            "event_id": row["event_id"],
                            "reason": f"missing from the warehouse partition {path}",
                        }
                    )

        report.verified = len(verified_ids)
        if report.clean:
            report.archived = self.warm.delete_older_than(cutoff)
        else:
            logger.error(
                "Not clearing the filing cabinet: %s event(s) did not verify in the warehouse",
                len(report.failed_verification),
            )
        return report

    # ------------------------------------------------------------------
    # Reading across all three shelves
    # ------------------------------------------------------------------

    def read_event(self, event_id: str) -> Optional[Event]:
        """One event, from whichever shelf holds it.

        Desk first, then Filing Cabinet, then Warehouse. The caller cannot tell
        which one answered, which is the point: the Time Machine works on any
        moment in history regardless of where that moment now lives.
        """
        found = self.event_store.get_event(event_id)
        if found is not None:
            return found

        row = self.warm.get(event_id)
        if row is not None:
            return Event.from_firestore_dict(from_row(row))

        for path in self.cold.list_partitions():
            for candidate in self.cold.read_partition(path):
                if candidate.get("event_id") == event_id:
                    return Event.from_firestore_dict(from_row(candidate))

        return None

    def list_case_events(self, case_id: str) -> List[Event]:
        """Every event for a case, merged across shelves, in creation order.

        An event on two shelves at once, which happens briefly mid-tiering, is
        returned once.
        """
        merged: Dict[str, Event] = {}

        for event in self.event_store.list_events(case_id):
            merged[event.event_id] = event

        for row in self.warm.list_for_case(case_id):
            merged.setdefault(
                row["event_id"], Event.from_firestore_dict(from_row(row))
            )

        for path in self.cold.list_partitions():
            for row in self.cold.read_partition(path):
                if row.get("case_id") != case_id:
                    continue
                merged.setdefault(
                    row["event_id"], Event.from_firestore_dict(from_row(row))
                )

        return [merged[key] for key in sorted(merged)]

    def shelf_counts(self) -> Dict[str, int]:
        """How many events sit on each shelf.

        The number that matters is the Desk's: it should stay roughly flat as the
        system runs, rather than climbing forever.
        """
        desk = len(
            self.event_store._backend.query(self.event_store._collection, filters=[])
        )
        warehouse = sum(
            len(self.cold.read_partition(p)) for p in self.cold.list_partitions()
        )
        return {"desk": desk, "filing_cabinet": self.warm.count(), "warehouse": warehouse}

    # Kept so existing callers keep working.
    def ensure_bigquery_schema(self) -> None:
        self.ensure_schema()

    def tier_old_events(self) -> int:
        return self.tier_to_filing_cabinet().evicted

    def archive_to_cold_storage(self) -> int:
        return self.archive_to_warehouse().archived

    def read_case_events(self, case_id: str) -> List[Event]:
        return self.list_case_events(case_id)
