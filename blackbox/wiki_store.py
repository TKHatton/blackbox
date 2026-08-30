"""Wiki store for the BLACKBOX memory layer.

The Wiki is what agents read. Pages are rewritten in place, so this store does
have an update path, unlike the Diary. Every rewrite is recorded in the Diary as
a MEMORY_WRITE event, which is how the history of a page survives even though the
page itself only holds the current version.

Agents read the Wiki. Agents never read the Diary during normal operation.
"""

from typing import List, Optional

from google.cloud import firestore

from .config import get_settings
from .event_store import EventStore
from .schema import EventType
from .wiki import WikiPage, WikiUpdate


class WikiStore:
    """Firestore-backed storage for Wiki pages."""

    def __init__(
        self,
        project_id: str,
        event_store: Optional[EventStore] = None,
        in_memory: Optional[bool] = None,
    ):
        settings = get_settings()
        self.project_id = project_id
        self._collection_name = settings.wiki_collection
        self._database = settings.firestore_database
        self._in_memory = settings.in_memory if in_memory is None else in_memory
        self._client = None
        self._memory: dict = {}
        # The Wiki records its own rewrites into the Diary, so it needs a writer.
        self._event_store = event_store or EventStore(
            project_id=project_id, in_memory=self._in_memory
        )

    @property
    def collection(self):
        """Lazy Firestore collection handle so imports need no credentials."""
        if self._client is None:
            self._client = firestore.Client(project=self.project_id, database=self._database)
        return self._client.collection(self._collection_name)

    def get_page(self, page_id: str) -> Optional[WikiPage]:
        """Retrieve a Wiki page by id."""
        if self._in_memory:
            data = self._memory.get(page_id)
            return WikiPage.from_firestore_dict(dict(data)) if data else None
        doc = self.collection.document(page_id).get()
        if not doc.exists:
            return None
        return WikiPage.from_firestore_dict(doc.to_dict())

    def create_page(self, page: WikiPage) -> None:
        """Create a new Wiki page."""
        self._write(page)

    def update_page(self, page: WikiPage) -> None:
        """Overwrite an existing Wiki page with a newer version."""
        self._write(page)

    def _write(self, page: WikiPage) -> None:
        if self._in_memory:
            self._memory[page.page_id] = page.to_firestore_dict()
            return
        self.collection.document(page.page_id).set(page.to_firestore_dict())

    def delete_page(self, page_id: str) -> None:
        """Remove a Wiki page.

        This deletes derived memory only. The Diary events the page was built
        from are untouched, so the history of the page survives its deletion.
        """
        if self._in_memory:
            self._memory.pop(page_id, None)
            return
        self.collection.document(page_id).delete()

    def list_pages_by_subject(self, subject: str) -> List[WikiPage]:
        """List every Wiki page about a given subject."""
        return self._list("subject", subject)

    def list_pages_by_subject_type(self, subject_type: str) -> List[WikiPage]:
        """List every Wiki page of a given subject type."""
        return self._list("subject_type", subject_type)

    def _list(self, field: str, value: str) -> List[WikiPage]:
        if self._in_memory:
            return [
                WikiPage.from_firestore_dict(dict(d))
                for d in self._memory.values()
                if d.get(field) == value
            ]
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = self.collection.where(filter=FieldFilter(field, "==", value))
        return [WikiPage.from_firestore_dict(doc.to_dict()) for doc in query.stream()]

    def record_update(self, update: WikiUpdate, case_id: str, caused_by: Optional[str] = None) -> str:
        """Record a Wiki page rewrite in the Diary.

        Args:
            update: What changed about the page.
            case_id: The case whose log this rewrite belongs to. Wiki rewrites are
                part of a case's story, so they go in that case's log rather than
                a separate one.
            caused_by: The event that caused the rewrite.

        Returns:
            The event id of the recorded MEMORY_WRITE.
        """
        return self._event_store.append_event(
            case_id=case_id,
            event_type=EventType.MEMORY_WRITE,
            payload={
                "memory_key": f"wiki:{update.page_id}",
                "content": {
                    "old_version": update.old_version,
                    "new_version": update.new_version,
                    "old_derived_from": update.old_derived_from,
                    "new_derived_from": update.new_derived_from,
                },
                "reason": update.reason,
            },
            actor="wiki_store",
            caused_by=caused_by,
        )
