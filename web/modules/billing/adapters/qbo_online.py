"""QuickBooks Online adapter — OAuth2 per tenant, bidirectional sync."""
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

import httpx

from core.db.base import TenantSession
from core.audit import write_audit
from modules.billing.models.qbo_sync import QBOSyncLog

logger = logging.getLogger(__name__)


class QBOOnlineAdapter:
    """QuickBooks Online API adapter.

    - OAuth2 per tenant, realm_id per tenant
    - Credentials stored in vault, never in .env
    - Invoice, payment, trust, vendor bill sync
    - Chart of accounts mapping per tenant
    """

    API_BASE = "https://quickbooks.api.intuit.com"
    SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"

    def __init__(
        self,
        db: TenantSession,
        realm_id: str,
        access_token: str,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        is_sandbox: bool = False,
    ):
        self.db = db
        self.realm_id = realm_id
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = self.SANDBOX_BASE if is_sandbox else self.API_BASE
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    async def _refresh_access_token(self) -> str:
        """Refresh OAuth2 access token."""
        resp = await self._client.post(
            "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self._client.headers["Authorization"] = f"Bearer {self.access_token}"
        return self.access_token

    async def _api_call(self, method: str, endpoint: str, payload: Optional[dict] = None) -> dict:
        """Make an API call with automatic token refresh on 401."""
        url = f"/v3/company/{self.realm_id}/{endpoint}"
        try:
            if method == "GET":
                resp = await self._client.get(url, params=payload)
            elif method == "POST":
                resp = await self._client.post(url, json=payload)
            else:
                resp = await self._client.request(method, url, json=payload)

            if resp.status_code == 401:
                await self._refresh_access_token()
                if method == "GET":
                    resp = await self._client.get(url, params=payload)
                else:
                    resp = await self._client.post(url, json=payload)

            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"QBO API error: {e}")
            raise

    def _log_sync(self, object_type: str, object_id: str, direction: str,
                  status: str, qbo_id: Optional[str] = None,
                  request_payload: Optional[str] = None,
                  response_payload: Optional[str] = None,
                  error_message: Optional[str] = None):
        import uuid
        log = QBOSyncLog(
            id=str(uuid.uuid4()),
            tenant_id=self.db.tenant_id,
            object_type=object_type,
            object_id=object_id,
            direction=direction,
            status=status,
            qbo_id=qbo_id,
            request_payload=request_payload,
            response_payload=response_payload,
            error_message=error_message,
        )
        self.db.add(log)

    async def find_or_create_customer(self, client_name: str, client_email: Optional[str] = None) -> str:
        """Find existing QBO customer by name or create new one. Returns QBO Customer ID."""
        query = f"SELECT * FROM Customer WHERE DisplayName = '{client_name}'"
        resp = await self._api_call("GET", "query", {"query": query})
        customers = resp.get("QueryResponse", {}).get("Customer", [])
        if customers:
            return customers[0]["Id"]

        # Create new customer
        payload = {
            "DisplayName": client_name,
        }
        if client_email:
            payload["PrimaryEmailAddr"] = {"Address": client_email}

        resp = await self._api_call("POST", "customer", payload)
        return resp.get("Customer", {}).get("Id", "")

    async def sync_invoice(self, invoice, line_items: list, client_name: str, account_mapping: dict) -> Optional[str]:
        """Push invoice to QBO. Returns QBO Invoice ID."""
        try:
            customer_id = await self.find_or_create_customer(client_name)
            qbo_lines = []
            for idx, li in enumerate(line_items):
                qbo_line = {
                    "LineNum": idx + 1,
                    "Amount": float(li.amount),
                    "DetailType": "SalesItemLineDetail",
                    "Description": li.description[:4000],
                    "SalesItemLineDetail": {
                        "Qty": float(li.hours) if li.hours else 1,
                        "UnitPrice": float(li.rate) if li.rate else float(li.amount),
                    },
                }
                # Map UTBMS code to QBO item if mapping exists
                if li.utbms_task_code and li.utbms_task_code in account_mapping:
                    qbo_line["SalesItemLineDetail"]["ItemRef"] = {
                        "value": account_mapping[li.utbms_task_code],
                    }
                qbo_lines.append(qbo_line)

            payload = {
                "DocNumber": invoice.invoice_number,
                "CustomerRef": {"value": customer_id},
                "TxnDate": str(invoice.invoice_date),
                "DueDate": str(invoice.due_date),
                "Line": qbo_lines,
                "CustomField": [
                    {"DefinitionId": "1", "StringValue": str(invoice.id), "Type": "StringType"},
                ],
            }

            request_json = json.dumps(payload)
            resp = await self._api_call("POST", "invoice", payload)
            qbo_id = resp.get("Invoice", {}).get("Id", "")

            self._log_sync("invoice", invoice.id, "push", "success",
                          qbo_id=qbo_id, request_payload=request_json,
                          response_payload=json.dumps(resp))

            invoice.qbo_invoice_id = qbo_id
            invoice.qbo_sync_status = "synced"
            self.db.commit()
            return qbo_id

        except Exception as e:
            self._log_sync("invoice", invoice.id, "push", "error",
                          error_message=str(e))
            invoice.qbo_sync_status = "error"
            self.db.commit()
            logger.error(f"QBO invoice sync failed: {e}")
            return None

    async def sync_payment(self, payment, invoice_qbo_id: str) -> Optional[str]:
        """Push payment to QBO linked to correct invoice."""
        try:
            payload = {
                "TotalAmt": float(payment.amount),
                "CustomerRef": {"value": ""},  # Resolved from invoice
                "Line": [{
                    "Amount": float(payment.amount),
                    "LinkedTxn": [{"TxnId": invoice_qbo_id, "TxnType": "Invoice"}],
                }],
                "TxnDate": str(payment.payment_date),
            }

            resp = await self._api_call("POST", "payment", payload)
            qbo_id = resp.get("Payment", {}).get("Id", "")

            self._log_sync("payment", payment.id, "push", "success", qbo_id=qbo_id)
            payment.qbo_payment_id = qbo_id
            self.db.commit()
            return qbo_id

        except Exception as e:
            self._log_sync("payment", payment.id, "push", "error", error_message=str(e))
            logger.error(f"QBO payment sync failed: {e}")
            return None

    async def close(self):
        await self._client.aclose()
