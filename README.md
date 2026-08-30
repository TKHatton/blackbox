# BLACKBOX: The Flight Recorder for AI Agents

An autonomous fleet of AI agents that performs a regulated business workflow, sitting on top of an unerasable recording layer. Because everything the agents do is recorded, the platform can do things no current agent system can do: rewind any decision, replay it under different rules, trace any output back to the data that shaped it, and prove regulated data never reached where it shouldn't.

## Current Status

✅ **Phase 1: The Flight Recorder** - Complete  
✅ **Phase 1.5: The Wiki and Three Shelves** - Complete  
⏳ **Phase 2: One Agent, Deployed** - Next  

## Architecture

### Phase 1: Flight Recorder (Event Store)

The append-only event log that is the sole source of truth for what happened.

**Key Components:**
- **Event Schema** (`blackbox/schema.py`): Pydantic models with strict validation
  - 10 event types: THOUGHT, TOOL_CALL, TOOL_RESULT, MEMORY_READ, MEMORY_WRITE, POLICY_CHECK, MESSAGE_SENT, SUSPEND, RESUME, ESCALATE
  - Each event type has a specific payload schema
  - Events are immutable (frozen Pydantic models)
  - ULIDs for sortable, collision-resistant IDs
  - Causal chains via `caused_by` field
  
- **Event Store** (`blackbox/event_store.py`): Class-based Firestore storage
  - `EventStore` class with lazy initialization
  - Methods: `append_event()`, `get_event()`, `list_events()`, `list_events_by_type()`, `get_causal_chain()`, `get_children()`
  - **Append-only guarantee**: No update/delete/patch/modify/remove methods exist
  - OpenTelemetry integration for distributed tracing
  
- **Fold Function** (`blackbox/fold.py`): Pure function to compute state
  - `fold_case()`: Computes current state from event log
  - `fold_events()`: Computes state from a list of events (for testing)
  - Deterministic: same events always produce same state
  - No caching, no side effects

**Storage Structure:**
```
Firestore:
  cases/{case_id}/events/{event_id}
```

### Phase 1.5: Wiki and Three Shelves

Keeps the hot path small and fast forever, while retaining full history.

**Key Components:**

- **Wiki** (`blackbox/wiki.py`): Condensed, current knowledge
  - `WikiPage` model with `derived_from` tracking
  - Pages are rewritten in place (unlike Diary events)
  - Tracks which events produced this content
  - Enables The Eraser (Phase 5) to cascade retractions
  
- **Wiki Store** (`blackbox/wiki_store.py`): Firestore-backed storage
  - CRUD operations for Wiki pages
  - Records updates in the Diary for audit trail
  
- **Tiering System** (`blackbox/tiering.py`): Three-shelf storage
  - **Shelf 1 (Desk)**: Firestore - active cases, recent events (< 7 days)
  - **Shelf 2 (Filing Cabinet)**: BigQuery - older events (7 days to retention window)
  - **Shelf 3 (Warehouse)**: Cloud Storage - cold storage (beyond retention window)
  - `TieringManager` with lazy initialization
  - Transparent reads across all shelves
  - Keeps Firestore small and fast

**Storage Structure:**
```
Firestore (Hot):
  cases/{case_id}/events/{event_id}  # Recent events
  wiki_pages/{page_id}               # Current knowledge

BigQuery (Warm):
  blackbox_events.events             # Older events (searchable)

Cloud Storage (Cold):
  gs://bucket/events/{date}/         # Archived events
```

## Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Install the package in development mode
pip install -e .
```

## Setup

Set up Google Cloud credentials:

```bash
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account-key.json"
export GOOGLE_CLOUD_PROJECT="your-project-id"
```

## Usage

### Phase 1: Writing Events

```python
from blackbox.event_store import EventStore
from blackbox.schema import EventType

# Initialize event store
store = EventStore(project_id="your-project-id")

# Root event (no parent)
event_id = store.append_event(
    case_id="CASE-001",
    event_type=EventType.THOUGHT,
    payload={
        "reasoning": "Customer complaint about unauthorized transaction",
        "decision": "Classify as fraud investigation",
        "confidence": 0.95,
        "context_summary": "Initial intake of complaint"
    },
    actor="intake_agent"
)

# Child event (with parent)
child_event_id = store.append_event(
    case_id="CASE-001",
    event_type=EventType.TOOL_CALL,
    payload={
        "tool_name": "CoreBank.get_transactions",
        "parameters": {"account_id": "ACC-123", "days": 30},
        "intended_outcome": "Retrieve recent transactions for investigation"
    },
    caused_by=event_id,
    actor="evidence_agent"
)
```

### Phase 1: Folding Events

```python
from blackbox.fold import fold_case

# Compute current state from event log
state = fold_case("CASE-001", project_id="your-project-id")
print(f"Status: {state.current_status}")
print(f"Events: {len(state.events)}")
print(f"Pending actions: {state.pending_actions}")
```

### Phase 1.5: Wiki Pages

```python
from blackbox.wiki import WikiPage
from blackbox.wiki_store import WikiStore
from datetime import datetime

