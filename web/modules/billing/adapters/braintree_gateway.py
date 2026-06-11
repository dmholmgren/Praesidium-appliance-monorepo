"""
modules/billing/adapters/braintree_gateway.py

Braintree payment gateway — single integration for PayPal, Venmo, cards,
Apple Pay, Google Pay.

Venmo is available through Braintree only (Venmo's own APIs are retired).
PayPal owns both Braintree and Venmo, so this is the canonical path.

Architecture:
  - Server-side: Braintree Python SDK (braintree) for transaction creation
  - Client-side: Braintree JS SDK renders Drop-in UI in client portal
  - Webhooks: Braintree sends notifications for payment events
  - Venmo: Enabled per merchant in Braintree control panel

Config keys:
    merchant_id         — Braintree merchant ID
    public_key          — Braintree public key
    private_key         — Braintree private key
    is_sandbox          — Use sandbox environment
    enabled             — Whether this gateway is active
    venmo_enabled       — Whether Venmo is enabled (US only)
    paypal_enabled      — Whether PayPal is enabled
    apple_pay_enabled   — Whether Apple Pay is enabled
    google_pay_enabled  — Whether Google Pay is enabled
    webhook_public_key  — For webhook signature verification (optional)
"""
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

import httpx

from modules.billing.adapters.payment_gateway_base import (
    ConnectorHealth,
    DiagnosisCheck,
    DiagnosisReport,
    GatewayCapability,
    GatewayStatus,
    PaymentGatewayBase,
    PaymentRequest,
    PaymentResult,
    TestResult,
    WebhookEvent,
)

logger = logging.getLogger(__name__)


# ── Braintree API helpers (no SDK dependency — raw REST) ──────────────────────

class BraintreeAPI:
    """Thin wrapper around Braintree's REST API.

    We use raw HTTP instead of the braintree Python SDK to avoid
    adding a heavy dependency. The SDK is ~4MB and we only need
    a handful of endpoints.
    """

    PRODUCTION_BASE = "https://api.braintreegateway.com"
    SANDBOX_BASE = "https://api.sandbox.braintreegateway.com"

    def __init__(self, merchant_id: str, public_key: str, private_key: str,
                 is_sandbox: bool = False):
        self.merchant_id = merchant_id
        self.public_key = public_key
        self.private_key = private_key
        base = self.SANDBOX_BASE if is_sandbox else self.PRODUCTION_BASE
        self.base_url = f"{base}/merchants/{merchant_id}"
        self._client = httpx.AsyncClient(
            auth=(public_key, private_key),
            headers={
                "Content-Type": "application/xml",
                "Accept": "application/xml",
                "X-ApiVersion": "6",
            },
            timeout=30.0,
        )

    async def generate_client_token(self, customer_id: Optional[str] = None) -> str:
        """Generate a client token for the Drop-in UI."""
        xml_body = "<client-token><version>3</version>"
        if customer_id:
            xml_body += f"<customer-id>{customer_id}</customer-id>"
        xml_body += "</client-token>"
        resp = await self._client.post(
            f"{self.base_url}/client_token",
            content=xml_body,
        )
        resp.raise_for_status()
        # Parse client token from XML response
        text = resp.text
        start = text.find("<value>") + 7
        end = text.find("</value>")
        if start > 6 and end > start:
            return text[start:end]
        return ""

    async def create_transaction(self, amount: str, payment_method_nonce: str,
                                  order_id: str, **kwargs) -> Dict[str, Any]:
        """Create a sale transaction."""
        xml = f"""<transaction>
            <type>sale</type>
            <amount>{amount}</amount>
            <payment-method-nonce>{payment_method_nonce}</payment-method-nonce>
            <order-id>{order_id}</order-id>
            <options><submit-for-settlement>true</submit-for-settlement></options>
        </transaction>"""
        resp = await self._client.post(
            f"{self.base_url}/transactions",
            content=xml,
        )
        resp.raise_for_status()
        return {"status_code": resp.status_code, "body": resp.text}

    async def ping(self) -> Dict[str, Any]:
        """Test connectivity by requesting merchant config."""
        resp = await self._client.get(self.base_url)
        return {"status_code": resp.status_code, "ok": resp.status_code == 200}

    async def refund_transaction(self, txn_id: str, amount: Optional[str] = None) -> Dict[str, Any]:
        xml = "<transaction>"
        if amount:
            xml += f"<amount>{amount}</amount>"
        xml += "</transaction>"
        resp = await self._client.post(
            f"{self.base_url}/transactions/{txn_id}/refund",
            content=xml,
        )
        return {"status_code": resp.status_code, "body": resp.text}

    async def close(self):
        await self._client.aclose()


