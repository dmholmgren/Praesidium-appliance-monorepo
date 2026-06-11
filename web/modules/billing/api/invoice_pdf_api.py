"""
Invoice rendering API v2.
GET /api/v1/billing/invoice-render/{invoice_id}/preview — HTML preview
GET /api/v1/billing/invoice-render/{invoice_id}/pdf — download PDF

Shows N/C entries on the invoice with "No Charge" and $0, 
W/O entries with strikethrough.
"""
from __future__ import annotations
import logging
from datetime import date
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/billing", tags=["billing-invoice-pdf"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()

def fmt_money(v):
    v = float(v or 0)
    return f"${v:,.2f}"

def fmt_date(d):
    if not d: return ''
    if isinstance(d, str): d = date.fromisoformat(d)
    return d.strftime('%m/%d/%Y')


async def build_invoice_html(tid: str, invoice_id: str) -> str:
    async with AsyncSessionLocal() as db:
        inv = (await db.execute(sa_text("""
            SELECT i.*, m.matter_name, m.matter_number,
                   c.client_name
            FROM invoices i
            LEFT JOIN matters m ON m.id = i.matter_id
            LEFT JOIN clients c ON c.id = COALESCE(i.client_id, m.client_id)
            WHERE i.id = CAST(:id AS uuid) AND TRIM(i.tenant_id) = :tid
        """), {"id": invoice_id, "tid": tid})).fetchone()

        if not inv:
            return "<h1>Invoice not found</h1>"

        brand = (await db.execute(sa_text("""
            SELECT * FROM tenant_branding WHERE TRIM(tenant_id) = :tid LIMIT 1
        """), {"tid": tid})).fetchone()

        # Resolve template
        template_html = None
        if inv.matter_id:
            tpl = (await db.execute(sa_text("""
                SELECT bt.html_content FROM bill_template_assignments bta
                JOIN bill_templates bt ON bt.id = bta.template_id
                WHERE TRIM(bta.tenant_id) = :tid AND bta.entity_type = 'matter'
                  AND bta.entity_id = CAST(:mid AS uuid) AND bt.is_active = true
            """), {"tid": tid, "mid": str(inv.matter_id)})).fetchone()
            if tpl: template_html = tpl.html_content

        if not template_html and inv.client_id:
            tpl = (await db.execute(sa_text("""
                SELECT bt.html_content FROM bill_template_assignments bta
                JOIN bill_templates bt ON bt.id = bta.template_id
                WHERE TRIM(bta.tenant_id) = :tid AND bta.entity_type = 'client'
                  AND bta.entity_id = CAST(:cid AS uuid) AND bt.is_active = true
            """), {"tid": tid, "cid": str(inv.client_id)})).fetchone()
            if tpl: template_html = tpl.html_content

        if not template_html:
            tpl = (await db.execute(sa_text("""
                SELECT html_content FROM bill_templates
                WHERE TRIM(tenant_id) = :tid AND is_active = true
                ORDER BY is_default DESC, sort_order LIMIT 1
            """), {"tid": tid})).fetchone()
            if tpl: template_html = tpl.html_content

        if not template_html:
            template_html = "<h1>No invoice template configured</h1>"

        # Get time entries
        entries = (await db.execute(sa_text("""
            SELECT te.id, te.date, te.hours, te.rate,
                   COALESCE(te.amount, te.hours * te.rate) AS value,
                   te.description, te.timekeeper_name
            FROM time_entries te
            WHERE TRIM(te.tenant_id) = :tid
              AND te.invoice_id = CAST(:inv AS uuid)
            ORDER BY te.date, te.timekeeper_name
        """), {"tid": tid, "inv": invoice_id})).fetchall()

        if not entries and inv.matter_id:
            entries = (await db.execute(sa_text("""
                SELECT te.id, te.date, te.hours, te.rate,
                       COALESCE(te.amount, te.hours * te.rate) AS value,
                       te.description, te.timekeeper_name
                FROM time_entries te
                WHERE TRIM(te.tenant_id) = :tid
                  AND te.matter_id = CAST(:mid AS uuid)
                  AND te.status = 'billed'
                ORDER BY te.date, te.timekeeper_name
                LIMIT 500
            """), {"tid": tid, "mid": str(inv.matter_id)})).fetchall()

        # Get N/C and W/O adjustments for this bill run matter
        # Find the bill_run_matter_id for this invoice
        nc_ids = set()
        wo_ids = set()
        if inv.bill_run_id:
            brm = (await db.execute(sa_text("""
                SELECT id FROM bill_run_matters
                WHERE TRIM(tenant_id) = :tid AND bill_run_id = :brid
                  AND (invoice_id = CAST(:inv AS uuid) OR matter_id = CAST(:mid AS uuid))
                LIMIT 1
            """), {"tid": tid, "brid": inv.bill_run_id, "inv": invoice_id,
                   "mid": str(inv.matter_id) if inv.matter_id else ''})).fetchone()

            if brm:
                adjs = (await db.execute(sa_text("""
                    SELECT time_entry_id, adjustment_type
                    FROM prebill_adjustments
                    WHERE TRIM(tenant_id) = :tid
                      AND bill_run_matter_id = :brmid
                      AND time_entry_id IS NOT NULL
                      AND adjustment_type IN ('no_charge', 'write_off')
                """), {"tid": tid, "brmid": brm.id})).fetchall()

                for a in adjs:
                    if a.adjustment_type == 'no_charge':
                        nc_ids.add(str(a.time_entry_id))
                    elif a.adjustment_type == 'write_off':
                        wo_ids.add(str(a.time_entry_id))

        # Build line_items
        line_items = '<table style="width:100%;border-collapse:collapse;font-size:11px;margin:16px 0;">\n'
        line_items += '<thead><tr style="border-bottom:1px solid #ccc;">'
        line_items += '<th style="text-align:left;padding:6px;">Date</th>'
        line_items += '<th style="text-align:left;padding:6px;">Timekeeper</th>'
        line_items += '<th style="text-align:left;padding:6px;">Description</th>'
        line_items += '<th style="text-align:right;padding:6px;">Hours</th>'
        line_items += '<th style="text-align:right;padding:6px;">Amount</th>'
        line_items += '</tr></thead><tbody>\n'

        billable_total = 0.0

        for e in entries:
            eid = str(e.id)
            hrs = float(e.hours or 0)
            val = float(e.value or 0)
            desc = (e.description or '').replace('<', '&lt;').replace('>', '&gt;')
            is_nc = eid in nc_ids
            is_wo = eid in wo_ids

            if is_wo:
                # Write-off: show with strikethrough, don't count
                line_items += (
                    f'<tr style="border-bottom:1px solid #eee;color:#999;text-decoration:line-through;">'
                    f'<td style="padding:6px;white-space:nowrap;">{fmt_date(e.date)}</td>'
                    f'<td style="padding:6px;">{e.timekeeper_name or ""}</td>'
                    f'<td style="padding:6px;">{desc}</td>'
                    f'<td style="text-align:right;padding:6px;">{hrs:.2f}</td>'
                    f'<td style="text-align:right;padding:6px;"><span style="text-decoration:line-through;">{fmt_money(val)}</span> <em style="font-size:9px;color:#dc2626;">W/O</em></td>'
                    f'</tr>\n'
                )
            elif is_nc:
                # No Charge: show entry but $0 with "No Charge" label
                line_items += (
                    f'<tr style="border-bottom:1px solid #eee;">'
                    f'<td style="padding:6px;white-space:nowrap;">{fmt_date(e.date)}</td>'
                    f'<td style="padding:6px;">{e.timekeeper_name or ""}</td>'
                    f'<td style="padding:6px;">{desc}</td>'
                    f'<td style="text-align:right;padding:6px;">{hrs:.2f}</td>'
                    f'<td style="text-align:right;padding:6px;color:#888;font-style:italic;">No Charge</td>'
                    f'</tr>\n'
                )
            else:
                # Normal billable entry
                billable_total += val
                line_items += (
                    f'<tr style="border-bottom:1px solid #eee;">'
                    f'<td style="padding:6px;white-space:nowrap;">{fmt_date(e.date)}</td>'
                    f'<td style="padding:6px;">{e.timekeeper_name or ""}</td>'
                    f'<td style="padding:6px;">{desc}</td>'
                    f'<td style="text-align:right;padding:6px;">{hrs:.2f}</td>'
                    f'<td style="text-align:right;padding:6px;">{fmt_money(val)}</td>'
                    f'</tr>\n'
                )

        line_items += '</tbody></table>'

        total_fees = float(inv.subtotal or inv.total_amount or 0)
        balance = float(inv.balance_due or inv.total_amount or 0)
        prior = balance - total_fees
        if prior < 0: prior = 0

        nc_total = sum(float(e.value or 0) for e in entries if str(e.id) in nc_ids)
        wo_total_val = sum(float(e.value or 0) for e in entries if str(e.id) in wo_ids)

        # Pull from billing_settings first, then branding
        bs = (await db.execute(sa_text("""
            SELECT * FROM billing_settings WHERE TRIM(tenant_id) = :tid LIMIT 1
        """), {"tid": tid})).fetchone()

        firm_name = bs.firm_name if bs else (brand.firm_name if brand else '')

        billing_period = ""
        if inv.bill_run_id:
            br = (await db.execute(sa_text("""
                SELECT billing_period_start, billing_period_end FROM bill_runs WHERE id = :brid
            """), {"brid": inv.bill_run_id})).fetchone()
            if br:
                billing_period = f"{fmt_date(br.billing_period_start)} \u2014 {fmt_date(br.billing_period_end)}"

        merge = {
            'firm_name': firm_name,
            'firm_logo': '',
            'firm_address_line1': bs.firm_address_line1 if bs else '',
            'firm_suite': bs.firm_suite if bs else '',
            'firm_city': bs.firm_city if bs else '',
            'firm_state': bs.firm_state if bs else '',
            'firm_zip': bs.firm_zip if bs else '',
            'firm_phone': bs.firm_phone if bs else '',
            'firm_email': bs.firm_email if bs else '',
            'firm_address': f"{bs.firm_address_line1} {bs.firm_suite}, {bs.firm_city}, {bs.firm_state} {bs.firm_zip}" if bs else '',
            'firm_detail': '',
            'invoice_number': inv.invoice_number or '',
            'invoice_date': fmt_date(inv.invoice_date),
            'due_date': fmt_date(inv.due_date) if hasattr(inv, 'due_date') and inv.due_date else '',
            'client_name': inv.client_name or '',
            'client_address': '',
            'matter_name': inv.matter_name or '',
            'matter_number': inv.matter_number or '',
            'billing_period': billing_period,
            'line_items': line_items,
            'time_entries_rows': '',
            'total_fees': fmt_money(total_fees),
            'total_hours': f"{sum(float(e.hours or 0) for e in entries):.1f}",
            'total_expenses': fmt_money(0),
            'writeoffs': fmt_money(wo_total_val) if wo_total_val > 0 else fmt_money(0),
            'no_charge_total': fmt_money(nc_total) if nc_total > 0 else fmt_money(0),
            'prior_balance': fmt_money(prior),
            'net_total': fmt_money(balance),
            'amount_due': fmt_money(balance),
            'payment_instructions': 'Please remit payment via wire transfer to:<br/>'
                '<strong>The Holmgren Law Firm, PLLC</strong><br/>'
                'Account Number: 3908332<br/>'
                'Routing Number: 111000960<br/>'
                'North Dallas Bank &amp; Trust Co.<br/>'
                '12900 Preston Rd.<br/>'
                'Dallas, TX 75230',
        }

        html = template_html
        for key, val in merge.items():
            html = html.replace('{{' + key + '}}', str(val))

        if '<html' not in html.lower():
            html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  @page {{ size: letter; margin: 0.75in; }}
  body {{ font-family: Georgia, serif; color: #1a1a1a; margin: 0; padding: 40px; }}
</style>
</head><body>{html}</body></html>"""

        return html


@router.get("/invoice-render/{invoice_id}/preview")
async def invoice_preview(request: Request, invoice_id: str):
    tid = _tid(request)
    html = await build_invoice_html(tid, invoice_id)
    return HTMLResponse(html)


@router.get("/invoice-render/{invoice_id}/pdf")
async def invoice_pdf(request: Request, invoice_id: str):
    tid = _tid(request)
    html = await build_invoice_html(tid, invoice_id)

    try:
        from weasyprint import HTML as WeasyHTML
        pdf_bytes = WeasyHTML(string=html).write_pdf()
    except ImportError:
        try:
            import pdfkit
            pdf_bytes = pdfkit.from_string(html, False)
        except Exception:
            return HTMLResponse(html + '<script>window.print();</script>')

    async with AsyncSessionLocal() as db:
        inv = (await db.execute(sa_text("""
            SELECT invoice_number FROM invoices
            WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = :tid
        """), {"id": invoice_id, "tid": tid})).fetchone()
    num = inv.invoice_number if inv else invoice_id[:8]

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="Invoice_{num}.pdf"'}
    )
