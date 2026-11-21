"""Abstract base adapter interface for all NagpurBus transit data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ingest.models import GTFSFeedBundle


class TransitIngestError(RuntimeError):
    """Explicit error raised when a data source fails validation or ingestion."""


class BaseTransitAdapter(ABC):
    """Common interface for all static transit adapters (GTFS, OSM, Maha Metro PDFs)."""

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Unique machine-readable name of this adapter."""

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if required URLs/files are present and ready to ingest."""

    @abstractmethod
    def fetch_and_normalize(self) -> GTFSFeedBundle:
        """Fetch source data and normalize to the internal GTFS bundle."""
