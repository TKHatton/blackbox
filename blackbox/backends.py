"""Storage backends for the Flight Recorder.

The Diary is append-only by construction. Every backend here exposes exactly
three operations: put a document, get a document, query documents. There is no
update and no delete, so no caller can reach for one.

The in-memory backend exists so the test suite and local runs can exercise the
real write path without Google Cloud credentials. It is not a cache in front of
Firestore: it is either the whole store or not used at all.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, List, Optional, Tuple


class AppendOnlyBackend(ABC):
    """A document store that can be written once and read many times."""

    @abstractmethod
    def put(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        """Write a document. Raises if the document id already exists."""

    @abstractmethod
    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        """Read one document, or None."""

    @abstractmethod
    def query(
        self,
        collection: str,
        filters: Iterable[Tuple[str, str, Any]],
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Read documents matching equality filters, optionally ordered and capped."""


class DocumentAlreadyExists(RuntimeError):
    """Raised when a write would overwrite an existing event.

    This is the append-only guarantee turning into a runtime error rather than
    silent data loss.
    """


class InMemoryBackend(AppendOnlyBackend):
    """Dict-backed store used by tests and local runs."""

    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def put(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        bucket = self._data.setdefault(collection, {})
        if doc_id in bucket:
            raise DocumentAlreadyExists(f"{collection}/{doc_id} already exists")
        bucket[doc_id] = dict(data)

    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        doc = self._data.get(collection, {}).get(doc_id)
        return dict(doc) if doc is not None else None

    def query(
        self,
        collection: str,
        filters: Iterable[Tuple[str, str, Any]],
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        rows = [dict(d) for d in self._data.get(collection, {}).values()]
        for field, op, value in filters:
            if op != "==":
                raise ValueError(f"InMemoryBackend supports only '==' filters, got {op!r}")
            rows = [r for r in rows if r.get(field) == value]
        if order_by:
            rows.sort(key=lambda r: (r.get(order_by) is None, r.get(order_by)))
        if limit is not None:
            rows = rows[:limit]
        return rows


class FirestoreBackend(AppendOnlyBackend):
    """Firestore-backed store.

    Writes go through ``create()``, not ``set()``. ``create()`` fails if the
    document already exists, which is the append-only guarantee enforced by the
    database rather than by convention. ``set()`` would silently overwrite.
    """

    def __init__(self, project_id: str, database: str = "(default)") -> None:
        self.project_id = project_id
        self.database = database
        self._client = None

    @property
    def client(self):
        """Lazy Firestore client so importing this module needs no credentials."""
        if self._client is None:
            from google.cloud import firestore

            # database= is required. BLACKBOX uses a named database, and omitting
            # this argument would quietly read and write "(default)" instead.
            self._client = firestore.Client(project=self.project_id, database=self.database)
        return self._client

    def put(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        from google.api_core.exceptions import AlreadyExists

        try:
            self.client.collection(collection).document(doc_id).create(data)
        except AlreadyExists as exc:
            raise DocumentAlreadyExists(f"{collection}/{doc_id} already exists") from exc

    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        doc = self.client.collection(collection).document(doc_id).get()
        return doc.to_dict() if doc.exists else None

    def query(
        self,
        collection: str,
        filters: Iterable[Tuple[str, str, Any]],
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = self.client.collection(collection)
        for field, op, value in filters:
            query = query.where(filter=FieldFilter(field, op, value))
        if order_by:
            query = query.order_by(order_by)
        if limit is not None:
            query = query.limit(limit)
        return [doc.to_dict() for doc in query.stream()]


def build_backend(
    project_id: str, in_memory: bool = False, database: str = "(default)"
) -> AppendOnlyBackend:
    """Pick a backend. In-memory when asked, Firestore otherwise."""
    if in_memory:
        return InMemoryBackend()
    return FirestoreBackend(project_id=project_id, database=database)
