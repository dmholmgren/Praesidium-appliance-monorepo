"""
modules/billing/adapters/paypal_gateway.py

PayPal Invoicing API gateway — REST API v2.

Uses PayPal's native invoicing flow:
  1. Platform creates PayPal invoice from Praesidium invoice
  2. PayPal emails client a payment link
  3. Client pays via PayPal, card, or Venmo (if enabled on PayPal business acct)
  4. Webhook notifies platform of payment
  5. Platform updates invoice status

This is distinct from Braintree — PayPal Invoicing is for firms that want
PayPal to handle the entire invoice-to-payment flow (including the email).
Braintree is for embedding a checkout widget in the client portal.

Config keys:
    client_id       — PayPal app client ID
    client_secret   — PayPal app client secret
    webhook_id      — PayPal webhook ID for signature verification
    is_sandbox      — Use sandbox environment
    enabled         — Whether this gateway is active
"""
import hashlib
import json
import logging
import time
from datetime import datetime
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


class PayPalGateway(PaymentGatewayBase):
    """PayPal Invoicing API gateway."""

    GATEWAY_TYPE = "paypal"
    DISPLAY_NAME = "PayPal Invoicing"
    CAPABILITIES = [
        GatewayCapability.OPERATING_PAYMENTS,
        GatewayCapability.CREDIT_CARD,
        GatewayCapability.DEBIT_CARD,
        GatewayCapability.PAYPAL,
        GatewayCapability.INVOICING,
        GatewayCapability.REFUNDS,
        GatewayCapability.PARTIAL_PAYMENTS,
        GatewayCapability.WEBHOOKS,
        GatewayCapability.QR_CODE,
    ]

    PRODUCTION_BASE = "https://api-m.paypal.com"
    SANDBOX_BASE = "https://api-m.sandbox.paypal.com"

    def __init__(self, tenant_id: str, config: Dict[str, Any]):
        super().__init__(tenant_id, config)
        self._client_id = config.get("client_id", "")
        self._client_secret = config.get("client_secret", "")
        self._webhook_id = config.get("webhook_id", "")
        self._is_sandbox = config.get("is_sandbox", False)
        self._enabled = config.get("enabled", True)
        self._base_url = self.SANDBOX_BASE if self._is_sandbox else self.PRODUCTION_BASE
        self._access_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=30.0,
            )
        return self._client

    async def _ensure_token(self) -> str:
        """Get or refresh OAuth2 access token."""
        if self._access_token and self._token_expires_at and datetime.utcnow() < self._token_expires_at:
            return self._access_token
        client = self._get_client()
        resp = await client.post(
            "/v1/oauth2/token",
            auth=(self._client_id, self._client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        expires_in = data.get("expires_in", 32400)  # Default 9 hours
        from datetime import timedelta
        self._token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in - 300)
        return self._access_token

    async def _api_call(self, method: str, endpoint: str,
                         payload: Optional[dict] = None) -> httpx.Response:
        token = await self._ensure_token()
        client = self._get_client()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if method == "GET":
            return await client.get(endpoint, headers=headers)
        elif method == "POST":
            return await client.post(endpoint, json=payload or {}, headers=headers)
        elif method == "PUT":
            return await client.put(endpoint, json=payload or {}, headers=headers)
        elif method == "DELETE":
            return await client.delete(endpoint, headers=headers)
        else:
            return await client.request(method, endpoint, json=payload, headers=headers)

    # ── Status / Diagnosis / Test ─────────────────────────────────────────

    async def status(self) -> ConnectorHealth:
        if not self._enabled:
            return ConnectorHealth(
                gateway_type=self.GATEWAY_TYPE,
                status=GatewayStatus.DISABLED,
                message="PayPal gateway is disabled.",
                capabilities=self.CAPABILITIES,
            )
        if not self._client_id or not self._client_secret:
            return ConnectorHealth(
                gateway_type=self.GATEWAY_TYPE,
                status=GatewayStatus.UNCONFIGURED,
                message="PayPal client ID and secret not configured.",
                capabilities=self.CAPABILITIES,
            )
        return ConnectorHealth(
            gateway_type=self.GATEWAY_TYPE,
            status=GatewayStatus.SANDBOX if self._is_sandbox else GatewayStatus.HEALTHY,
            message="Sandbox mode" if self._is_sandbox else "PayPal gateway operational.",
            last_successful_call=self._last_success,
            last_error=self._last_error,
            last_error_at=self._last_error_at,
            credentials_valid=bool(self._access_token),
            credentials_expire_at=self._token_expires_at,
            is_sandbox=self._is_sandbox,
            capabilities=self.CAPABILITIES,
        )

    async def diagnose(self) -> DiagnosisReport:
        t0 = time.monotonic()
        checks: List[DiagnosisCheck] = []

        # Check 1: Credentials
        creds_ok = bool(self._client_id and self._client_secret)
        checks.append(DiagnosisCheck(
            name="credentials_present",
            passed=creds_ok,
            message="Client ID and secret configured" if creds_ok else "Missing PayPal credentials",
        ))

        # Check 2: OAuth2 token acquisition
        if creds_ok:
            ct0 = time.monotonic()
            try:
                token = await self._ensure_token()
                tok_ok = bool(token)
                checks.append(DiagnosisCheck(
                    name="oauth2_token",
                    passed=tok_ok,
                    message="Access token acquired" if tok_ok else "Token acquisition returned empty",
                    duration_ms=(time.monotonic() - ct0) * 1000,
                    details={"expires_at": self._token_expires_at.isoformat() if self._token_expires_at else None},
                ))
                if tok_ok:
                    self._record_success()
            except Exception as e:
                checks.append(DiagnosisCheck(
                    name="oauth2_token",
                    passed=False,
                    message=f"Token acquisition failed: {e}",
                    duration_ms=(time.monotonic() - ct0) * 1000,
                ))
                self._record_error(str(e))
        else:
            checks.append(DiagnosisCheck(
                name="oauth2_token",
                passed=False,
                message="Skipped — credentials not configured",
            ))

        # Check 3: Invoicing API access
        if creds_ok and self._access_token:
            ct0 = time.monotonic()
            try:
                resp = await self._api_call("POST", "/v2/invoicing/generate-next-invoice-number")
                inv_ok = resp.status_code == 200
                checks.append(DiagnosisCheck(
                    name="invoicing_api",
                    passed=inv_ok,
                    message="Invoicing API accessible" if inv_ok
                            else f"Invoicing API returned HTTP {resp.status_code}",
                    duration_ms=(time.monotonic() - ct0) * 1000,
                ))
            except Exception as e:
                checks.append(DiagnosisCheck(
                    name="invoicing_api",
                    passed=False,
                    message=f"Invoicing API error: {e}",
                    duration_ms=(time.monotonic() - ct0) * 1000,
                ))

        # Check 4: Webhook configuration
        webhook_ok = bool(self._webhook_id)
        checks.append(DiagnosisCheck(
            name="webhook_configured",
            passed=webhook_ok,
            message="Webhook ID configured" if webhook_ok
                    else "Webhook ID not configured — payment notifications won't be verified",
        ))

        # Check 5: Environment
        checks.append(DiagnosisCheck(
            name="environment",
            passed=True,
            message=f"{'Sandbox' if self._is_sandbox else 'Production'} environment",
            details={"base_url": self._base_url},
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
        if not self._client_id or not self._client_secret:
            return TestResult(
                success=False,
                gateway_type=self.GATEWAY_TYPE,
                test_type="token_refresh",
                message="Cannot test — PayPal credentials not configured",
            )
        try:
            token = await self._ensure_token()
            duration = (time.monotonic() - t0) * 1000
            if token:
                self._record_success()
                return TestResult(
                    success=True,
                    gateway_type=self.GATEWAY_TYPE,
                    test_type="token_refresh",
                    message="PayPal OAuth2 token acquired — credentials valid",
                    duration_ms=duration,
                )
            else:
                return TestResult(
                    success=False,
                    gateway_type=self.GATEWAY_TYPE,
                    test_type="token_refresh",
                    message="Token acquisition returned empty",
                    duration_ms=duration,
                )
        except Exception as e:
            self._record_error(str(e))
            return TestResult(
                success=False,
                gateway_type=self.GATEWAY_TYPE,
                test_type="token_refresh",
                message=f"Connection failed: {e}",
                duration_ms=(time.monotonic() - t0) * 1000,
            )

    # ── Payment Operations ────────────────────────────────────────────────

    async def create_payment(self, request: PaymentRequest) -> PaymentResult:
        """Create a PayPal invoice and send it to the client."""
        try:
            amount_str = f"{request.amount_cents / 100:.2f}"
            invoice_payload = {
                "detail": {
                    "invoice_number": request.invoice_number,
                    "currency_code": request.currency,
                    "note": request.description,
                    "payment_term": {"term_type": "DUE_ON_RECEIPT"},
                },
                "primary_recipients": [{
                    "billing_info": {
                        "name": {"given_name": request.client_name},
                        "email_address": request.client_email,
                    }
                }],
                "items": [{
                    "name": request.description or f"Invoice {request.invoice_number}",
                    "quantity": "1",
                    "unit_amount": {"currency_code": request.currency, "value": amount_str},
                }],
            }

            # Create draft invoice
            resp = await self._api_call("POST", "/v2/invoicing/invoices", invoice_payload)
            if resp.status_code != 201:
                self._record_error(f"Invoice creation HTTP {resp.status_code}")
                return PaymentResult(
                    success=False,
                    gateway_type=self.GATEWAY_TYPE,
                    error_message=f"PayPal invoice creation failed: HTTP {resp.status_code}",
                )

            invoice_data = resp.json()
            # Extract invoice ID from href
            invoice_id = ""
            for link in invoice_data.get("links", []):
                if link.get("rel") == "self":
                    href = link.get("href", "")
                    invoice_id = href.split("/")[-1] if href else ""
                    break

            # Send the invoice
            if invoice_id:
                send_resp = await self._api_call(
                    "POST",
                    f"/v2/invoicing/invoices/{invoice_id}/send",
                    {"send_to_invoicer": True},
                )
                if send_resp.status_code not in (200, 202):
                    logger.warning("PayPal invoice send returned HTTP %s", send_resp.status_code)

            self._record_success()
            return PaymentResult(
                success=True,
                gateway_type=self.GATEWAY_TYPE,
                payment_url=f"https://www.paypal.com/invoice/p/#{invoice_id}" if not self._is_sandbox
                            else f"https://www.sandbox.paypal.com/invoice/p/#{invoice_id}",
                transaction_id=invoice_id,
                status="sent",
                raw_response=invoice_data,
            )
        except Exception as e:
            self._record_error(str(e))
            logger.error("PayPal create_payment failed: %s", e)
            return PaymentResult(
                success=False,
                gateway_type=self.GATEWAY_TYPE,
                error_message=str(e),
            )

    async def verify_webhook(self, headers: Dict[str, str], body: bytes) -> bool:
        # PayPal webhook verification requires calling PayPal's verify endpoint
        if self._is_sandbox:
            return True
        if not self._webhook_id:
            return False
        try:
            verify_payload = {
                "auth_algo": headers.get("paypal-auth-algo", ""),
                "cert_url": headers.get("paypal-cert-url", ""),
                "transmission_id": headers.get("paypal-transmission-id", ""),
                "transmission_sig": headers.get("paypal-transmission-sig", ""),
                "transmission_time": headers.get("paypal-transmission-time", ""),
                "webhook_id": self._webhook_id,
                "webhook_event": json.loads(body),
            }
            resp = await self._api_call(
                "POST",
                "/v1/notifications/verify-webhook-signature",
                verify_payload,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("verification_status") == "SUCCESS"
            return False
        except Exception as e:
            logger.error("PayPal webhook verification failed: %s", e)
            return False

    async def process_webhook(self, headers: Dict[str, str], body: bytes) -> Optional[WebhookEvent]:
        try:
            payload = json.loads(body)
            event_type_raw = payload.get("event_type", "")
            resource = payload.get("resource", {})

            # Map PayPal event types
            event_map = {
                "INVOICING.INVOICE.PAID": "payment.completed",
                "INVOICING.INVOICE.CANCELLED": "payment.cancelled",
                "INVOICING.INVOICE.REFUNDED": "refund.completed",
                "INVOICING.INVOICE.PARTIALLY_PAID": "payment.partial",
                "PAYMENT.CAPTURE.COMPLETED": "payment.completed",
                "PAYMENT.CAPTURE.DENIED": "payment.failed",
            }
            event_type = event_map.get(event_type_raw, f"paypal.{event_type_raw}")

            # Extract invoice reference
            invoice_ref = resource.get("invoice", {}).get("detail", {}).get("invoice_number", "")
            if not invoice_ref:
                invoice_ref = resource.get("custom_id", "")

            # Extract amount
            amount_data = resource.get("amount", {})
            amount_str = amount_data.get("value", "0")
            amount_cents = int(float(amount_str) * 100)

            return WebhookEvent(
                gateway_type=self.GATEWAY_TYPE,
                event_type=event_type,
                transaction_id=resource.get("id", ""),
                invoice_reference=invoice_ref,
                amount_cents=amount_cents,
                currency=amount_data.get("currency_code", "USD"),
                status=resource.get("status", ""),
                raw_payload=payload,
            )
        except Exception as e:
            logger.error("PayPal webhook parse error: %s", e)
            return None

    async def refund(self, transaction_id: str, amount_cents: Optional[int] = None,
                     reason: str = "") -> PaymentResult:
        try:
            payload: Dict[str, Any] = {}
            if amount_cents:
                payload["amount"] = {
                    "value": f"{amount_cents / 100:.2f}",
                    "currency_code": "USD",
                }
            if reason:
                payload["note_to_payer"] = reason

            resp = await self._api_call(
                "POST",
                f"/v2/invoicing/invoices/{transaction_id}/refunds",
                payload,
            )
            ok = resp.status_code in (200, 201)
            self._record_success() if ok else self._record_error(f"Refund HTTP {resp.status_code}")
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
        if self._client and not self._client.is_closed:
            await self._client.aclose()
