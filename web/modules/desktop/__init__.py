"""
M-DESK — Praesidium Desktop Client backend.

This package implements the FastAPI surface that the Windows VSTO add-in
suite calls. It is the desktop counterpart to the Office JS web add-in
(modules/dms/services/office_addins.py). The two surfaces are
intentionally independent:

  * Office JS add-in (web)   — session-cookie auth, browser-hosted task
                                pane, lives under /api/v1/addin/.
  * VSTO desktop client      — JWT auth, native Office COM/.NET host,
                                lives under /api/v1/desktop/.

Components in this package (built incrementally as M-DESK C1):

  jwt_service        Pure functions to issue/verify access + refresh tokens.
  checkout_service   JSONB checkout state machine on documents.metadata.
  auth_router        POST /auth/token, /auth/refresh.
  checkout_router    POST /checkout, /checkin, /checkout/{id}/release.
  manifest_router    GET  /manifest, POST /heartbeat.
  compare_router     POST /compare with RQ enqueue + status poll.
  compare_worker     RQ job that produces a redlined .docx via LibreOffice.

Architectural rules followed throughout:

  * AsyncSessionLocal only — never get_session_factory() (that returns the
    SYNC factory due to a name collision in core/db/base.py).
  * tenant_id values are .strip()'d before use; SQL uses TRIM(tenant_id)
    in WHERE clauses for tables with the legacy CHAR(36) trailing-space
    issue.
  * Checkout state lives in documents.metadata JSONB. No new schema for
    checkout. Refresh tokens are the one exception (Alembic 0009 created
    desktop_refresh_tokens because session-state-in-credentials_vault was
    a wrong-shape fit).
  * brand.get('key') or 'default' — never the two-arg .get() form.
"""

__all__: list[str] = []
