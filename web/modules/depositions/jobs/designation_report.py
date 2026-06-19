"""designation_report.py -- deterministic designation-report compiler (scope §6).

The copy-paste killer: a SELECT over depo_designations + a data-driven template,
not a hand-reformat in Word. Default template (the firm's most common) is the
four-column chart:

    Page:Line | Testimony | Objections | Counter-Designations

grouped by designating party. The column set is data-driven (TEMPLATES) so a
court's local form swaps in without touching the build. DOCX via python-docx;
PDF by converting that DOCX through LibreOffice headless -- the same soffice lane
the eDiscovery render path uses (no extra dependency).

Association rule (deterministic): objection / counter designations are attached to
the affirmative row whose page:line span they overlap; any that overlap nothing
get their own row. Designations stay deterministic page:line spans -- this only
arranges them.
"""
from __future__ import annotations

import logging
import os
import subprocess
import uuid

logger = logging.getLogger(__name__)

OUT_DIR = "/tmp/depo_reports"

# Data-driven column templates. key -> ordered column headers. The 4-column form
# is the seeded default; add a local-rule form by adding a key here.
TEMPLATES = {
    "four_column": ["Page:Line", "Testimony", "Objections", "Counter-Designations"],
    "affirmative_counter": ["Affirmative (Page:Line / Testimony)", "Counter-Designations"],
}
DEFAULT_TEMPLATE = "four_column"


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def _key(p, l):
    return (p or 0) * 100000 + (l or 0)


def _overlaps(a, b):
    return a[0] <= b[1] and b[0] <= a[1]


