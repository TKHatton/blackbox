"""Tiering system for BLACKBOX event storage"""

from datetime import datetime, timedelta
from typing import List, Optional
from google.cloud import bigquery, storage
from .event_store import EventStore
from .schema import Event


class TieringManager:
    """
    Manages the three-shelf storage system for events.
    
    Shelf 1 (Desk): Firestore - active cases, recent events (< 7 days)
    Shelf 2 (Filing Cabinet): BigQuery - older events (7 days to retention window)
    Shelf 3 (Warehouse): Cloud Storage - cold storage (beyond retention window)
    
    This keeps Firestore small and fast while maintaining full history.
    """
    
    def __init__(
        self,
        project_id: str,
        event_store: EventStore,
        hot_ttl_days: int = 7,
        cold_ttl_days: int = 365,
        bucket_name: Optional[str] = None,
    ):
        self.project_id = project_id
        self.event_store = event_store
        self.hot_ttl_days = hot_ttl_days
        self.cold_ttl_days = cold_ttl_days
        self.bucket_name = bucket_name
        
        # Lazy-initialized clients
        self._bq_client = None
        self._storage_client = None
        
        # BigQuery dataset and table
        self.dataset_id = f"{project_id}.blackbox_events"
        self.table_id = f"{self.dataset_id}.events"
    
    @property
    def bq_client(self):
        """Lazy initialization of BigQuery client"""
        if self._bq_client is None:
            from google.cloud import bigquery
            self._bq_client = bigquery.Client(project=self.project_id)
        return self._bq_client
    
    @property
    def storage_client(self):
        """Lazy initialization of Storage client"""
        if self._storage_client is None and self.bucket_name:
            from google.cloud import storage
            self._storage_client = storage.Client(project=self.project_id)
        return self._storage_client
    
    def ensure_bigquery_schema(self) -> None:
        """Create BigQuery dataset and table if they don't exist"""
        # Create dataset
        dataset = bigquery.Dataset(self.dataset_id)
        dataset.location = "US"
        try:
            self.bq_client.create_dataset(dataset, exists_ok=True)
        except Exception as e:
            print(f"Warning: Could not create dataset: {e}")
        
        # Create table schema
        schema = [
            bigquery.SchemaField("event_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("actor", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("event_type", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("caused_by", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("payload", "JSON", mode="REQUIRED"),
            bigquery.SchemaField("labels", "JSON", mode="NULLABLE"),
            bigquery.SchemaField("trace_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("span_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("case_id", "STRING", mode="REQUIRED"),
        ]
        
        table = bigquery.Table(self.table_id, schema=schema)
        try:
            self.bq_client.create_table(table, exists_ok=True)
        except Exception as e:
            print(f"Warning: Could not create table: {e}")
    
    def tier_old_events(self) -> int:
        """
        Move events older than hot_ttl_days from Firestore to BigQuery.
        
        Returns the number of events moved.
        """
        cutoff = datetime.utcnow() - timedelta(days=self.hot_ttl_days)
        
        # Get all events older than cutoff
        # Note: This is a simplified implementation. In production, you'd want
        # to query by timestamp index or use a more efficient method.
        events_to_move = []
        
        # For now, we'll skip the actual implementation since we don't have
        # a way to query all events across all cases efficiently.
        # In production, you'd maintain a separate index or use Firestore queries.
        
        print(f"Tiering: Would move events older than {cutoff}")
        print("Note: Full implementation requires efficient event querying")
        
        return len(events_to_move)
    
    def archive_to_cold_storage(self) -> int:
        """
        Move events older than cold_ttl_days from BigQuery to Cloud Storage.
        
        Returns the number of events archived.
        """
        if not self.bucket_name:
            print("Warning: No bucket_name configured, skipping cold storage")
            return 0
        
        cutoff = datetime.utcnow() - timedelta(days=self.cold_ttl_days)
        
        # Query BigQuery for old events
        query = f"""
            SELECT * FROM `{self.table_id}`
            WHERE timestamp < @cutoff
        """
        
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("cutoff", "TIMESTAMP", cutoff)
            ]
        )
        
        try:
            query_job = self.bq_client.query(query, job_config=job_config)
            results = query_job.result()
            
            # Export to Cloud Storage as JSON
            bucket = self.storage_client.bucket(self.bucket_name)
            date_str = datetime.utcnow().strftime("%Y%m%d")
            blob_name = f"events/{date_str}/archive.json"
            blob = bucket.blob(blob_name)
            
            # Convert results to JSON and upload
            events_data = [dict(row) for row in results]
            import json
            blob.upload_from_string(
                json.dumps(events_data, default=str),
                content_type="application/json"
            )
            
            # Delete from BigQuery
            delete_query = f"""
                DELETE FROM `{self.table_id}`
                WHERE timestamp < @cutoff
            """
            delete_job = self.bq_client.query(
                delete_query,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("cutoff", "TIMESTAMP", cutoff)
                    ]
                )
            )
            delete_job.result()
            
            return len(events_data)
            
        except Exception as e:
            print(f"Error archiving to cold storage: {e}")
            return 0
    
    def read_event(self, event_id: str) -> Optional[Event]:
        """
        Read an event from any shelf.
        
        Checks Firestore first, then BigQuery, then Cloud Storage.
        """
        # Try Firestore first (hot storage)
        event = self.event_store.get_event(event_id)
        if event:
            return event
        
        # Try BigQuery (warm storage)
        try:
            query = f"""
                SELECT * FROM `{self.table_id}`
                WHERE event_id = @event_id
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("event_id", "STRING", event_id)
                ]
            )
            results = self.bq_client.query(query, job_config=job_config).result()
            
            for row in results:
                data = dict(row)
                # Convert types
                if isinstance(data.get("timestamp"), str):
                    data["timestamp"] = datetime.fromisoformat(data["timestamp"])
                return Event(**data)
        except Exception as e:
            print(f"Error reading from BigQuery: {e}")
        
        # Try Cloud Storage (cold storage)
        # This would require knowing which archive file contains the event
        # For now, we'll skip this implementation
        print("Warning: Cold storage retrieval not yet implemented")
        
        return None
    
    def read_case_events(self, case_id: str) -> List[Event]:
        """
        Read all events for a case from any shelf.
        
        Combines results from Firestore, BigQuery, and Cloud Storage.
        """
        events = []
        
        # Get from Firestore
        firestore_events = self.event_store.list_events(case_id)
        events.extend(firestore_events)
        
        # Get from BigQuery
        try:
            query = f"""
                SELECT * FROM `{self.table_id}`
                WHERE case_id = @case_id
                ORDER BY timestamp ASC
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("case_id", "STRING", case_id)
                ]
            )
            results = self.bq_client.query(query, job_config=job_config).result()
            
            for row in results:
                data = dict(row)
                if isinstance(data.get("timestamp"), str):
                    data["timestamp"] = datetime.fromisoformat(data["timestamp"])
                events.append(Event(**data))
        except Exception as e:
            print(f"Error reading from BigQuery: {e}")
        
        # Sort by timestamp
        events.sort(key=lambda e: e.timestamp)
        
        return events
