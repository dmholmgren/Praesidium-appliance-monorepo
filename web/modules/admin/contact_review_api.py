"""
modules/admin/contact_review_api.py
===================================

Phase C — Dedup + Proposals review queues (rewired to billing layout).

Templates live in core/templates/contacts/ and extend billing/layout.html.
Auth uses request.state.current_user.

Patent Pending - Series 2/3 - D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.admin.contact_review")

router = APIRouter(prefix="/contacts", tags=["contact-review"])

templates = Jinja2Templates(directory=[
    "core/templates",
    "modules/billing/templates",
])


async def _require_admin(request: Request) -> Dict[str, Any]:
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(401, "Not authenticated")
    tid = getattr(request.state, "tenant_id", None) or getattr(user, "tenant_id", None)
    if not tid:
        raise HTTPException(401, "Tenant not resolved")
    return {
        "user_id": int(user.id),
        "tenant_id": (tid or "").strip(),
        "role": getattr(user, "role", None) or "staff",
    }


def _ctx(request: Request, sess: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    from modules.billing.brand_helper import get_brand
    user = getattr(request.state, "current_user", None)
    # DB-driven tab strip (canonical layout_tabs via tab_service); [] falls back.
    try:
        from core.services.tab_service import get_tabs_sync
        _tabs = get_tabs_sync("billing", sess.get("tenant_id"), sess.get("role"))
    except Exception:
        _tabs = []
    return {
        "request": request,
        "brand": get_brand(request),
        "page": "billing",
        "bill_tab": "contacts",
        "module_tabs": _tabs,
        "active_tab": "contacts",
        "user": user,
        "current_user": user,
        **kwargs,
    }


# ===========================================================================
# DEDUP QUEUE
# ===========================================================================
@router.get("/dedup-review", response_class=HTMLResponse)
async def dedup_list(request: Request):
    sess = await _require_admin(request)
    tid = sess["tenant_id"]

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("""
                SELECT id, contact_ids, signal_type, confidence,
                       notes, hygiene_run_id, detected_at
                FROM contact_dedup_candidates
                WHERE TRIM(tenant_id) = :tid
                  AND review_outcome IS NULL
                ORDER BY confidence DESC, detected_at DESC
                LIMIT 200
            """),
            {"tid": tid},
        )
        candidates = r.fetchall()

        all_ids: set = set()
        for c in candidates:
            ids = c.contact_ids or []
            if isinstance(ids, list):
                all_ids.update(int(i) for i in ids)
        contacts_by_id: Dict[int, Any] = {}
        if all_ids:
            r = await db.execute(
                text("""
                    SELECT id, full_name, email, phone, company,
                           firm_name, contact_type, created_at,
                           (SELECT COUNT(*) FROM matter_contacts mc
                            WHERE mc.contact_id = c.id
                              AND TRIM(mc.tenant_id) = TRIM(c.tenant_id))
                            AS n_matters
                    FROM contacts c
                    WHERE TRIM(c.tenant_id) = :tid
                      AND c.id = ANY(:ids)
                """),
                {"tid": tid, "ids": list(all_ids)},
            )
            for row in r.fetchall():
                contacts_by_id[int(row.id)] = row

    enriched = []
    for c in candidates:
        ids = c.contact_ids or []
        if isinstance(ids, list):
            cluster_contacts = [
                contacts_by_id[int(i)]
                for i in ids
                if int(i) in contacts_by_id
            ]
        else:
            cluster_contacts = []
        if not cluster_contacts:
            continue
        enriched.append({
            "id": c.id,
            "signal_type": c.signal_type,
            "confidence": float(c.confidence or 0),
            "notes": c.notes,
            "detected_at": c.detected_at,
            "contacts": cluster_contacts,
            "suggested_canonical_id": _pick_canonical(cluster_contacts),
        })

    # === v4 total_losers computation ===
    # Compute the total number of contacts that would be archived if the
    # user clicks "Accept All" — it's the sum of (cluster_size - 1) for each
    # cluster (one record kept as canonical, the rest archived). The accept-all
    # endpoint dedupes overlapping clusters, so this is a slight upper bound,
    # but accurate enough for a confirm dialog.
    total_losers = sum(max(0, len(c.get("contacts") or []) - 1) for c in enriched)

    return templates.TemplateResponse(
        request, "contacts/dedup_review.html",
        _ctx(request, sess, candidates=enriched, total_losers=total_losers),
    )


def _pick_canonical(contacts: List[Any]) -> Optional[int]:
    if not contacts:
        return None
    def score(c):
        return (
            1 if c.email else 0,
            int(c.n_matters or 0),
            -int(c.id),
        )
    return int(max(contacts, key=score).id)


@router.post("/dedup-review/{candidate_id}/merge")
async def dedup_merge(
    request: Request,
    candidate_id: str,
    canonical_contact_id: int = Form(...),
):
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    user_id = sess["user_id"]

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("""
                SELECT id, contact_ids
                FROM contact_dedup_candidates
                WHERE id = :id
                  AND TRIM(tenant_id) = :tid
                  AND review_outcome IS NULL
            """),
            {"id": candidate_id, "tid": tid},
        )
        candidate = r.fetchone()
        if not candidate:
            raise HTTPException(404, "Candidate not found or already reviewed")

        ids_raw = candidate.contact_ids or []
        cluster_ids = [int(i) for i in ids_raw if int(i) != canonical_contact_id]
        if not cluster_ids:
            raise HTTPException(400, "Cluster has no losers to merge")

        if int(canonical_contact_id) not in [int(i) for i in ids_raw]:
            raise HTTPException(400, "canonical_contact_id not in cluster")

        for loser_id in cluster_ids:
            r = await db.execute(
                text("""
                    SELECT id, matter_id, role, is_primary, notes
                    FROM matter_contacts
                    WHERE contact_id = :loser
                      AND TRIM(tenant_id) = :tid
                """),
                {"loser": loser_id, "tid": tid},
            )
            for link in r.fetchall():
                r2 = await db.execute(
                    text("""
                        SELECT id FROM matter_contacts
                        WHERE contact_id = :canon
                          AND matter_id = :mid
                          AND TRIM(tenant_id) = :tid
                    """),
                    {
                        "canon": canonical_contact_id,
                        "mid": link.matter_id,
                        "tid": tid,
                    },
                )
                if r2.fetchone():
                    await db.execute(
                        text("DELETE FROM matter_contacts WHERE id = :id"),
                        {"id": link.id},
                    )
                else:
                    await db.execute(
                        text("""
                            UPDATE matter_contacts
                            SET contact_id = :canon
                            WHERE id = :id
                        """),
                        {"canon": canonical_contact_id, "id": link.id},
                    )

        for loser_id in cluster_ids:
            await db.execute(
                text("""
                    UPDATE matter_contact_proposals
                    SET review_status = 'superseded',
                        reviewed_by = :uid,
                        reviewed_at = NOW(),
                        review_notes = :note
                    WHERE contact_id = :loser
                      AND TRIM(tenant_id) = :tid
                      AND review_status = 'pending'
                """),
                {
                    "loser": loser_id,
                    "tid": tid,
                    "uid": user_id,
                    "note": f"superseded by merge into canonical "
                            f"contact_id={canonical_contact_id}",
                },
            )

        await db.execute(
            text("""
                UPDATE contacts
                SET contact_type = 'archived', updated_at = NOW()
                WHERE id = ANY(:ids)
                  AND TRIM(tenant_id) = :tid
            """),
            {"ids": cluster_ids, "tid": tid},
        )

        await db.execute(
            text("""
                UPDATE contact_dedup_candidates
                SET review_outcome = 'merged',
                    canonical_contact_id = :canon,
                    reviewed_by = :uid,
                    reviewed_at = NOW()
                WHERE TRIM(tenant_id) = :tid
                  AND review_outcome IS NULL
                  AND contact_ids ?| :id_strs
            """),
            {
                "canon": canonical_contact_id,
                "uid": user_id,
                "tid": tid,
                "id_strs": [str(i) for i in cluster_ids],
            },
        )

        await db.execute(
            text("""
                UPDATE contact_dedup_candidates
                SET review_outcome = 'merged',
                    canonical_contact_id = :canon,
                    reviewed_by = :uid,
                    reviewed_at = NOW()
                WHERE id = :id
            """),
            {
                "canon": canonical_contact_id,
                "uid": user_id,
                "id": candidate_id,
            },
        )

        await db.commit()

    return RedirectResponse("/contacts/dedup-review", status_code=303)


@router.post("/dedup-review/{candidate_id}/keep-separate")
async def dedup_keep_separate(request: Request, candidate_id: str):
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""
                UPDATE contact_dedup_candidates
                SET review_outcome = 'kept_separate',
                    reviewed_by = :uid,
                    reviewed_at = NOW()
                WHERE id = :id AND TRIM(tenant_id) = :tid
            """),
            {"id": candidate_id, "tid": tid, "uid": sess["user_id"]},
        )
        await db.commit()
    return RedirectResponse("/contacts/dedup-review", status_code=303)


