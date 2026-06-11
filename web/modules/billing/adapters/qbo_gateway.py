"""
modules/billing/adapters/qbo_gateway.py

QuickBooks Online accounting gateway — upgraded with status/diagnosis/testing.

This wraps the existing QBOOnlineAdapter with the PaymentGatewayBase-style
status/diagnosis/test interface. QBO is not a payment processor — it's an
accounting sync target. But we give it the same health/diagnosis contract
so the connector dashboard is uniform across all billing integrations.

QBO connects to banks internally via:
  1. Intuit's own bank feed service (transitioning to OAuth)
  2. Plaid middleware aggregator (11,000+ institutions)
Praesidium does NOT need to integrate with Plaid — the firm connects
their bank to QBO directly. We sync invoices/payments/trust to QBO,
and QBO reconciles against its own bank feed.
"""
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
    TestResult,
)

logger = logging.getLogger(__name__)


class QBOGateway:
    """QuickBooks Online accounting sync gateway with health monitoring.

    Not a PaymentGatewayBase subclass — QBO is accounting, not payments.
    But implements the same status/diagnose/test contract for the
    connector dashboard.

    Config keys:
        client_id       — Intuit app client ID
        client_secret   — Intuit app client secret
        realm_id        — QBO company ID (set during OAuth callback)
        access_token    — Current OAuth2 access token
        refresh_token   — OAuth2 refresh token
        is_sandbox      — Use sandbox environment
        enabled         — Whether this connector is active
        sync_invoices   — Sync invoices to QBO
        sync_payments   — Sync payments to QBO
        sync_trust      — Sync trust transactions to QBO
        sync_direction  — "push", "pull", or "bidirectional"
    """

    GATEWAY_TYPE = "qbo_online"
    DISPLAY_NAME = "QuickBooks Online"

    API_BASE = "https://quickbooks.api.intuit.com"
    SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"
    TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

    def __init__(self, tenant_id: str, config: Dict[str, Any]):
        self.tenant_id = tenant_id.strip()
        self.config = config
        self._client_id = config.get("client_id", "")
        self._client_secret = config.get("client_secret", "")
        self._realm_id = config.get("realm_id", "")
        self._access_token = config.get("access_token", "")
        self._refresh_token = config.get("refresh_token", "")
        self._is_sandbox = config.get("is_sandbox", False)
        self._enabled = config.get("enabled", True)
        self._sync_invoices = config.get("sync_invoices", True)
        self._sync_payments = config.get("sync_payments", True)
        self._sync_trust = config.get("sync_trust", True)
        self._sync_direction = config.get("sync_direction", "push")
        self._base_url = self.SANDBOX_BASE if self._is_sandbox else self.API_BASE
        self._last_success: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._last_error_at: Optional[datetime] = None
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
        return self._client

    async def _refresh_access_token(self) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self.TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data["access_token"]
            self._refresh_token = data.get("refresh_token", self._refresh_token)
            if self._client and not self._client.is_closed:
                self._client.headers["Authorization"] = f"Bearer {self._access_token}"
            return self._access_token

    def _record_success(self):
        self._last_success = datetime.utcnow()

    def _record_error(self, msg: str):
        self._last_error = msg
        self._last_error_at = datetime.utcnow()

    # ── Status / Diagnosis / Test ─────────────────────────────────────────

    async def status(self) -> ConnectorHealth:
        if not self._enabled:
            return ConnectorHealth(
                gateway_type=self.GATEWAY_TYPE,
                status=GatewayStatus.DISABLED,
                message="QuickBooks Online sync is disabled.",
            )
        if not self._client_id or not self._client_secret:
            return ConnectorHealth(
                gateway_type=self.GATEWAY_TYPE,
                status=GatewayStatus.UNCONFIGURED,
                message="QBO OAuth2 credentials not configured.",
            )
        if not self._realm_id:
            return ConnectorHealth(
                gateway_type=self.GATEWAY_TYPE,
                status=GatewayStatus.UNCONFIGURED,
                message="QBO company not connected — OAuth authorization required.",
            )
        return ConnectorHealth(
            gateway_type=self.GATEWAY_TYPE,
            status=GatewayStatus.SANDBOX if self._is_sandbox else GatewayStatus.HEALTHY,
            message="Sandbox mode" if self._is_sandbox else "QBO sync operational.",
            last_successful_call=self._last_success,
            last_error=self._last_error,
            last_error_at=self._last_error_at,
            credentials_valid=bool(self._access_token),
            is_sandbox=self._is_sandbox,
        )

    async def diagnose(self) -> DiagnosisReport:
        t0 = time.monotonic()
        checks: List[DiagnosisCheck] = []

        # Check 1: OAuth2 credentials
        creds_ok = bool(self._client_id and self._client_secret)
        checks.append(DiagnosisCheck(
            name="oauth2_credentials",
            passed=creds_ok,
            message="Client ID and secret configured" if creds_ok else "Missing QBO OAuth2 credentials",
        ))

        # Check 2: Company connected
        realm_ok = bool(self._realm_id)
        checks.append(DiagnosisCheck(
            name="company_connected",
            passed=realm_ok,
            message=f"Connected to QBO company {self._realm_id}" if realm_ok
                    else "No QBO company connected — tenant admin must complete OAuth flow",
        ))

        # Check 3: Token refresh
        if creds_ok and self._refresh_token:
            ct0 = time.monotonic()
            try:
                token = await self._refresh_access_token()
                tok_ok = bool(token)
                checks.append(DiagnosisCheck(
                    name="token_refresh",
                    passed=tok_ok,
                    message="Access token refreshed" if tok_ok else "Token refresh returned empty",
                    duration_ms=(time.monotonic() - ct0) * 1000,
                ))
                if tok_ok:
                    self._record_success()
            except Exception as e:
                checks.append(DiagnosisCheck(
                    name="token_refresh",
                    passed=False,
                    message=f"Token refresh failed: {e}",
                    duration_ms=(time.monotonic() - ct0) * 1000,
                ))
                self._record_error(str(e))
        else:
            checks.append(DiagnosisCheck(
                name="token_refresh",
                passed=False,
                message="Skipped — no refresh token available" if not self._refresh_token
                        else "Skipped — credentials not configured",
            ))

        # Check 4: Company info query
        if realm_ok and self._access_token:
            ct0 = time.monotonic()
            try:
                client = self._get_client()
                resp = await client.get(f"/v3/company/{self._realm_id}/companyinfo/{self._realm_id}")
                if resp.status_code == 401:
                    # Try refresh
                    await self._refresh_access_token()
                    client = self._get_client()
                    resp = await client.get(f"/v3/company/{self._realm_id}/companyinfo/{self._realm_id}")
                api_ok = resp.status_code == 200
                if api_ok:
                    data = resp.json()
                    company_name = data.get("CompanyInfo", {}).get("CompanyName", "Unknown")
                    msg = f"Connected to '{company_name}'"
                else:
                    msg = f"Company info query returned HTTP {resp.status_code}"
                checks.append(DiagnosisCheck(
                    name="company_info",
                    passed=api_ok,
                    message=msg,
                    duration_ms=(time.monotonic() - ct0) * 1000,
                ))
            except Exception as e:
                checks.append(DiagnosisCheck(
                    name="company_info",
                    passed=False,
                    message=f"Company info query failed: {e}",
                    duration_ms=(time.monotonic() - ct0) * 1000,
                ))

        # Check 5: Sync configuration
        sync_items = []
        if self._sync_invoices:
            sync_items.append("Invoices")
        if self._sync_payments:
            sync_items.append("Payments")
        if self._sync_trust:
            sync_items.append("Trust")
        checks.append(DiagnosisCheck(
            name="sync_config",
            passed=bool(sync_items),
            message=f"Sync enabled: {', '.join(sync_items)} ({self._sync_direction})"
                    if sync_items else "No sync types enabled",
            details={
                "invoices": self._sync_invoices,
                "payments": self._sync_payments,
                "trust": self._sync_trust,
                "direction": self._sync_direction,
            },
        ))

        # Check 6: Environment
        checks.append(DiagnosisCheck(
            name="environment",
            passed=True,
            message=f"{'Sandbox' if self._is_sandbox else 'Production'} environment",
        ))

        overall = GatewayStatus.HEALTHY if all(c.passed for c in checks) else GatewayStatus.ERROR
        if not creds_ok or not realm_ok:
            overall = GatewayStatus.UNCONFIGURED

        return DiagnosisReport(
            gateway_type=self.GATEWAY_TYPE,
            overall_status=overall,
            checks=checks,
            duration_ms=(time.monotonic() - t0) * 1000,
        )

    async def test_connection(self) -> TestResult:
        t0 = time.monotonic()
        if not self._client_id or not self._client_secret or not self._refresh_token:
            return TestResult(
                success=False,
                gateway_type=self.GATEWAY_TYPE,
                test_type="token_refresh",
                message="Cannot test — QBO OAuth2 credentials or refresh token not configured",
            )
        try:
            token = await self._refresh_access_token()
            duration = (time.monotonic() - t0) * 1000
            if token:
                self._record_success()
                return TestResult(
                    success=True,
                    gateway_type=self.GATEWAY_TYPE,
                    test_type="token_refresh",
                    message="QBO token refreshed — OAuth2 connection valid",
                    duration_ms=duration,
                )
            else:
                return TestResult(
                    success=False,
                    gateway_type=self.GATEWAY_TYPE,
                    test_type="token_refresh",
                    message="Token refresh returned empty",
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

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
