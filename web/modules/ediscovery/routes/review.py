"""
modules/ediscovery/routes/review.py
=====================================
eDiscovery Review Workspace routes.

Registered routes:
    GET  /ediscovery/collections/{collection_id}/review
         The three-pane workspace shell. Loads layout_registry['ediscovery_review']
         and seeds default tags on first open (idempotent).

    GET  /ediscovery/documents/{doc_id}/panes
         Multi-widget HTMX response — viewer + right-rail widgets in one swap.
         Used when a row is clicked in the doc list.

    GET  /ediscovery/documents/{doc_id}/file
         Streams the document file from /mnt/ediscovery. Handles:
           - Native browser mimes (PDF, images, text) — inline
           - Office docs — converts to PDF via LibreOffice on demand, cached
             at /mnt/ediscovery/{tenant_id}/_rendered/{hash[:2]}/{hash}.pdf
           - ?download=1 query param forces attachment disposition

    GET  /ediscovery/documents/{doc_id}/text
         Returns extracted_text as plain text. Used by text-fallback viewer.

    POST /ediscovery/documents/{doc_id}/tags
         Apply or remove a tag. Body: tag_id, action='apply'|'remove'.
         Returns the re-rendered tags widget body (HTMX swap).

    POST /ediscovery/documents/{doc_id}/review-status
         Update review_status and/or privilege_status.
         Returns the re-rendered review_status widget body.

    POST /ediscovery/collections/{collection_id}/tags
         Create a new collection-scoped tag. Optional apply_to_doc_id
         immediately applies it to the given document.
         Returns the re-rendered tags widget body.

Implementation notes:
    - All file paths resolved under /mnt/ediscovery (CIFS_EDISCOVERY_MOUNT).
    - NO references to FBRG-01 / CIFS_URL — reads are local to the WEB-01
      container via bind-mounted CIFS.
    - LibreOffice conversion is synchronous in v1 (acceptable for demo).
      Background-pass RQ enqueue scoped for a later build.
"""

import asyncio
import re
import hashlib
import logging
import mimetypes
import os
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.ediscovery.services.review_widget_service import (
    seed_default_tags_for_collection,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Email body extraction helper for raw .eml files
# ---------------------------------------------------------------------------
import email as _email_mod
import email.policy as _email_policy

def _extract_email_body(raw_bytes: bytes) -> str:
    """Parse a raw .eml file and return the best text representation."""
    try:
        msg = _email_mod.message_from_bytes(raw_bytes, policy=_email_policy.default)
    except Exception:
        return raw_bytes.decode("utf-8", errors="replace")
    
    # Build header prefix
    headers = []
    subj = msg.get("Subject", "")
    if subj:
        headers.append(f"Subject: {subj}")
    
    # Get body - prefer HTML, fall back to plain text
    html_body = ""
    text_body = ""
    
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html" and not html_body:
                try:
                    html_body = part.get_content()
                except Exception:
                    try:
                        html_body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                    except Exception:
                        pass
            elif ct == "text/plain" and not text_body:
                try:
                    text_body = part.get_content()
                except Exception:
                    try:
                        text_body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                    except Exception:
                        pass
    else:
        ct = msg.get_content_type()
        try:
            body = msg.get_content()
        except Exception:
            try:
                body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
            except Exception:
                body = ""
        if ct == "text/html":
            html_body = body
        else:
            text_body = body
    
    # Return with header prefix
    prefix = "\n".join(headers) + "\n\n" if headers else ""
    if html_body:
        return prefix + html_body
    if text_body:
        return prefix + text_body
    return prefix + "(no body content)"



router = APIRouter(tags=["ediscovery-review"])

# ---------------------------------------------------------------------------
# Filesystem roots
# ---------------------------------------------------------------------------

EDISCOVERY_ROOT = os.environ.get("CIFS_EDISCOVERY_MOUNT", "/mnt/ediscovery")
RENDERED_CACHE_DIRNAME = "_rendered"
SOFFICE_BIN = os.environ.get("SOFFICE_BIN", "soffice")
SOFFICE_TIMEOUT_SECS = int(os.environ.get("SOFFICE_TIMEOUT_SECS", "90"))


# ---------------------------------------------------------------------------
# Template env — reuse the widget template dirs (widget router sets these,
# but this router needs its own Jinja2 Environment for the workspace shell
# and for re-rendering widget partials on POST responses).
# ---------------------------------------------------------------------------

_TEMPLATE_DIRS = [
    "/app/modules/ediscovery/templates",
    "/app/modules/widgets/templates",
    "/app/modules/dashboard/templates",
    "/app/core/templates",
]

_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIRS),
    autoescape=select_autoescape(["html"]),
)


def _render(template_name: str, context: dict) -> str:
    try:
        tmpl = _env.get_template(template_name)
        return tmpl.render(context)
    except Exception as exc:
        logger.exception("Template render error [%s]: %s", template_name, exc)
        return (
            f'<div style="padding:10px 12px;background:#fef2f2;border:1px solid #fecaca;'
            f'border-radius:6px;font-size:11px;color:#991b1b;">'
            f'⚠ Render error: {template_name}</div>'
        )


