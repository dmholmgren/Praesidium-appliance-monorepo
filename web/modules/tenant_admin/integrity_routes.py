"""
Integrity Report — route module.
Shows reconciliation results: orphans, ghosts, mismatches.
"""
import os, logging, subprocess
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text as sa_text

log = logging.getLogger("app")
PAGE_SIZE = 50


def _esc(s):
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;").replace("'","&#39;")


def register_integrity_routes(router, AsyncSessionLocal, _unused=None):

    def _t(request):
        from app import templates
        return templates

    def _tid(request):
        tid = getattr(request.state, "tenant_id", None)
        if tid: return tid.strip()
        return (request.query_params.get("tenant_id") or "").strip()

    @router.get("/integrity", response_class=HTMLResponse)
    async def integrity_page(request: Request):
        tid = _tid(request)
        filt = request.query_params.get("filter", "all")

        recon = None
        try:
            async with AsyncSessionLocal() as session:
                # Get latest run summary
                rr = (await session.execute(sa_text("""
                    SELECT run_id::text, MIN(created_at) as created_at,
                        COUNT(*) FILTER (WHERE issue_type = 'orphan') as orphans,
                        COUNT(*) FILTER (WHERE issue_type = 'ghost') as ghosts,
                        COUNT(*) FILTER (WHERE issue_type = 'size_mismatch') as mismatches,
                        COUNT(*) as total_issues
                    FROM praesidium_reconciliation WHERE TRIM(tenant_id) = :tid
                    GROUP BY run_id ORDER BY MIN(created_at) DESC LIMIT 1
                """), {"tid": tid})).mappings().fetchone()

                if rr:
                    recon = dict(rr)
                    recon["created_at"] = rr["created_at"].strftime("%Y-%m-%d %H:%M") if rr["created_at"] else "—"
                    # Count verified docs
                    vc = (await session.execute(sa_text("""
                        SELECT COUNT(*) as cnt FROM dms_documents
                        WHERE TRIM(tenant_id) = :tid AND verified_at IS NOT NULL
                    """), {"tid": tid})).fetchone()
                    recon["verified"] = vc[0] if vc else 0
        except Exception as e:
            log.warning(f"Integrity page recon query failed: {e}")

        return _t(request).TemplateResponse(request, "tenant_admin/integrity.html", {
            "tenant_id": tid, "recon": recon, "current_filter": filt, "brand": None,
        })

    @router.get("/integrity/batch", response_class=HTMLResponse)
    async def integrity_batch(request: Request):
        tid = _tid(request)
        filt = request.query_params.get("filter", "all")
        offset = int(request.query_params.get("offset", "0"))
        limit = int(request.query_params.get("limit", str(PAGE_SIZE)))

        type_filter = ""
        if filt != "all":
            type_filter = "AND pr.issue_type = :filt"

        try:
            async with AsyncSessionLocal() as session:
                # Get latest run_id
                run_row = (await session.execute(sa_text("""
                    SELECT run_id::text FROM praesidium_reconciliation
                    WHERE TRIM(tenant_id) = :tid ORDER BY created_at DESC LIMIT 1
                """), {"tid": tid})).fetchone()

                if not run_row:
                    return HTMLResponse('<tr><td colspan="5" style="padding:24px;text-align:center;color:var(--muted);font-size:12px;">No reconciliation data.</td></tr>')

                run_id = run_row[0]
                params = {"tid": tid, "rid": run_id, "off": offset, "lim": limit}
                if filt != "all":
                    params["filt"] = filt

                rows = (await session.execute(sa_text(f"""
                    SELECT pr.id, pr.issue_type, pr.file_path, pr.folder_root,
                           pr.file_size_bytes, pr.file_hash, pr.db_doc_id::text,
                           pr.details
                    FROM praesidium_reconciliation pr
                    WHERE TRIM(pr.tenant_id) = :tid AND pr.run_id = CAST(:rid AS uuid)
                    {type_filter}
                    ORDER BY pr.issue_type, pr.file_path
                    OFFSET :off LIMIT :lim
                """), params)).mappings().fetchall()
        except Exception as e:
            return HTMLResponse(f'<tr><td colspan="5" style="padding:12px;color:#991B1B;font-size:12px;">Error: {_esc(str(e))}</td></tr>')

        html = []
        for r in rows:
            itype = r["issue_type"]
            fpath = r["file_path"] or ""
            fname = os.path.basename(fpath)
            fdir = os.path.dirname(fpath)
            # Shorten display path
            short_dir = fdir.replace("/mnt/praesidium/", "…/").replace("/mnt/legacy-qnap/D Drive Backup/Clients/", "…/")
            size = r["file_size_bytes"]
            size_str = f"{size:,}" if size else "—"

            badge_map = {
                "orphan": ('<span style="padding:2px 8px;background:#FEF3C7;color:#92400E;border-radius:10px;font-size:10px;font-weight:500;">Orphan</span>',
                           "On disk, not in DB"),
                "ghost": ('<span style="padding:2px 8px;background:#FEE2E2;color:#991B1B;border-radius:10px;font-size:10px;font-weight:500;">Ghost</span>',
                          "In DB, not on disk"),
                "size_mismatch": ('<span style="padding:2px 8px;background:#F3E8FF;color:#6B21A8;border-radius:10px;font-size:10px;font-weight:500;">Mismatch</span>',
                                  "Size differs"),
            }
            badge, tip = badge_map.get(itype, ("", ""))

            if itype == "orphan":
                actions = f'<button onclick="registerOrphan({r["id"]})" class="btn-primary" style="padding:2px 8px;font-size:10px;">Register</button>'
            elif itype == "ghost":
                actions = f'<button onclick="removeGhost({r["id"]})" class="btn-secondary" style="padding:2px 8px;font-size:10px;color:#991B1B;">Remove</button>'
            else:
                details = r.get("details") or {}
                db_size = details.get("db_size", "?")
                disk_size = details.get("disk_size", "?")
                actions = f'<span style="font-size:10px;color:var(--muted);">DB: {db_size} / Disk: {disk_size}</span>'

            fe = _esc(fpath)
            html.append(
                f'<tr class="issue-row" data-path="{fe.lower()}" style="border-bottom:1px solid var(--border);">'
                f'<td style="padding:5px 8px;" title="{_esc(tip)}">{badge}</td>'
                f'<td style="padding:5px 8px;"><div style="font-size:12px;font-weight:500;">{_esc(fname)}</div>'
                f'<div style="font-size:10px;color:var(--muted);max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="{fe}">{_esc(short_dir)}</div></td>'
                f'<td style="padding:5px 8px;font-size:11px;color:var(--muted);">{_esc(r.get("folder_root") or "").replace("/mnt/praesidium/","…/")}</td>'
                f'<td style="padding:5px 8px;text-align:center;font-size:11px;">{size_str}</td>'
                f'<td style="padding:5px 8px;">{actions}</td>'
                f'</tr>'
            )

        if len(rows) >= limit:
            noff = offset + limit
            html.append(f'<tr hx-get="/tenant-admin/integrity/batch?filter={filt}&offset={noff}&limit={limit}" '
                        f'hx-trigger="revealed" hx-swap="afterend" hx-target="this">'
                        f'<td colspan="5" style="padding:12px;text-align:center;color:var(--muted);font-size:11px;">Loading more...</td></tr>')

        return HTMLResponse("\n".join(html))

    @router.post("/integrity/run")
    async def integrity_run(request: Request):
        tid = _tid(request)
        try:
            result = subprocess.run(
                ["python3", "/app/jobs/reconcile_praesidium.py", "--tenant", tid],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                return JSONResponse({"status": "error", "detail": result.stderr[-500:]}, status_code=500)
            # Parse summary from output
            lines = result.stdout.split("\n")
            summary_parts = []
            for line in lines:
                for key in ["Verified:", "Orphans:", "Ghosts:", "Mismatches:"]:
                    if key in line:
                        summary_parts.append(line.strip().split("  ")[-1].strip())
            return JSONResponse({"status": "ok", "message": "Reconciliation complete. " + " | ".join(summary_parts)})
        except subprocess.TimeoutExpired:
            return JSONResponse({"status": "error", "detail": "Timed out after 120s"}, status_code=500)
        except Exception as e:
            return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

    @router.post("/integrity/register")
    async def integrity_register(request: Request):
        """Register an orphan file — create a dms_documents row for it."""
        body = await request.json()
        tid = _tid(request)
        issue_id = body.get("issue_id")

        async with AsyncSessionLocal() as session:
            issue = (await session.execute(sa_text("""
                SELECT file_path, file_size_bytes, file_hash, folder_root
                FROM praesidium_reconciliation
                WHERE id = :iid AND TRIM(tenant_id) = :tid AND issue_type = 'orphan'
            """), {"iid": issue_id, "tid": tid})).mappings().fetchone()

            if not issue:
                return JSONResponse({"status": "error", "detail": "Issue not found"}, status_code=404)

            fpath = issue["file_path"]
            ext = os.path.splitext(fpath)[1].lower()
            ocr_exts = {".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png"}
            ocr_status = "ocr_pending" if ext in ocr_exts else "text_native"

            await session.execute(sa_text("""
                INSERT INTO dms_documents (id, tenant_id, file_path, folder_root, file_hash,
                    file_size_bytes, ocr_status, extraction_status, source, indexed_at, updated_at, verified_at)
                VALUES (gen_random_uuid(), :tid, :fp, :fr, :fh, :fs, :ocr, 'pending', 'reconciliation', NOW(), NOW(), NOW())
                ON CONFLICT (tenant_id, file_path) DO UPDATE SET verified_at = NOW()
            """), {"tid": tid, "fp": fpath, "fr": issue["folder_root"],
                   "fh": issue["file_hash"], "fs": issue["file_size_bytes"], "ocr": ocr_status})

            # Remove from reconciliation table
            await session.execute(sa_text("DELETE FROM praesidium_reconciliation WHERE id = :iid"), {"iid": issue_id})
            await session.commit()

        return JSONResponse({"status": "ok"})

    @router.post("/integrity/remove-ghost")
    async def integrity_remove_ghost(request: Request):
        """Remove a ghost — delete the dms_documents row where file doesn't exist."""
        body = await request.json()
        tid = _tid(request)
        issue_id = body.get("issue_id")

        async with AsyncSessionLocal() as session:
            issue = (await session.execute(sa_text("""
                SELECT db_doc_id::text FROM praesidium_reconciliation
                WHERE id = :iid AND TRIM(tenant_id) = :tid AND issue_type = 'ghost'
            """), {"iid": issue_id, "tid": tid})).fetchone()

            if not issue or not issue[0]:
                return JSONResponse({"status": "error", "detail": "Issue not found"}, status_code=404)

            await session.execute(sa_text("DELETE FROM dms_documents WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid"),
                                  {"did": issue[0], "tid": tid})
            await session.execute(sa_text("DELETE FROM praesidium_reconciliation WHERE id = :iid"), {"iid": issue_id})
            await session.commit()

        return JSONResponse({"status": "ok"})

    return router
