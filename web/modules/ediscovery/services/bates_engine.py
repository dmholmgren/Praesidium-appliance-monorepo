"""
modules/ediscovery/services/bates_engine.py
============================================
Bates numbering engine for outbound productions.

Responsibilities:
  - Format Bates strings (prefix + zero-padded number + optional suffix)
  - Atomic range allocation via SELECT FOR UPDATE on bates_counters
  - PDF stamping via PyMuPDF (fitz) — writes branded PDFs to disk
  - Placeholder PDF generation for privileged/withheld documents

Architecture:
  - bates_counters is the ONLY serialization point (SELECT FOR UPDATE)
  - Bates numbers are write-once: once staged, never change
  - Branded PDFs persist on disk as litigation artifacts (not dynamically rendered)
  - save != export: stamping is a DB+filesystem event; ZIP export is a delivery event
"""

import logging
import os
from pathlib import Path

import fitz  # PyMuPDF

from sqlalchemy import text as sa_text

logger = logging.getLogger(__name__)

EDISCOVERY_ROOT = os.environ.get("CIFS_EDISCOVERY_MOUNT", "/mnt/ediscovery")


def format_bates(prefix: str, number: int, num_digits: int = 7,
                 suffix: str = "") -> str:
    return f"{prefix}{str(number).zfill(num_digits)}{suffix or ''}"


async def allocate_range_async(session, counter_id: str,
                               total_pages: int) -> tuple:
    """Atomic Bates allocation. Returns (start, end) inclusive. Caller owns txn."""
    r = await session.execute(sa_text("""
        SELECT next_number, prefix, num_digits, suffix
        FROM bates_counters WHERE id = CAST(:cid AS uuid) FOR UPDATE
    """), {"cid": counter_id})
    row = r.mappings().fetchone()
    if not row:
        raise ValueError(f"Bates counter {counter_id} not found")
    start = int(row["next_number"])
    end = start + total_pages - 1
    await session.execute(sa_text("""
        UPDATE bates_counters SET next_number = :n, updated_at = now()
        WHERE id = CAST(:cid AS uuid)
    """), {"cid": counter_id, "n": end + 1})
    return start, end


async def release_range_async(session, counter_id: str, released: int) -> None:
    await session.execute(sa_text("""
        UPDATE bates_counters SET next_number = GREATEST(1, next_number - :n),
        updated_at = now() WHERE id = CAST(:cid AS uuid)
    """), {"cid": counter_id, "n": released})


def get_page_count(path: str) -> int:
    try:
        doc = fitz.open(path)
        n = len(doc); doc.close(); return n
    except Exception:
        return 1


def stamp_pdf(src_path: str, dst_path: str, bates_numbers: list,
              confidentiality: str = "none", font_size: float = 9.0) -> bool:
    """Stamp Bates + confidentiality on each page. Writes permanent artifact to dst_path.
    Falls back to placeholder PDF if source is encrypted or corrupt."""
    try:
        Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
        doc = fitz.open(str(src_path))
        if doc.is_encrypted:
            doc.close()
            raise ValueError("document encrypted")
        conf_text = ""
        if confidentiality and confidentiality != "none":
            conf_text = confidentiality.upper().replace("_", " ")
        for i, page in enumerate(doc):
            bates = bates_numbers[i] if i < len(bates_numbers) else ""
            rect = page.rect
            if bates:
                bw = fitz.get_text_length(bates, fontname="helv", fontsize=font_size)
                page.insert_text(fitz.Point(rect.width - 36 - bw, rect.height - 36),
                                 bates, fontsize=font_size, fontname="helv", color=(0, 0, 0))
            if conf_text:
                tw = fitz.get_text_length(conf_text, fontname="helv", fontsize=font_size - 1)
                page.insert_text(fitz.Point((rect.width - tw) / 2, rect.height - 36),
                                 conf_text, fontsize=font_size - 1, fontname="helv", color=(0.5, 0, 0))
        doc.save(str(dst_path), garbage=4, deflate=True)
        doc.close()
        return True
    except Exception as exc:
        logger.warning("stamp_pdf failed for %s, generating placeholder: %s",
                       os.path.basename(src_path), exc)
        # Fallback: generate a placeholder noting the original is a native-only file
        fname = os.path.basename(src_path)
        bates = bates_numbers[0] if bates_numbers else ""
        return generate_placeholder_pdf(
            dst_path,
            f"NATIVE FILE: {fname}",
            f"This document could not be converted to PDF.\n"
            f"Original file is included in the NATIVES/ folder.\n"
            f"Reason: {str(exc)[:100]}",
            bates, confidentiality,
        )