@router.post("/dedup-review/{candidate_id}/reject")
async def dedup_reject(request: Request, candidate_id: str):
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""
                UPDATE contact_dedup_candidates
                SET review_outcome = 'rejected_false_positive',
                    reviewed_by = :uid,
                    reviewed_at = NOW()
                WHERE id = :id AND TRIM(tenant_id) = :tid
            """),
            {"id": candidate_id, "tid": tid, "uid": sess["user_id"]},
        )
        await db.commit()
    return RedirectResponse("/contacts/dedup-review", status_code=303)


# ===========================================================================
# PROPOSALS QUEUE
# ===========================================================================
@router.get("/proposals-review", response_class=HTMLResponse)
async def proposals_list(
    request: Request,
    min_confidence: float = 0.0,
    signal_type: Optional[str] = None,
):
    sess = await _require_admin(request)
    tid = sess["tenant_id"]

    where = ["TRIM(p.tenant_id) = :tid", "p.review_status = 'pending'",
             "p.confidence >= :minc"]
    params: Dict[str, Any] = {"tid": tid, "minc": min_confidence}
    if signal_type:
        where.append("p.signal_type = :sig")
        params["sig"] = signal_type

    sql = f"""
        SELECT
            p.id           AS proposal_id,
            p.contact_id,
            p.matter_id,
            p.proposed_role,
            p.proposed_is_primary,
            p.signal_type,
            p.confidence,
            p.signal_count,
            LEFT(p.notes, 250) AS notes,
            p.created_at,
            c.full_name    AS contact_name,
            c.email        AS contact_email,
            c.phone        AS contact_phone,
            c.company      AS contact_company,
            c.contact_type,
            m.matter_number,
            m.matter_name,
            cl.client_name
        FROM matter_contact_proposals p
        JOIN contacts c ON c.id = p.contact_id
        JOIN matters m  ON m.id = p.matter_id
        LEFT JOIN clients cl ON cl.id = m.client_id
        WHERE {' AND '.join(where)}
        ORDER BY p.confidence DESC, p.signal_count DESC, c.full_name
        LIMIT 500
    """

    async with AsyncSessionLocal() as db:
        r = await db.execute(text(sql), params)
        proposals = r.fetchall()

        r = await db.execute(
            text("""
                SELECT
                  COUNT(*) FILTER (WHERE confidence >= 1.00) AS n_perfect,
                  COUNT(*) FILTER (WHERE confidence >= 0.95) AS n_high,
                  COUNT(*) FILTER (WHERE confidence >= 0.85) AS n_strong,
                  COUNT(*) AS n_total
                FROM matter_contact_proposals
                WHERE TRIM(tenant_id) = :tid
                  AND review_status = 'pending'
            """),
            {"tid": tid},
        )
        agg = r.fetchone()

        r = await db.execute(
            text("""
                SELECT DISTINCT signal_type
                FROM matter_contact_proposals
                WHERE TRIM(tenant_id) = :tid
                  AND review_status = 'pending'
                ORDER BY signal_type
            """),
            {"tid": tid},
        )
        signal_types = [row.signal_type for row in r.fetchall()]

    return templates.TemplateResponse(
        request, "contacts/proposals_review.html",
        _ctx(request, sess,
             proposals=proposals,
             min_confidence=min_confidence,
             signal_type=signal_type,
             signal_types=signal_types,
             counts={
                 "n_perfect": int(agg.n_perfect or 0) if agg else 0,
                 "n_high": int(agg.n_high or 0) if agg else 0,
                 "n_strong": int(agg.n_strong or 0) if agg else 0,
                 "n_total": int(agg.n_total or 0) if agg else 0,
             }),
    )


async def _promote_proposal(db, tenant_id: str, user_id: int,
                             proposal_id: str) -> Optional[int]:
    r = await db.execute(
        text("""
            SELECT contact_id, matter_id, proposed_role, proposed_is_primary
            FROM matter_contact_proposals
            WHERE id = :id
              AND TRIM(tenant_id) = :tid
              AND review_status = 'pending'
        """),
        {"id": proposal_id, "tid": tenant_id},
    )
    p = r.fetchone()
    if not p:
        return None

    r = await db.execute(
        text("""
            SELECT id FROM matter_contacts
            WHERE contact_id = :cid
              AND matter_id = :mid
              AND TRIM(tenant_id) = :tid
        """),
        {"cid": p.contact_id, "mid": p.matter_id, "tid": tenant_id},
    )
    existing = r.fetchone()

    if existing:
        new_link_id = existing.id
    else:
        r = await db.execute(
            text("""
                INSERT INTO matter_contacts
                    (tenant_id, matter_id, contact_id, role, is_primary)
                VALUES
                    (:tid, :mid, :cid, :role, :primary)
                RETURNING id
            """),
            {
                "tid": tenant_id,
                "mid": p.matter_id,
                "cid": p.contact_id,
                "role": p.proposed_role or "client",
                "primary": p.proposed_is_primary or "N",
            },
        )
        new_link_id = int(r.scalar())

    await db.execute(
        text("""
            UPDATE matter_contact_proposals
            SET review_status = 'approved',
                reviewed_by = :uid,
                reviewed_at = NOW(),
                promoted_matter_contact_id = :link_id,
                promoted_at = NOW()
            WHERE id = :id
        """),
        {
            "uid": user_id,
            "link_id": new_link_id,
            "id": proposal_id,
        },
    )
    return new_link_id


@router.post("/proposals-review/{proposal_id}/approve")
async def proposal_approve(request: Request, proposal_id: str):
    sess = await _require_admin(request)
    async with AsyncSessionLocal() as db:
        await _promote_proposal(db, sess["tenant_id"], sess["user_id"], proposal_id)
        await db.commit()
    return RedirectResponse("/contacts/proposals-review", status_code=303)


@router.post("/proposals-review/{proposal_id}/reject")
async def proposal_reject(request: Request, proposal_id: str):
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""
                UPDATE matter_contact_proposals
                SET review_status = 'rejected',
                    reviewed_by = :uid,
                    reviewed_at = NOW()
                WHERE id = :id AND TRIM(tenant_id) = :tid
            """),
            {"id": proposal_id, "tid": tid, "uid": sess["user_id"]},
        )
        await db.commit()
    return RedirectResponse("/contacts/proposals-review", status_code=303)


