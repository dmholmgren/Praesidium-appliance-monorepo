"""
Central model registry — import all models here so Alembic can discover them.
"""

from core.models.tenant import Tenant, TenantBranding
from core.models.user import User
from core.models.client import Client
from core.models.matter import Matter, MatterTimekeeper
from core.models.contact import Contact, MatterContact
from core.models.audit import AuditLog
from core.models.billing import TimeEntry, Invoice, InvoiceMatter, Payment, Distribution
from core.models.document import Document, DocumentTimeTracking
from core.models.court import Deadline
from core.models.ediscovery import EDiscoveryCollection, EDiscoveryDocument
from core.models.intelligence import LearningSignal, LearnedPreference, AIApiCall

__all__ = [
    "Tenant", "TenantBranding",
    "User",
    "Client",
    "Matter", "MatterTimekeeper",
    "Contact", "MatterContact",
    "AuditLog",
    "TimeEntry", "Invoice", "InvoiceMatter", "Payment", "Distribution",
    "Document", "DocumentTimeTracking",
    "Deadline",
    "EDiscoveryCollection", "EDiscoveryDocument",
    "LearningSignal", "LearnedPreference", "AIApiCall",
]