def _fetch(conn, tenant, transcript_id=None, session_id=None):
    cur = conn.cursor()
    where = ["TRIM(d.tenant_id) = %s"]
    params = [tenant.strip()]
    if transcript_id:
        where.append("d.transcript_id = CAST(%s AS uuid)")
        params.append(str(transcript_id))
    if session_id:
        where.append("d.session_id = %s")
        params.append(session_id)
    cur.execute(
        "SELECT d.id::text, d.designating_party, d.designation_type, d.start_page, "
        "       d.start_line, d.end_page, d.end_line, d.excerpt_text, d.issue_code, "
        "       d.note, t.deponent, t.matter_id::text "
        "FROM depo_designations d JOIN deposition_transcripts t ON t.id = d.transcript_id "
        "WHERE " + " AND ".join(where) +
        " ORDER BY d.designating_party NULLS LAST, d.start_page, d.start_line", params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _matter_name(conn, tenant, matter_id):
    if not matter_id:
        return ""
    cur = conn.cursor()
    cur.execute("SELECT matter_name FROM matters WHERE id = CAST(%s AS uuid) "
                "AND TRIM(tenant_id) = %s", (matter_id, tenant.strip()))
    r = cur.fetchone()
    return r[0] if r else ""


def _pl(d, start=True):
    if start:
        return "%d:%02d" % (d["start_page"], d["start_line"])
    return "%d:%02d" % (d["end_page"], d["end_line"])


def _span_label(d):
    return "%s–%s" % (_pl(d, True), _pl(d, False))


def build_docx(tenant, transcript_id=None, session_id=None,
               template=DEFAULT_TEMPLATE, report_date="") -> dict:
    """Compile the designation report to a DOCX. Returns {path, designations, ...}."""
    from docx import Document
    from docx.shared import Pt
    os.makedirs(OUT_DIR, exist_ok=True)
    conn = _connect()
    try:
        desigs = _fetch(conn, tenant, transcript_id, session_id)
        deponent = desigs[0]["deponent"] if desigs else "Deposition"
        matter = _matter_name(conn, tenant, desigs[0]["matter_id"]) if desigs else ""
    finally:
        conn.close()

    cols = TEMPLATES.get(template, TEMPLATES[DEFAULT_TEMPLATE])
    doc = Document()
    doc.add_heading("Deposition Designations", level=0)
    sub = doc.add_paragraph()
    sub.add_run("Deponent: ").bold = True
    sub.add_run(deponent)
    if matter:
        sub.add_run("    Matter: ").bold = True
        sub.add_run(matter)
    if report_date:
        sub.add_run("    Date: ").bold = True
        sub.add_run(report_date)

    # group by designating party
    parties = {}
    for d in desigs:
        parties.setdefault(d["designating_party"] or "(unspecified)", []).append(d)

    total_rows = 0
    for party, items in parties.items():
        affirmatives = [d for d in items if d["designation_type"] == "affirmative"]
        objections = [d for d in items if d["designation_type"] == "objection"]
        counters = [d for d in items if d["designation_type"] == "counter"]
        doc.add_heading("Designating party: %s" % party, level=2)
        table = doc.add_table(rows=1, cols=len(cols))
        table.style = "Light Grid Accent 1"
        for i, h in enumerate(cols):
            c = table.rows[0].cells[i].paragraphs[0].add_run(h)
            c.bold = True
        used_obj, used_ctr = set(), set()
        for d in affirmatives:
            span = (_key(d["start_page"], d["start_line"]), _key(d["end_page"], d["end_line"]))
            row_obj = [o for o in objections
                       if _overlaps(span, (_key(o["start_page"], o["start_line"]),
                                           _key(o["end_page"], o["end_line"])))]
            row_ctr = [c for c in counters
                       if _overlaps(span, (_key(c["start_page"], c["start_line"]),
                                           _key(c["end_page"], c["end_line"])))]
            for o in row_obj:
                used_obj.add(o["id"])
            for c in row_ctr:
                used_ctr.add(c["id"])
            _add_row(table, cols, d, row_obj, row_ctr)
            total_rows += 1
        # objections/counters that didn't attach to any affirmative -> own rows
        for o in [o for o in objections if o["id"] not in used_obj]:
            _add_row(table, cols, o, [o], [])
            total_rows += 1
        for c in [c for c in counters if c["id"] not in used_ctr]:
            _add_row(table, cols, c, [], [c])
            total_rows += 1

    if not desigs:
        doc.add_paragraph("No designations to report.")

    path = os.path.join(OUT_DIR, "designation_report_%s.docx" % uuid.uuid4().hex[:12])
    doc.save(path)
    return {"path": path, "designations": len(desigs), "rows": total_rows,
            "deponent": deponent, "template": template}


def _add_row(table, cols, primary, objections, counters):
    cells = table.add_row().cells
    obj_text = "; ".join(filter(None, [
        (o.get("issue_code") or "objection") + (": " + o["note"] if o.get("note") else "")
        for o in objections])) or "—"
    ctr_text = "; ".join(_span_label(c) + (" " + (c["excerpt_text"] or "")[:80] if c.get("excerpt_text") else "")
                         for c in counters) or "—"
    if cols == TEMPLATES["four_column"]:
        cells[0].text = _span_label(primary)
        cells[1].text = (primary.get("excerpt_text") or "")
        cells[2].text = obj_text
        cells[3].text = ctr_text
    else:  # affirmative_counter
        cells[0].text = _span_label(primary) + "\n" + (primary.get("excerpt_text") or "")
        cells[1].text = ctr_text


def to_pdf(docx_path) -> str:
    """Convert the DOCX to PDF via LibreOffice headless (the eDiscovery soffice
    lane). Returns the PDF path."""
    outdir = os.path.dirname(docx_path)
    prof = os.path.join(outdir, ".lo_profile")
    subprocess.run(
        ["soffice", "--headless", "-env:UserInstallation=file://%s" % prof,
         "--convert-to", "pdf", "--outdir", outdir, docx_path],
        check=True, capture_output=True, timeout=120)
    pdf_path = docx_path[:-5] + ".pdf"
    if not os.path.exists(pdf_path):
        raise RuntimeError("soffice produced no pdf")
    return pdf_path


def build_report(tenant, transcript_id=None, session_id=None, fmt="docx",
                 template=DEFAULT_TEMPLATE, report_date="") -> dict:
    out = build_docx(tenant, transcript_id, session_id, template, report_date)
    if fmt == "pdf":
        out["pdf_path"] = to_pdf(out["path"])
    return out
