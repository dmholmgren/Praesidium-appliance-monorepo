"""
modules/billing/services/payment_gateway_service.py

Central registry for all payment gateway and accounting connectors.
Provides unified status, diagnosis, and testing across all integrations.

Usage:
    svc = PaymentGatewayService(tenant_id, db)
    await svc.load_gateways()

    # Dashboard: all connector statuses
    statuses = await svc.all_statuses()

    # Deep diagnosis on one connector
    report = await svc.diagnose("lawpay")

    # Test connectivity
    result = await svc.test("braintree")

    # Create payment via specific gateway
    result = await svc.create_payment("lawpay", request)
"""
import logging
from typing import Any, Dict, List, Optional

from modules.billing.adapters.payment_gateway_base import (
    ConnectorHealth,
    DiagnosisReport,
    GatewayStatus,
    PaymentGatewayBase,
    PaymentRequest,
    PaymentResult,
    TestResult,
    WebhookEvent,
)
from modules.billing.adapters.lawpay_gateway import LawPayGateway
from modules.billing.adapters.braintree_gateway import BraintreeGateway
from modules.billing.adapters.paypal_gateway import PayPalGateway
from modules.billing.adapters.qbo_gateway import QBOGateway

logger = logging.getLogger(__name__)


# ── Gateway type → class mapping ──────────────────────────────────────────────

GATEWAY_CLASSES: Dict[str, type] = {
    "lawpay": LawPayGateway,
    "braintree": BraintreeGateway,
    "paypal": PayPalGateway,
}

ACCOUNTING_CLASSES: Dict[str, type] = {
    "qbo_online": QBOGateway,
}

# All connector types for the dashboard
ALL_CONNECTOR_META = {
    "lawpay": {
        "display_name": "LawPay",
        "description": "Bar-approved payment processing. Trust and operating payments. Credit/debit/ACH.",
        "category": "payment",
        "icon": "shield-check",
        "bar_approved": True,
    },
    "braintree": {
        "display_name": "Braintree (PayPal / Venmo)",
        "description": "PayPal, Venmo, credit/debit cards, Apple Pay, Google Pay. Single checkout integration.",
        "category": "payment",
        "icon": "credit-card",
        "bar_approved": False,
    },
    "paypal": {
        "display_name": "PayPal Invoicing",
        "description": "PayPal-hosted invoice flow. PayPal emails client, client pays via PayPal/card. QR code support.",
        "category": "payment",
        "icon": "send",
        "bar_approved": False,
    },
    "qbo_online": {
        "display_name": "QuickBooks Online",
        "description": "Bidirectional accounting sync. Invoices, payments, trust, vendor bills. OAuth2 per tenant.",
        "category": "accounting",
        "icon": "book-open",
        "bar_approved": False,
    },
    "qbo_export": {
        "display_name": "QuickBooks Export (IIF/CSV)",
        "description": "File export for QuickBooks Desktop/Online. No API credentials required. Monthly batch workflow.",
        "category": "accounting",
        "icon": "download",
        "bar_approved": False,
    },
}


