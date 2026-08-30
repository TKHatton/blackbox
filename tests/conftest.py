"""Shared test setup.

Every test runs against the in-memory backend with tracing turned off, so the
suite needs no Google Cloud credentials and makes no network calls. The
environment is set before ``blackbox`` is imported anywhere, because the settings
object is cached for the life of the process.
"""

import os

os.environ.setdefault("BLACKBOX_IN_MEMORY", "1")
os.environ.setdefault("TRACE_EXPORTER", "none")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "blackbox-test")

import pytest  # noqa: E402

from blackbox.backends import InMemoryBackend  # noqa: E402
from blackbox.config import reset_settings_cache  # noqa: E402
from blackbox.event_store import EventStore  # noqa: E402
from blackbox.recorder import Recorder  # noqa: E402
from blackbox.stubs.systems import SourceSystems  # noqa: E402
from blackbox.wiki_store import WikiStore  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_settings():
    """Drop cached settings between tests so env changes take effect."""
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def backend() -> InMemoryBackend:
    return InMemoryBackend()


@pytest.fixture
def store(backend: InMemoryBackend) -> EventStore:
    return EventStore(project_id="blackbox-test", backend=backend)


@pytest.fixture
def wiki(store: EventStore) -> WikiStore:
    return WikiStore(project_id="blackbox-test", event_store=store, in_memory=True)


@pytest.fixture
def systems() -> SourceSystems:
    """A fresh stub estate per test, so CommsVault job ids do not leak between them."""
    return SourceSystems()


@pytest.fixture
def recorder(store: EventStore) -> Recorder:
    return Recorder(case_id="CASE-TEST-001", actor="test_actor", store=store)