def _tenant(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


def _user_id(request: Request) -> Optional[int]:
    current_user = getattr(request.state, "current_user", None)
    return getattr(current_user, "id", None) if current_user else None


# ---------------------------------------------------------------------------
# Helpers for re-rendering widgets after POST
# Each POST returns the inner body of the updated widget so HTMX swaps it
# in place. Simpler than full widget re-render through the generic router.
# ---------------------------------------------------------------------------

async def _render_tags_body(request: Request, doc_id: str) -> str:
    from modules.ediscovery.services.review_widget_service import get_doc_tags
    scope = {
        "tenant_id": _tenant(request),
        "user_id": _user_id(request),
        "request": _FakeRequest(request, {"ediscovery_doc_id": doc_id}),
    }
    data = await get_doc_tags(scope)
    # Render just the body fragment by delegating to full template, then
    # extracting the body. Simpler: render full widget, HTMX swap target
    # is the .edr-card-body so we only need the inner HTML. We use the
    # full template and post-process — but actually simpler: render a
    # body-only mini template inline.
    return _render_tags_body_inline(doc_id, data)


def _render_tags_body_inline(doc_id: str, data: dict) -> str:
    """Minimal re-render of just the tags card body (what HTMX swaps)."""
    # We call the full widget template, which wraps with .edr-card shell,
    # and return only the body subtree via a focused template string.
    # For simplicity and consistency, we render the full widget and let
    # HTMX target .edr-card-body — but the POST responses target
    # closest .edr-card-body with hx-swap="innerHTML", so we need to
    # return ONLY the body contents.
    body_tmpl = _env.from_string(TAGS_BODY_ONLY)
    return body_tmpl.render(data)


async def _render_review_status_body(request: Request, doc_id: str) -> str:
    from modules.ediscovery.services.review_widget_service import get_doc_review_status
    scope = {
        "tenant_id": _tenant(request),
        "user_id": _user_id(request),
        "request": _FakeRequest(request, {"ediscovery_doc_id": doc_id}),
    }
    data = await get_doc_review_status(scope)
    body_tmpl = _env.from_string(REVIEW_STATUS_BODY_ONLY)
    return body_tmpl.render(data)


class _FakeRequest:
    """
    Wraps a FastAPI Request but overrides query_params so service functions
    pulling params via scope['request'].query_params see what we pass in,
    without mutating the real request.
    """
    class _QP:
        def __init__(self, d): self._d = d
        def get(self, k, default=None): return self._d.get(k, default)
        def items(self): return self._d.items()

    def __init__(self, real_request: Request, overrides: dict):
        self._real = real_request
        self._qp = self._QP(dict(overrides))

    @property
    def query_params(self): return self._qp

    @property
    def state(self): return self._real.state


# ---------------------------------------------------------------------------
# Body-only templates (used for HTMX swap-innerHTML responses on POST)
# These match the structure inside .edr-card-body in the full widget
# templates. Keeping them co-located ensures they stay in sync — if you
# edit ediscovery_doc_tags.html, also edit TAGS_BODY_ONLY below.
# ---------------------------------------------------------------------------

TAGS_BODY_ONLY = """\
{% if error %}
<div style="font-size:11px; color:#991b1b; padding:6px 8px;
            background:#fef2f2; border-radius:4px;">
  ⚠ {{ error }}
</div>
{% elif not doc_id %}
<div style="font-size:11px; color:var(--muted,#64748b);">
  No document selected.
</div>
{% else %}
  {% if applied_tags %}
  <div style="display:flex; flex-wrap:wrap; gap:4px; margin-bottom:10px;">
    {% for t in applied_tags %}
    <span class="edr-tag-chip"
          data-tag-id="{{ t.tag_id }}"
          style="display:inline-flex; align-items:center; gap:4px;
                 padding:2px 4px 2px 8px; border-radius:10px;
                 font-size:10px; font-weight:600;
                 background:{{ t.color or '#6B7280' }}; color:#fff;">
      {{ t.name }}
      <button
        hx-post="/ediscovery/documents/{{ doc_id }}/tags"
        hx-vals='{"tag_id": "{{ t.tag_id }}", "action": "remove"}'
        hx-target="closest .edr-card-body"
        hx-swap="innerHTML"
        title="Remove tag"
        style="background:rgba(255,255,255,0.2); color:#fff;
               border:0; border-radius:50%; width:14px; height:14px;
               font-size:9px; line-height:1; cursor:pointer;
               display:inline-flex; align-items:center; justify-content:center;
               padding:0;">×</button>
    </span>
    {% endfor %}
  </div>
  {% endif %}

  {% if available_tags %}
  <div style="font-size:10px; font-weight:600; color:var(--muted,#64748b);
              text-transform:uppercase; letter-spacing:0.04em;
              margin-bottom:6px;">
    Apply tag
  </div>
  <div style="display:flex; flex-wrap:wrap; gap:4px;">
    {% for t in available_tags %}
    {% if not t.is_applied %}
    <button class="edr-tag-apply"
            hx-post="/ediscovery/documents/{{ doc_id }}/tags"
            hx-vals='{"tag_id": "{{ t.id }}", "action": "apply"}'
            hx-target="closest .edr-card-body"
            hx-swap="innerHTML"
            title="{{ t.description or '' }}"
            style="padding:2px 8px; border-radius:10px; font-size:10px;
                   font-weight:600; cursor:pointer;
                   border:1px solid {{ t.color or '#6B7280' }};
                   color:{{ t.color or '#374151' }};
                   background:var(--surface,#fff);
                   white-space:nowrap;">
      + {{ t.name }}
    </button>
    {% endif %}
    {% endfor %}
  </div>
  {% else %}
  <div style="font-size:11px; color:var(--muted,#64748b);">
    No tags available for this collection.
  </div>
  {% endif %}

  <div style="margin-top:10px; padding-top:8px;
              border-top:1px solid var(--border-color,#f1f5f9);">
    <details style="font-size:11px;">
      <summary style="cursor:pointer; color:var(--primary,#2563eb);
                      font-size:10px; font-weight:600;">
        + Create new tag
      </summary>
      <div style="margin-top:6px; display:flex; gap:4px;">
        <input type="text"
               id="edr-new-tag-name-{{ doc_id }}"
               placeholder="Tag name"
               style="flex:1; padding:4px 6px; font-size:11px;
                      border:1px solid var(--border-color,#e2e8f0);
                      border-radius:4px; box-sizing:border-box;">
        <button
          hx-post="/ediscovery/collections/{{ collection_id }}/tags"
          hx-include="#edr-new-tag-name-{{ doc_id }}"
          hx-vals='js:{
            name: document.getElementById("edr-new-tag-name-{{ doc_id }}").value,
            apply_to_doc_id: "{{ doc_id }}"
          }'
          hx-target="closest .edr-card-body"
          hx-swap="innerHTML"
          style="padding:4px 10px; font-size:11px; border-radius:4px;
                 border:0; background:var(--primary,#2563eb); color:#fff;
                 cursor:pointer; font-weight:500;">
          Add
        </button>
      </div>
    </details>
  </div>
{% endif %}
"""


REVIEW_STATUS_BODY_ONLY = """\
{% if error %}
<div style="font-size:11px; color:#991b1b; padding:6px 8px;
            background:#fef2f2; border-radius:4px;">
  ⚠ {{ error }}
</div>
{% elif not doc_id %}
<div style="font-size:11px; color:var(--muted,#64748b);">
  No document selected.
</div>
{% else %}
<div style="font-size:10px; font-weight:600; color:var(--muted,#64748b);
            text-transform:uppercase; letter-spacing:0.04em;
            margin-bottom:6px;">
  Responsiveness
</div>
<div style="display:flex; flex-direction:column; gap:2px; margin-bottom:12px;">
  {% for val, label, color in statuses %}
  <button
    hx-post="/ediscovery/documents/{{ doc_id }}/review-status"
    hx-vals='{"review_status": "{{ val }}"}'
    hx-target="closest .edr-card-body"
    hx-swap="innerHTML"
    style="display:flex; align-items:center; gap:8px;
           padding:5px 8px; border-radius:4px; cursor:pointer;
           border:1px solid {% if current_status == val %}var(--primary,#2563eb){% else %}var(--border-color,#e2e8f0){% endif %};
           background:{% if current_status == val %}#EFF6FF{% else %}var(--surface,#fff){% endif %};
           font-size:11px; text-align:left; color:var(--text,#1a202c);">
    <span style="display:inline-block; width:9px; height:9px; border-radius:50%;
                 {% if color == 'green' %}background:#10B981;
                 {% elif color == 'red' %}background:#EF4444;
                 {% elif color == 'yellow' %}background:#F59E0B;
                 {% else %}background:#9CA3AF;{% endif %}"></span>
    <span style="flex:1;">{{ label }}</span>
    {% if current_status == val %}
    <span style="font-size:10px; color:var(--primary,#2563eb); font-weight:700;">✓</span>
    {% endif %}
  </button>
  {% endfor %}
</div>

<div style="font-size:10px; font-weight:600; color:var(--muted,#64748b);
            text-transform:uppercase; letter-spacing:0.04em;
            margin-bottom:6px;">
  Privilege
</div>
<div style="display:flex; flex-direction:column; gap:2px; margin-bottom:10px;">
  {% for val, label, color in privilege_statuses %}
  <button
    hx-post="/ediscovery/documents/{{ doc_id }}/review-status"
    hx-vals='{"privilege_status": "{{ val }}"}'
    hx-target="closest .edr-card-body"
    hx-swap="innerHTML"
    style="display:flex; align-items:center; gap:8px;
           padding:5px 8px; border-radius:4px; cursor:pointer;
           border:1px solid {% if current_privilege == val %}var(--primary,#2563eb){% else %}var(--border-color,#e2e8f0){% endif %};
           background:{% if current_privilege == val %}#EFF6FF{% else %}var(--surface,#fff){% endif %};
           font-size:11px; text-align:left; color:var(--text,#1a202c);">
    <span style="display:inline-block; width:9px; height:9px; border-radius:50%;
                 {% if color == 'red' %}background:#EF4444;
                 {% elif color == 'yellow' %}background:#F59E0B;
                 {% else %}background:#9CA3AF;{% endif %}"></span>
    <span style="flex:1;">{{ label }}</span>
    {% if current_privilege == val %}
    <span style="font-size:10px; color:var(--primary,#2563eb); font-weight:700;">✓</span>
    {% endif %}
  </button>
  {% endfor %}
</div>

{% if reviewer_name or reviewed_at %}
<div style="font-size:10px; color:var(--muted,#64748b); padding-top:8px;
            border-top:1px solid var(--border-color,#f1f5f9);">
  {% if reviewer_name %}Reviewed by <strong style="color:var(--text,#1a202c);">{{ reviewer_name }}</strong>{% endif %}
  {% if reviewed_at and reviewed_at != '' %}
    {% if reviewer_name %}<br>{% endif %}
    {{ reviewed_at }}
  {% endif %}
</div>
{% endif %}
{% endif %}
"""


# ---------------------------------------------------------------------------
# GET /ediscovery/collections/{collection_id}/review
#   Three-pane workspace shell. Seeds default tags on first open.
# ---------------------------------------------------------------------------

@router.get("/ediscovery/collections/{collection_id}/review",
            response_class=HTMLResponse)
async def review_workspace(request: Request, collection_id: str):
    tenant_id = _tenant(request)
    user_id = _user_id(request)

    if not tenant_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Resolve collection + matter for header context
    collection = None
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT c.id, c.name, c.collection_name, c.matter_id,
                       c.total_docs, c.reviewed_docs, c.status,
                       m.matter_name, m.matter_number
                FROM ediscovery_collections c
                LEFT JOIN matters m ON m.id = c.matter_id
                WHERE c.id = CAST(:cid AS uuid)
                  AND trim(c.tenant_id::text) = trim(:tid)
                LIMIT 1
            """), {"cid": collection_id, "tid": tenant_id})
            row = r.mappings().fetchone()
            if row:
                collection = dict(row)
                collection["display_name"] = (
                    collection.get("name") or collection.get("collection_name") or "Collection"
                )
    except Exception as exc:
        logger.exception("review_workspace collection lookup: %s", exc)

    if not collection:
        return HTMLResponse(_render("base.html", {
            "brand": getattr(request.state, "branding", None),
            "content_body": (
                '<div style="padding:40px; text-align:center; color:#991b1b;">'
                'Collection not found or access denied.</div>'
            ),
        }), status_code=404)

    # Seed default tags (idempotent — returns 0 if already seeded)
    try:
        seeded = await seed_default_tags_for_collection(tenant_id, collection_id, user_id)
        if seeded:
            logger.info("Seeded %d default tags for collection %s", seeded, collection_id)
    except Exception as exc:
        logger.warning("Tag seed failed (non-fatal): %s", exc)

    brand = getattr(request.state, "branding", None)
    user = getattr(request.state, "current_user", None)

    # Use TemplateResponse (not _render) so shell.html gets nav_items
    from fastapi.templating import Jinja2Templates
    from core.services.nav_context import get_nav_context
    templates = Jinja2Templates(directory=["core/templates", "modules/ediscovery/templates"])
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(request, "ediscovery/ediscovery_review_react.html", {
        "brand": brand,
        "current_user": user,
        "collection": collection,
        "collection_id": collection_id,
        "page": "ediscovery",
        **nav_ctx,
    })


# ---------------------------------------------------------------------------
# GET /ediscovery/documents/{doc_id}/panes
#   Returns all right-rail cards + viewer in one HTMX swap.
#   This is what a doc-list row click fetches.
# ---------------------------------------------------------------------------

@router.get("/ediscovery/documents/{doc_id}/panes",
            response_class=HTMLResponse)
async def document_panes(request: Request, doc_id: str,
                         collection_id: Optional[str] = Query(None)):
    """
    Assembles the center viewer + right-rail widgets as a single HTMX payload.
    Each widget is rendered by calling its data fn + template, all wrapped
    in the two top-level grid-area containers (#doc-viewer, #doc-right).
    """
    tenant_id = _tenant(request)

    from modules.ediscovery.services.review_widget_service import (
        get_doc_viewer, get_doc_metadata, get_doc_tags,
        get_doc_review_status, get_doc_family,
    )

    # Shared scope — reuse for all five widget calls below
    fake_req = _FakeRequest(request, {"ediscovery_doc_id": doc_id})
    scope = {
        "tenant_id": tenant_id,
        "user_id": _user_id(request),
        "request": fake_req,
    }

    # Fan out in parallel — all 5 are independent DB queries
    viewer_data, meta_data, tags_data, status_data, family_data = await asyncio.gather(
        get_doc_viewer(scope),
        get_doc_metadata(scope),
        get_doc_tags(scope),
        get_doc_review_status(scope),
        get_doc_family(scope),
        return_exceptions=False,
    )

    viewer_html = _render("widgets/ediscovery_doc_viewer.html", viewer_data)
    tags_html = _render("widgets/ediscovery_doc_tags.html", tags_data)
    status_html = _render("widgets/ediscovery_doc_review_status.html", status_data)
    family_html = _render("widgets/ediscovery_doc_family.html", family_data)
    meta_html = _render("widgets/ediscovery_doc_metadata.html", meta_data)

    # Assemble in the grid areas the shell expects: center + right stack
    combined = f"""
<div id="doc-viewer" style="grid-area:center; min-height:0; overflow:hidden;">{viewer_html}</div>
<div id="doc-right" style="grid-area:right; min-height:0; overflow-y:auto; padding-right:2px;">
  {tags_html}
  {status_html}
  {family_html}
  {meta_html}
</div>
"""
    return HTMLResponse(combined)


# ---------------------------------------------------------------------------
# GET /ediscovery/documents/{doc_id}/file
#   Serves the actual file from the CIFS mount, converting on demand.
# ---------------------------------------------------------------------------

def _rendered_cache_path(tenant_id: str, file_hash: str) -> Path:
    """Compute the deterministic cache location for a converted PDF."""
    tid = (tenant_id or "").strip()
    # hash[:2] shards prevent a single huge directory
    return Path(EDISCOVERY_ROOT) / tid / RENDERED_CACHE_DIRNAME / file_hash[:2] / f"{file_hash}.pdf"


def _needs_conversion(mime: Optional[str], path: str) -> bool:
    """Decide whether a file needs LibreOffice conversion before serving."""
    m = (mime or "").lower()
    name = (path or "").lower()
    if m == "application/pdf" or name.endswith(".pdf"):
        return False
    if m.startswith(("image/", "text/")):
        return False
    # Common extensions that warrant conversion
    convertible_exts = (
        ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".odt", ".ods", ".odp", ".rtf",
    )
    if any(name.endswith(ext) for ext in convertible_exts):
        return True
    if m.startswith(("application/msword",
                     "application/vnd.openxmlformats-officedocument",
                     "application/vnd.ms-excel",
                     "application/vnd.ms-powerpoint",
                     "application/vnd.oasis.opendocument",
                     "application/rtf")):
        return True
    return False


def _convert_to_pdf_sync(src_path: Path, dst_path: Path) -> bool:
    """
    Convert src_path to PDF at dst_path using LibreOffice headless.
    Returns True on success. Synchronous; blocks the request worker.
    """
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    # soffice writes the output to --outdir with the source basename + .pdf
    # so we convert to a temp dir then move into place.
    tmp_out = dst_path.parent / f".tmp_{os.getpid()}_{dst_path.name}"
    try:
        cmd = [
            SOFFICE_BIN,
            "--headless", "--convert-to", "pdf",
            "--outdir", str(dst_path.parent),
            str(src_path),
        ]
        logger.info("LibreOffice convert: %s", " ".join(cmd))
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SOFFICE_TIMEOUT_SECS,
        )
        if result.returncode != 0:
            logger.error("soffice returncode=%d stderr=%s",
                         result.returncode,
                         result.stderr.decode("utf-8", errors="ignore")[:500])
            return False

        # soffice names output as <basename>.pdf in outdir
        produced = dst_path.parent / (src_path.stem + ".pdf")
        if not produced.exists():
            logger.error("soffice produced no output at %s", produced)
            return False
        # Move to deterministic hash-based name
        if produced != dst_path:
            produced.rename(dst_path)
        return True
    except subprocess.TimeoutExpired:
        logger.error("soffice timed out after %ds on %s",
                     SOFFICE_TIMEOUT_SECS, src_path)
        return False
    except Exception as exc:
        logger.exception("soffice conversion error: %s", exc)
        return False


@router.get("/ediscovery/documents/{doc_id}/file")
async def document_file(request: Request, doc_id: str,
                        download: int = Query(0)):
    tenant_id = _tenant(request)
    if not tenant_id:
        raise HTTPException(status_code=401)

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT ed.id, ed.file_name, ed.file_path, ed.working_path,
                   ed.native_path, ed.rendition_path, ed.page_count,
                   ed.mime_type, ed.file_hash, ec.storage_path as collection_storage_path,
                   ed.collection_id::text as collection_id, ec.matter_id::text as matter_id
            FROM ediscovery_documents ed
            LEFT JOIN ediscovery_collections ec ON ec.id = ed.collection_id
            WHERE ed.id = CAST(:did AS uuid)
              AND trim(ed.tenant_id::text) = trim(:tid)
            LIMIT 1
        """), {"did": doc_id, "tid": tenant_id})
        row = r.mappings().fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Document not found")

    rec = dict(row)

    # --- access log (Deal Center §1.10) — append-only, fire-and-forget -------
    try:
        from modules.ediscovery.services.access_log import (
            log_document_access, actor_from_request, context_from_referer,
        )
        _uid, _email, _guest = actor_from_request(request)
        await log_document_access(
            tenant_id=tenant_id,
            ediscovery_document_id=rec.get("id"),
            matter_id=rec.get("matter_id"),
            collection_id=rec.get("collection_id"),
            document_name=rec.get("file_name"),
            actor_user_id=_uid, actor_email=_email, actor_is_guest=_guest,
            action="download" if download else "view",
            context=context_from_referer(request.headers.get("referer")),
            ip=(request.client.host if request.client else None),
            user_agent=request.headers.get("user-agent"),
        )
    except Exception:
        pass
    # ------------------------------------------------------------------------

    # Prefer working_path (normalized location); fall back to file_path
    # FIX: file_path is the original document; working_path is extracted text (.txt)
    src = rec.get("file_path") or rec.get("native_path") or rec.get("working_path")
    if not src:
        raise HTTPException(status_code=500,
                            detail="Document has no file path on disk")

    # Resolve path:
    # 1. Already absolute → use as-is
    # 2. Relative + collection has absolute storage_path → join against it
    # 3. Relative, no collection storage_path → join against EDISCOVERY_ROOT
    col_root = rec.get("collection_storage_path")
    if Path(src).is_absolute():
        src_path = Path(src)
    elif col_root and Path(col_root).is_absolute():
        src_path = Path(col_root) / src
    else:
        src_path = Path(EDISCOVERY_ROOT) / src
    # Safety — paths must live under EDISCOVERY_ROOT (no escape)
    try:
        resolved = src_path.resolve()
        root = Path(EDISCOVERY_ROOT).resolve()
        if not str(resolved).startswith(str(root)):
            logger.error("Document path escape attempt: %s", src)
            raise HTTPException(status_code=403, detail="Path outside mount")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Path resolution error [%s]: %s", src, exc)
        raise HTTPException(status_code=500, detail="Path resolution failed")

    if not src_path.exists() or not src_path.is_file():
        logger.error("File missing on disk: %s", src_path)
        raise HTTPException(status_code=404,
                            detail=f"File missing: {src_path.name}")

    file_name = rec.get("file_name") or src_path.name
    mime = rec.get("mime_type") or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    file_hash = rec.get("file_hash")

    # Force download — always return original, never converted
    if download:
        return FileResponse(
            path=str(src_path),
            filename=file_name,
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
        )

    # Prefer the durable pipeline rendition (renditions/{lane}/{doc_id}.pdf).
    # It is the same PDF the production export engine will emboss, so the
    # reviewer sees exactly what will be produced -- and nothing renders twice.
    # Single-page browser-renderable images serve the ORIGINAL so the image
    # zoom viewer works; TIFF / multi-page stitches serve the PDF rendition.
    _m_low = (mime or "").lower()
    _browser_img = _m_low in ("image/jpeg", "image/png", "image/gif", "image/webp")
    _single_browser_img = _browser_img and (rec.get("page_count") or 1) <= 1
    rend = rec.get("rendition_path")
    if rend and col_root and not _single_browser_img:
        rend_path = Path(col_root) / rend
        try:
            r_resolved = rend_path.resolve()
            r_root = Path(EDISCOVERY_ROOT).resolve()
            if str(r_resolved).startswith(str(r_root)) and rend_path.is_file():
                return FileResponse(
                    path=str(rend_path),
                    filename=src_path.stem + ".pdf",
                    media_type="application/pdf",
                    headers={"Content-Disposition": 'inline'},
                )
        except Exception:
            pass  # fall through to the converter

    # Inline viewing path — convert if needed (fallback for docs that have
    # no pipeline rendition yet)
    if _needs_conversion(mime, str(src_path)):
        if not file_hash:
            # Conversion requires a stable cache key. Compute one if missing.
            file_hash = _compute_file_hash(src_path)

        cache = _rendered_cache_path(tenant_id, file_hash)
        if not cache.exists():
            ok = await asyncio.to_thread(_convert_to_pdf_sync, src_path, cache)
            if not ok or not cache.exists():
                # Conversion failed — return a small HTML message inline
                return HTMLResponse(
                    '<div style="padding:20px; font-size:12px; color:#991b1b;">'
                    '⚠ Preview conversion failed. Use Download to retrieve the original.'
                    '</div>',
                    status_code=200,
                )
        return FileResponse(
            path=str(cache),
            filename=src_path.stem + ".pdf",
            media_type="application/pdf",
            headers={"Content-Disposition": 'inline'},
        )

    # Native browser mime — serve original inline
    return FileResponse(
        path=str(src_path),
        filename=file_name,
        media_type=mime,
        headers={"Content-Disposition": 'inline'},
    )


