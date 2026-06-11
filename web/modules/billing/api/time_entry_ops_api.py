"""
Time entry operations API.
POST /api/v1/billing/time-entries/reassign
POST /api/v1/billing/time-entries/create
POST /api/v1/billing/bill-run-matters/{brm_id}/unapprove
"""
from __future__ import annotations
import logging
import uuid as _uuid
from datetime import date
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/billing", tags=["billing-time-entry-ops"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()

@router.post("/time-entries/reassign")
async def reassign_time_entries(request: Request):
    """Move time entries from one matter to another."""
    tid = _tid(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid body"}, status_code=400)

    entry_ids = body.get("time_entry_ids", [])
    target_matter_id = body.get("target_matter_id")

    if not entry_ids or not target_matter_id:
        return JSONResponse({"error": "time_entry_ids and target_matter_id required"}, status_code=400)

    async with AsyncSessionLocal() as db:
        m = (await db.execute(sa_text("""
            SELECT id, matter_name, matter_number FROM matters
            WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"mid": target_matter_id, "tid": tid})).fetchone()
        if not m:
            return JSONResponse({"error": "Target matter not found"}, status_code=404)

        updated = 0
        for eid in entry_ids:
            r = await db.execute(sa_text("""
                UPDATE time_entries
                SET matter_id = CAST(:mid AS uuid), updated_at = NOW()
                WHERE TRIM(tenant_id) = :tid
                  AND id = CAST(:eid AS uuid)
                  AND status = 'draft'
            """), {"mid": target_matter_id, "tid": tid, "eid": str(eid)})
            updated += r.rowcount
        await db.commit()

    return JSONResponse({
        "updated": updated,
        "target_matter": {"id": str(m.id), "matter_name": m.matter_name, "matter_number": m.matter_number}
    })


@router.post("/time-entries/create")
async def create_time_entry(request: Request):
    """Manually create a new time entry."""
    tid = _tid(request)
    user = getattr(request.state, "current_user", None)
    uid = user.id if user and hasattr(user, "id") else None
    user_name = user.full_name if user and hasattr(user, "full_name") else ""

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid body"}, status_code=400)

    matter_id = body.get("matter_id")
    entry_date = body.get("date")
    hours = body.get("hours", 0)
    rate = body.get("rate", 0)
    description = body.get("description", "")
    timekeeper_name = body.get("timekeeper_name", user_name)

    if not matter_id or not entry_date:
        return JSONResponse({"error": "matter_id and date required"}, status_code=400)

    new_id = str(_uuid.uuid4())
    amount = float(hours or 0) * float(rate or 0)

    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            INSERT INTO time_entries
                (id, tenant_id, matter_id, user_id, description, hours, rate,
                 date, billable, status, amount, entry_date,
                 source, source_system, timekeeper_name, created_at, updated_at)
            VALUES
                (CAST(:id AS uuid), :tid, CAST(:mid AS uuid), :uid, :desc, :hrs, :rate,
                 CAST(:dt AS date), true, 'draft', :amt, CAST(:dt AS date),
                 'manual', 'praesidium', :tkname, NOW(), NOW())
        """), {
            "id": new_id, "tid": tid, "mid": matter_id, "uid": uid,
            "desc": description, "hrs": float(hours or 0), "rate": float(rate or 0),
            "dt": entry_date, "amt": amount, "tkname": timekeeper_name,
        })
        await db.commit()

    return JSONResponse({"id": new_id, "amount": amount}, status_code=201)


@router.post("/bill-run-matters/{brm_id}/unapprove")
async def unapprove_matter(request: Request, brm_id: int):
    """Reopen an approved matter back to pending for further edits."""
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        r = await db.execute(sa_text("""
            UPDATE bill_run_matters
            SET status = 'pending', reviewed_by_id = NULL, reviewed_at = NULL, reviewer_notes = NULL
            WHERE id = :id AND tenant_id = :tid AND status = 'approved'
        """), {"id": brm_id, "tid": tid})
        if r.rowcount == 0:
            return JSONResponse({"error": "Not found or not approved"}, status_code=404)
        await db.commit()
    return JSONResponse({"status": "pending", "brm_id": brm_id})


@router.post("/adjustments/{adjustment_id}/reverse")
async def reverse_adjustment_api(request: Request, adjustment_id: int):
    """Reverse a no-charge or write-off adjustment."""
    tid = _tid(request)
    from modules.billing.services.bill_run_service import reverse_adjustment
    result = await reverse_adjustment(tid, adjustment_id)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    return JSONResponse(result)


@router.get("/bill-runs/{bill_run_id}/statement")
async def generate_statement(request: Request, bill_run_id: int):
    """Generate a consolidated statement PDF for all invoiced matters in a bill run."""
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        br = (await db.execute(sa_text("""
            SELECT * FROM bill_runs WHERE id = :brid AND TRIM(tenant_id) = :tid
        """), {"brid": bill_run_id, "tid": tid})).fetchone()
        if not br:
            return JSONResponse({"error": "Bill run not found"}, status_code=404)

        matters = (await db.execute(sa_text("""
            SELECT brm.*, i.invoice_number, i.balance_due as inv_balance
            FROM bill_run_matters brm
            LEFT JOIN invoices i ON i.id = brm.invoice_id
            WHERE brm.bill_run_id = :brid AND TRIM(brm.tenant_id) = :tid
              AND brm.status = 'invoiced'
            ORDER BY brm.matter_number
        """), {"brid": bill_run_id, "tid": tid})).fetchall()

        bs = (await db.execute(sa_text("""
            SELECT * FROM billing_settings WHERE TRIM(tenant_id) = :tid LIMIT 1
        """), {"tid": tid})).fetchone()

        brand = (await db.execute(sa_text("""
            SELECT * FROM tenant_branding WHERE TRIM(tenant_id) = :tid LIMIT 1
        """), {"tid": tid})).fetchone()

    firm_name = bs.firm_name if bs else (brand.firm_name if brand else '')
    firm_addr = f"{bs.firm_address_line1} {bs.firm_suite}, {bs.firm_city}, {bs.firm_state} {bs.firm_zip}" if bs else ''
    firm_phone = bs.firm_phone if bs else ''

    # Build statement HTML
    rows_html = ""
    tot_fees = tot_adj = tot_prior = tot_net = 0
    for m in matters:
        fees = float(m.fee_total or 0)
        adj = float(m.writeoff_total or 0)
        prior = float(m.prior_balance or 0)
        net = float(m.net_total or 0)
        tot_fees += fees; tot_adj += adj; tot_prior += prior; tot_net += net
        rows_html += f"""<tr style="border-bottom:1px solid #eee;">
            <td style="padding:8px 10px;">{m.invoice_number or ''}</td>
            <td style="padding:8px 10px;">{m.matter_number or ''} \u2014 {m.matter_name or ''}</td>
            <td style="padding:8px 10px;text-align:right;font-family:monospace;">${fees:,.2f}</td>
            <td style="padding:8px 10px;text-align:right;font-family:monospace;color:#888;">{'${:,.2f}'.format(adj) if adj > 0 else '\u2014'}</td>
            <td style="padding:8px 10px;text-align:right;font-family:monospace;color:#888;">{'${:,.2f}'.format(prior) if prior > 0 else '\u2014'}</td>
            <td style="padding:8px 10px;text-align:right;font-family:monospace;font-weight:600;">${net:,.2f}</td>
        </tr>"""

    rows_html += f"""<tr style="border-top:2px solid #1a3a5c;font-weight:bold;">
        <td style="padding:10px;"></td>
        <td style="padding:10px;">TOTAL</td>
        <td style="padding:10px;text-align:right;font-family:monospace;">${tot_fees:,.2f}</td>
        <td style="padding:10px;text-align:right;font-family:monospace;">${tot_adj:,.2f}</td>
        <td style="padding:10px;text-align:right;font-family:monospace;">${tot_prior:,.2f}</td>
        <td style="padding:10px;text-align:right;font-family:monospace;font-size:14px;">${tot_net:,.2f}</td>
    </tr>"""

    from datetime import date as _date
    stmt_date = _date.today().strftime('%B %d, %Y')
    period = f"{br.billing_period_start.strftime('%m/%d/%Y')} \u2014 {br.billing_period_end.strftime('%m/%d/%Y')}"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
    @page {{ size: letter; margin: 0.75in; }}
    body {{ font-family: Georgia, serif; color: #1a1a1a; margin: 0; padding: 40px; }}
</style></head><body>
<div style="max-width:700px;margin:0 auto;">
    <div style="text-align:center;border-bottom:2px solid #1a3a5c;padding-bottom:16px;margin-bottom:24px;">
        <h1 style="font-size:20px;margin:0;">{firm_name}</h1>
        <p style="font-size:11px;color:#666;margin:4px 0 0;">{firm_addr} \u00b7 {firm_phone}</p>
    </div>
    <h2 style="text-align:center;font-size:16px;margin:0 0 4px;">STATEMENT OF ACCOUNT</h2>
    <p style="text-align:center;font-size:11px;color:#666;margin:0 0 16px;">{stmt_date}</p>
    <p style="font-size:12px;margin:0 0 4px;"><strong>To:</strong> {br.run_name or 'Client'}</p>
    <p style="font-size:12px;color:#444;margin:0 0 20px;"><strong>Period:</strong> {period}</p>
    <table style="width:100%;border-collapse:collapse;font-size:11px;">
        <thead><tr style="border-bottom:1px solid #1a3a5c;background:#f8f9fa;">
            <th style="padding:8px 10px;text-align:left;font-size:9px;color:#666;text-transform:uppercase;">Invoice #</th>
            <th style="padding:8px 10px;text-align:left;font-size:9px;color:#666;text-transform:uppercase;">Matter</th>
            <th style="padding:8px 10px;text-align:right;font-size:9px;color:#666;text-transform:uppercase;">Current Fees</th>
            <th style="padding:8px 10px;text-align:right;font-size:9px;color:#666;text-transform:uppercase;">No Charge</th>
            <th style="padding:8px 10px;text-align:right;font-size:9px;color:#666;text-transform:uppercase;">Prior Balance</th>
            <th style="padding:8px 10px;text-align:right;font-size:9px;color:#666;text-transform:uppercase;">Amount Due</th>
        </tr></thead>
        <tbody>{rows_html}</tbody>
    </table>
    <p style="text-align:right;font-size:16px;font-weight:bold;margin:20px 0;">Total Amount Due: ${tot_net:,.2f}</p>
    <div style="margin-top:32px;font-size:10px;color:#888;border-top:1px solid #ddd;padding-top:12px;">
        Please remit payment via wire transfer to:<br/>
        <strong>The Holmgren Law Firm, PLLC</strong><br/>
        Account Number: 3908332<br/>
        Routing Number: 111000960<br/>
        North Dallas Bank &amp; Trust Co.<br/>
        12900 Preston Rd., Dallas, TX 75230
    </div>
</div>
</body></html>"""

    try:
        from weasyprint import HTML as WeasyHTML
        pdf_bytes = WeasyHTML(string=html).write_pdf()
        return Response(content=pdf_bytes, media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="Statement_{br.run_name or bill_run_id}.pdf"'})
    except ImportError:
        return HTMLResponse(html)

