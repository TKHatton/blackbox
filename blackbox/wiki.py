"""Wiki schema for BLACKBOX memory layer"""

from datetime import datetime
from typing import List, Dict, Any, Optional
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
    derived_from: List[str] = Field(..., description="List of event_ids that produced this content")
    version: int = Field(default=1, description="Version number, incremented on each update")
    created_at: datetime = Field(..., description="When this page was first created")
    updated_at: datetime = Field(..., description="When this page was last updated")
    
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
        new_derived_from: List[str]
    ) -> "WikiPage":
        """
        Create a new version of this page with updated content.
        
        This is used when new events arrive and the page needs to be regenerated.
        The old version is recorded in the Diary as an event.
        """
        return WikiPage(
            page_id=self.page_id,
            subject=self.subject,
            subject_type=self.subject_type,
            content=new_content,
            derived_from=new_derived_from,
            version=self.version + 1,
            created_at=self.created_at,
            updated_at=datetime.utcnow(),
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
