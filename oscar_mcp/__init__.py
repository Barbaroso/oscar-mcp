"""Read-only MCP server for OSCAR CPAP therapy data."""

from .analysis import DISCLAIMER, severity_band
from .database import OscarDatabase, ReadOnlyViolation, therapy_date
from .discovery import DataFolderNotFound, OscarLocation, discover

__version__ = "0.1.0"

__all__ = [
    "DISCLAIMER",
    "DataFolderNotFound",
    "OscarDatabase",
    "OscarLocation",
    "ReadOnlyViolation",
    "__version__",
    "discover",
    "severity_band",
    "therapy_date",
]
