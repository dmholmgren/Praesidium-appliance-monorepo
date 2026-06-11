"""
jobs/run_production.py
======================
RQ job: stamp PDFs with Bates numbers and confidentiality designation.
Reads source PDFs, writes branded artifacts to production output folder.
Branded PDFs persist on disk permanently -- they are litigation artifacts.

Queue: ediscovery, timeout: 7200s
Entry: jobs.run_production.run(production_set_id, tenant_id)
"""

import logging
import os
from pathlib import Path

from sqlalchemy import create_engine, text as sa_text

logger = logging.getLogger(__name__)

EDISCOVERY_ROOT = os.environ.get("CIFS_EDISCOVERY_MOUNT", "/mnt/ediscovery")
DATABASE_URL = os.environ.get("DATABASE_URL", "")



def generate_dat_and_companions(conn, production_set_id, tid, ps,
                                prefix, digits, suffix, conf, images_dir):
    """Generate TEXT/, NATIVES/ directories and Concordance DAT load file."""
    from modules.ediscovery.services.bates_engine import generate_dat
    import shutil

    prod_root = images_dir.parent  # _productions/{ps_id}/
    text_dir = prod_root / "TEXT"
    natives_dir = prod_root / "NATIVES"
    text_dir.mkdir(parents=True, exist_ok=True)
    natives_dir.mkdir(parents=True, exist_ok=True)

    # Check if include_natives is set
    r_ps = conn.execute(sa_text("""
        SELECT include_natives, include_text_files FROM production_sets
        WHERE id = CAST(:pid AS uuid)
    """), {"pid": production_set_id})
    ps_row = r_ps.mappings().fetchone()
    do_natives = ps_row and ps_row["include_natives"]
    do_text = True  # always include text

    # Fetch all completed production documents with full metadata
    r_docs = conn.execute(sa_text("""
        SELECT pd.begin_bates, pd.end_bates, pd.page_count, pd.produced_path,
               ed.file_name, ed.file_path, ed.file_size, ed.file_hash,
               ed.doc_type, ed.mime_type, ed.custodian,
               ed.email_from, ed.email_to, ed.email_cc, ed.email_subject,
               ed.email_date, ed.extracted_text, ed.working_path,
               ed.is_attachment, ed.parent_id,
               ec.storage_path AS col_storage
        FROM production_documents pd
        JOIN ediscovery_documents ed ON ed.id = pd.ediscovery_document_id
        LEFT JOIN ediscovery_collections ec ON ec.id = ed.collection_id
        WHERE pd.production_set_id = CAST(:pid AS uuid)
          AND pd.status = 'completed'
        ORDER BY pd.sort_order
    """), {"pid": production_set_id})
    docs = [dict(row) for row in r_docs.mappings().fetchall()]

    dat_rows = []
    for d in docs:
        bates = d["begin_bates"] or ""
        col = d.get("col_storage") or ""
        fp = d.get("file_path") or ""
        src_path = str(Path(col) / fp) if fp and col else ""

        # TEXT: write extracted text
        text_content = d.get("extracted_text") or ""
        if do_text and text_content:
            txt_name = f"{bates}.txt"
            txt_path = text_dir / txt_name
            try:
                txt_path.write_text(text_content, encoding="utf-8")
            except Exception as e:
                logger.warning("TEXT write failed for %s: %s", bates, e)

        # NATIVES: copy original file
        native_rel = ""
        if do_natives and src_path and Path(src_path).exists():
            ext = Path(src_path).suffix
            native_name = f"{bates}{ext}"
            native_dst = natives_dir / native_name
            try:
                shutil.copy2(src_path, str(native_dst))
                native_rel = f"NATIVES/{native_name}"
            except Exception as e:
                logger.warning("NATIVE copy failed for %s: %s", bates, e)

        # Build DAT row
        email_date_str = ""
        ed = d.get("email_date")
        if ed:
            try:
                email_date_str = ed.strftime("%m/%d/%Y %I:%M %p") if hasattr(ed, 'strftime') else str(ed)
            except Exception:
                email_date_str = str(ed) if ed else ""

        dat_rows.append({
            "begin_bates": bates,
            "end_bates": d.get("end_bates") or bates,
            "begin_attach": "",
            "end_attach": "",
            "custodian": d.get("custodian") or "",
            "doc_type": d.get("doc_type") or "",
            "file_name": d.get("file_name") or "",
            "file_path": f"IMAGES/{bates}.pdf",
            "file_size": d.get("file_size") or 0,
            "email_date": email_date_str,
            "email_from": d.get("email_from") or "",
            "email_to": d.get("email_to") or "",
            "email_cc": d.get("email_cc") or "",
            "email_subject": d.get("email_subject") or "",
            "file_hash": d.get("file_hash") or "",
            "text_path": f"TEXT/{bates}.txt" if text_content else "",
            "native_path": native_rel,
        })

    # Generate DAT load file
    dat_path = str(prod_root / "load_file.dat")
    ok = generate_dat(dat_rows, dat_path)
    if ok:
        logger.info("Generated load file: %s (%d rows)", dat_path, len(dat_rows))
    else:
        logger.error("Failed to generate load file")

    # Also generate an OPT file (image cross-reference)
    opt_path = str(prod_root / "load_file.opt")
    try:
        lines = []
        for d in dat_rows:
            bates = d["begin_bates"]
            lines.append(f"{bates},VOL001,IMAGES\\{bates}.pdf,Y,,,")
        Path(opt_path).write_text("\n".join(lines), encoding="utf-8")
        logger.info("Generated OPT file: %s", opt_path)
    except Exception as e:
        logger.warning("OPT generation failed: %s", e)

    conn.commit()
    logger.info("Post-stamp complete: %d TEXT files, %d NATIVE files, DAT: %s",
                sum(1 for r in dat_rows if r.get("text_path")),
                sum(1 for r in dat_rows if r.get("native_path")),
                "OK" if ok else "FAILED")