def _compute_file_hash(path: Path) -> str:
    """SHA-256 of a file. Used only when file_hash is missing in the DB."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# GET /ediscovery/documents/{doc_id}/text
#   Plain-text fallback for email/unsupported. Safe for HTMX <pre> fill.
# ---------------------------------------------------------------------------

@router.get("/ediscovery/documents/{doc_id}/native")
async def document_native(request: Request, doc_id: str, download: int = Query(0)):
    """Serve the native file (MP3, M4A, DOCX, etc.) for a document."""
    tenant_id = _tenant(request)
    if not tenant_id:
        raise HTTPException(status_code=401)

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT ed.id, ed.file_name, ed.native_path, ed.mime_type,
                   ec.storage_path as collection_storage_path
            FROM ediscovery_documents ed
            LEFT JOIN ediscovery_collections ec ON ec.id = ed.collection_id
            WHERE ed.id = CAST(:did AS uuid)
              AND trim(ed.tenant_id::text) = trim(:tid)
            LIMIT 1
        """), {"did": doc_id, "tid": tenant_id})
        row = r.mappings().fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Document not found")

    rec = dict(row)
    native_rel = rec.get("native_path")
    if not native_rel:
        raise HTTPException(status_code=404, detail="No native file for this document")

    col_root = rec.get("collection_storage_path")
    if Path(native_rel).is_absolute():
        native_path = Path(native_rel)
    elif col_root and Path(col_root).is_absolute():
        native_path = Path(col_root) / native_rel
    else:
        native_path = Path(EDISCOVERY_ROOT) / native_rel

    if not native_path.exists() or not native_path.is_file():
        raise HTTPException(status_code=404, detail=f"Native file missing: {native_path.name}")

    ext = native_path.suffix.lower()
    AUDIO_MIME = {
        ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".m4r": "audio/mp4",
        ".wav": "audio/wav", ".ogg": "audio/ogg", ".aac": "audio/aac",
        ".flac": "audio/flac", ".wma": "audio/x-ms-wma",
    }
    mime = AUDIO_MIME.get(ext) or mimetypes.guess_type(str(native_path))[0] or "application/octet-stream"

    file_name = rec.get("file_name") or native_path.name

    # Forced download -- always the true native, attachment disposition
    if download:
        return FileResponse(
            path=str(native_path),
            filename=file_name,
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
        )

    # NATIVE_STREAM_V1 -- stream inline. Office natives are not
    # browser-renderable; serve a cached PDF rendering (derived artifact --
    # the stored native is never mutated) so the viewer streams instead of
    # triggering a download. ?download=1 still returns the true native.
    if _needs_conversion(mime, str(native_path)):
        file_hash = _compute_file_hash(native_path)
        cache = _rendered_cache_path(tenant_id, file_hash)
        if not cache.exists():
            ok = await asyncio.to_thread(_convert_to_pdf_sync, native_path, cache)
            if not ok or not cache.exists():
                # Conversion failed -- fall back to attachment of the native
                return FileResponse(
                    path=str(native_path),
                    filename=file_name,
                    media_type=mime,
                    headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
                )
        pdf_name = native_path.stem + ".pdf"
        return FileResponse(
            path=str(cache),
            filename=pdf_name,
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{pdf_name}"'},
        )

    return FileResponse(
        path=str(native_path),
        filename=file_name,
        media_type=mime,
        headers={"Content-Disposition": f'inline; filename="{file_name}"'},
    )


