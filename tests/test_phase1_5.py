"""Tests for BLACKBOX Phase 1.5: Wiki and Three Shelves"""

import pytest
from datetime import datetime, timedelta, timezone
from blackbox.wiki import WikiPage, WikiUpdate
from blackbox.wiki_store import WikiStore
from blackbox.tiering import TieringManager
from blackbox.schema import EventType
from blackbox.event_store import EventStore


class TestWikiSchema:
    """Test Wiki page schema"""
    
    def test_create_wiki_page(self):
        """Test creating a Wiki page"""
        page = WikiPage(
            page_id="wiki-001",
            subject="CASE-001",
            subject_type="case",
            content={
                "status": "open",
                "summary": "Customer complaint about unauthorized transaction",
                "assigned_to": "intake_agent",
            },
            derived_from=["event-001", "event-002"],
            version=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        
        assert page.page_id == "wiki-001"
        assert page.subject == "CASE-001"
        assert page.version == 1
        assert len(page.derived_from) == 2
    
    def test_wiki_page_serialization(self):
        """Test Wiki page serialization to/from Firestore"""
        page = WikiPage(
            page_id="wiki-001",
            subject="CASE-001",
            subject_type="case",
            content={"status": "open"},
            derived_from=["event-001"],
            version=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        
        # Serialize
        data = page.to_firestore_dict()
        assert data["page_id"] == "wiki-001"
        assert isinstance(data["created_at"], str)
        
        # Deserialize
        restored = WikiPage.from_firestore_dict(data)
        assert restored.page_id == page.page_id
        assert restored.version == page.version
    
    def test_wiki_page_regeneration(self):
        """Test regenerating a Wiki page"""
        original = WikiPage(
            page_id="wiki-001",
            subject="CASE-001",
            subject_type="case",
            content={"status": "open"},
            derived_from=["event-001"],
            version=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        
        # Regenerate with new content
        regenerated = original.regenerate(
            new_content={"status": "closed", "resolution": "refunded"},
            new_derived_from=["event-001", "event-002", "event-003"],
        )
        
        assert regenerated.page_id == original.page_id
        assert regenerated.version == 2
        assert regenerated.content["status"] == "closed"
        assert len(regenerated.derived_from) == 3
        assert regenerated.created_at == original.created_at
        assert regenerated.updated_at > original.updated_at
    
    def test_wiki_update_record(self):
        """Test Wiki update record"""
        update = WikiUpdate(
            page_id="wiki-001",
            old_version=1,
            new_version=2,
            old_derived_from=["event-001"],
            new_derived_from=["event-001", "event-002"],
            timestamp=datetime.now(timezone.utc),
            reason="New event arrived",
        )
        
        assert update.page_id == "wiki-001"
        assert update.old_version == 1
        assert update.new_version == 2
        
        # Serialize
        data = update.to_firestore_dict()
        assert data["page_id"] == "wiki-001"
        assert isinstance(data["timestamp"], str)


class TestTiering:
    """Test three-shelf tiering system"""
    
    def test_tiering_manager_initialization(self):
        """Test TieringManager initialization.

        The dataset id moved onto the warm shelf when tiering was rewritten to
        actually move events, so this asserts against where it lives now.
        """
        event_store = EventStore(project_id="test-project")
        tiering = TieringManager(
            project_id="test-project",
            event_store=event_store,
            hot_ttl_days=7,
            cold_ttl_days=365,
        )

        assert tiering.hot_ttl_days == 7
        assert tiering.cold_ttl_days == 365
        # Shelves are built lazily, so constructing a manager needs no credentials.
        assert tiering._warm is None
        assert tiering._cold is None

    def test_the_warm_shelf_knows_its_dataset(self):
        from blackbox.shelves import BigQueryWarmShelf

        shelf = BigQueryWarmShelf(project_id="test-project")
        assert shelf.dataset_id == "test-project.blackbox_events"
        assert shelf.table_id == "test-project.blackbox_events.events"
        assert shelf._client is None, "the client should be lazy"

    def test_tiering_moves_events_rather_than_only_copying(self):
        """The Phase 1.5 failure mode, asserted directly.

        The earlier implementation printed a line and returned zero. Full
        coverage of the movement logic lives in test_tiering.py; this is the
        guard that the stub never comes back.
        """
        import inspect

        from blackbox import tiering as tiering_module

        source = inspect.getsource(tiering_module.TieringManager.tier_to_filing_cabinet)
        assert "evict" in source, "tiering copies without deleting"
        assert "events_match" in source, "tiering evicts without verifying"


class TestIntegration:
    """Integration tests for Phase 1.5"""
    
    def test_wiki_derived_from_tracking(self):
        """Test that Wiki pages track their derived_from correctly"""
        page = WikiPage(
            page_id="wiki-001",
            subject="CASE-001",
            subject_type="case",
            content={"status": "open"},
            derived_from=["event-001", "event-002"],
            version=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        
        # Verify derived_from is tracked
        assert "event-001" in page.derived_from
        assert "event-002" in page.derived_from
        
        # Regenerate with new events
        new_page = page.regenerate(
            new_content={"status": "in_progress"},
            new_derived_from=["event-001", "event-002", "event-003"],
        )
        
        # Verify new derived_from includes all events
        assert "event-003" in new_page.derived_from
        assert len(new_page.derived_from) == 3
    
    def test_wiki_update_creates_diary_event(self):
        """Test that Wiki updates create Diary events"""
        update = WikiUpdate(
            page_id="wiki-001",
            old_version=1,
            new_version=2,
            old_derived_from=["event-001"],
            new_derived_from=["event-001", "event-002"],
            timestamp=datetime.now(timezone.utc),
            reason="New event arrived",
        )
        
        # Verify the update record can be serialized
        data = update.to_firestore_dict()
        assert data["page_id"] == "wiki-001"
        assert data["old_version"] == 1
        assert data["new_version"] == 2
        assert data["reason"] == "New event arrived"
    
    def test_tiering_preserves_event_order(self):
        """Test that tiering preserves event order across shelves"""
        # This is a conceptual test - in production, you'd verify that
        # events read from BigQuery + Firestore are correctly ordered
        events = [
            {"timestamp": datetime.now(timezone.utc) - timedelta(days=10), "event_id": "old"},
            {"timestamp": datetime.now(timezone.utc) - timedelta(days=1), "event_id": "recent"},
        ]
        
        # Sort by timestamp
        events.sort(key=lambda e: e["timestamp"])
        
        assert events[0]["event_id"] == "old"
        assert events[1]["event_id"] == "recent"
