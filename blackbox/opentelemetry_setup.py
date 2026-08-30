"""OpenTelemetry setup for BLACKBOX.

Two jobs:

1. Configure exactly one TracerProvider for the process. The previous version
   built a new provider on every call, which orphaned spans and meant the
   exporter was replaced continuously.
2. Expose the *current* trace and span ids, so every Flight Recorder event can
   record where in the causal tree it was written. The Event schema requires
   these, and they are what makes the log readable in Cloud Trace.
"""

import logging
from typing import Tuple

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from .config import get_settings

logger = logging.getLogger(__name__)

_INSTRUMENTATION_NAME = "blackbox"

# All-zero ids are what OpenTelemetry reports when no span is active. The Event
# schema still needs a value, so we record the invalid ids rather than inventing
# plausible-looking ones.
INVALID_TRACE_ID = "0" * 32
INVALID_SPAN_ID = "0" * 16

_configured = False


def configure_tracing(force: bool = False) -> None:
    """Install the global TracerProvider. Safe to call more than once."""
    global _configured
    if _configured and not force:
        return

    settings = get_settings()
    resource = Resource.create(
        {
            "service.name": "blackbox",
            "service.version": "0.2.0",
        }
    )
    provider = TracerProvider(resource=resource)

    exporter = None
    if settings.trace_exporter == "cloud_trace":
        try:
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

            exporter = CloudTraceSpanExporter(project_id=settings.require_project_id())
        except Exception as exc:  # pragma: no cover - depends on cloud credentials
            logger.warning("Cloud Trace exporter unavailable, falling back to console: %s", exc)
    elif settings.trace_exporter == "none":
        exporter = None
    else:
        exporter = ConsoleSpanExporter()

    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    _configured = True


def get_tracer() -> trace.Tracer:
    """Return the BLACKBOX tracer, configuring the provider on first use."""
    configure_tracing()
    return trace.get_tracer(_INSTRUMENTATION_NAME)


def current_trace_ids() -> Tuple[str, str]:
    """Return (trace_id, span_id) for the active span as zero-padded hex.

    Returns the all-zero ids when no span is active, which is what the
    OpenTelemetry spec calls an invalid context. Callers should not treat that
    as an error: it simply means the event was written outside a span.
    """
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return INVALID_TRACE_ID, INVALID_SPAN_ID
    return format(span_context.trace_id, "032x"), format(span_context.span_id, "016x")
