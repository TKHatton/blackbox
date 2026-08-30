"""Wiki schema for BLACKBOX memory layer"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator


class WikiPage(BaseModel):
    """
    A Wiki page represents condensed, current knowledge about a subject.
    
    Unlike the Diary (Flight Recorder), Wiki pages are rewritten in place.
    They are derived from events and can be regenerated when new events arrive.
    
    The derived_from field tracks which events and facts produced this content,
    enabling The Eraser (Phase 5) to cascade retractions correctly.
    """
    
    model_config = {"frozen": False}  # Wiki pages can be updated
    
    page_id: str = Field(..., description="Unique identifier for this page")
    subject: str = Field(..., description="What this page is about (case, customer, agent context)")
    subject_type: str = Field(..., description="Type of subject: 'case', 'customer', 'agent_context'")
    content: Dict[str, Any] = Field(..., description="The condensed current summary")
    derived_from: List[str] = Field(
        ...,
        description=(
            "Everything this page was built from. Holds both event ids, which are "
            "ULIDs, and other page ids, which are not. Page ids are what make the "
            "derived_from edges a graph rather than a flat list, and the graph is "
            "what The Eraser walks."
        ),
    )
    version: int = Field(default=1, description="Version number, incremented on each update")
    created_at: datetime = Field(..., description="When this page was first created")
    updated_at: datetime = Field(..., description="When this page was last updated")

    # Phase 4 and 5.
    jurisdiction: Optional[str] = Field(
        None, description="Which regime governs the content, for example EU_IE"
    )
    invalidated_by: Optional[str] = Field(
        None,
        description=(
            "The retraction that invalidated this page, if it has not been "
            "regenerated since. A page in this state is not safe to read."
        ),
    )

    def source_event_ids(self) -> List[str]:
        """The event ids this page was built from.

        Event ids are ULIDs: 26 characters from Crockford's base32. Page ids
        carry a colon, so the two are told apart without a second field to keep
        in sync.
        """
        return [ref for ref in self.derived_from if ":" not in ref]

    def source_page_ids(self) -> List[str]:
        """The other Wiki pages this page was built from."""
        return [ref for ref in self.derived_from if ":" in ref]

    @property
    def is_valid(self) -> bool:
        """False while a retraction has invalidated this page and it has not been rebuilt."""
        return self.invalidated_by is None
    
    def to_firestore_dict(self) -> Dict[str, Any]:
        """Convert to Firestore-compatible dictionary"""
        return {
            "page_id": self.page_id,
            "subject": self.subject,
            "subject_type": self.subject_type,
            "content": self.content,
            "derived_from": self.derived_from,
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "jurisdiction": self.jurisdiction,
            "invalidated_by": self.invalidated_by,
        }
    
    @classmethod
    def from_firestore_dict(cls, data: Dict[str, Any]) -> "WikiPage":
        """Create WikiPage from Firestore dictionary"""
        # Convert timestamp strings back to datetime
        for field in ["created_at", "updated_at"]:
            if isinstance(data.get(field), str):
                data[field] = datetime.fromisoformat(data[field])
        
        return cls(**data)
    
    def regenerate(
        self,
        new_content: Dict[str, Any],
        new_derived_from: List[str],
        jurisdiction: Optional[str] = None,
    ) -> "WikiPage":
        """Create the next version of this page.

        Regenerating clears ``invalidated_by``: a page rebuilt from its remaining
        valid sources is valid again. Note that the new content is passed in
        whole. This method never merges with the old content, because a merge is
        exactly how a retracted fact would survive a regeneration.
        """
        return WikiPage(
            page_id=self.page_id,
            subject=self.subject,
            subject_type=self.subject_type,
            content=new_content,
            derived_from=new_derived_from,
            version=self.version + 1,
            created_at=self.created_at,
            updated_at=datetime.now(timezone.utc),
            jurisdiction=jurisdiction if jurisdiction is not None else self.jurisdiction,
            invalidated_by=None,
        )

    def invalidated(self, retraction_id: str) -> "WikiPage":
        """Mark this page as no longer safe to read.

        A new version, because the invalidation is a change to the page and the
        version number is what tells a reader they are looking at something new.
        """
        return WikiPage(
            page_id=self.page_id,
            subject=self.subject,
            subject_type=self.subject_type,
            content=self.content,
            derived_from=self.derived_from,
            version=self.version + 1,
            created_at=self.created_at,
            updated_at=datetime.now(timezone.utc),
            jurisdiction=self.jurisdiction,
            invalidated_by=retraction_id,
        )


class WikiUpdate(BaseModel):
    """
    Record of a Wiki page update, stored in the Diary.
    
    This allows us to track the history of Wiki pages even though
    they are rewritten in place.
    """
    
    page_id: str = Field(..., description="Which page was updated")
    old_version: int = Field(..., description="Previous version number")
    new_version: int = Field(..., description="New version number")
    old_derived_from: List[str] = Field(..., description="Previous derived_from list")
    new_derived_from: List[str] = Field(..., description="New derived_from list")
    timestamp: datetime = Field(..., description="When the update occurred")
    reason: str = Field(..., description="Why the page was updated")
    
    def to_firestore_dict(self) -> Dict[str, Any]:
        """Convert to Firestore-compatible dictionary"""
        return {
            "page_id": self.page_id,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "old_derived_from": self.old_derived_from,
            "new_derived_from": self.new_derived_from,
            "timestamp": self.timestamp.isoformat(),
            "reason": self.reason,
        }