@router.post("/proposals-review/bulk-approve")
async def proposals_bulk_approve(
    request: Request,
    min_confidence: float = Form(1.00),
    signal_type: Optional[str] = Form(None),
):
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    user_id = sess["user_id"]

    where = ["TRIM(tenant_id) = :tid", "review_status = 'pending'",
             "confidence >= :minc"]
    params: Dict[str, Any] = {"tid": tid, "minc": min_confidence}
    if signal_type:
        where.append("signal_type = :sig")
        params["sig"] = signal_type

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text(f"""
                SELECT id FROM matter_contact_proposals
                WHERE {' AND '.join(where)}
                ORDER BY confidence DESC
            """),
            params,
        )
        ids = [row.id for row in r.fetchall()]
        log.info("Bulk approve: %d proposals (min_conf=%s, sig=%s)",
                 len(ids), min_confidence, signal_type)
        for pid in ids:
            try:
                await _promote_proposal(db, tid, user_id, pid)
            except Exception as exc:
                log.warning("Promote %s failed: %s", pid, exc)
        await db.commit()
    return RedirectResponse("/contacts/proposals-review", status_code=303)


# ===========================================================================
# === v4 accept-all + approve-selected ===
# ===========================================================================
@router.post("/dedup-review/accept-all")
async def dedup_accept_all(request: Request):
    """Accept every pending dedup cluster using its heuristic suggested
    canonical. Iterates over candidates exactly the way the list view
    displays them so the UX matches what the user sees.
    """
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    user_id = sess["user_id"]

    async with AsyncSessionLocal() as db:
        # Pull every pending candidate
        r = await db.execute(
            text("""
                SELECT id, contact_ids
                FROM contact_dedup_candidates
                WHERE TRIM(tenant_id) = :tid
                  AND review_outcome IS NULL
                ORDER BY confidence DESC, detected_at DESC
            """),
            {"tid": tid},
        )
        candidates = r.fetchall()

        # Hydrate every contact id once
        all_ids: set = set()
        for c in candidates:
            ids = c.contact_ids or []
            if isinstance(ids, list):
                all_ids.update(int(i) for i in ids)
        contacts_by_id: Dict[int, Any] = {}
        if all_ids:
            r = await db.execute(
                text("""
                    SELECT id, full_name, email, phone, contact_type,
                           (SELECT COUNT(*) FROM matter_contacts mc
                            WHERE mc.contact_id = c.id
                              AND TRIM(mc.tenant_id) = TRIM(c.tenant_id))
                            AS n_matters
                    FROM contacts c
                    WHERE TRIM(c.tenant_id) = :tid
                      AND c.id = ANY(:ids)
                """),
                {"tid": tid, "ids": list(all_ids)},
            )
            for row in r.fetchall():
                contacts_by_id[int(row.id)] = row

        merged = 0
        skipped = 0
        archived_total: set = set()

        for c in candidates:
            ids = c.contact_ids or []
            if not isinstance(ids, list):
                skipped += 1
                continue
            cluster_contacts = [
                contacts_by_id[int(i)]
                for i in ids
                if int(i) in contacts_by_id
            ]
            if len(cluster_contacts) < 2:
                skipped += 1
                continue
            # Skip if this cluster overlaps with one already merged in this
            # batch (the contact_ids may have been archived already)
            cluster_id_set = {int(i) for i in ids}
            if cluster_id_set & archived_total:
                # Mark as superseded — losers were already merged elsewhere
                await db.execute(
                    text("""
                        UPDATE contact_dedup_candidates
                        SET review_outcome = 'merged',
                            reviewed_by = :uid,
                            reviewed_at = NOW()
                        WHERE id = :id
                    """),
                    {"uid": user_id, "id": c.id},
                )
                skipped += 1
                continue

            canonical_id = _pick_canonical(cluster_contacts)
            if not canonical_id:
                skipped += 1
                continue

            loser_ids = [int(i) for i in ids if int(i) != canonical_id]
            if not loser_ids:
                skipped += 1
                continue

            # Re-point matter_contacts (with uniqueness conflict handling)
            for loser_id in loser_ids:
                r = await db.execute(
                    text("""
                        SELECT id, matter_id
                        FROM matter_contacts
                        WHERE contact_id = :loser
                          AND TRIM(tenant_id) = :tid
                    """),
                    {"loser": loser_id, "tid": tid},
                )
                for link in r.fetchall():
                    r2 = await db.execute(
                        text("""
                            SELECT id FROM matter_contacts
                            WHERE contact_id = :canon
                              AND matter_id = :mid
                              AND TRIM(tenant_id) = :tid
                        """),
                        {
                            "canon": canonical_id,
                            "mid": link.matter_id,
                            "tid": tid,
                        },
                    )
                    if r2.fetchone():
                        await db.execute(
                            text("DELETE FROM matter_contacts WHERE id = :id"),
                            {"id": link.id},
                        )
                    else:
                        await db.execute(
                            text("""
                                UPDATE matter_contacts
                                SET contact_id = :canon
                                WHERE id = :id
                            """),
                            {"canon": canonical_id, "id": link.id},
                        )

            # Supersede losers' pending proposals
            await db.execute(
                text("""
                    UPDATE matter_contact_proposals
                    SET review_status = 'superseded',
                        reviewed_by = :uid,
                        reviewed_at = NOW(),
                        review_notes = :note
                    WHERE contact_id = ANY(:losers)
                      AND TRIM(tenant_id) = :tid
                      AND review_status = 'pending'
                """),
                {
                    "losers": loser_ids,
                    "tid": tid,
                    "uid": user_id,
                    "note": f"superseded by accept-all merge into "
                            f"contact_id={canonical_id}",
                },
            )

            # Archive losers
            await db.execute(
                text("""
                    UPDATE contacts
                    SET contact_type = 'archived', updated_at = NOW()
                    WHERE id = ANY(:ids)
                      AND TRIM(tenant_id) = :tid
                """),
                {"ids": loser_ids, "tid": tid},
            )
            archived_total.update(loser_ids)

            # Mark the candidate merged
            await db.execute(
                text("""
                    UPDATE contact_dedup_candidates
                    SET review_outcome = 'merged',
                        canonical_contact_id = :canon,
                        reviewed_by = :uid,
                        reviewed_at = NOW()
                    WHERE id = :id
                """),
                {
                    "canon": canonical_id,
                    "uid": user_id,
                    "id": c.id,
                },
            )
            merged += 1

        await db.commit()

    log.info("Accept-all dedup: merged=%d skipped=%d archived=%d",
             merged, skipped, len(archived_total))
    return RedirectResponse("/contacts/dedup-review", status_code=303)


@router.post("/proposals-review/approve-selected")
async def proposals_approve_selected(request: Request):
    """Approve a list of proposal ids submitted as repeated form fields
    (HTML checkbox semantics). Field name: 'proposal_id' (multiple).
    """
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    user_id = sess["user_id"]

    form = await request.form()
    raw_ids = form.getlist("proposal_id")
    proposal_ids = [pid for pid in raw_ids if pid]

    if not proposal_ids:
        log.info("approve-selected: no proposals selected")
        return RedirectResponse("/contacts/proposals-review", status_code=303)

    promoted = 0
    failed = 0
    async with AsyncSessionLocal() as db:
        for pid in proposal_ids:
            try:
                result = await _promote_proposal(db, tid, user_id, pid)
                if result is not None:
                    promoted += 1
            except Exception as exc:
                log.warning("approve-selected: %s failed: %s", pid, exc)
                failed += 1
        await db.commit()
    log.info("approve-selected: %d promoted, %d failed (of %d submitted)",
             promoted, failed, len(proposal_ids))
    return RedirectResponse("/contacts/proposals-review", status_code=303)
