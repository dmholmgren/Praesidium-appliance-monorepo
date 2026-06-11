"""
modules/ediscovery/services/review_widget_service.py
=====================================================
Widget data functions for the eDiscovery Review Workspace.

Scope contract: every function receives the canonical scope dict and returns
a plain dict of template context variables. Scope keys used:

    tenant_id           — always required, resolved from session
    user_id             — current user, for review_status updates
    request             — used to pull collection_id / ediscovery_doc_id
                          from query params (not part of standard scope)

Registered widget slugs (widget_registry rows seeded in 0039):

    ediscovery_doc_list           collection scope  (requires collection_id)
    ediscovery_doc_viewer         document scope    (requires ediscovery_doc_id)
    ediscovery_doc_metadata       document scope
    ediscovery_doc_tags           document scope
    ediscovery_doc_review_status  document scope
    ediscovery_doc_family         document scope

All functions follow the scope contract:
    async def get_X(scope: dict) -> dict:
        return {...template context...}
"""

import logging
import os
from datetime import datetime, date
from typing import Optional

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _qp(scope: dict, key: str) -> Optional[str]:
    """Pull a query param value from the request. Returns None if absent."""
    req = scope.get("request")
    if req is None:
        return None
    val = req.query_params.get(key)
    return val if val else None


def _fmt_date(val) -> str:
    if isinstance(val, (datetime, date)):
        return val.strftime("%Y-%m-%d")
    return str(val or "")[:10]


def _fmt_dt(val) -> str:
    if isinstance(val, (datetime, date)):
        return val.strftime("%b %d, %Y %I:%M %p")
    return str(val or "")[:16]


def _fmt_size(n) -> str:
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _shorten_hash(h: Optional[str]) -> str:
    if not h:
        return "—"
    return f"{h[:8]}…{h[-4:]}" if len(h) > 14 else h


def _badge_color_for_status(status: Optional[str]) -> str:
    """Return a semantic color hint for review_status badges."""
    s = (status or "unreviewed").lower()
    if s in ("responsive", "hot"):
        return "green"
    if s == "privileged":
        return "red"
    if s == "non_responsive":
        return "gray"
    if s == "needs_further_review":
        return "yellow"
    return "gray"  # unreviewed default


# ---------------------------------------------------------------------------
# Widget 1 — ediscovery_doc_list
#   Paginated list of documents in a collection, with filters.
#   Query params (via request.query_params):
#     collection_id  — required
#     page           — 1-based, default 1
#     per_page       — default 50, max 200
#     status_filter  — optional: 'unreviewed' | 'reviewed' | 'all'
#     privilege_filter — optional: 'privileged' | 'non_privileged' | 'all'
#     q              — optional filename/subject text search
#     selected_id    — ediscovery_doc_id currently selected (for row highlight)
# ---------------------------------------------------------------------------

