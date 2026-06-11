"""
modules/billing/adapters/lawpay_gateway.py

LawPay payment gateway — bar-approved payment processing.
Trust payments handled correctly (IOLTA compliance).

Replaces the old lawpay.py stub with full PaymentGatewayBase implementation
including status, diagnosis, and testing.
"""
import hashlib
import hmac
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


class LawPayGateway(PaymentGatewayBase):
    """LawPay payment gateway adapter.

    Config keys:
        api_key         — LawPay API key
        secret_key      — LawPay secret key
        webhook_secret  — Webhook signature verification secret
        base_url        — API base URL (default: https://api.lawpay.com)
        is_sandbox      — Use sandbox environment
        enabled         — Whether this gateway is active
    """

    GATEWAY_TYPE = "lawpay"
    DISPLAY_NAME = "LawPay"
    CAPABILITIES = [
        GatewayCapability.OPERATING_PAYMENTS,
        GatewayCapability.TRUST_PAYMENTS,
        GatewayCapability.CREDIT_CARD,
        GatewayCapability.DEBIT_CARD,
        GatewayCapability.ACH,
        GatewayCapability.REFUNDS,
        GatewayCapability.PARTIAL_PAYMENTS,
        GatewayCapability.WEBHOOKS,
    ]

    def __init__(self, tenant_id: str, config: Dict[str, Any]):
        super().__init__(tenant_id, config)
        self._base_url = config.get("base_url", "https://api.lawpay.com").rstrip("/")
        self._api_key = config.get("api_key", "")
        self._secret_key = config.get("secret_key", "")
        self._webhook_secret = config.get("webhook_secret", "")
        self._is_sandbox = config.get("is_sandbox", False)
        self._enabled = config.get("enabled", True)
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                auth=(self._api_key, self._secret_key),
                timeout=30.0,
            )
        return self._client

    # ── Status / Diagnosis / Test ─────────────────────────────────────────

    async def status(self) -> ConnectorHealth:
        if not self._enabled:
            return ConnectorHealth(
                gateway_type=self.GATEWAY_TYPE,
                status=GatewayStatus.DISABLED,
                message="LawPay gateway is disabled for this tenant.",
                capabilities=self.CAPABILITIES,
            )
        if not self._api_key or not self._secret_key:
            return ConnectorHealth(
                gateway_type=self.GATEWAY_TYPE,
                status=GatewayStatus.UNCONFIGURED,
                message="LawPay API credentials not configured.",
                capabilities=self.CAPABILITIES,
            )
        return ConnectorHealth(
            gateway_type=self.GATEWAY_TYPE,
            status=GatewayStatus.SANDBOX if self._is_sandbox else GatewayStatus.HEALTHY,
            message="Sandbox mode" if self._is_sandbox else "LawPay gateway operational.",
            last_successful_call=self._last_success,
            last_error=self._last_error,
            last_error_at=self._last_error_at,
            credentials_valid=True,
            is_sandbox=self._is_sandbox,
            capabilities=self.CAPABILITIES,
        )

    async def diagnose(self) -> DiagnosisReport:
        t0 = time.monotonic()
        checks: List[DiagnosisCheck] = []

        # Check 1: Credentials present
        creds_ok = bool(self._api_key and self._secret_key)
        checks.append(DiagnosisCheck(
            name="credentials_present",
            passed=creds_ok,
            message="API key and secret key configured" if creds_ok else "Missing API key or secret key",
        ))

        # Check 2: Webhook secret present
        webhook_ok = bool(self._webhook_secret)
        checks.append(DiagnosisCheck(
            name="webhook_secret_present",
            passed=webhook_ok,
            message="Webhook secret configured" if webhook_ok else "Webhook secret not configured — inbound payment notifications will not be verified",
        ))

        # Check 3: API connectivity
        if creds_ok:
            ct0 = time.monotonic()
            try:
                client = self._get_client()
                resp = await client.get("/v1/merchant")
                api_ok = resp.status_code in (200, 401, 403)
                msg = f"API reachable (HTTP {resp.status_code})"
                if resp.status_code == 200:
                    msg = "API reachable — credentials valid"
                elif resp.status_code in (401, 403):
                    msg = f"API reachable but credentials rejected (HTTP {resp.status_code})"
                    api_ok = False
                checks.append(DiagnosisCheck(
                    name="api_connectivity",
                    passed=api_ok,
                    message=msg,
                    duration_ms=(time.monotonic() - ct0) * 1000,
                ))
                self._record_success() if api_ok else self._record_error(msg)
            except Exception as e:
                checks.append(DiagnosisCheck(
                    name="api_connectivity",
                    passed=False,
                    message=f"API unreachable: {e}",
                    duration_ms=(time.monotonic() - ct0) * 1000,
                ))
                self._record_error(str(e))
        else:
            checks.append(DiagnosisCheck(
                name="api_connectivity",
                passed=False,
                message="Skipped — credentials not configured",
            ))

        # Check 4: Sandbox vs production
        checks.append(DiagnosisCheck(
            name="environment",
            passed=True,
            message=f"{'Sandbox' if self._is_sandbox else 'Production'} environment",
            details={"base_url": self._base_url, "is_sandbox": self._is_sandbox},
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
        if not self._api_key or not self._secret_key:
            return TestResult(
                success=False,
                gateway_type=self.GATEWAY_TYPE,
                test_type="ping",
                message="Cannot test — API credentials not configured",
            )
        try:
            client = self._get_client()
            resp = await client.get("/v1/merchant")
            duration = (time.monotonic() - t0) * 1000
            if resp.status_code == 200:
                self._record_success()
                return TestResult(
                    success=True,
                    gateway_type=self.GATEWAY_TYPE,
                    test_type="ping",
                    message="LawPay API responded — credentials valid",
                    duration_ms=duration,
                )
            else:
                msg = f"LawPay API returned HTTP {resp.status_code}"
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
        try:
            client = self._get_client()
            payload = {
                "amount": request.amount_cents,
                "currency": request.currency,
                "reference": request.invoice_number,
                "custom_id": request.invoice_number,
                "description": request.description,
                "email": request.client_email,
                "name": request.client_name,
                "type": "trust" if request.is_trust else "operating",
            }
            resp = await client.post("/v1/charges", json=payload)
            resp.raise_for_status()
            data = resp.json()
            self._record_success()
            return PaymentResult(
                success=True,
                gateway_type=self.GATEWAY_TYPE,
                payment_url=data.get("payment_url", ""),
                transaction_id=data.get("id", ""),
                status="pending",
                raw_response=data,
            )
        except Exception as e:
            self._record_error(str(e))
            logger.error("LawPay create_payment failed: %s", e)
            return PaymentResult(
                success=False,
                gateway_type=self.GATEWAY_TYPE,
                error_message=str(e),
            )

    async def verify_webhook(self, headers: Dict[str, str], body: bytes) -> bool:
        signature = headers.get("x-lawpay-signature", "")
        if not signature or not self._webhook_secret:
            return False
        expected = hmac.new(
            self._webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def process_webhook(self, headers: Dict[str, str], body: bytes) -> Optional[WebhookEvent]:
        try:
            payload = json.loads(body)
            event_type = payload.get("event_type", "payment.completed")
            txn_id = payload.get("transaction_id") or payload.get("id", "")
            invoice_ref = payload.get("custom_id") or payload.get("reference", "")
            amount = int(payload.get("amount", 0))
            return WebhookEvent(
                gateway_type=self.GATEWAY_TYPE,
                event_type=event_type,
                transaction_id=txn_id,
                invoice_reference=invoice_ref,
                amount_cents=amount,
                status=payload.get("status", "completed"),
                raw_payload=payload,
            )
        except Exception as e:
            logger.error("LawPay webhook parse error: %s", e)
            return None

    async def refund(self, transaction_id: str, amount_cents: Optional[int] = None,
                     reason: str = "") -> PaymentResult:
        try:
            client = self._get_client()
            payload: Dict[str, Any] = {}
            if amount_cents is not None:
                payload["amount"] = amount_cents
            if reason:
                payload["reason"] = reason
            resp = await client.post(f"/v1/charges/{transaction_id}/refund", json=payload)
            resp.raise_for_status()
            data = resp.json()
            self._record_success()
            return PaymentResult(
                success=True,
                gateway_type=self.GATEWAY_TYPE,
                transaction_id=data.get("id", ""),
                status="refunded",
                raw_response=data,
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
