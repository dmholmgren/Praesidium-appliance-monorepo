"""RQ jobs for billing — all run on MAIN-PRD-PROC-01."""
import os, uuid, json, logging
from datetime import datetime, date, timedelta
from decimal import Decimal
from calendar import monthrange
logger = logging.getLogger(__name__)

def _get_db_session(tenant_id: str):
    from core.db.base import get_tenant_session
    return get_tenant_session(tenant_id)

def run_billing_qc(tenant_id: str, entry_ids: list = None, user_id: int = 0):
    """AI billing QC — block billing, vague narrative, excessive time, UTBMS."""
    from core.services.ai import AIService
    from core.audit import write_audit
    from modules.billing.models.time_entry import TimeEntry
    from modules.billing.models.billing_qc import BillingQCResult
    db = _get_db_session(tenant_id)
    qc_run_id = str(uuid.uuid4())
    try:
        query = db.query(TimeEntry).filter(TimeEntry.status.in_(["draft","ai_suggested","reviewed"]))
        if entry_ids:
            query = query.filter(TimeEntry.id.in_(entry_ids))
        entries = query.limit(100).all()
        ai = AIService()
        for entry in entries:
            flags = []
            if entry.hours >= Decimal("2.0") and entry.description:
                try:
                    resp = ai.complete(f"Analyze for block billing: {entry.hours}h - \"{entry.description}\". JSON: {{\"is_block_billing\":bool,\"confidence\":float,\"reason\":str}}", max_tokens=500)
                    result = json.loads(resp)
                    if result.get("is_block_billing"):
                        flags.append({"check_type":"block_billing","confidence":result.get("confidence",0.7),"message":result.get("reason","Block billing detected"),"severity":"warning"})
                except Exception:
                    pass
            if entry.description and len(entry.description) < 20:
                flags.append({"check_type":"vague_narrative","confidence":0.9,"message":f"Very short description ({len(entry.description)} chars)","severity":"info"})
            if entry.hours > Decimal("10.0"):
                flags.append({"check_type":"excessive_time","confidence":0.85,"message":f"Entry exceeds 10h ({entry.hours}h)","severity":"warning"})
            if not entry.utbms_code:
                flags.append({"check_type":"missing_task_code","confidence":0.95,"message":"Missing UTBMS code","severity":"error"})
            for flag in flags:
                qc = BillingQCResult(tenant_id=tenant_id, time_entry_id=entry.id, qc_run_id=qc_run_id,
                    check_type=flag["check_type"], severity=flag["severity"],
                    confidence=Decimal(str(flag["confidence"])), message=flag["message"])
                db.add(qc)
        write_audit(db, tenant_id=tenant_id, user_id=user_id, action="billing_qc.run", entity_type="qc_run", entity_id=qc_run_id, new_value={"entries_checked": len(entries)})
        db.commit()
        return {"qc_run_id": qc_run_id, "entries_checked": len(entries)}
    except Exception as e:
        db.rollback()
        logger.error(f"Billing QC failed: {e}")
        raise
    finally:
        db.close()

def run_reconciliation(tenant_id: str, date_from: str, date_to: str):
    import asyncio
    from modules.billing.services.reconciliation_service import ReconciliationService
    db = _get_db_session(tenant_id)
    try:
        svc = ReconciliationService(db)
        return asyncio.get_event_loop().run_until_complete(svc.run_full_reconciliation(date.fromisoformat(date_from), date.fromisoformat(date_to)))
    finally:
        db.close()

def run_monthly_distribution(tenant_id: str, year: int, month: int, overhead_rate: float = 0.35):
    import asyncio
    from modules.billing.services.distribution_service import DistributionService
    db = _get_db_session(tenant_id)
    try:
        svc = DistributionService(db)
        _, last_day = monthrange(year, month)
        return asyncio.get_event_loop().run_until_complete(svc.run_monthly_distribution(date(year, month, 1), date(year, month, last_day), Decimal(str(overhead_rate))))
    finally:
        db.close()

def poll_manictime(tenant_id: str, user_email: str, user_id: int, matter_patterns: dict, hours_back: int = 1):
    import asyncio
    from modules.billing.adapters.manictime import ManicTimeAdapter
    from modules.billing.services.time_entry_service import TimeEntryService
    db = _get_db_session(tenant_id)
    try:
        adapter = ManicTimeAdapter(os.environ.get("MANICTIME_SERVER_URL",""), os.environ.get("MANICTIME_API_KEY",""))
        svc = TimeEntryService(db)
        from_dt = datetime.utcnow() - timedelta(hours=hours_back)
        drafts = asyncio.get_event_loop().run_until_complete(adapter.poll_and_create_entries(user_email, user_id, matter_patterns, from_dt, datetime.utcnow()))
        created = 0
        for d in drafts:
            asyncio.get_event_loop().run_until_complete(svc.create_from_source(d["entry_data"], d["source_data"], user_id))
            created += 1
        asyncio.get_event_loop().run_until_complete(adapter.close())
        return {"entries_created": created}
    finally:
        db.close()

def poll_freepbx(tenant_id: str, extension: str, user_id: int, contact_phone_map: dict, hours_back: int = 1):
    import asyncio
    from modules.billing.adapters.freepbx import FreePBXAdapter
    from modules.billing.services.time_entry_service import TimeEntryService
    db = _get_db_session(tenant_id)
    try:
        adapter = FreePBXAdapter(os.environ.get("FREEPBX_API_URL",""), os.environ.get("FREEPBX_API_TOKEN",""))
        svc = TimeEntryService(db)
        from_dt = datetime.utcnow() - timedelta(hours=hours_back)
        drafts = asyncio.get_event_loop().run_until_complete(adapter.poll_and_create_entries(extension, user_id, contact_phone_map, from_dt, datetime.utcnow()))
        created = 0
        for d in drafts:
            asyncio.get_event_loop().run_until_complete(svc.create_from_source(d["entry_data"], d["source_data"], user_id))
            created += 1
        asyncio.get_event_loop().run_until_complete(adapter.close())
        return {"entries_created": created}
    finally:
        db.close()

def run_monthly_qbo_export(tenant_id: str, year: int, month: int):
    import asyncio
    from modules.billing.adapters.qbo_export import QBOExportAdapter
    from modules.billing.models.qbo_sync import QBOMapping
    db = _get_db_session(tenant_id)
    try:
        mappings = db.query(QBOMapping).filter(QBOMapping.mapping_type == "chart_of_accounts").all()
        chart_map = {m.platform_code: m.qbo_name for m in mappings}
        adapter = QBOExportAdapter(db, chart_map)
        bundle = asyncio.get_event_loop().run_until_complete(adapter.export_monthly_bundle(year, month))
        return {"period": bundle["period"], "files": list(bundle.keys())}
    finally:
        db.close()