def generate_placeholder_pdf(dst_path: str, title: str, body_text: str,
                             bates_number: str, confidentiality: str = "none") -> bool:
    try:
        Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        rect = page.rect
        tw = fitz.get_text_length(title, fontname="helv", fontsize=18)
        page.insert_text(fitz.Point((rect.width - tw) / 2, rect.height * 0.38),
                         title, fontsize=18, fontname="helv", color=(0.3, 0, 0))
        if body_text:
            for i, line in enumerate(body_text.split("\n")[:8]):
                lw = fitz.get_text_length(line, fontname="helv", fontsize=11)
                page.insert_text(fitz.Point((rect.width - lw) / 2, rect.height * 0.45 + i * 16),
                                 line, fontsize=11, fontname="helv", color=(0.3, 0.3, 0.3))
        if bates_number:
            bnw = fitz.get_text_length(bates_number, fontname="helv", fontsize=9)
            page.insert_text(fitz.Point(rect.width - 36 - bnw, rect.height - 36),
                             bates_number, fontsize=9, fontname="helv", color=(0, 0, 0))
        if confidentiality and confidentiality != "none":
            ct = confidentiality.upper().replace("_", " ")
            cw = fitz.get_text_length(ct, fontname="helv", fontsize=8)
            page.insert_text(fitz.Point((rect.width - cw) / 2, rect.height - 36),
                             ct, fontsize=8, fontname="helv", color=(0.5, 0, 0))
        doc.save(str(dst_path), garbage=4, deflate=True)
        doc.close()
        return True
    except Exception as exc:
        logger.exception("generate_placeholder_pdf failed: %s", exc)
        return False


def generate_dat(rows: list, output_path: str) -> bool:
    """Concordance DAT load file with thorn/pilcrow delimiters."""
    try:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        D, Q, NL = "\xfe", "\x14", "\xae"
        fields = ["BEGBATES", "ENDBATES", "BEGATTACH", "ENDATTACH", "CUSTODIAN",
                   "DOCTYPE", "FILENAME", "FILEPATH", "FILEEXT", "FILESIZE",
                   "DATESENT", "EMAILFROM", "EMAILTO", "EMAILCC", "EMAILSUBJECT", "MD5HASH"]
        lines = [D.join(Q + f + Q for f in fields)]
        for r in rows:
            vals = [r.get("begin_bates", ""), r.get("end_bates", ""),
                    r.get("begin_attach", ""), r.get("end_attach", ""),
                    r.get("custodian", ""), r.get("doc_type", ""),
                    r.get("file_name", ""), r.get("file_path", ""),
                    (r.get("file_name") or "").rsplit(".", 1)[-1] if r.get("file_name") else "",
                    str(r.get("file_size", "")), r.get("email_date", ""),
                    r.get("email_from", ""), r.get("email_to", ""),
                    r.get("email_cc", ""), r.get("email_subject", ""),
                    r.get("file_hash", "")]
            lines.append(D.join(Q + (v or "").replace("\n", NL) + Q for v in vals))
        Path(output_path).write_text("\n".join(lines), encoding="utf-8")
        return True
    except Exception as exc:
        logger.exception("generate_dat failed: %s", exc)
        return False