@router.get("/ediscovery/documents/{doc_id}/text", response_class=PlainTextResponse)
async def document_text(request: Request, doc_id: str):
    tenant_id = _tenant(request)
    if not tenant_id:
        raise HTTPException(status_code=401)

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT ed.extracted_text, ed.text_path, ed.working_path,
                   ec.storage_path as col_storage_path,
                   ec.dms_source_path as col_source_path
            FROM ediscovery_documents ed
            LEFT JOIN ediscovery_collections ec ON ec.id = ed.collection_id
            WHERE ed.id = CAST(:did AS uuid)
              AND trim(ed.tenant_id::text) = trim(:tid)
            LIMIT 1
        """), {"did": doc_id, "tid": tenant_id})
        row = r.mappings().fetchone()

    if not row:
        raise HTTPException(status_code=404)
    rec = dict(row)

    text = (rec.get("extracted_text") or "").strip().lstrip("\ufeff").strip()
    # Quality check: if extracted_text looks like raw MIME headers, discard it
    # and re-extract from the original .eml file
    if text and re.match(r'^(Subject:\s*\n|DKIM-|Received:|ARC-|Return-Path:|MIME-Version:)', text[:200]):
        # Check if the text after any Subject: line is just raw headers, not real content
        lines = text.split('\n', 10)
        has_mime_junk = any(l.strip().startswith(('DKIM-', 'Received:', 'ARC-', 'Return-Path:', 'spf=', 'dkim=', 'dmarc=')) for l in lines[:10])
        if has_mime_junk:
            logger.info("document_text: extracted_text for %s looks like raw MIME, will re-extract from .eml", doc_id)
            text = ""  # Force fallback to file-based extraction
    if not text:
        # Try dms_source_path first (where extracted files live), then storage_path
        col_root = rec.get("col_source_path") or rec.get("col_storage_path") or ""
        for path_col in ("text_path", "working_path"):
            rel = (rec.get(path_col) or "").strip().replace("//", "/")
            if not rel:
                continue
            candidate = Path(rel) if Path(rel).is_absolute() else Path(col_root) / rel
            if candidate.exists() and candidate.is_file():
                try:
                    # Check if this is a raw .eml file — parse MIME instead of returning raw
                    if candidate.suffix.lower() in (".eml", ".msg"):
                        raw_bytes = candidate.read_bytes()
                        text = _extract_email_body(raw_bytes).strip()
                    else:
                        text = candidate.read_text(encoding="utf-8", errors="replace").strip().lstrip("\ufeff").strip()
                    if text:
                        break
                except Exception:
                    pass
        # If still no text and we have a file_path that's an .eml, try that too
        if not text:
            file_path_rel = rec.get("col_source_path") or rec.get("col_storage_path") or ""
            # Check the original file_path from the documents table
            from sqlalchemy import text as sa_text2
            try:
                async with AsyncSessionLocal() as session2:
                    r2 = await session2.execute(sa_text2("""
                        SELECT ed.file_path, ec.storage_path
                        FROM ediscovery_documents ed
                        LEFT JOIN ediscovery_collections ec ON ec.id = ed.collection_id
                        WHERE ed.id = CAST(:did AS uuid)
                          AND trim(ed.tenant_id::text) = trim(:tid)
                        LIMIT 1
                    """), {"did": doc_id, "tid": tenant_id})
                    frow = r2.mappings().fetchone()
                if frow:
                    fp = frow.get("file_path", "")
                    sp = frow.get("storage_path", "")
                    if fp and fp.lower().endswith((".eml", ".msg")):
                        fp_full = Path(fp) if Path(fp).is_absolute() else Path(sp or EDISCOVERY_ROOT) / fp
                        if fp_full.exists():
                            text = _extract_email_body(fp_full.read_bytes()).strip()
            except Exception:
                pass

    return PlainTextResponse(text or "(no transcript available — Whisper transcription pending)")


@router.post("/ediscovery/documents/{doc_id}/tags", response_class=HTMLResponse)
async def apply_tag(request: Request, doc_id: str,
                    tag_id: str = Form(...),
                    action: str = Form("apply")):
    tenant_id = _tenant(request)
    user_id = _user_id(request)
    if not tenant_id:
        raise HTTPException(status_code=401)

    try:
        async with AsyncSessionLocal() as session:
            if action == "apply":
                # Idempotent — skip if already applied
                r_exists = await session.execute(sa_text("""
                    SELECT 1 FROM document_tags
                    WHERE document_id = CAST(:did AS uuid)
                      AND tag_id = CAST(:tid AS uuid)
                      AND trim(tenant_id::text) = trim(:tenant)
                    LIMIT 1
                """), {"did": doc_id, "tid": tag_id, "tenant": tenant_id})
                if not r_exists.fetchone():
                    # users.id is bigint; document_tags.applied_by is uuid — write NULL
                    await session.execute(sa_text("""
                        INSERT INTO document_tags
                            (tenant_id, document_id, tag_id, source,
                             applied_by, applied_at)
                        VALUES
                            (:tenant, CAST(:did AS uuid), CAST(:tid AS uuid),
                             'manual', NULL, now())
                    """), {"tenant": tenant_id, "did": doc_id, "tid": tag_id})
                    await session.commit()

            elif action == "remove":
                await session.execute(sa_text("""
                    DELETE FROM document_tags
                    WHERE document_id = CAST(:did AS uuid)
                      AND tag_id = CAST(:tid AS uuid)
                      AND trim(tenant_id::text) = trim(:tenant)
                """), {"did": doc_id, "tid": tag_id, "tenant": tenant_id})
                await session.commit()
    except Exception as exc:
        logger.exception("apply_tag error: %s", exc)
        return HTMLResponse(
            f'<div style="padding:10px;color:#991b1b;font-size:11px;">⚠ Tag update failed: {exc}</div>'
        )

    # Re-render just the tags card body for HTMX swap
    body = await _render_tags_body(request, doc_id)
    return HTMLResponse(body)


# ---------------------------------------------------------------------------
# POST /ediscovery/documents/{doc_id}/review-status
#   Form fields (both optional, at least one must be present):
#     review_status       — one of the values in _REVIEW_STATUSES
#     privilege_status    — 'none'|'privileged'|'attorney_work_product'|'redact'
# ---------------------------------------------------------------------------

@router.post("/ediscovery/documents/{doc_id}/review-status",
             response_class=HTMLResponse)
async def update_review_status(request: Request, doc_id: str,
                               review_status: Optional[str] = Form(None),
                               privilege_status: Optional[str] = Form(None)):
    tenant_id = _tenant(request)
    user_id = _user_id(request)
    if not tenant_id:
        raise HTTPException(status_code=401)

    # Build dynamic UPDATE — only set fields that were sent
    updates = []
    params = {"did": doc_id, "tenant": tenant_id}

    if review_status is not None:
        updates.append("review_status = :rs")
        params["rs"] = review_status
        # When moving away from 'unreviewed', stamp reviewer + timestamp
        if review_status != "unreviewed":
            updates.append("reviewed_at = now()")
            # reviewed_by is uuid, user_id is bigint — write NULL (schema drift)
            # Leave existing value intact by not setting reviewed_by.
        else:
            updates.append("reviewed_at = NULL")

    if privilege_status is not None:
        # 'none' in the UI maps to NULL in the DB
        if privilege_status == "none":
            updates.append("privilege_status = NULL")
        else:
            updates.append("privilege_status = :ps")
            params["ps"] = privilege_status

    if not updates:
        raise HTTPException(status_code=400,
                            detail="review_status or privilege_status required")

    updates.append("updated_at = now()")
    set_clause = ", ".join(updates)

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text(f"""
                UPDATE ediscovery_documents
                SET {set_clause}
                WHERE id = CAST(:did AS uuid)
                  AND trim(tenant_id::text) = trim(:tenant)
            """), params)
            # Bump collection reviewed_docs count lazily (non-blocking accuracy)
            await session.execute(sa_text("""
                UPDATE ediscovery_collections c
                SET reviewed_docs = (
                    SELECT COUNT(*) FROM ediscovery_documents
                    WHERE collection_id = c.id
                      AND COALESCE(review_status, 'unreviewed') <> 'unreviewed'
                )
                WHERE c.id = (
                    SELECT collection_id FROM ediscovery_documents
                    WHERE id = CAST(:did AS uuid)
                      AND trim(tenant_id::text) = trim(:tenant)
                )
            """), {"did": doc_id, "tenant": tenant_id})
            await session.commit()
    except Exception as exc:
        logger.exception("update_review_status error: %s", exc)
        return HTMLResponse(
            f'<div style="padding:10px;color:#991b1b;font-size:11px;">⚠ Status update failed: {exc}</div>'
        )

    body = await _render_review_status_body(request, doc_id)
    return HTMLResponse(body)


# ---------------------------------------------------------------------------
# POST /ediscovery/collections/{collection_id}/tags
#   Create a new collection-scoped tag (user-generated, not system-seeded).
#   Optional apply_to_doc_id — immediately apply the new tag to that doc.
#   Returns the re-rendered tags widget body.
# ---------------------------------------------------------------------------

@router.post("/ediscovery/collections/{collection_id}/tags",
             response_class=HTMLResponse)
async def create_collection_tag(request: Request, collection_id: str,
                                name: str = Form(...),
                                apply_to_doc_id: Optional[str] = Form(None)):
    tenant_id = _tenant(request)
    if not tenant_id:
        raise HTTPException(status_code=401)

    name = (name or "").strip()
    if not name:
        if apply_to_doc_id:
            body = await _render_tags_body(request, apply_to_doc_id)
            return HTMLResponse(body)
        raise HTTPException(status_code=400, detail="Tag name required")

    try:
        async with AsyncSessionLocal() as session:
            # Reuse if a same-name tag already exists in this collection
            r = await session.execute(sa_text("""
                SELECT id FROM tags
                WHERE trim(tenant_id::text) = trim(:tenant)
                  AND collection_id = CAST(:cid AS uuid)
                  AND name = :name
                LIMIT 1
            """), {"tenant": tenant_id, "cid": collection_id, "name": name})
            row = r.fetchone()
            if row:
                tag_id = str(row[0])
            else:
                r_new = await session.execute(sa_text("""
                    INSERT INTO tags
                        (tenant_id, collection_id, name, category, color,
                         is_system, created_by, created_at)
                    VALUES
                        (:tenant, CAST(:cid AS uuid), :name, 'custom',
                         '#2563EB', false, NULL, now())
                    RETURNING id
                """), {"tenant": tenant_id, "cid": collection_id, "name": name})
                tag_id = str(r_new.scalar())
            await session.commit()

            # Optionally apply to the current document
            if apply_to_doc_id:
                r_exists = await session.execute(sa_text("""
                    SELECT 1 FROM document_tags
                    WHERE document_id = CAST(:did AS uuid)
                      AND tag_id = CAST(:tid AS uuid)
                      AND trim(tenant_id::text) = trim(:tenant)
                    LIMIT 1
                """), {"did": apply_to_doc_id, "tid": tag_id,
                        "tenant": tenant_id})
                if not r_exists.fetchone():
                    await session.execute(sa_text("""
                        INSERT INTO document_tags
                            (tenant_id, document_id, tag_id, source,
                             applied_by, applied_at)
                        VALUES
                            (:tenant, CAST(:did AS uuid), CAST(:tid AS uuid),
                             'manual', NULL, now())
                    """), {"tenant": tenant_id, "did": apply_to_doc_id,
                           "tid": tag_id})
                    await session.commit()
    except Exception as exc:
        logger.exception("create_collection_tag error: %s", exc)
        return HTMLResponse(
            f'<div style="padding:10px;color:#991b1b;font-size:11px;">⚠ Tag create failed: {exc}</div>'
        )

    if apply_to_doc_id:
        body = await _render_tags_body(request, apply_to_doc_id)
        return HTMLResponse(body)
    return HTMLResponse(
        '<div style="padding:6px;font-size:11px;color:var(--muted);">Tag created.</div>'
    )


# ---------------------------------------------------------------------------
# GET /ediscovery/matters/{matter_id}/collections
#   Fills the dead link from the Review page — lists collections
#   in a matter, with Open links to the review workspace.
# ---------------------------------------------------------------------------

@router.get("/ediscovery/matters/{matter_id}/collections",
            response_class=HTMLResponse)
async def matter_collections(request: Request, matter_id: str):
    tenant_id = _tenant(request)
    if not tenant_id:
        raise HTTPException(status_code=401)

    matter = None
    collections = []
    try:
        async with AsyncSessionLocal() as session:
            r_m = await session.execute(sa_text("""
                SELECT id, matter_name, matter_number
                FROM matters
                WHERE id = CAST(:mid AS uuid)
                  AND trim(tenant_id::text) = trim(:tid)
                LIMIT 1
            """), {"mid": matter_id, "tid": tenant_id})
            m_row = r_m.mappings().fetchone()
            if m_row:
                matter = dict(m_row)

            r_c = await session.execute(sa_text("""
                SELECT id, name, collection_name, status, source_party,
                       received_date, total_docs, reviewed_docs,
                       received_method, stated_bates_range, created_at
                FROM ediscovery_collections
                WHERE matter_id = CAST(:mid AS uuid)
                  AND trim(tenant_id::text) = trim(:tid)
                ORDER BY COALESCE(received_date, created_at::date) DESC
            """), {"mid": matter_id, "tid": tenant_id})
            collections = [dict(r) for r in r_c.mappings().fetchall()]

            for c in collections:
                c["display_name"] = (c.get("name") or c.get("collection_name")
                                     or "Collection")
                total = c.get("total_docs") or 0
                reviewed = c.get("reviewed_docs") or 0
                c["progress_pct"] = int((reviewed / total * 100)
                                        if total else 0)
    except Exception as exc:
        logger.exception("matter_collections error: %s", exc)

    brand = getattr(request.state, "branding", None)
    user = getattr(request.state, "current_user", None)

    return HTMLResponse(_render("ediscovery/matter_collections.html", {
        "brand": brand,
        "user": user,
        "matter": matter,
        "collections": collections,
        "matter_id": matter_id,
        "page": "ediscovery",
    }))
