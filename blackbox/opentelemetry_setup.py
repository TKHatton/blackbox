"""OpenTelemetry setup for BLACKBOX"""

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.resources import Resource


def get_tracer():
    """Get or create a tracer instance"""
    # Create resource
    resource = Resource.create({
        "service.name": "blackbox",
        "service.version": "1.0.0"
    })
    
    # Create tracer provider
    provider = TracerProvider(resource=resource)
    
    # Add console exporter for now (can be replaced with Cloud Trace in production)
    exporter = ConsoleSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    
    # Set as global tracer provider
    trace.set_tracer_provider(provider)
    
    # Return tracer
    return trace.get_tracer(__name__)
