"""
bill_run_statement_api.py — Combined statement + invoice PDF endpoint.
GET /api/v1/billing/bill-runs/{bill_run_id}/statement-pdf
Generates a single PDF: cover statement page + each invoice on its own page.
"""
from __future__ import annotations
import logging
from datetime import date
from fastapi import APIRouter, Request
from fastapi.responses import Response, HTMLResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal
from modules.billing.api.invoice_pdf_api import build_invoice_html, fmt_money, fmt_date

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/billing", tags=["billing-statement"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()


async def build_statement_html(tid: str, bill_run_id: int) -> str:
    """Build combined HTML: statement cover + page-break + each invoice."""
    async with AsyncSessionLocal() as db:
        br = (await db.execute(sa_text("""
            SELECT * FROM bill_runs WHERE id = :id AND TRIM(tenant_id) = :tid
        """), {"id": bill_run_id, "tid": tid})).fetchone()
        if not br:
            return "<h1>Bill run not found</h1>"

        bs = (await db.execute(sa_text("""
            SELECT * FROM billing_settings WHERE TRIM(tenant_id) = :tid LIMIT 1
        """), {"tid": tid})).fetchone()

        invoices = (await db.execute(sa_text("""
            SELECT i.id, i.invoice_number, i.total_amount, i.balance_due,
                   m.matter_name, m.matter_number, c.client_name
            FROM invoices i
            LEFT JOIN matters m ON m.id = i.matter_id
            LEFT JOIN clients c ON c.id = COALESCE(i.client_id, m.client_id)
            WHERE TRIM(i.tenant_id) = :tid AND i.bill_run_id = :brid
            ORDER BY m.matter_number
        """), {"tid": tid, "brid": bill_run_id})).fetchall()

    if not invoices:
        return "<h1>No invoices in this bill run</h1>"

    firm_name = bs.firm_name if bs else 'Firm'
    firm_addr = f"{bs.firm_address_line1} {bs.firm_suite}, {bs.firm_city}, {bs.firm_state} {bs.firm_zip}" if bs else ''
    firm_phone = bs.firm_phone if bs else ''
    client_name = invoices[0].client_name or 'Client'
    grand_total = sum(float(i.total_amount or 0) for i in invoices)
    period = f"{fmt_date(br.billing_period_start)} — {fmt_date(br.billing_period_end)}"

    # Build statement rows
    rows = ""
    for inv in invoices:
        rows += f"""<tr>
            <td style="padding:4px 8px;border-bottom:1px solid #eee;">{inv.invoice_number}</td>
            <td style="padding:4px 8px;border-bottom:1px solid #eee;">{inv.matter_number or ''}</td>
            <td style="padding:4px 8px;border-bottom:1px solid #eee;">{inv.matter_name or ''}</td>
            <td style="padding:4px 8px;border-bottom:1px solid #eee;text-align:right;">{fmt_money(inv.total_amount)}</td>
        </tr>"""

    cover = f"""
    <div style="max-width:700px;margin:0 auto;font-family:Georgia,serif;color:#1a1a1a;">
      <div style="text-align:center;border-bottom:2px solid #1a3a5c;padding-bottom:16px;margin-bottom:24px;">
        <h1 style="font-size:20px;margin:0;">{firm_name}</h1>
        <p style="font-size:11px;color:#666;margin:4px 0 0;">{firm_addr} &middot; {firm_phone}</p>
      </div>
      <table style="width:100%;margin-bottom:20px;font-size:12px;">
      <tr>
        <td style="vertical-align:top;"><strong>Statement To:</strong><br>{client_name}</td>
        <td style="text-align:right;vertical-align:top;">
          <strong>Statement Date:</strong> {fmt_date(date.today())}<br>
          <strong>Bill Run:</strong> {br.run_name}<br>
          <strong>Period:</strong> {period}<br>
          <strong>Net Terms:</strong> Due Upon Receipt
        </td>
      </tr>
      </table>
      <h2 style="font-size:14px;color:#1a3a5c;border-bottom:1px solid #ccc;padding-bottom:4px;margin-top:24px;">Invoices</h2>
      <table style="width:100%;border-collapse:collapse;font-size:11px;">
      <thead><tr>
        <th style="text-align:left;padding:6px 8px;border-bottom:2px solid #1a3a5c;font-size:10px;text-transform:uppercase;color:#1a3a5c;">Invoice #</th>
        <th style="text-align:left;padding:6px 8px;border-bottom:2px solid #1a3a5c;font-size:10px;text-transform:uppercase;color:#1a3a5c;">Matter #</th>
        <th style="text-align:left;padding:6px 8px;border-bottom:2px solid #1a3a5c;font-size:10px;text-transform:uppercase;color:#1a3a5c;">Matter</th>
        <th style="text-align:right;padding:6px 8px;border-bottom:2px solid #1a3a5c;font-size:10px;text-transform:uppercase;color:#1a3a5c;">Amount</th>
      </tr></thead>
      <tbody>{rows}
      <tr>
        <td colspan="3" style="padding:8px;border-top:2px solid #1a3a5c;font-weight:bold;font-size:13px;">Total — {len(invoices)} Matters</td>
        <td style="text-align:right;padding:8px;border-top:2px solid #1a3a5c;font-weight:bold;font-size:13px;">{fmt_money(grand_total)}</td>
      </tr>
      </tbody></table>
      <div style="margin-top:24px;padding:12px;background:#f5f7fa;border-left:3px solid #1a3a5c;font-size:11px;">
        <strong>Amount Due: {fmt_money(grand_total)}</strong>
      </div>
      <div style="margin-top:32px;font-size:10px;color:#888;border-top:1px solid #ddd;padding-top:12px;">
        Please remit payment to {firm_name}.<br>
        For questions, contact Dennis Holmgren at {firm_phone} or dholmgren@hjmmlegal.com.
      </div>
    </div>
    """

    # Build each invoice HTML
    invoice_pages = []
    for inv in invoices:
        inv_html = await build_invoice_html(tid, str(inv.id))
        # Strip any wrapping <html>/<body> if present — we'll wrap once
        for tag in ['<!DOCTYPE html>', '<html>', '</html>', '<head>', '</head>', '<body>', '</body>']:
            inv_html = inv_html.replace(tag, '')
        # Also strip <meta> and <style> blocks from individual invoices
        import re
        inv_html = re.sub(r'<meta[^>]*>', '', inv_html)
        inv_html = re.sub(r'<style[^>]*>.*?</style>', '', inv_html, flags=re.DOTALL)
        invoice_pages.append(inv_html)

    # Combine: statement cover + page breaks + each invoice
    combined = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  @page {{ size: letter; margin: 0.75in; }}
  body {{ font-family: Georgia, serif; color: #1a1a1a; margin: 0; padding: 0; }}
  .page-break {{ page-break-before: always; }}
</style>
</head><body>
{cover}
"""
    for page_html in invoice_pages:
        combined += f'\n<div class="page-break">{page_html}</div>\n'

    combined += "</body></html>"
    return combined


@router.get("/bill-runs/{bill_run_id}/statement-pdf")
async def statement_pdf(request: Request, bill_run_id: int):
    tid = _tid(request)
    html = await build_statement_html(tid, bill_run_id)

    try:
        from weasyprint import HTML as WeasyHTML
        pdf_bytes = WeasyHTML(string=html).write_pdf()
    except ImportError:
        return HTMLResponse(html + '<script>window.print();</script>')

    # Build filename: Statement_ClientName_YYYY-MM-DD
    async with AsyncSessionLocal() as db:
        row = (await db.execute(sa_text("""
            SELECT DISTINCT c.client_name FROM invoices i
            JOIN matters m ON m.id = i.matter_id
            JOIN clients c ON c.id = m.client_id
            WHERE TRIM(i.tenant_id) = :tid AND i.bill_run_id = :brid LIMIT 1
        """), {"tid": tid, "brid": bill_run_id})).fetchone()
    client_slug = (row.client_name if row else 'Client').replace(' ', '_').replace(',', '').replace("'", '')
    filename = f"Statement_{client_slug}_{date.today().isoformat()}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.get("/bill-runs/{bill_run_id}/statement-preview")
async def statement_preview(request: Request, bill_run_id: int):
    tid = _tid(request)
    html = await build_statement_html(tid, bill_run_id)
    return HTMLResponse(html)
