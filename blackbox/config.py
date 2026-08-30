"""Runtime configuration for BLACKBOX, loaded from the environment."""

import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Load .env from the repo root if present. Cloud Run supplies real env vars instead.
load_dotenv()


class Settings(BaseModel):
    """Every knob BLACKBOX reads at runtime.

    Nothing here has a secret as a default. A missing value fails loudly at the
    point of use rather than silently pointing at the wrong Google Cloud project.
    """

    # Google Cloud
    project_id: str = Field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_PROJECT", ""))
    location: str = Field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"))

    # Gemini. The spec allows either Vertex AI or the Gemini API; both are Gemini,
    # and no other model provider appears anywhere in this codebase.
    #
    # Vertex AI is the default and what production uses. The Gemini API path exists
    # because Vertex AI inference requires billing on the project, and the Gemini
    # API free tier does not, so development is not blocked while billing is sorted.
    gemini_model: str = Field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-3.5-flash"))
    use_vertex: bool = Field(
        default_factory=lambda: os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "TRUE").upper() == "TRUE"
    )
    gemini_api_key: str = Field(
        default_factory=lambda: os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY", "")
    )

    def apply_genai_env(self) -> None:
        """Push the Gemini settings into the environment the SDK reads.

        google-genai and ADK both resolve their transport from environment
        variables rather than from arguments. Setting them in one place keeps the
        choice of Vertex AI or the Gemini API a configuration decision instead of
        something scattered through the agent modules.
        """
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE" if self.use_vertex else "FALSE"
        if self.use_vertex:
            if self.project_id:
                os.environ["GOOGLE_CLOUD_PROJECT"] = self.project_id
            os.environ["GOOGLE_CLOUD_LOCATION"] = self.location
        elif self.gemini_api_key:
            os.environ["GOOGLE_API_KEY"] = self.gemini_api_key

    # Pub/Sub
    complaints_topic: str = Field(
        default_factory=lambda: os.getenv("COMPLAINTS_TOPIC", "blackbox-complaints")
    )

    # Firestore. The database is a named (non-default) one, so every client must
    # pass database= explicitly or it silently talks to "(default)" instead.
    firestore_database: str = Field(
        default_factory=lambda: os.getenv("FIRESTORE_DATABASE", "(default)")
    )
    events_collection: str = Field(
        default_factory=lambda: os.getenv("EVENTS_COLLECTION", "events")
    )
    wiki_collection: str = Field(
        default_factory=lambda: os.getenv("WIKI_COLLECTION", "wiki_pages")
    )

    # Tracing. Console exporter is the local default; Cloud Trace is used on Cloud Run.
    trace_exporter: str = Field(
        default_factory=lambda: os.getenv("TRACE_EXPORTER", "console").lower()
    )

    # Test mode swaps Firestore for an in-memory store so the suite runs with no credentials.
    in_memory: bool = Field(
        default_factory=lambda: os.getenv("BLACKBOX_IN_MEMORY", "").lower() in ("1", "true", "yes")
    )

    def require_project_id(self) -> str:
        if not self.project_id:
            raise RuntimeError(
                "GOOGLE_CLOUD_PROJECT is not set. Copy .env.example to .env and fill it in."
            )
        return self.project_id


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings. Used by tests that manipulate the environment."""
    get_settings.cache_clear()