async def get_doc_list(scope: dict) -> dict:
    tenant_id = (scope.get("tenant_id") or "").strip()
    collection_id = _qp(scope, "collection_id")

    if not collection_id:
        return {
            "docs": [], "total": 0, "collection": None, "page": 1,
            "per_page": 50, "total_pages": 1, "collection_id": None,
            "selected_id": None, "status_filter": "all",
            "privilege_filter": "all", "q": "",
            "error": "No collection specified",
        }

    try:
        page = int(_qp(scope, "page") or "1")
    except ValueError:
        page = 1
    page = max(1, page)

    try:
        per_page = int(_qp(scope, "per_page") or "50")
    except ValueError:
        per_page = 50
    per_page = max(1, min(200, per_page))

    status_filter = (_qp(scope, "status_filter") or "all").lower()
    privilege_filter = (_qp(scope, "privilege_filter") or "all").lower()
    q = (_qp(scope, "q") or "").strip()
    selected_id = _qp(scope, "selected_id")

    offset = (page - 1) * per_page

    # Build dynamic WHERE clauses safely with bound params
    where = [
        "trim(ed.tenant_id::text) = trim(:tid)",
        "ed.collection_id = CAST(:cid AS uuid)",
    ]
    params = {"tid": tenant_id, "cid": collection_id,
              "limit": per_page, "offset": offset}

    if status_filter == "unreviewed":
        where.append("COALESCE(ed.review_status, 'unreviewed') = 'unreviewed'")
    elif status_filter == "reviewed":
        where.append("COALESCE(ed.review_status, 'unreviewed') <> 'unreviewed'")
    # 'all' — no filter added

    if privilege_filter == "privileged":
        where.append("ed.privilege_status = 'privileged'")
    elif privilege_filter == "non_privileged":
        where.append("(ed.privilege_status IS NULL OR ed.privilege_status <> 'privileged')")

    if q:
        where.append("""(
            LOWER(COALESCE(ed.file_name, '')) LIKE '%' || LOWER(:q) || '%'
            OR LOWER(COALESCE(ed.email_subject, '')) LIKE '%' || LOWER(:q) || '%'
            OR LOWER(COALESCE(ed.custodian, '')) LIKE '%' || LOWER(:q) || '%'
        )""")
        params["q"] = q

    where_sql = " AND ".join(where)

    docs = []
    total = 0
    collection = None

    try:
        async with AsyncSessionLocal() as session:
            # Collection header info
            r_c = await session.execute(sa_text("""
                SELECT c.id, c.name, c.collection_name, c.matter_id,
                       c.total_docs, c.reviewed_docs, c.status,
                       c.source_party, c.received_date,
                       m.matter_name, m.matter_number
                FROM ediscovery_collections c
                LEFT JOIN matters m ON m.id = c.matter_id
                WHERE c.id = CAST(:cid AS uuid)
                  AND trim(c.tenant_id::text) = trim(:tid)
                LIMIT 1
            """), {"cid": collection_id, "tid": tenant_id})
            c_row = r_c.mappings().fetchone()
            if c_row:
                collection = dict(c_row)
                collection["display_name"] = (
                    collection.get("name") or collection.get("collection_name") or "Collection"
                )

            # Total count
            r_total = await session.execute(
                sa_text(f"SELECT COUNT(*) AS n FROM ediscovery_documents ed WHERE {where_sql}"),
                params,
            )
            total = int(r_total.scalar() or 0)

            # Page of rows — skip duplicates in default view (they add noise)
            r = await session.execute(sa_text(f"""
                SELECT
                    ed.id,
                    ed.file_name,
                    ed.file_size,
                    ed.mime_type,
                    ed.doc_date,
                    ed.custodian,
                    ed.email_from,
                    ed.email_subject,
                    ed.page_count,
                    ed.bates_begin,
                    ed.bates_end,
                    ed.review_status,
                    ed.privilege_status,
                    ed.is_duplicate,
                    ed.relevance_score
                FROM ediscovery_documents ed
                WHERE {where_sql}
                ORDER BY
                    COALESCE(ed.doc_date, ed.ingested_at::date) DESC NULLS LAST,
                    ed.file_name ASC
                LIMIT :limit OFFSET :offset
            """), params)
            rows = r.mappings().fetchall()

        for row in rows:
            rec = dict(row)
            name = rec.get("file_name") or rec.get("email_subject") or "(untitled)"
            display_name = name.replace("\\", "/").rsplit("/", 1)[-1]

            rec["display_name"] = display_name
            rec["display_size"] = _fmt_size(rec.get("file_size"))
            rec["display_date"] = _fmt_date(rec.get("doc_date"))
            rec["is_selected"] = (selected_id and str(rec["id"]) == str(selected_id))
            rec["status_badge_color"] = _badge_color_for_status(rec.get("review_status"))
            rec["status_display"] = (rec.get("review_status") or "unreviewed").replace("_", " ")
            rec["is_privileged"] = (rec.get("privilege_status") == "privileged")
            docs.append(rec)

        total_pages = max(1, (total + per_page - 1) // per_page)

        return {
            "docs": docs,
            "total": total,
            "collection": collection,
            "collection_id": collection_id,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "selected_id": selected_id,
            "status_filter": status_filter,
            "privilege_filter": privilege_filter,
            "q": q,
        }

    except Exception as exc:
        logger.exception("get_doc_list error: %s", exc)
        return {
            "docs": [], "total": 0, "collection": None,
            "collection_id": collection_id, "page": 1, "per_page": per_page,
            "total_pages": 1, "selected_id": selected_id,
            "status_filter": status_filter, "privilege_filter": privilege_filter,
            "q": q, "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Widget 2 — ediscovery_doc_viewer
#   Iframe PDF viewer or extracted-text fallback.
#   Query params:
#     ediscovery_doc_id — required
# ---------------------------------------------------------------------------

# Mimes that render natively in the browser via iframe
_BROWSER_NATIVE_MIMES = {
    "application/pdf",
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml",
    "text/plain", "text/html",
}

# Mimes that LibreOffice can convert to PDF
_CONVERTIBLE_MIMES_PREFIX = (
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml",
    "application/vnd.oasis.opendocument",
    "application/rtf",
    "text/csv",
    "text/rtf",
)


def _viewer_mode(mime: Optional[str], file_name: Optional[str],
                 doc_type: Optional[str] = None, has_text: bool = False) -> str:
    """Return 'pdf' | 'image' | 'text' | 'convert' | 'unsupported'.
    
    Decision chain (first match wins):
      1. PDF by mime or extension
      2. Image by mime or extension
      3. Plain text by mime or extension
      4. Office docs convertible via LibreOffice
      5. Email by mime, extension, or doc_type
      6. Fallback: if extracted_text exists, show text viewer
      7. Otherwise unsupported
    """
    m = (mime or "").lower()
    name = (file_name or "").lower()
    dtype = (doc_type or "").lower()

    if m == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    if m.startswith("image/") or any(name.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
        return "image"
    if m.startswith("text/") or any(name.endswith(ext) for ext in (".txt", ".log", ".csv", ".md")):
        return "text"
    if m.startswith(_CONVERTIBLE_MIMES_PREFIX) or any(
        name.endswith(ext) for ext in (
            ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
            ".odt", ".ods", ".odp", ".rtf",
        )
    ):
        return "convert"
    # Email: by mime, extension, or doc_type column
    if (m in ("message/rfc822", "application/vnd.ms-outlook")
            or any(name.endswith(ext) for ext in (".msg", ".eml"))
            or dtype == "email"):
        return "text"
    # Fallback: if we have extracted text, show the text viewer
    # rather than "unsupported" — covers octet-stream with parsed content
    if has_text:
        return "text"
    return "unsupported"


async def get_doc_viewer(scope: dict) -> dict:
    tenant_id = (scope.get("tenant_id") or "").strip()
    doc_id = _qp(scope, "ediscovery_doc_id")

    if not doc_id:
        return {
            "doc": None, "viewer_mode": "empty",
            "file_url": None, "text_url": None, "native_url": None,
            "has_native": False, "has_image": False, "productions": [],
            "error": None,
        }

    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT
                    ed.id, ed.file_name, ed.mime_type, ed.file_size,
                    ed.page_count, ed.working_path, ed.file_path,
                    ed.native_path, ed.text_path,
                    ed.bates_begin, ed.bates_end, ed.collection_id,
                    ed.doc_type,
                    ed.extracted_text IS NOT NULL AS has_text
                FROM ediscovery_documents ed
                WHERE ed.id = CAST(:did AS uuid)
                  AND trim(ed.tenant_id::text) = trim(:tid)
                LIMIT 1
            """), {"did": doc_id, "tid": tenant_id})
            row = r.mappings().fetchone()

            # Productions this document appears in
            productions = []
            if row:
                pr = await session.execute(sa_text("""
                    SELECT ep.production_name, epd.bates_number,
                           epd.output_path, ep.id::text as production_id
                    FROM ediscovery_production_documents epd
                    JOIN ediscovery_productions ep ON ep.id = epd.production_id
                    WHERE epd.document_id = CAST(:did AS uuid)
                    ORDER BY ep.created_at DESC
                """), {"did": doc_id})
                productions = [dict(r) for r in pr.mappings().fetchall()]

        if not row:
            return {"doc": None, "viewer_mode": "empty",
                    "file_url": None, "text_url": None, "native_url": None,
                    "has_native": False, "has_image": False, "productions": [],
                    "error": "Document not found"}

        rec = dict(row)

        # Determine native file type for viewer mode
        native_path = rec.get("native_path") or ""
        native_ext = native_path.rsplit(".", 1)[-1].lower() if native_path else ""
        AUDIO_EXTS = {"mp3", "m4a", "m4r", "wav", "ogg", "aac", "flac", "wma"}

        # Default mode: show native audio if available, else PDF image
        if native_ext in AUDIO_EXTS:
            mode = "audio"
        else:
            mode = _viewer_mode(rec.get("mime_type"), rec.get("file_name"), doc_type=rec.get("doc_type"), has_text=bool(rec.get("has_text")))

        file_url   = f"/ediscovery/documents/{rec['id']}/file"
        text_url   = f"/ediscovery/documents/{rec['id']}/text"
        native_url = f"/ediscovery/documents/{rec['id']}/native" if native_path else None

        return {
            "doc":          rec,
            "viewer_mode":  mode,
            "file_url":     file_url,
            "text_url":     text_url,
            "native_url":   native_url,
            "has_native":   bool(native_path),
            "has_image":    bool(rec.get("file_path")),
            "native_ext":   native_ext,
            "productions":  productions,
            "display_name": (rec.get("file_name") or "(untitled)").replace("\\", "/").rsplit("/", 1)[-1],
        }

    except Exception as exc:
        logger.exception("get_doc_viewer error: %s", exc)
        return {"doc": None, "viewer_mode": "error",
                "file_url": None, "text_url": None, "native_url": None,
                "has_native": False, "has_image": False, "productions": [],
                "error": str(exc)}


# ---------------------------------------------------------------------------
# Widget 3 — ediscovery_doc_metadata
#   Bates, custodian, dates, hash, file info, email headers if applicable.
# ---------------------------------------------------------------------------

async def get_doc_metadata(scope: dict) -> dict:
    tenant_id = (scope.get("tenant_id") or "").strip()
    doc_id = _qp(scope, "ediscovery_doc_id")

    if not doc_id:
        return {"doc": None}

    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT
                    ed.id, ed.file_name, ed.file_size, ed.mime_type,
                    ed.file_hash, ed.doc_hash, ed.page_count,
                    ed.doc_date, ed.ingested_at, ed.doc_type,
                    ed.custodian, ed.bates_begin, ed.bates_end,
                    ed.original_path, ed.working_path, ed.produced_in,
                    ed.email_from, ed.email_to, ed.email_cc,
                    ed.email_subject, ed.email_date, ed.email_message_id,
                    ed.email_thread_id,
                    ed.is_duplicate, ed.is_near_duplicate, ed.near_dupe_score,
                    ed.relevance_score, ed.review_tier
                FROM ediscovery_documents ed
                WHERE ed.id = CAST(:did AS uuid)
                  AND trim(ed.tenant_id::text) = trim(:tid)
                LIMIT 1
            """), {"did": doc_id, "tid": tenant_id})
            row = r.mappings().fetchone()

        if not row:
            return {"doc": None}

        rec = dict(row)
        # Group into sections for rendering
        is_email = bool(rec.get("email_subject") or rec.get("email_from"))

        file_info = [
            ("Filename", rec.get("file_name") or "—"),
            ("Size", _fmt_size(rec.get("file_size"))),
            ("MIME type", rec.get("mime_type") or "—"),
            ("Pages", str(rec["page_count"]) if rec.get("page_count") else "—"),
            ("Doc type", rec.get("doc_type") or "—"),
            ("Custodian", rec.get("custodian") or "—"),
            ("Doc date", _fmt_date(rec.get("doc_date"))),
            ("Ingested", _fmt_dt(rec.get("ingested_at"))),
        ]

        bates_info = [
            ("Bates range",
                f"{rec.get('bates_begin') or '—'} – {rec.get('bates_end') or '—'}"
                if (rec.get("bates_begin") or rec.get("bates_end")) else "—"),
            ("Produced in", rec.get("produced_in") or "—"),
        ]

        hash_info = [
            ("File hash (SHA-256)", _shorten_hash(rec.get("file_hash"))),
            ("Doc hash", _shorten_hash(rec.get("doc_hash"))),
        ]

        email_info = []
        if is_email:
            email_info = [
                ("From", rec.get("email_from") or "—"),
                ("To", rec.get("email_to") or "—"),
                ("Cc", rec.get("email_cc") or "—"),
                ("Subject", rec.get("email_subject") or "—"),
                ("Sent", _fmt_dt(rec.get("email_date"))),
                ("Message-ID", rec.get("email_message_id") or "—"),
                ("Thread ID", rec.get("email_thread_id") or "—"),
            ]

        dup_info = []
        if rec.get("is_duplicate"):
            dup_info.append(("Duplicate", "Yes (exact)"))
        elif rec.get("is_near_duplicate"):
            score = rec.get("near_dupe_score")
            dup_info.append(("Near-duplicate",
                             f"Yes (score {score:.3f})" if score else "Yes"))

        return {
            "doc": rec,
            "is_email": is_email,
            "file_info": file_info,
            "bates_info": bates_info,
            "hash_info": hash_info,
            "email_info": email_info,
            "dup_info": dup_info,
            "file_hash_full": rec.get("file_hash") or "",
        }

    except Exception as exc:
        logger.exception("get_doc_metadata error: %s", exc)
        return {"doc": None, "error": str(exc)}


# ---------------------------------------------------------------------------
# Widget 4 — ediscovery_doc_tags
#   Tag chips + apply/remove. Lists available collection tags and which
#   are currently applied to the document.
# ---------------------------------------------------------------------------

async def get_doc_tags(scope: dict) -> dict:
    tenant_id = (scope.get("tenant_id") or "").strip()
    doc_id = _qp(scope, "ediscovery_doc_id")

    if not doc_id:
        return {"doc_id": None, "applied_tags": [], "available_tags": []}

    try:
        async with AsyncSessionLocal() as session:
            # Find the collection this document belongs to
            r_col = await session.execute(sa_text("""
                SELECT ed.collection_id
                FROM ediscovery_documents ed
                WHERE ed.id = CAST(:did AS uuid)
                  AND trim(ed.tenant_id::text) = trim(:tid)
                LIMIT 1
            """), {"did": doc_id, "tid": tenant_id})
            col_row = r_col.mappings().fetchone()
            if not col_row:
                return {"doc_id": doc_id, "applied_tags": [],
                        "available_tags": [], "error": "Document not found"}

            collection_id = str(col_row["collection_id"])

            # All tags in scope: collection-scoped tags + tenant-global tags
            r_avail = await session.execute(sa_text("""
                SELECT t.id, t.name, t.category, t.color, t.description,
                       t.is_system, t.collection_id
                FROM tags t
                WHERE trim(t.tenant_id::text) = trim(:tid)
                  AND (t.collection_id IS NULL
                       OR t.collection_id = CAST(:cid AS uuid))
                ORDER BY
                    CASE WHEN t.is_system THEN 0 ELSE 1 END,
                    COALESCE(t.category, 'zzz'),
                    t.name
            """), {"tid": tenant_id, "cid": collection_id})
            avail_rows = r_avail.mappings().fetchall()

            # Applied tags for this doc
            r_app = await session.execute(sa_text("""
                SELECT dt.tag_id, dt.source, dt.confidence, dt.applied_at,
                       t.name, t.category, t.color
                FROM document_tags dt
                JOIN tags t ON t.id = dt.tag_id
                WHERE dt.document_id = CAST(:did AS uuid)
                  AND trim(dt.tenant_id::text) = trim(:tid)
                ORDER BY dt.applied_at DESC
            """), {"did": doc_id, "tid": tenant_id})
            app_rows = r_app.mappings().fetchall()

        applied_ids = {str(r["tag_id"]) for r in app_rows}
        available_tags = []
        for row in avail_rows:
            rec = dict(row)
            rec["is_applied"] = str(rec["id"]) in applied_ids
            available_tags.append(rec)

        applied_tags = [dict(r) for r in app_rows]

        return {
            "doc_id": doc_id,
            "collection_id": collection_id,
            "applied_tags": applied_tags,
            "available_tags": available_tags,
            "applied_count": len(applied_tags),
        }

    except Exception as exc:
        logger.exception("get_doc_tags error: %s", exc)
        return {"doc_id": doc_id, "applied_tags": [],
                "available_tags": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# Widget 5 — ediscovery_doc_review_status
#   Review status radios + reviewer/timestamp display.
# ---------------------------------------------------------------------------

_REVIEW_STATUSES = [
    ("unreviewed", "Unreviewed", "gray"),
    ("responsive", "Responsive", "green"),
    ("non_responsive", "Non-Responsive", "gray"),
    ("needs_further_review", "Needs Further Review", "yellow"),
    ("hot", "Hot", "red"),
]

_PRIVILEGE_STATUSES = [
    ("none", "Not Privileged", "gray"),
    ("privileged", "Privileged", "red"),
    ("attorney_work_product", "Attorney Work Product", "red"),
    ("redact", "Needs Redaction", "yellow"),
]


async def get_doc_review_status(scope: dict) -> dict:
    tenant_id = (scope.get("tenant_id") or "").strip()
    doc_id = _qp(scope, "ediscovery_doc_id")

    if not doc_id:
        return {"doc_id": None, "statuses": _REVIEW_STATUSES,
                "privilege_statuses": _PRIVILEGE_STATUSES,
                "current_status": "unreviewed", "current_privilege": "none"}

    try:
        async with AsyncSessionLocal() as session:
            # reviewed_by is uuid on ediscovery_documents, but users.id is bigint.
            # The join fails for purely-integer reviewed_by values when the
            # existing data is inconsistent. We look up via a best-effort
            # cast that gracefully returns no reviewer if the join can't resolve.
            r = await session.execute(sa_text("""
                SELECT ed.review_status, ed.privilege_status,
                       ed.reviewed_at, ed.reviewed_by, ed.coding_notes,
                       u.email AS reviewer_email,
                       u.full_name AS reviewer_full_name
                FROM ediscovery_documents ed
                LEFT JOIN users u
                    ON ed.reviewed_by::text = u.id::text
                WHERE ed.id = CAST(:did AS uuid)
                  AND trim(ed.tenant_id::text) = trim(:tid)
                LIMIT 1
            """), {"did": doc_id, "tid": tenant_id})
            row = r.mappings().fetchone()

        if not row:
            return {"doc_id": doc_id, "statuses": _REVIEW_STATUSES,
                    "privilege_statuses": _PRIVILEGE_STATUSES,
                    "current_status": "unreviewed", "current_privilege": "none",
                    "error": "Document not found"}

        rec = dict(row)
        current = (rec.get("review_status") or "unreviewed").lower()
        current_priv = (rec.get("privilege_status") or "none").lower()

        reviewer_name = rec.get("reviewer_full_name") or rec.get("reviewer_email")

        return {
            "doc_id": doc_id,
            "statuses": _REVIEW_STATUSES,
            "privilege_statuses": _PRIVILEGE_STATUSES,
            "current_status": current,
            "current_privilege": current_priv,
            "reviewed_at": _fmt_dt(rec.get("reviewed_at")),
            "reviewer_name": reviewer_name,
            "coding_notes": rec.get("coding_notes") or "",
        }

    except Exception as exc:
        logger.exception("get_doc_review_status error: %s", exc)
        return {"doc_id": doc_id, "statuses": _REVIEW_STATUSES,
                "privilege_statuses": _PRIVILEGE_STATUSES,
                "current_status": "unreviewed", "current_privilege": "none",
                "error": str(exc)}


# ---------------------------------------------------------------------------
# Widget 6 — ediscovery_doc_family
#   Parent/children/related docs. For v1 we implement what the schema
#   actually supports:
#     - Email thread siblings (same email_thread_id)
#     - Exact duplicates (same file_hash)
#   Parent/attachment relationships require family_id column (post-Sunday).
# ---------------------------------------------------------------------------

async def get_doc_family(scope: dict) -> dict:
    tenant_id = (scope.get("tenant_id") or "").strip()
    doc_id = _qp(scope, "ediscovery_doc_id")

    if not doc_id:
        return {"doc_id": None, "thread_members": [], "duplicates": [],
                "has_family": False}

    try:
        async with AsyncSessionLocal() as session:
            # Get this doc's identifying fields
            r_self = await session.execute(sa_text("""
                SELECT ed.email_thread_id, ed.file_hash, ed.collection_id
                FROM ediscovery_documents ed
                WHERE ed.id = CAST(:did AS uuid)
                  AND trim(ed.tenant_id::text) = trim(:tid)
                LIMIT 1
            """), {"did": doc_id, "tid": tenant_id})
            self_row = r_self.mappings().fetchone()
            if not self_row:
                return {"doc_id": doc_id, "thread_members": [],
                        "duplicates": [], "has_family": False,
                        "error": "Document not found"}

            thread_id = self_row.get("email_thread_id")
            file_hash = self_row.get("file_hash")

            thread_members = []
            if thread_id:
                r_t = await session.execute(sa_text("""
                    SELECT id, file_name, email_subject, email_from,
                           email_date, doc_date, bates_begin,
                           review_status
                    FROM ediscovery_documents
                    WHERE email_thread_id = :thread
                      AND id <> CAST(:did AS uuid)
                      AND trim(tenant_id::text) = trim(:tid)
                    ORDER BY COALESCE(email_date, doc_date) ASC
                    LIMIT 50
                """), {"thread": thread_id, "did": doc_id, "tid": tenant_id})
                thread_members = [dict(r) for r in r_t.mappings().fetchall()]

            duplicates = []
            if file_hash:
                r_d = await session.execute(sa_text("""
                    SELECT id, file_name, custodian, doc_date,
                           bates_begin, collection_id
                    FROM ediscovery_documents
                    WHERE file_hash = :fhash
                      AND id <> CAST(:did AS uuid)
                      AND trim(tenant_id::text) = trim(:tid)
                    ORDER BY doc_date DESC NULLS LAST
                    LIMIT 20
                """), {"fhash": file_hash, "did": doc_id, "tid": tenant_id})
                duplicates = [dict(r) for r in r_d.mappings().fetchall()]

        # Format for display
        for m in thread_members:
            m["display_date"] = _fmt_date(m.get("email_date") or m.get("doc_date"))
            m["display_name"] = (m.get("email_subject") or m.get("file_name")
                                 or "(untitled)")
        for d in duplicates:
            d["display_date"] = _fmt_date(d.get("doc_date"))
            d["display_name"] = (d.get("file_name") or "(untitled)").replace(
                "\\", "/").rsplit("/", 1)[-1]

        return {
            "doc_id": doc_id,
            "thread_members": thread_members,
            "duplicates": duplicates,
            "has_family": bool(thread_members or duplicates),
            "thread_count": len(thread_members),
            "duplicate_count": len(duplicates),
        }

    except Exception as exc:
        logger.exception("get_doc_family error: %s", exc)
        return {"doc_id": doc_id, "thread_members": [],
                "duplicates": [], "has_family": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tag seeding helper — called on first entry to a collection's review workspace.
# Idempotent. Invoked from routes/review.py.
# ---------------------------------------------------------------------------

DEFAULT_COLLECTION_TAGS = [
    # (name, category, color, description, is_system)
    ("Responsive", "responsiveness", "#10B981",
     "Document is responsive to the request for production", True),
    ("Non-Responsive", "responsiveness", "#6B7280",
     "Document is not responsive", True),
    ("Privileged", "privilege", "#EF4444",
     "Attorney-client privilege applies", True),
    ("Attorney Work Product", "privilege", "#DC2626",
     "Attorney work product doctrine applies", True),
    ("Needs Further Review", "workflow", "#F59E0B",
     "Requires a second-pass review", True),
    ("Hot", "significance", "#DC2626",
     "High-value document of particular significance", True),
    ("Confidential", "designation", "#7C3AED",
     "Confidential under the protective order", True),
    ("Redact", "workflow", "#F59E0B",
     "Requires redaction prior to production", True),
]


async def seed_default_tags_for_collection(tenant_id: str,
                                           collection_id: str,
                                           user_id: Optional[int]) -> int:
    """
    Seed the default tag set for a collection. Idempotent — uses unique
    lookup on (tenant_id, collection_id, name) before inserting. Returns
    the number of tags actually inserted.
    """
    tenant_id = (tenant_id or "").strip()
    inserted = 0
    try:
        async with AsyncSessionLocal() as session:
            for name, category, color, desc, is_system in DEFAULT_COLLECTION_TAGS:
                r = await session.execute(sa_text("""
                    SELECT 1 FROM tags
                    WHERE trim(tenant_id::text) = trim(:tid)
                      AND collection_id = CAST(:cid AS uuid)
                      AND name = :name
                    LIMIT 1
                """), {"tid": tenant_id, "cid": collection_id, "name": name})
                if r.fetchone():
                    continue

                # tags.created_by is uuid while users.id is bigint — a known
                # schema inconsistency. For system-seeded default tags we
                # write NULL (no specific user created them).
                await session.execute(sa_text("""
                    INSERT INTO tags
                        (tenant_id, collection_id, name, category, color,
                         description, is_system, created_by, created_at)
                    VALUES
                        (:tid, CAST(:cid AS uuid), :name, :cat, :color,
                         :desc, :sys, NULL, now())
                """), {
                    "tid": tenant_id, "cid": collection_id, "name": name,
                    "cat": category, "color": color, "desc": desc,
                    "sys": is_system,
                })
                inserted += 1
            await session.commit()
        return inserted
    except Exception as exc:
        logger.exception("seed_default_tags_for_collection error: %s", exc)
        return 0
