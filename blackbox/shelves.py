"""The warm and cold shelves: BigQuery and Cloud Storage.

Shelf 1, the Desk, is Firestore and lives in ``backends.py``. This module holds
the two shelves an event moves to as it ages:

- **Shelf 2, the Filing Cabinet.** BigQuery. The long-term searchable home of the
  Flight Recorder, where a question like "every policy check involving EU-origin
  data in the last six months" is answerable.
- **Shelf 3, the Warehouse.** Cloud Storage, as Parquet partitioned by date.
  Cheap, cold, and rarely read, but still readable.

Each shelf has a real implementation and an in-memory double. The doubles are not
mocks: they implement the same operations with the same semantics, so the tiering
tests exercise the actual movement logic rather than asserting that a mock was
called. A tiering bug that only appears against real BigQuery would be a bug in
the client wiring, and that is what the live probe is for.
"""

import gzip
import io
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

#: The column order used for both BigQuery and Parquet, so a row written to one
#: shelf reads back identically from the other.
EVENT_COLUMNS = [
    "event_id",
    "timestamp",
    "actor",
    "event_type",
    "caused_by",
    "payload",
    "labels",
    "trace_id",
    "span_id",
    "case_id",
]

#: Columns held as JSON text rather than structured fields. Payload shapes vary
#: by event type, and forcing them into a fixed schema would either lose fields
#: or require a migration every time an event type gains one.
JSON_COLUMNS = {"payload", "labels"}


