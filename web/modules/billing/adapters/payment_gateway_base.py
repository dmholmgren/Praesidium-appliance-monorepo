"""
modules/billing/adapters/payment_gateway_base.py

Payment gateway abstraction layer with status, diagnosis, and testing.

Every payment connector implements this interface. The platform supports
multiple gateways per tenant — LawPay for trust (bar-required), Braintree
for client portal payments (PayPal, Venmo, cards, Apple Pay, Google Pay).

Status/diagnosis/test contract:
  - status()     → ConnectorHealth (quick — cached, no external calls)
  - diagnose()   → DiagnosisReport (deep — makes test API calls, checks creds)
  - test()       → TestResult (creates sandbox transaction or validates connectivity)
"""
from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Status & Diagnosis Types ──────────────────────────────────────────────────

class GatewayStatus(str, Enum):
    HEALTHY      = "healthy"
    DEGRADED     = "degraded"
    ERROR        = "error"
    UNCONFIGURED = "unconfigured"
    DISABLED     = "disabled"
    SANDBOX      = "sandbox"


class GatewayCapability(str, Enum):
    """What a gateway can do — drives UI display logic."""
    OPERATING_PAYMENTS = "operating_payments"
    TRUST_PAYMENTS     = "trust_payments"
    CREDIT_CARD        = "credit_card"
    DEBIT_CARD         = "debit_card"
    ACH                = "ach"
    PAYPAL             = "paypal"
    VENMO              = "venmo"
    APPLE_PAY          = "apple_pay"
    GOOGLE_PAY         = "google_pay"
    INVOICING          = "invoicing"
    REFUNDS            = "refunds"
    PARTIAL_PAYMENTS   = "partial_payments"
    WEBHOOKS           = "webhooks"
    QR_CODE            = "qr_code"


@dataclass
class ConnectorHealth:
    """Quick status check — no external calls, reads from cached state."""
    gateway_type: str
    status: GatewayStatus
    message: str
    last_successful_call: Optional[datetime] = None
    last_error: Optional[str] = None
    last_error_at: Optional[datetime] = None
    credentials_valid: bool = False
    credentials_expire_at: Optional[datetime] = None
    is_sandbox: bool = False
    capabilities: List[GatewayCapability] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gateway_type": self.gateway_type,
            "status": self.status.value,
            "message": self.message,
            "last_successful_call": self.last_successful_call.isoformat() if self.last_successful_call else None,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            "credentials_valid": self.credentials_valid,
            "credentials_expire_at": self.credentials_expire_at.isoformat() if self.credentials_expire_at else None,
            "is_sandbox": self.is_sandbox,
            "capabilities": [c.value for c in self.capabilities],
        }


@dataclass
class DiagnosisCheck:
    """Single check in a diagnosis run."""
    name: str
    passed: bool
    message: str
    duration_ms: float = 0.0
    details: Optional[Dict[str, Any]] = None


@dataclass
class DiagnosisReport:
    """Deep connectivity and configuration validation."""
    gateway_type: str
    overall_status: GatewayStatus
    checks: List[DiagnosisCheck] = field(default_factory=list)
    run_at: datetime = field(default_factory=datetime.utcnow)
    duration_ms: float = 0.0

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed_checks(self) -> List[DiagnosisCheck]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gateway_type": self.gateway_type,
            "overall_status": self.overall_status.value,
            "passed": self.passed,
            "checks": [
                {"name": c.name, "passed": c.passed, "message": c.message,
                 "duration_ms": c.duration_ms, "details": c.details}
                for c in self.checks
            ],
            "failed_checks": [c.name for c in self.failed_checks],
            "run_at": self.run_at.isoformat(),
            "duration_ms": self.duration_ms,
        }


@dataclass
class TestResult:
    """Result of a sandbox/test transaction."""
    success: bool
    gateway_type: str
    test_type: str  # "ping", "sandbox_charge", "token_refresh", "webhook_verify"
    message: str
    transaction_id: Optional[str] = None
    response_data: Optional[Dict[str, Any]] = None
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "gateway_type": self.gateway_type,
            "test_type": self.test_type,
            "message": self.message,
            "transaction_id": self.transaction_id,
            "duration_ms": self.duration_ms,
        }


