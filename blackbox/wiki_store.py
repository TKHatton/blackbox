"""Wiki store for the BLACKBOX memory layer.

The Wiki is what agents read. Pages are rewritten in place, so this store does
have an update path, unlike the Diary. Every rewrite is recorded in the Diary as
a MEMORY_WRITE event, which is how the history of a page survives even though the
page itself only holds the current version.

Agents read the Wiki. Agents never read the Diary during normal operation.
"""

import logging
from typing import List, Optional

from google.cloud import firestore

from .config import get_settings
from .event_store import EventStore
from .regions import RegionRoutingRefused, evaluate_routing
from .schema import EventType
from .wiki import WikiPage, WikiUpdate

logger = logging.getLogger(__name__)


class WikiStore:
    """Firestore-backed storage for Wiki pages."""

    def __init__(
        self,
        project_id: str,
        event_store: Optional[EventStore] = None,
        in_memory: Optional[bool] = None,
        worker_region: Optional[str] = None,
    ):
        settings = get_settings()
        self.project_id = project_id
        self._collection_name = settings.wiki_collection
        self._database = settings.firestore_database
        self._in_memory = settings.in_memory if in_memory is None else in_memory
        # Which region this instance runs in. Region pinning is checked against
        # this on every read, which is what makes it a control rather than a
        # label on a diagram.
        self.worker_region = worker_region or settings.worker_region
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

    def get_page(self, page_id: str, enforce_region: bool = True) -> Optional[WikiPage]:
        """Retrieve a Wiki page by id.

        Refuses rather than returns when the page is pinned to a region this
        worker may not read from. The refusal is an exception, not an empty
        result: a caller handed None would carry on with a gap it cannot see.

        Args:
            page_id: The page to read.
            enforce_region: Left true everywhere on the agent path. The Eraser
                sets it false while walking the dependency graph, because a
                retraction has to reach every derived page regardless of which
                region the machine running the cascade happens to be in.

        Raises:
            RegionRoutingRefused: If reading here would cross a border.
        """
        page = self._read(page_id)
        if page is None:
            return None

        if enforce_region:
            decision = evaluate_routing(page_id, page.jurisdiction, self.worker_region)
            if not decision.allowed:
                self._record_routing_refusal(page, decision)
                raise RegionRoutingRefused(
                    page_id, decision.page_region, self.worker_region, decision.reasoning
                )

        return page

    def _read(self, page_id: str) -> Optional[WikiPage]:
        """The raw read, with no region check."""
        if self._in_memory:
            data = self._memory.get(page_id)
            return WikiPage.from_firestore_dict(dict(data)) if data else None
        doc = self.collection.document(page_id).get()
        if not doc.exists:
            return None
        return WikiPage.from_firestore_dict(doc.to_dict())

    def _record_routing_refusal(self, page: WikiPage, decision) -> None:
        """Write the refusal to the Diary, with its reasoning.

        A control that refuses silently cannot be audited, and cannot be
        distinguished later from a machine that simply never tried.
        """
        payload = decision.to_policy_check()
        case_id = page.subject if page.subject_type == "case" else f"wiki:{page.page_id}"
        try:
            self._event_store.append_event(
                case_id=case_id,
                event_type=EventType.POLICY_CHECK,
                payload=payload,
                actor="region_router",
            )
        except Exception:  # pragma: no cover - recording must not mask the refusal
            logger.exception("Could not record a region routing refusal for %s", page.page_id)

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

    def list_pages_by_subject(
        self, subject: str, enforce_region: bool = True
    ) -> List[WikiPage]:
        """List every Wiki page about a given subject."""
        return self._list("subject", subject, enforce_region)

    def list_pages_by_subject_type(
        self, subject_type: str, enforce_region: bool = True
    ) -> List[WikiPage]:
        """List every Wiki page of a given subject type.

        Region pinning applies here too. A control that guards single reads but
        lets a caller list its way around them is not a control, so pages this
        worker may not hold are withheld from the result.

        Withholding is recorded rather than silent. The caller gets a shorter
        list, and the Diary gets an event saying how many pages were kept back
        and why, so a scan that quietly saw less than it should have is
        answerable afterwards.
        """
        return self._list("subject_type", subject_type, enforce_region)

    def _list(self, field: str, value: str, enforce_region: bool = True) -> List[WikiPage]:
        pages = self._raw_list(field, value)
        if not enforce_region:
            return pages

        allowed, withheld = [], []
        for page in pages:
            if evaluate_routing(page.page_id, page.jurisdiction, self.worker_region).allowed:
                allowed.append(page)
            else:
                withheld.append(page)

        if withheld:
            self._record_list_withholding(field, value, withheld)
        return allowed

    def _raw_list(self, field: str, value: str) -> List[WikiPage]:
        """The raw listing, with no region check."""
        if self._in_memory:
            return [
                WikiPage.from_firestore_dict(dict(d))
                for d in self._memory.values()
                if d.get(field) == value
            ]
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = self.collection.where(filter=FieldFilter(field, "==", value))
        return [WikiPage.from_firestore_dict(doc.to_dict()) for doc in query.stream()]

    def _record_list_withholding(self, field: str, value: str, withheld: List[WikiPage]) -> None:
        """Record that a listing returned less than the store holds."""
        logger.warning(
            "Withheld %s page(s) from a %s=%s listing on a %s worker",
            len(withheld),
            field,
            value,
            self.worker_region,
        )
        try:
            self._event_store.append_event(
                case_id=f"region:{self.worker_region}",
                event_type=EventType.POLICY_CHECK,
                payload={
                    "policy_id": "region_pinning_listing",
                    "check_type": "data_transfer",
                    "input_data": {
                        "query": f"{field}={value}",
                        "worker_region": self.worker_region,
                        "withheld_page_ids": sorted(p.page_id for p in withheld),
                    },
                    "decision": "block",
                    "reasoning": (
                        f"{len(withheld)} page(s) matching {field}={value} are pinned to "
                        f"regions a {self.worker_region} worker may not hold, so they were "
                        f"withheld from this listing. The result is shorter than the store. "
                        f"Run this scan on an instance in the pinned region to see them."
                    ),
                },
                actor="region_router",
            )
        except Exception:  # pragma: no cover
            logger.exception("Could not record a region listing withholding")

    def record_update(
        self,
        update: WikiUpdate,
        case_id: str,
        caused_by: Optional[str] = None,
        content: Optional[dict] = None,
    ) -> str:
        """Record a Wiki page rewrite in the Diary.

        The resulting page content is recorded alongside the version bookkeeping.
        That is what lets the Time Machine rebuild the Wiki as it stood at a past
        moment instead of reading today's pages, and a replay that read current
        state would produce confident nonsense. An earlier version of this method
        recorded only the version numbers, which made the Wiki unreconstructable.

        Args:
            update: What changed about the page.
            case_id: The case whose log this rewrite belongs to. Wiki rewrites are
                part of a case's story, so they go in that case's log rather than
                a separate one.
            caused_by: The event that caused the rewrite.
            content: The page content after the rewrite.

        Returns:
            The event id of the recorded MEMORY_WRITE.
        """
        payload_content = dict(content) if content else {}
        payload_content["_version"] = {
            "old_version": update.old_version,
            "new_version": update.new_version,
            "old_derived_from": update.old_derived_from,
            "new_derived_from": update.new_derived_from,
        }
        return self._event_store.append_event(
            case_id=case_id,
            event_type=EventType.MEMORY_WRITE,
            payload={
                "memory_key": f"wiki:{update.page_id}",
                "content": payload_content,
                "reason": update.reason,
            },
            actor="wiki_store",
            caused_by=caused_by,
        )