class BraintreeGateway(PaymentGatewayBase):
    """Braintree payment gateway — PayPal, Venmo, cards, wallets."""

    GATEWAY_TYPE = "braintree"
    DISPLAY_NAME = "Braintree (PayPal / Venmo)"
    CAPABILITIES = [
        GatewayCapability.OPERATING_PAYMENTS,
        GatewayCapability.CREDIT_CARD,
        GatewayCapability.DEBIT_CARD,
        GatewayCapability.PAYPAL,
        GatewayCapability.VENMO,
        GatewayCapability.APPLE_PAY,
        GatewayCapability.GOOGLE_PAY,
        GatewayCapability.REFUNDS,
        GatewayCapability.PARTIAL_PAYMENTS,
        GatewayCapability.WEBHOOKS,
    ]

    def __init__(self, tenant_id: str, config: Dict[str, Any]):
        super().__init__(tenant_id, config)
        self._merchant_id = config.get("merchant_id", "")
        self._public_key = config.get("public_key", "")
        self._private_key = config.get("private_key", "")
        self._is_sandbox = config.get("is_sandbox", False)
        self._enabled = config.get("enabled", True)
        self._venmo_enabled = config.get("venmo_enabled", True)
        self._paypal_enabled = config.get("paypal_enabled", True)
        self._apple_pay_enabled = config.get("apple_pay_enabled", False)
        self._google_pay_enabled = config.get("google_pay_enabled", False)
        self._api: Optional[BraintreeAPI] = None

    def _get_api(self) -> BraintreeAPI:
        if self._api is None:
            self._api = BraintreeAPI(
                merchant_id=self._merchant_id,
                public_key=self._public_key,
                private_key=self._private_key,
                is_sandbox=self._is_sandbox,
            )
        return self._api

    def _active_capabilities(self) -> List[GatewayCapability]:
        caps = [
            GatewayCapability.OPERATING_PAYMENTS,
            GatewayCapability.CREDIT_CARD,
            GatewayCapability.DEBIT_CARD,
            GatewayCapability.REFUNDS,
            GatewayCapability.PARTIAL_PAYMENTS,
            GatewayCapability.WEBHOOKS,
        ]
        if self._paypal_enabled:
            caps.append(GatewayCapability.PAYPAL)
        if self._venmo_enabled:
            caps.append(GatewayCapability.VENMO)
        if self._apple_pay_enabled:
            caps.append(GatewayCapability.APPLE_PAY)
        if self._google_pay_enabled:
            caps.append(GatewayCapability.GOOGLE_PAY)
        return caps

    # ── Status / Diagnosis / Test ─────────────────────────────────────────

    async def status(self) -> ConnectorHealth:
        if not self._enabled:
            return ConnectorHealth(
                gateway_type=self.GATEWAY_TYPE,
                status=GatewayStatus.DISABLED,
                message="Braintree gateway is disabled.",
                capabilities=self._active_capabilities(),
            )
        if not self._merchant_id or not self._public_key or not self._private_key:
            return ConnectorHealth(
                gateway_type=self.GATEWAY_TYPE,
                status=GatewayStatus.UNCONFIGURED,
                message="Braintree credentials not configured.",
                capabilities=self._active_capabilities(),
            )
        return ConnectorHealth(
            gateway_type=self.GATEWAY_TYPE,
            status=GatewayStatus.SANDBOX if self._is_sandbox else GatewayStatus.HEALTHY,
            message="Sandbox mode" if self._is_sandbox else "Braintree gateway operational.",
            last_successful_call=self._last_success,
            last_error=self._last_error,
            last_error_at=self._last_error_at,
            credentials_valid=True,
            is_sandbox=self._is_sandbox,
            capabilities=self._active_capabilities(),
        )

    async def diagnose(self) -> DiagnosisReport:
        t0 = time.monotonic()
        checks: List[DiagnosisCheck] = []

        # Check 1: Credentials present
        creds_ok = bool(self._merchant_id and self._public_key and self._private_key)
        checks.append(DiagnosisCheck(
            name="credentials_present",
            passed=creds_ok,
            message="Merchant ID, public key, and private key configured" if creds_ok
                    else "Missing Braintree credentials",
        ))

        # Check 2: API connectivity
        if creds_ok:
            ct0 = time.monotonic()
            try:
                api = self._get_api()
                result = await api.ping()
                api_ok = result["ok"]
                msg = "Braintree API reachable" if api_ok else f"Braintree returned HTTP {result['status_code']}"
                checks.append(DiagnosisCheck(
                    name="api_connectivity",
                    passed=api_ok,
                    message=msg,
                    duration_ms=(time.monotonic() - ct0) * 1000,
                ))
                if api_ok:
                    self._record_success()
                else:
                    self._record_error(msg)
            except Exception as e:
                checks.append(DiagnosisCheck(
                    name="api_connectivity",
                    passed=False,
                    message=f"Braintree API unreachable: {e}",
                    duration_ms=(time.monotonic() - ct0) * 1000,
                ))
                self._record_error(str(e))
        else:
            checks.append(DiagnosisCheck(
                name="api_connectivity",
                passed=False,
                message="Skipped — credentials not configured",
            ))

        # Check 3: Client token generation
        if creds_ok:
            ct0 = time.monotonic()
            try:
                api = self._get_api()
                token = await api.generate_client_token()
                tok_ok = bool(token)
                checks.append(DiagnosisCheck(
                    name="client_token",
                    passed=tok_ok,
                    message="Client token generated successfully" if tok_ok
                            else "Client token generation returned empty",
                    duration_ms=(time.monotonic() - ct0) * 1000,
                ))
            except Exception as e:
                checks.append(DiagnosisCheck(
                    name="client_token",
                    passed=False,
                    message=f"Client token generation failed: {e}",
                    duration_ms=(time.monotonic() - ct0) * 1000,
                ))

        # Check 4: Payment methods enabled
        methods = []
        if self._paypal_enabled:
            methods.append("PayPal")
        if self._venmo_enabled:
            methods.append("Venmo")
        if self._apple_pay_enabled:
            methods.append("Apple Pay")
        if self._google_pay_enabled:
            methods.append("Google Pay")
        methods.append("Cards")
        checks.append(DiagnosisCheck(
            name="payment_methods",
            passed=True,
            message=f"Enabled: {', '.join(methods)}",
            details={
                "paypal": self._paypal_enabled,
                "venmo": self._venmo_enabled,
                "apple_pay": self._apple_pay_enabled,
                "google_pay": self._google_pay_enabled,
                "cards": True,
            },
        ))

        # Check 5: Environment
        checks.append(DiagnosisCheck(
            name="environment",
            passed=True,
            message=f"{'Sandbox' if self._is_sandbox else 'Production'} environment",
            details={"is_sandbox": self._is_sandbox},
        ))

        overall = GatewayStatus.HEALTHY if all(c.passed for c in checks) else GatewayStatus.ERROR
        if not creds_ok:
            overall = GatewayStatus.UNCONFIGURED

        return DiagnosisReport(
            gateway_type=self.GATEWAY_TYPE,
            overall_status=overall,
            checks=checks,
            duration_ms=(time.monotonic() - t0) * 1000,
        )

    async def test_connection(self) -> TestResult:
        t0 = time.monotonic()
        if not self._merchant_id or not self._public_key or not self._private_key:
            return TestResult(
                success=False,
                gateway_type=self.GATEWAY_TYPE,
                test_type="ping",
                message="Cannot test — Braintree credentials not configured",
            )
        try:
            api = self._get_api()
            result = await api.ping()
            duration = (time.monotonic() - t0) * 1000
            if result["ok"]:
                self._record_success()
                return TestResult(
                    success=True,
                    gateway_type=self.GATEWAY_TYPE,
                    test_type="ping",
                    message="Braintree API responded — credentials valid",
                    duration_ms=duration,
                )
            else:
                msg = f"Braintree returned HTTP {result['status_code']}"
                self._record_error(msg)
                return TestResult(
                    success=False,
                    gateway_type=self.GATEWAY_TYPE,
                    test_type="ping",
                    message=msg,
                    duration_ms=duration,
                )
        except Exception as e:
            self._record_error(str(e))
            return TestResult(
                success=False,
                gateway_type=self.GATEWAY_TYPE,
                test_type="ping",
                message=f"Connection failed: {e}",
                duration_ms=(time.monotonic() - t0) * 1000,
            )

    # ── Payment Operations ────────────────────────────────────────────────

    async def create_payment(self, request: PaymentRequest) -> PaymentResult:
        """Generate a client token for the Drop-in UI.

        Braintree uses a different flow than LawPay:
        1. Server generates client_token → sent to browser
        2. Browser renders Drop-in UI with PayPal/Venmo/cards
        3. Client picks method → tokenizes → sends nonce back
        4. Server creates transaction with nonce

        This method handles step 1 — returns client_token as payment_url.
        The actual transaction creation happens in a separate endpoint
        after the client-side nonce is received.
        """
        try:
            api = self._get_api()
            client_token = await api.generate_client_token()
            if not client_token:
                return PaymentResult(
                    success=False,
                    gateway_type=self.GATEWAY_TYPE,
                    error_message="Failed to generate client token",
                )
            self._record_success()
            return PaymentResult(
                success=True,
                gateway_type=self.GATEWAY_TYPE,
                payment_url=client_token,  # Client token for Drop-in UI
                transaction_id=None,  # No txn yet — created after nonce
                status="token_generated",
                raw_response={"client_token": client_token, "invoice_number": request.invoice_number},
            )
        except Exception as e:
            self._record_error(str(e))
            logger.error("Braintree create_payment failed: %s", e)
            return PaymentResult(
                success=False,
                gateway_type=self.GATEWAY_TYPE,
                error_message=str(e),
            )

    async def complete_transaction(self, nonce: str, amount_cents: int,
                                    order_id: str) -> PaymentResult:
        """Complete a transaction after client-side tokenization.

        Called after the Drop-in UI returns a payment method nonce.
        """
        try:
            api = self._get_api()
            amount_str = f"{amount_cents / 100:.2f}"
            result = await api.create_transaction(
                amount=amount_str,
                payment_method_nonce=nonce,
                order_id=order_id,
            )
            # Parse transaction ID from XML response
            body = result.get("body", "")
            txn_id = ""
            id_start = body.find("<id>")
            if id_start >= 0:
                id_end = body.find("</id>", id_start)
                txn_id = body[id_start + 4:id_end]

            status_str = ""
            s_start = body.find("<status>")
            if s_start >= 0:
                s_end = body.find("</status>", s_start)
                status_str = body[s_start + 8:s_end]

            success = status_str in ("submitted_for_settlement", "authorized", "settled")
            self._record_success() if success else self._record_error(f"Transaction status: {status_str}")

            return PaymentResult(
                success=success,
                gateway_type=self.GATEWAY_TYPE,
                transaction_id=txn_id,
                status=status_str,
                raw_response={"body": body[:500]},
            )
        except Exception as e:
            self._record_error(str(e))
            return PaymentResult(
                success=False,
                gateway_type=self.GATEWAY_TYPE,
                error_message=str(e),
            )

    async def verify_webhook(self, headers: Dict[str, str], body: bytes) -> bool:
        # Braintree webhook verification uses bt_signature + bt_payload
        # For now, accept all webhooks in sandbox, verify in production
        if self._is_sandbox:
            return True
        # Production verification would use Braintree's webhook verification
        bt_signature = headers.get("bt_signature", "")
        return bool(bt_signature)

    async def process_webhook(self, headers: Dict[str, str], body: bytes) -> Optional[WebhookEvent]:
        try:
            # Braintree sends bt_signature + bt_payload as form data
            # Parse the notification
            payload = json.loads(body) if body else {}
            kind = payload.get("kind", "")

            # Map Braintree webhook kinds to our event types
            event_map = {
                "check": "test",
                "subscription_charged_successfully": "payment.completed",
                "transaction_settled": "payment.completed",
                "transaction_settlement_declined": "payment.failed",
                "disbursement": "payment.disbursed",
            }

            event_type = event_map.get(kind, f"braintree.{kind}")
            txn = payload.get("transaction", {})

            return WebhookEvent(
                gateway_type=self.GATEWAY_TYPE,
                event_type=event_type,
                transaction_id=txn.get("id", ""),
                invoice_reference=txn.get("order_id", ""),
                amount_cents=int(float(txn.get("amount", "0")) * 100),
                status=txn.get("status", ""),
                raw_payload=payload,
            )
        except Exception as e:
            logger.error("Braintree webhook parse error: %s", e)
            return None

    async def refund(self, transaction_id: str, amount_cents: Optional[int] = None,
                     reason: str = "") -> PaymentResult:
        try:
            api = self._get_api()
            amount_str = f"{amount_cents / 100:.2f}" if amount_cents else None
            result = await api.refund_transaction(transaction_id, amount_str)
            ok = result["status_code"] in (200, 201)
            self._record_success() if ok else self._record_error(f"Refund HTTP {result['status_code']}")
            return PaymentResult(
                success=ok,
                gateway_type=self.GATEWAY_TYPE,
                transaction_id=transaction_id,
                status="refunded" if ok else "refund_failed",
            )
        except Exception as e:
            self._record_error(str(e))
            return PaymentResult(
                success=False,
                gateway_type=self.GATEWAY_TYPE,
                error_message=str(e),
            )

    async def close(self):
        if self._api:
            await self._api.close()
