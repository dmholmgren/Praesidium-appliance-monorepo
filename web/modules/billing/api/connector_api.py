"""
modules/billing/api/connector_api.py

Payment connector status, diagnosis, testing, and webhook endpoints.

Routes:
    GET  /billing/connectors/status                  — All connector statuses (dashboard)
    GET  /billing/connectors/{type}/status            — Single connector status
    POST /billing/connectors/{type}/diagnose          — Deep diagnosis
    POST /billing/connectors/{type}/test              — Connectivity test
    POST /billing/connectors/{type}/config            — Save/update config
    GET  /billing/connectors/{type}/config            — Get config (redacted)
    POST /billing/webhooks/{type}                     — Webhook receiver
"""
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from core.auth.dependencies import get_current_user
from core.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing/connectors", tags=["billing-connectors"])
webhook_router = APIRouter(prefix="/billing/webhooks", tags=["billing-webhooks"])


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _load_gateway_service(tenant_id: str):
    """Load PaymentGatewayService with configs from DB."""
    from modules.billing.services.payment_gateway_service import PaymentGatewayService
    configs = await _load_configs_from_db(tenant_id)
    svc = PaymentGatewayService(tenant_id=tenant_id, gateway_configs=configs)
    await svc.load_gateways()
    return svc


async def _load_configs_from_db(tenant_id: str) -> dict:
    """Load gateway configs from payment_gateway_config table."""
    configs = {}
    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import text
            result = await session.execute(
                text("""
                    SELECT gateway_type, credentials_json, config_json,
                           is_sandbox, enabled
                    FROM payment_gateway_config
                    WHERE TRIM(tenant_id) = TRIM(:tid)
                """),
                {"tid": tenant_id},
            )
            for row in result.mappings():
                gw_type = row["gateway_type"]
                # Merge credentials + config into one dict
                creds = {}
                if row["credentials_json"]:
                    try:
                        creds = json.loads(row["credentials_json"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                config = row["config_json"] or {}
                if isinstance(config, str):
                    try:
                        config = json.loads(config)
                    except (json.JSONDecodeError, TypeError):
                        config = {}
                configs[gw_type] = {
                    **creds,
                    **config,
                    "is_sandbox": row["is_sandbox"],
                    "enabled": row["enabled"],
                }
    except Exception as e:
        logger.error("Failed to load gateway configs: %s", e)
    return configs


def _redact_creds(config: dict) -> dict:
    """Redact sensitive values for display."""
    sensitive = {"api_key", "secret_key", "private_key", "client_secret",
                 "webhook_secret", "access_token", "refresh_token",
                 "webhook_public_key"}
    redacted = {}
    for k, v in config.items():
        if k in sensitive and v:
            redacted[k] = v[:4] + "****" + v[-4:] if len(str(v)) > 8 else "****"
        else:
            redacted[k] = v
    return redacted


# ── Dashboard Endpoints ───────────────────────────────────────────────────────

@router.get("/status")
async def connector_status_all(user=Depends(get_current_user)):
    """All connector statuses for the dashboard."""
    svc = await _load_gateway_service(user.tenant_id)
    try:
        statuses = await svc.all_statuses()
        return {"connectors": statuses}
    finally:
        await svc.close()


@router.get("/{gateway_type}/status")
async def connector_status_single(gateway_type: str, user=Depends(get_current_user)):
    """Single connector status."""
    svc = await _load_gateway_service(user.tenant_id)
    try:
        health = await svc.get_status(gateway_type)
        return health.to_dict()
    finally:
        await svc.close()


@router.post("/{gateway_type}/diagnose")
async def connector_diagnose(gateway_type: str, user=Depends(get_current_user)):
    """Run deep diagnosis on a connector."""
    svc = await _load_gateway_service(user.tenant_id)
    try:
        report = await svc.diagnose(gateway_type)
        # Persist diagnosis result
        await _save_diagnosis(user.tenant_id, gateway_type, report)
        return report.to_dict()
    finally:
        await svc.close()


@router.post("/{gateway_type}/test")
async def connector_test(gateway_type: str, user=Depends(get_current_user)):
    """Test connectivity for a connector."""
    svc = await _load_gateway_service(user.tenant_id)
    try:
        result = await svc.test(gateway_type)
        return result.to_dict()
    finally:
        await svc.close()


# ── Config Management ─────────────────────────────────────────────────────────

class ConnectorConfigPayload(BaseModel):
    credentials: dict = {}
    config: dict = {}
    is_sandbox: bool = False
    enabled: bool = True


@router.post("/{gateway_type}/config")
async def save_connector_config(gateway_type: str, payload: ConnectorConfigPayload,
                                 user=Depends(get_current_user)):
    """Save or update gateway configuration."""
    from modules.billing.services.payment_gateway_service import ALL_CONNECTOR_META
    if gateway_type not in ALL_CONNECTOR_META:
        raise HTTPException(404, f"Unknown gateway type: {gateway_type}")

    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import text
            # Upsert
            await session.execute(
                text("""
                    INSERT INTO payment_gateway_config
                        (tenant_id, gateway_type, credentials_json, config_json,
                         is_sandbox, enabled, updated_at, updated_by)
                    VALUES
                        (TRIM(:tid), :gw, :creds, CAST(:config AS jsonb),
                         :sandbox, :enabled, now(), :uid)
                    ON CONFLICT ON CONSTRAINT uq_pgc_tenant_gateway
                    DO UPDATE SET
                        credentials_json = EXCLUDED.credentials_json,
                        config_json = EXCLUDED.config_json,
                        is_sandbox = EXCLUDED.is_sandbox,
                        enabled = EXCLUDED.enabled,
                        updated_at = now(),
                        updated_by = EXCLUDED.updated_by
                """),
                {
                    "tid": user.tenant_id,
                    "gw": gateway_type,
                    "creds": json.dumps(payload.credentials) if payload.credentials else None,
                    "config": json.dumps(payload.config),
                    "sandbox": payload.is_sandbox,
                    "enabled": payload.enabled,
                    "uid": user.id,
                },
            )
            await session.commit()
        return {"status": "saved", "gateway_type": gateway_type}
    except Exception as e:
        logger.error("Failed to save connector config: %s", e)
        raise HTTPException(500, f"Failed to save config: {e}")


@router.get("/{gateway_type}/config")
async def get_connector_config(gateway_type: str, user=Depends(get_current_user)):
    """Get gateway configuration (credentials redacted)."""
    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import text
            result = await session.execute(
                text("""
                    SELECT credentials_json, config_json, is_sandbox, enabled,
                           last_status, last_status_message,
                           last_successful_call, last_error, last_error_at,
                           last_diagnosis_at, created_at, updated_at
                    FROM payment_gateway_config
                    WHERE TRIM(tenant_id) = TRIM(:tid) AND gateway_type = :gw
                """),
                {"tid": user.tenant_id, "gw": gateway_type},
            )
            row = result.mappings().first()
            if not row:
                return {"gateway_type": gateway_type, "configured": False}

            creds = {}
            if row["credentials_json"]:
                try:
                    creds = json.loads(row["credentials_json"])
                except (json.JSONDecodeError, TypeError):
                    pass

            return {
                "gateway_type": gateway_type,
                "configured": True,
                "credentials": _redact_creds(creds),
                "config": row["config_json"] or {},
                "is_sandbox": row["is_sandbox"],
                "enabled": row["enabled"],
                "last_status": row["last_status"],
                "last_status_message": row["last_status_message"],
                "last_successful_call": row["last_successful_call"].isoformat() if row["last_successful_call"] else None,
                "last_error": row["last_error"],
                "last_error_at": row["last_error_at"].isoformat() if row["last_error_at"] else None,
                "last_diagnosis_at": row["last_diagnosis_at"].isoformat() if row["last_diagnosis_at"] else None,
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            }
    except Exception as e:
        logger.error("Failed to get connector config: %s", e)
        raise HTTPException(500, str(e))


# ── Webhook Endpoints ─────────────────────────────────────────────────────────

@webhook_router.post("/{gateway_type}")
async def receive_webhook(gateway_type: str, request: Request):
    """Receive and process a webhook from a payment gateway.

    Webhooks are tenant-agnostic at the URL level — the gateway doesn't know
    about tenants. We look up the tenant from the invoice reference in the
    webhook payload, or from the gateway config if there's only one tenant.
    """
    body = await request.body()
    headers = dict(request.headers)

    # Log the raw webhook
    log_id = await _log_webhook(gateway_type, headers, body)

    # For now, process against the single appliance tenant
    # Multi-tenant: resolve tenant from invoice reference in webhook payload
    tenant_id = "986c0fee-1390-43bb-ad28-8cd1db6de53f"

    svc = await _load_gateway_service(tenant_id)
    try:
        event = await svc.process_webhook(gateway_type, headers, body)
        if event:
            # Update webhook log
            await _mark_webhook_processed(log_id, tenant_id, event)
            # Process the payment event
            await _handle_payment_event(tenant_id, event)
            return {"status": "processed", "event_type": event.event_type}
        else:
            return Response(status_code=200, content="OK")
    except Exception as e:
        logger.error("Webhook processing error for %s: %s", gateway_type, e)
        await _mark_webhook_error(log_id, str(e))
        return Response(status_code=200, content="OK")  # Always 200 to prevent retries
    finally:
        await svc.close()


# ── Internal Helpers ──────────────────────────────────────────────────────────

async def _save_diagnosis(tenant_id: str, gateway_type: str, report):
    """Persist diagnosis result to payment_gateway_config."""
    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import text
            await session.execute(
                text("""
                    UPDATE payment_gateway_config
                    SET last_status = :status,
                        last_status_message = :msg,
                        last_diagnosis_json = CAST(:diag AS jsonb),
                        last_diagnosis_at = now(),
                        updated_at = now()
                    WHERE TRIM(tenant_id) = TRIM(:tid) AND gateway_type = :gw
                """),
                {
                    "tid": tenant_id,
                    "gw": gateway_type,
                    "status": report.overall_status.value,
                    "msg": "; ".join(c.message for c in report.failed_checks) if report.failed_checks
                           else "All checks passed",
                    "diag": json.dumps(report.to_dict()),
                },
            )
            await session.commit()
    except Exception as e:
        logger.error("Failed to save diagnosis: %s", e)


async def _log_webhook(gateway_type: str, headers: dict, body: bytes) -> Optional[str]:
    """Log raw webhook to payment_webhook_log."""
    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import text
            result = await session.execute(
                text("""
                    INSERT INTO payment_webhook_log
                        (gateway_type, raw_headers, raw_body, received_at)
                    VALUES
                        (:gw, CAST(:headers AS jsonb), :body, now())
                    RETURNING id
                """),
                {
                    "gw": gateway_type,
                    "headers": json.dumps({k: v for k, v in headers.items()
                                          if k.lower() not in ("authorization",)}),
                    "body": body.decode("utf-8", errors="replace")[:50000],
                },
            )
            await session.commit()
            row = result.first()
            return str(row[0]) if row else None
    except Exception as e:
        logger.error("Failed to log webhook: %s", e)
        return None


async def _mark_webhook_processed(log_id: str, tenant_id: str, event):
    """Update webhook log with processed event data."""
    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import text
            await session.execute(
                text("""
                    UPDATE payment_webhook_log
                    SET tenant_id = TRIM(:tid),
                        event_type = :etype,
                        transaction_id = :txn,
                        invoice_reference = :inv,
                        amount_cents = :amt,
                        currency = :cur,
                        status = :status,
                        signature_valid = true,
                        processed = true,
                        processed_at = now()
                    WHERE id = CAST(:lid AS uuid)
                """),
                {
                    "tid": tenant_id,
                    "etype": event.event_type,
                    "txn": event.transaction_id,
                    "inv": event.invoice_reference,
                    "amt": event.amount_cents,
                    "cur": event.currency,
                    "status": event.status,
                    "lid": log_id,
                },
            )
            await session.commit()
    except Exception as e:
        logger.error("Failed to update webhook log: %s", e)


async def _mark_webhook_error(log_id: str, error: str):
    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import text
            await session.execute(
                text("""
                    UPDATE payment_webhook_log
                    SET error_message = :err, processed_at = now()
                    WHERE id = CAST(:lid AS uuid)
                """),
                {"err": error, "lid": log_id},
            )
            await session.commit()
    except Exception as e:
        logger.error("Failed to mark webhook error: %s", e)


async def _handle_payment_event(tenant_id: str, event):
    """Process a payment event — update invoice/payment records."""
    if event.event_type in ("payment.completed",):
        try:
            async with AsyncSessionLocal() as session:
                from sqlalchemy import text
                # Look up invoice by reference
                result = await session.execute(
                    text("""
                        SELECT id FROM invoices
                        WHERE TRIM(tenant_id) = TRIM(:tid)
                          AND invoice_number = :inv
                        LIMIT 1
                    """),
                    {"tid": tenant_id, "inv": event.invoice_reference},
                )
                row = result.first()
                if not row:
                    logger.warning("Webhook payment for unknown invoice: %s", event.invoice_reference)
                    return

                invoice_id = row[0]
                amount_decimal = event.amount_cents / 100

                # Record the payment
                await session.execute(
                    text("""
                        INSERT INTO payments
                            (tenant_id, invoice_id, amount, payment_date, method,
                             gateway_type, gateway_transaction_id)
                        VALUES
                            (TRIM(:tid), :inv_id, :amt, now(), :method, :gw, :txn)
                    """),
                    {
                        "tid": tenant_id,
                        "inv_id": invoice_id,
                        "amt": amount_decimal,
                        "method": event.gateway_type,
                        "gw": event.gateway_type,
                        "txn": event.transaction_id,
                    },
                )

                # Update invoice balance
                await session.execute(
                    text("""
                        UPDATE invoices
                        SET amount_paid = COALESCE(amount_paid, 0) + :amt,
                            balance_due = GREATEST(0, COALESCE(balance_due, 0) - :amt),
                            status = CASE
                                WHEN COALESCE(balance_due, 0) - :amt <= 0 THEN 'paid'
                                ELSE 'partial'
                            END,
                            updated_at = now()
                        WHERE id = :inv_id
                    """),
                    {"amt": amount_decimal, "inv_id": invoice_id},
                )
                await session.commit()
                logger.info("Webhook payment processed: %s → invoice %s ($%.2f via %s)",
                           event.transaction_id, event.invoice_reference,
                           amount_decimal, event.gateway_type)
        except Exception as e:
            logger.error("Failed to handle payment event: %s", e)
    else:
        logger.info("Unhandled webhook event type: %s", event.event_type)