# ── Payment Data Types ────────────────────────────────────────────────────────

@dataclass
class PaymentRequest:
    """Standardized payment request across all gateways."""
    invoice_id: str
    invoice_number: str
    amount_cents: int
    currency: str = "USD"
    client_name: str = ""
    client_email: str = ""
    description: str = ""
    is_trust: bool = False
    matter_number: Optional[str] = None
    return_url: Optional[str] = None
    cancel_url: Optional[str] = None
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class PaymentResult:
    """Standardized payment response across all gateways."""
    success: bool
    gateway_type: str
    payment_url: Optional[str] = None  # Redirect URL for hosted checkout
    transaction_id: Optional[str] = None  # Gateway's transaction/charge ID
    status: str = ""  # pending, completed, failed
    error_message: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None


@dataclass
class WebhookEvent:
    """Standardized webhook event across all gateways."""
    gateway_type: str
    event_type: str  # payment.completed, payment.failed, refund.completed, etc.
    transaction_id: str
    invoice_reference: str
    amount_cents: int
    currency: str = "USD"
    status: str = ""
    raw_payload: Optional[Dict[str, Any]] = None


# ── Base Class ────────────────────────────────────────────────────────────────

class PaymentGatewayBase(ABC):
    """
    Abstract base for all payment gateway adapters.

    Every adapter MUST implement:
      - status()           Quick health check from cached state
      - diagnose()         Deep validation (API calls, cred checks)
      - test_connection()  Lightweight connectivity test
      - create_payment()   Create payment link / checkout session
      - verify_webhook()   Validate webhook signature
      - process_webhook()  Parse webhook into standardized WebhookEvent

    Optional overrides:
      - refund()           Process refund
      - get_transaction()  Look up transaction details
      - close()            Cleanup resources
    """

    GATEWAY_TYPE: str = ""
    DISPLAY_NAME: str = ""
    CAPABILITIES: List[GatewayCapability] = []

    def __init__(self, tenant_id: str, config: Dict[str, Any]):
        self.tenant_id = tenant_id.strip()
        self.config = config
        self._last_success: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._last_error_at: Optional[datetime] = None

    def _record_success(self):
        self._last_success = datetime.utcnow()

    def _record_error(self, msg: str):
        self._last_error = msg
        self._last_error_at = datetime.utcnow()

    # ── Status / Diagnosis / Test ─────────────────────────────────────────

    @abstractmethod
    async def status(self) -> ConnectorHealth:
        """Quick status — no external calls. Read from cached/config state."""
        ...

    @abstractmethod
    async def diagnose(self) -> DiagnosisReport:
        """Deep diagnosis — makes test API calls, validates creds, checks config."""
        ...

    @abstractmethod
    async def test_connection(self) -> TestResult:
        """Lightweight connectivity test — ping endpoint or validate token."""
        ...

    # ── Payment Operations ────────────────────────────────────────────────

    @abstractmethod
    async def create_payment(self, request: PaymentRequest) -> PaymentResult:
        """Create a payment link or checkout session."""
        ...

    @abstractmethod
    async def verify_webhook(self, headers: Dict[str, str], body: bytes) -> bool:
        """Validate webhook signature. Returns True if authentic."""
        ...

    @abstractmethod
    async def process_webhook(self, headers: Dict[str, str], body: bytes) -> Optional[WebhookEvent]:
        """Parse webhook payload into standardized event."""
        ...

    # ── Optional Operations ───────────────────────────────────────────────

    async def refund(self, transaction_id: str, amount_cents: Optional[int] = None,
                     reason: str = "") -> PaymentResult:
        """Process full or partial refund. Override in subclasses that support it."""
        return PaymentResult(success=False, gateway_type=self.GATEWAY_TYPE,
                             error_message="Refunds not supported by this gateway")

    async def get_transaction(self, transaction_id: str) -> Optional[Dict[str, Any]]:
        """Look up transaction details by ID. Override in subclasses."""
        return None

    async def close(self):
        """Cleanup HTTP clients, connections. Override in subclasses."""
        pass
