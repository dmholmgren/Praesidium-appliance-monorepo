"""LawPay integration — payment links + webhook processing."""
import hmac, hashlib, logging
from typing import Optional
import httpx
logger = logging.getLogger(__name__)

class LawPayAdapter:
    def __init__(self, base_url: str, api_key: str, secret_key: str):
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), auth=(api_key, secret_key), timeout=30.0)

    async def create_payment_link(self, invoice_number: str, amount_cents: int, client_name: str, client_email: str, description: str, is_trust: bool = False) -> dict:
        payload = {"amount": amount_cents, "currency": "USD", "reference": invoice_number, "custom_id": invoice_number, "description": description, "email": client_email, "name": client_name, "type": "trust" if is_trust else "operating"}
        resp = await self._client.post("/v1/charges", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return {"payment_url": data.get("payment_url",""), "charge_id": data.get("id","")}

    def verify_webhook_signature(self, payload: bytes, signature: str, secret: str) -> bool:
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def close(self):
        await self._client.aclose()