class PaymentGatewayService:
    """Unified gateway management service."""

    def __init__(self, tenant_id: str, gateway_configs: Optional[Dict[str, Dict[str, Any]]] = None):
        """
        Args:
            tenant_id: Current tenant ID
            gateway_configs: Dict of {gateway_type: config_dict}
                             Loaded from payment_gateway_config table.
        """
        self.tenant_id = tenant_id.strip()
        self._configs = gateway_configs or {}
        self._gateways: Dict[str, PaymentGatewayBase] = {}
        self._accounting: Dict[str, QBOGateway] = {}

    async def load_gateways(self):
        """Instantiate all configured gateways."""
        for gw_type, config in self._configs.items():
            if gw_type in GATEWAY_CLASSES:
                try:
                    self._gateways[gw_type] = GATEWAY_CLASSES[gw_type](
                        tenant_id=self.tenant_id,
                        config=config,
                    )
                except Exception as e:
                    logger.error("Failed to load gateway %s: %s", gw_type, e)
            elif gw_type in ACCOUNTING_CLASSES:
                try:
                    self._accounting[gw_type] = ACCOUNTING_CLASSES[gw_type](
                        tenant_id=self.tenant_id,
                        config=config,
                    )
                except Exception as e:
                    logger.error("Failed to load accounting connector %s: %s", gw_type, e)

    # ── Status Dashboard ──────────────────────────────────────────────────

    async def all_statuses(self) -> List[Dict[str, Any]]:
        """Get status of all known connector types — configured or not."""
        results = []

        for gw_type, meta in ALL_CONNECTOR_META.items():
            if gw_type in self._gateways:
                health = await self._gateways[gw_type].status()
            elif gw_type in self._accounting:
                health = await self._accounting[gw_type].status()
            elif gw_type == "qbo_export":
                # QBO Export is always available (no creds needed)
                health = ConnectorHealth(
                    gateway_type="qbo_export",
                    status=GatewayStatus.HEALTHY,
                    message="File export available — no credentials required.",
                )
            else:
                health = ConnectorHealth(
                    gateway_type=gw_type,
                    status=GatewayStatus.UNCONFIGURED,
                    message=f"{meta['display_name']} not configured.",
                )

            results.append({
                **meta,
                "gateway_type": gw_type,
                **health.to_dict(),
            })

        return results

    async def get_status(self, gateway_type: str) -> ConnectorHealth:
        """Get status for a specific connector."""
        if gateway_type in self._gateways:
            return await self._gateways[gateway_type].status()
        if gateway_type in self._accounting:
            return await self._accounting[gateway_type].status()
        return ConnectorHealth(
            gateway_type=gateway_type,
            status=GatewayStatus.UNCONFIGURED,
            message=f"{gateway_type} not configured.",
        )

    # ── Diagnosis ─────────────────────────────────────────────────────────

    async def diagnose(self, gateway_type: str) -> DiagnosisReport:
        """Run deep diagnosis on a specific connector."""
        if gateway_type in self._gateways:
            return await self._gateways[gateway_type].diagnose()
        if gateway_type in self._accounting:
            return await self._accounting[gateway_type].diagnose()
        return DiagnosisReport(
            gateway_type=gateway_type,
            overall_status=GatewayStatus.UNCONFIGURED,
            checks=[],
        )

    async def diagnose_all(self) -> Dict[str, DiagnosisReport]:
        """Run diagnosis on all configured connectors."""
        reports = {}
        for gw_type, gw in {**self._gateways, **self._accounting}.items():
            try:
                reports[gw_type] = await gw.diagnose()
            except Exception as e:
                logger.error("Diagnosis failed for %s: %s", gw_type, e)
                reports[gw_type] = DiagnosisReport(
                    gateway_type=gw_type,
                    overall_status=GatewayStatus.ERROR,
                )
        return reports

    # ── Testing ───────────────────────────────────────────────────────────

    async def test(self, gateway_type: str) -> TestResult:
        """Test connectivity for a specific connector."""
        if gateway_type in self._gateways:
            return await self._gateways[gateway_type].test_connection()
        if gateway_type in self._accounting:
            return await self._accounting[gateway_type].test_connection()
        return TestResult(
            success=False,
            gateway_type=gateway_type,
            test_type="ping",
            message=f"{gateway_type} not configured — cannot test.",
        )

    async def test_all(self) -> Dict[str, TestResult]:
        """Test all configured connectors."""
        results = {}
        for gw_type, gw in {**self._gateways, **self._accounting}.items():
            try:
                results[gw_type] = await gw.test_connection()
            except Exception as e:
                logger.error("Test failed for %s: %s", gw_type, e)
                results[gw_type] = TestResult(
                    success=False,
                    gateway_type=gw_type,
                    test_type="ping",
                    message=f"Test error: {e}",
                )
        return results

    # ── Payment Operations ────────────────────────────────────────────────

    async def create_payment(self, gateway_type: str,
                              request: PaymentRequest) -> PaymentResult:
        """Create payment via a specific gateway."""
        gw = self._gateways.get(gateway_type)
        if not gw:
            return PaymentResult(
                success=False,
                gateway_type=gateway_type,
                error_message=f"Gateway {gateway_type} not configured.",
            )
        return await gw.create_payment(request)

    async def process_webhook(self, gateway_type: str,
                               headers: Dict[str, str],
                               body: bytes) -> Optional[WebhookEvent]:
        """Process a webhook from a specific gateway."""
        gw = self._gateways.get(gateway_type)
        if not gw:
            logger.warning("Webhook for unconfigured gateway: %s", gateway_type)
            return None

        if not await gw.verify_webhook(headers, body):
            logger.warning("Webhook signature verification failed for %s", gateway_type)
            return None

        return await gw.process_webhook(headers, body)

    async def get_preferred_gateway(self, is_trust: bool = False) -> Optional[str]:
        """Get the preferred active gateway for a payment type.

        Trust payments MUST use LawPay (bar-approved).
        Operating payments prefer Braintree (more payment methods),
        falling back to LawPay, then PayPal.
        """
        if is_trust:
            if "lawpay" in self._gateways:
                health = await self._gateways["lawpay"].status()
                if health.status in (GatewayStatus.HEALTHY, GatewayStatus.SANDBOX):
                    return "lawpay"
            return None  # No bar-approved gateway available

        # Operating payments: prefer Braintree → LawPay → PayPal
        for gw_type in ["braintree", "lawpay", "paypal"]:
            if gw_type in self._gateways:
                health = await self._gateways[gw_type].status()
                if health.status in (GatewayStatus.HEALTHY, GatewayStatus.SANDBOX):
                    return gw_type
        return None

    # ── Cleanup ───────────────────────────────────────────────────────────

    async def close(self):
        for gw in self._gateways.values():
            await gw.close()
        for acct in self._accounting.values():
            await acct.close()