def to_row(event_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Render an event for shelf storage, with JSON columns serialized."""
    row = {}
    for column in EVENT_COLUMNS:
        value = event_dict.get(column)
        if column in JSON_COLUMNS:
            row[column] = json.dumps(value or {}, sort_keys=True, default=str)
        else:
            row[column] = value
    return row


def from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild an event dictionary from a shelf row.

    The inverse of ``to_row``. Round-tripping has to be exact, because the
    tiering verification compares an event read back from a shelf against the one
    still in Firestore before deleting anything.
    """
    event = {}
    for column in EVENT_COLUMNS:
        value = row.get(column)
        if column in JSON_COLUMNS:
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (ValueError, TypeError):
                    value = {}
            elif value is None:
                value = {}
        if column == "timestamp" and isinstance(value, datetime):
            value = value.isoformat()
        event[column] = value
    return event


# ----------------------------------------------------------------------
# Shelf 2: the Filing Cabinet
# ----------------------------------------------------------------------


class WarmShelf(ABC):
    """BigQuery, or something that behaves like it."""

    @abstractmethod
    def ensure_schema(self) -> None:
        """Create the dataset and table if they do not exist."""

    @abstractmethod
    def insert(self, rows: List[Dict[str, Any]]) -> int:
        """Write rows. Returns how many landed."""

    @abstractmethod
    def get(self, event_id: str) -> Optional[Dict[str, Any]]:
        """One event, or None."""

    @abstractmethod
    def list_for_case(self, case_id: str) -> List[Dict[str, Any]]:
        """Every event for a case, in creation order."""

    @abstractmethod
    def older_than(self, cutoff: datetime) -> List[Dict[str, Any]]:
        """Every event recorded before a cutoff."""

    @abstractmethod
    def delete_older_than(self, cutoff: datetime) -> int:
        """Remove events before a cutoff. Used only after archiving them."""

    @abstractmethod
    def count(self) -> int:
        """How many events this shelf holds."""


class InMemoryWarmShelf(WarmShelf):
    """A BigQuery double with the same semantics."""

    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.schema_ready = False

    def ensure_schema(self) -> None:
        self.schema_ready = True

    def insert(self, rows: List[Dict[str, Any]]) -> int:
        for row in rows:
            self.rows[row["event_id"]] = dict(row)
        return len(rows)

    def get(self, event_id: str) -> Optional[Dict[str, Any]]:
        row = self.rows.get(event_id)
        return dict(row) if row else None

    def list_for_case(self, case_id: str) -> List[Dict[str, Any]]:
        matching = [dict(r) for r in self.rows.values() if r.get("case_id") == case_id]
        return sorted(matching, key=lambda r: r["event_id"])

    def older_than(self, cutoff: datetime) -> List[Dict[str, Any]]:
        iso = cutoff.isoformat()
        return sorted(
            (dict(r) for r in self.rows.values() if str(r.get("timestamp", "")) < iso),
            key=lambda r: r["event_id"],
        )

    def delete_older_than(self, cutoff: datetime) -> int:
        doomed = [r["event_id"] for r in self.older_than(cutoff)]
        for event_id in doomed:
            self.rows.pop(event_id, None)
        return len(doomed)

    def count(self) -> int:
        return len(self.rows)


class BigQueryWarmShelf(WarmShelf):
    """The real Filing Cabinet."""

    def __init__(self, project_id: str, dataset: str = "blackbox_events", table: str = "events"):
        self.project_id = project_id
        self.dataset_id = f"{project_id}.{dataset}"
        self.table_id = f"{self.dataset_id}.{table}"
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from google.cloud import bigquery

            self._client = bigquery.Client(project=self.project_id)
        return self._client

    def ensure_schema(self) -> None:
        from google.cloud import bigquery

        dataset = bigquery.Dataset(self.dataset_id)
        dataset.location = "US"
        self.client.create_dataset(dataset, exists_ok=True)

        schema = [
            bigquery.SchemaField("event_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("actor", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("event_type", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("caused_by", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("payload", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("labels", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("trace_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("span_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("case_id", "STRING", mode="REQUIRED"),
        ]
        table = bigquery.Table(self.table_id, schema=schema)
        # Partitioned by day so a six month query scans six months, not everything.
        table.time_partitioning = bigquery.TimePartitioning(field="timestamp")
        table.clustering_fields = ["case_id", "event_type"]
        self.client.create_table(table, exists_ok=True)

    def insert(self, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        # load_table_from_json rather than insert_rows_json: streaming inserts sit
        # in a buffer that cannot be read back immediately, and this tiering path
        # must verify durability before deleting anything.
        from google.cloud import bigquery

        job_config = bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            schema_update_options=[],
        )
        job = self.client.load_table_from_json(rows, self.table_id, job_config=job_config)
        job.result()
        if job.errors:
            raise RuntimeError(f"BigQuery load failed: {job.errors}")
        return len(rows)

    def _query(self, sql: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
        from google.cloud import bigquery

        config = bigquery.QueryJobConfig(query_parameters=params or [])
        return [dict(row) for row in self.client.query(sql, job_config=config).result()]

    def get(self, event_id: str) -> Optional[Dict[str, Any]]:
        from google.cloud import bigquery

        rows = self._query(
            f"SELECT * FROM `{self.table_id}` WHERE event_id = @event_id LIMIT 1",
            [bigquery.ScalarQueryParameter("event_id", "STRING", event_id)],
        )
        return rows[0] if rows else None

    def list_for_case(self, case_id: str) -> List[Dict[str, Any]]:
        from google.cloud import bigquery

        return self._query(
            f"SELECT * FROM `{self.table_id}` WHERE case_id = @case_id ORDER BY event_id",
            [bigquery.ScalarQueryParameter("case_id", "STRING", case_id)],
        )

    def older_than(self, cutoff: datetime) -> List[Dict[str, Any]]:
        from google.cloud import bigquery

        return self._query(
            f"SELECT * FROM `{self.table_id}` WHERE timestamp < @cutoff ORDER BY event_id",
            [bigquery.ScalarQueryParameter("cutoff", "TIMESTAMP", cutoff)],
        )

    def delete_older_than(self, cutoff: datetime) -> int:
        from google.cloud import bigquery

        doomed = len(self.older_than(cutoff))
        self._query(
            f"DELETE FROM `{self.table_id}` WHERE timestamp < @cutoff",
            [bigquery.ScalarQueryParameter("cutoff", "TIMESTAMP", cutoff)],
        )
        return doomed

    def count(self) -> int:
        rows = self._query(f"SELECT COUNT(*) AS n FROM `{self.table_id}`")
        return int(rows[0]["n"]) if rows else 0


# ----------------------------------------------------------------------
# Shelf 3: the Warehouse
# ----------------------------------------------------------------------


def rows_to_parquet(rows: List[Dict[str, Any]]) -> bytes:
    """Serialize rows as compressed Parquet.

    Every column is written as a string. Payloads are already JSON text by this
    point, and a fixed string schema means a partition written a year ago still
    reads today without a schema migration.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table(
        {
            column: pa.array(
                [None if r.get(column) is None else str(r.get(column)) for r in rows],
                type=pa.string(),
            )
            for column in EVENT_COLUMNS
        }
    )
    sink = io.BytesIO()
    pq.write_table(table, sink, compression="snappy")
    return sink.getvalue()


def parquet_to_rows(blob: bytes) -> List[Dict[str, Any]]:
    """Read a Parquet partition back into rows."""
    import pyarrow.parquet as pq

    table = pq.read_table(io.BytesIO(blob))
    return table.to_pylist()


class ColdShelf(ABC):
    """Cloud Storage, or something that behaves like it."""

    @abstractmethod
    def write_partition(self, partition: str, rows: List[Dict[str, Any]]) -> str:
        """Write one date partition. Returns the object path."""

    @abstractmethod
    def read_partition(self, path: str) -> List[Dict[str, Any]]:
        """Read one partition back."""

    @abstractmethod
    def list_partitions(self) -> List[str]:
        """Every partition held."""


class InMemoryColdShelf(ColdShelf):
    """A Cloud Storage double, holding real Parquet bytes."""

    def __init__(self) -> None:
        self.objects: Dict[str, bytes] = {}

    def write_partition(self, partition: str, rows: List[Dict[str, Any]]) -> str:
        path = f"events/{partition}/events.parquet"
        self.objects[path] = rows_to_parquet(rows)
        return path

    def read_partition(self, path: str) -> List[Dict[str, Any]]:
        blob = self.objects.get(path)
        return parquet_to_rows(blob) if blob else []

    def list_partitions(self) -> List[str]:
        return sorted(self.objects)


class CloudStorageColdShelf(ColdShelf):
    """The real Warehouse."""

    def __init__(self, project_id: str, bucket_name: str):
        self.project_id = project_id
        self.bucket_name = bucket_name
        self._client = None

    @property
    def bucket(self):
        if self._client is None:
            from google.cloud import storage

            self._client = storage.Client(project=self.project_id)
        return self._client.bucket(self.bucket_name)

    def write_partition(self, partition: str, rows: List[Dict[str, Any]]) -> str:
        path = f"events/{partition}/events.parquet"
        self.bucket.blob(path).upload_from_string(
            rows_to_parquet(rows), content_type="application/octet-stream"
        )
        return path

    def read_partition(self, path: str) -> List[Dict[str, Any]]:
        blob = self.bucket.blob(path)
        if not blob.exists():
            return []
        return parquet_to_rows(blob.download_as_bytes())

    def list_partitions(self) -> List[str]:
        return sorted(b.name for b in self.bucket.list_blobs(prefix="events/"))