def run(production_set_id: str, tenant_id: str):
    from modules.ediscovery.services.bates_engine import (
        stamp_pdf, generate_placeholder_pdf, format_bates, get_page_count,
    )
    engine = create_engine(DATABASE_URL.replace("+asyncpg", "").replace("postgresql+asyncpg", "postgresql"))
    tid = (tenant_id or "").strip()
    with engine.connect() as conn:
        try:
            r = conn.execute(sa_text("""
                SELECT ps.id, ps.name, ps.confidentiality_designation,
                       ps.bates_counter_id, ps.matter_id,
                       bc.prefix, bc.num_digits, bc.suffix
                FROM production_sets ps
                LEFT JOIN bates_counters bc ON bc.id = ps.bates_counter_id
                WHERE ps.id = CAST(:pid AS uuid) AND trim(ps.tenant_id::text) = trim(:tid)
            """), {"pid": production_set_id, "tid": tid})
            ps = r.mappings().fetchone()
            if not ps:
                logger.error("Production set %s not found", production_set_id)
                return
            conf = ps["confidentiality_designation"] or "none"
            prefix = ps["prefix"] or ""
            digits = ps["num_digits"] or 7
            suffix = ps["suffix"] or ""
            out_dir = Path(EDISCOVERY_ROOT) / tid / "_productions" / str(ps["id"]) / "IMAGES"
            out_dir.mkdir(parents=True, exist_ok=True)
            r_docs = conn.execute(sa_text("""
                SELECT pd.id, pd.begin_bates, pd.end_bates, pd.page_count,
                       pd.production_type, pd.placeholder_id,
                       ed.file_path, ed.mime_type,
                       ec.storage_path AS col_storage
                FROM production_documents pd
                JOIN ediscovery_documents ed ON ed.id = pd.ediscovery_document_id
                LEFT JOIN ediscovery_collections ec ON ec.id = ed.collection_id
                WHERE pd.production_set_id = CAST(:pid AS uuid)
                ORDER BY pd.sort_order
            """), {"pid": production_set_id})
            docs = [dict(row) for row in r_docs.mappings().fetchall()]
            completed = 0
            errors = 0
            for i, d in enumerate(docs):
                pd_id = str(d["id"])
                begin = d["begin_bates"]
                pc = d["page_count"] or 1
                prod_type = d["production_type"] or "image"
                try:
                    num_str = begin.replace(prefix, "").replace(suffix, "")
                    start_num = int(num_str)
                except (ValueError, TypeError):
                    start_num = 1
                bates_list = [format_bates(prefix, start_num + j, digits, suffix) for j in range(pc)]
                dst_path = str(out_dir / f"{begin}.pdf")
                if prod_type == "placeholder":
                    ph_id = d.get("placeholder_id")
                    ph_title = "PRIVILEGED -- WITHHELD"
                    ph_body = ""
                    if ph_id:
                        r_ph = conn.execute(sa_text("""
                            SELECT name, body_text FROM production_placeholders
                            WHERE id = CAST(:phid AS uuid) LIMIT 1
                        """), {"phid": str(ph_id)})
                        ph_row = r_ph.fetchone()
                        if ph_row:
                            ph_title = ph_row[0]
                            ph_body = ph_row[1] or ""
                    ok = generate_placeholder_pdf(dst_path, ph_title, ph_body, begin, conf)
                else:
                    fp = d.get("file_path") or ""
                    col = d.get("col_storage") or ""
                    src_path = str(Path(col) / fp) if fp and col else (str(Path(EDISCOVERY_ROOT) / fp) if fp else "")
                    if not src_path or not Path(src_path).exists():
                        conn.execute(sa_text("""
                            UPDATE production_documents SET status = 'error',
                            error_message = 'Source file not found' WHERE id = CAST(:pdid AS uuid)
                        """), {"pdid": pd_id})
                        errors += 1
                        continue
                    mime = (d.get("mime_type") or "").lower()
                    if mime == "application/pdf":
                        ok = stamp_pdf(src_path, dst_path, bates_list, conf)
                    else:
                        fname = Path(src_path).name
                        ok = generate_placeholder_pdf(
                            dst_path, f"NATIVE FILE: {fname}",
                            f"Original format: {mime}\nSee NATIVES/ folder for original file.",
                            begin, conf)
                if ok:
                    conn.execute(sa_text("""
                        UPDATE production_documents SET status = 'completed',
                        produced_path = :pp WHERE id = CAST(:pdid AS uuid)
                    """), {"pp": dst_path, "pdid": pd_id})
                    completed += 1
                else:
                    conn.execute(sa_text("""
                        UPDATE production_documents SET status = 'error',
                        error_message = 'Stamping failed' WHERE id = CAST(:pdid AS uuid)
                    """), {"pdid": pd_id})
                    errors += 1
                if (i + 1) % 25 == 0:
                    conn.commit()
            # ── Post-stamp: generate TEXT/, NATIVES/, and load file ──────
            generate_dat_and_companions(
                conn, production_set_id, tid, ps,
                prefix, digits, suffix, conf, out_dir,
            )

            final_status = "completed" if errors == 0 else ("error" if completed == 0 else "completed")
            err_msg = f"{errors} documents failed" if errors else None
            conn.execute(sa_text("""
                UPDATE production_sets SET status = :st, error_message = :em,
                produced_at = now(), updated_at = now() WHERE id = CAST(:pid AS uuid)
            """), {"st": final_status, "em": err_msg, "pid": production_set_id})
            conn.execute(sa_text("""
                INSERT INTO production_audit_log
                    (tenant_id, production_set_id, action, performed_by, details_json, created_at)
                VALUES (:tid, CAST(:pid AS uuid), 'run_completed', 0, CAST(:det AS jsonb), now())
            """), {"tid": tid, "pid": production_set_id,
                   "det": '{"completed": ' + str(completed) + ', "errors": ' + str(errors) + '}'})
            conn.commit()
            logger.info("Production %s: %d completed, %d errors", production_set_id, completed, errors)
        except Exception as exc:
            logger.exception("run_production failed: %s", exc)
            try:
                conn.execute(sa_text("""
                    UPDATE production_sets SET status = 'error',
                    error_message = :em, updated_at = now() WHERE id = CAST(:pid AS uuid)
                """), {"em": str(exc)[:500], "pid": production_set_id})
                conn.commit()
            except Exception:
                pass
