"""
modules/connectors/base.py

Connector adapter base class and registry.

ARCHITECTURE NOTE:
connector_registry in PostgreSQL is the single source of truth for all
connector metadata — display_name, description, config_fields,
credential_fields, connector_group, sync_type, display_order.

CONNECTOR_TYPES below is retained only for backward compatibility with
RQ background jobs that cannot await a DB call. Do not add new connectors
here. Add a row to connector_registry via Alembic migration instead.
"""
from __future__ import annotations
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── Backward-compat stub — DO NOT ADD NEW CONNECTORS HERE ─────────────────────

CONNECTOR_TYPES: Dict[str, Dict[str, Any]] = {
    "timeslips": {
        "label": "Sage Timeslips",
        "description": "Live billing sync from Sage Timeslips via Windows agent.",
        "type": "agent_push",
        "icon": "clock",
        "slug": "timeslips",
    },
    "file_crawler": {
        "label": "CIFS File Crawler",
        "description": "Crawl CIFS/SMB shares — ingest documents into DMS.",
        "type": "agent_push",
        "icon": "folder",
        "slug": "file_crawler",
    },
    "exchange": {
        "label": "Microsoft Exchange",
        "description": "On-premises Exchange via EWS — email and billing reconciliation.",
        "type": "api_pull",
        "icon": "mail",
        "slug": "exchange",
    },
    "manictime": {
        "label": "ManicTime",
        "description": "Desktop activity tracking — feeds AI timesheet reconciliation.",
        "type": "api_pull",
        "icon": "activity",
        "slug": "manictime",
    },
    "pbx_cdr": {
        "label": "FreePBX Call Records",
        "description": "Call detail records — feeds billing reconciliation.",
        "type": "csv_import",
        "icon": "phone",
        "slug": "pbx_cdr",
    },
    "courtlistener": {
        "label": "CourtListener",
        "description": "Federal court data — dockets, opinions, filings.",
        "type": "api_pull",
        "icon": "gavel",
        "slug": "courtlistener",
    },
    "windows_agent": {
        "label": "Windows File Agent",
        "description": "Local file indexing agent on Windows Server.",
        "type": "agent_push",
        "icon": "server",
        "slug": "windows_agent",
    },
}


def get_connector_meta(connector_type: str) -> Dict[str, Any]:
    """
    Synchronous fallback metadata lookup from in-process dict.
    Used only by RQ background jobs that cannot await a DB call.
    All UI routes use ConnectorService methods instead.
    """
    return CONNECTOR_TYPES.get(connector_type, {
        "label": connector_type,
        "description": "",
        "type": "unknown",
        "icon": "plug",
        "slug": connector_type,
    })


def all_connector_types() -> list:
    return list(CONNECTOR_TYPES.keys())


# ── Status constants ───────────────────────────────────────────────────────────

class ConnectorStatus:
    HEALTHY      = "healthy"
    DEGRADED     = "degraded"
    ERROR        = "error"
    UNCONFIGURED = "unconfigured"
    DISABLED     = "disabled"


# ── Base class ─────────────────────────────────────────────────────────────────

class ConnectorBase(ABC):
    """
    Abstract base for all data connectors.
    Subclasses implement run_sync() for live connectors
    or process_csv() for CSV connectors.
    """
    connector_type: str = ""

    def __init__(self, tenant_id: str, connector_id: str, config: Dict[str, Any]):
        self.tenant_id    = tenant_id.strip()
        self.connector_id = connector_id
        self.config       = config
        self._sync_log_id: Optional[str] = None

    @abstractmethod
    def validate_config(self) -> tuple[bool, str]:
        """
        Validate connector config without making external connections.
        Returns (is_valid, error_message).
        """
        ...

    def run_sync(self) -> Dict[str, Any]:
        """Override for live connectors. Returns sync result dict."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support run_sync")

    def process_csv(self, file_path: str, original_filename: str) -> Dict[str, Any]:
        """Override for CSV import connectors. Returns import result dict."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support process_csv")

    def test_connection(self) -> tuple[bool, str]:
        """
        Test connectivity to the data source.
        Returns (success, message).
        Override in subclasses that support live connection testing.
        """
        return False, "Connection test not implemented for this connector type"

    def get_status(self) -> str:
        return ConnectorStatus.UNCONFIGURED