# Initialize Wiki store
wiki_store = WikiStore(project_id="your-project-id")

# Create a Wiki page
page = WikiPage(
    page_id="wiki-case-001",
    subject="CASE-001",
    subject_type="case",
    content={
        "status": "open",
        "summary": "Customer complaint about unauthorized transaction",
        "assigned_to": "intake_agent",
        "priority": "high"
    },
    derived_from=["event-001", "event-002", "event-003"],
    version=1,
    created_at=datetime.utcnow(),
    updated_at=datetime.utcnow()
)

wiki_store.create_page(page)

# Update the page when new events arrive
updated_page = page.regenerate(
    new_content={
        "status": "in_progress",
        "summary": "Evidence gathered, awaiting assessment",
        "assigned_to": "assessment_agent",
        "priority": "high"
    },
    new_derived_from=["event-001", "event-002", "event-003", "event-004"]
)

wiki_store.update_page(updated_page)
```

### Phase 1.5: Tiering

```python
from blackbox.tiering import TieringManager
from blackbox.event_store import EventStore

# Initialize tiering manager
event_store = EventStore(project_id="your-project-id")
tiering = TieringManager(
    project_id="your-project-id",
    event_store=event_store,
    hot_ttl_days=7,      # Events older than 7 days move to BigQuery
    cold_ttl_days=365,   # Events older than 365 days move to Cloud Storage
    bucket_name="blackbox-archive"
)

# Create BigQuery schema (run once)
tiering.ensure_bigquery_schema()

# Tier old events (run periodically, e.g., daily)
events_moved = tiering.tier_old_events()
print(f"Moved {events_moved} events to BigQuery")

# Archive to cold storage (run periodically, e.g., monthly)
events_archived = tiering.archive_to_cold_storage()
print(f"Archived {events_archived} events to Cloud Storage")

# Read events (transparent across all shelves)
events = tiering.read_case_events("CASE-001")
print(f"Total events: {len(events)}")
```

## Testing

Run all tests:

```bash
pytest tests/ -v
```

Run specific test suites:

```bash
# Phase 1: Schema tests
pytest tests/test_schema.py -v

# Phase 1: Destructive tests (proves append-only guarantee)
pytest tests/test_destructive.py -v

# Phase 1.5: Wiki and tiering tests
pytest tests/test_phase1_5.py -v
```

**Test Results:**
- 39 tests passing
- 0 failures
- Coverage: Event schema, payload validation, causal chains, append-only guarantee, Wiki pages, tiering system

## Hard Constraints

✓ All inference through Gemini 3.5 Flash (Phase 2+)  
✓ Google Cloud only (Firestore, BigQuery, Cloud Storage, Cloud Trace)  
✓ Append-only: no update() or delete() methods exist on EventStore  
✓ Causal tree: caused_by links form parent-child relationships  
✓ Immutable events: Pydantic frozen models prevent mutation  
✓ ULIDs: Sortable, collision-resistant event IDs  
✓ Lazy initialization: Clients only connect when needed  

## What's Next

### Phase 2: One Agent, Deployed
- Deploy Intake Agent to Cloud Run
- Integrate with Gemini 3.5 Flash via Vertex AI
- Record all reasoning as THOUGHT events
- Stub services for CoreBank, CRM360, CommsVault
- Live `.run` URL with Cloud Trace visualization

### Phase 3: The Fleet Wakes Itself Up
- Suspend/resume mechanism for long-running workflows
- Pub/Sub triggers for external events
- Cloud Scheduler for heartbeat and wake conditions
- Multiple agents collaborating on complaint workflow

### Phase 4: Invisible Ink
- Data labeling system for sensitive information
- Label propagation through Gemini calls
- Exit checks before outbound actions
- Taint path queries for blocked disclosures

## Domain: Regulated Complaint Handling

This system is designed for a mid-size retail bank operating in the US, UK, and EU. The workflow involves:

- **6 specialized agents**: Intake, Evidence, Assessment, Remediation, Correspondence, Compliance Officer
- **Statutory deadlines**: Complaint handling runs on legal clocks
- **Sensitive data**: Health disclosures, financial records, PII across jurisdictions
- **Human approval gates**: Monetary thresholds and systemic pattern flags
- **Cross-border transfers**: EU data protection rules

Every feature in BLACKBOX has work to do in this workflow.

## Project Structure

```
blackbox/
├── blackbox/
│   ├── __init__.py
│   ├── schema.py              # Event and payload schemas
│   ├── event_store.py         # Append-only event storage
│   ├── fold.py                # State computation from events
│   ├── wiki.py                # Wiki page schemas
│   ├── wiki_store.py          # Wiki storage
│   ├── tiering.py             # Three-shelf storage system
│   └── opentelemetry_setup.py # Tracing configuration
├── tests/
│   ├── test_schema.py         # Schema validation tests
│   ├── test_destructive.py    # Append-only guarantee tests
│   └── test_phase1_5.py       # Wiki and tiering tests
├── requirements.txt
├── setup.py
└── README.md
```

## License

Private - Hackathon Project

## Contact

Built for the "All Things Agentic" hackathon (Google, August 2026).
