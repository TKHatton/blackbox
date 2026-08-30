"""Wiki store for BLACKBOX memory layer"""

from typing import List, Optional, Dict, Any
from datetime import datetime
from google.cloud import firestore
from .wiki import WikiPage, WikiUpdate


class WikiStore:
    """
    Firestore-backed storage for Wiki pages.
    
    Wiki pages are rewritten in place, unlike Diary events which are append-only.
    This store manages the current state of each page.
    """
    
    def __init__(self, project_id: str):
        self.client = firestore.Client(project=project_id)
        self.collection = self.client.collection("wiki_pages")
    
    def get_page(self, page_id: str) -> Optional[WikiPage]:
        """Retrieve a Wiki page by ID"""
        doc = self.collection.document(page_id).get()
        if not doc.exists:
            return None
        return WikiPage.from_firestore_dict(doc.to_dict())
    
    def create_page(self, page: WikiPage) -> None:
        """Create a new Wiki page"""
        self.collection.document(page.page_id).set(page.to_firestore_dict())
    
    def update_page(self, page: WikiPage) -> None:
        """Update an existing Wiki page (overwrite)"""
        self.collection.document(page.page_id).set(page.to_firestore_dict())
    
    def delete_page(self, page_id: str) -> None:
        """Delete a Wiki page"""
        self.collection.document(page_id).delete()
    
    def list_pages_by_subject(self, subject: str) -> List[WikiPage]:
        """List all Wiki pages for a given subject"""
        query = self.collection.where("subject", "==", subject)
        pages = []
        for doc in query.stream():
            pages.append(WikiPage.from_firestore_dict(doc.to_dict()))
        return pages
    
    def list_pages_by_subject_type(self, subject_type: str) -> List[WikiPage]:
        """List all Wiki pages of a given subject type"""
        query = self.collection.where("subject_type", "==", subject_type)
        pages = []
        for doc in query.stream():
            pages.append(WikiPage.from_firestore_dict(doc.to_dict()))
        return pages
    
    def record_update(self, update: WikiUpdate) -> None:
        """
        Record a Wiki page update in the Diary.
        
        This creates an event in the Diary that tracks the change,
        allowing us to reconstruct the history of Wiki pages.
        """
        from .event_store import append_event
        from .schema import EventType
        
        # Create a MEMORY_WRITE event to record the Wiki update
        append_event(
            case_id=f"wiki:{update.page_id}",  # Special case_id for Wiki updates
            event_type=EventType.MEMORY_WRITE,
            payload={
                "memory_key": f"wiki:{update.page_id}",
                "content": {
                    "old_version": update.old_version,
                    "new_version": update.new_version,
                    "reason": update.reason,
                },
                "reason": update.reason,
            },
            actor="wiki_store",
        )
