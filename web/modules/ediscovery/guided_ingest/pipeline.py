"""
modules/ediscovery/guided_ingest/pipeline.py

Orchestration for guided ingestion: PROPOSE (observe -> deterministic family
classify -> frontier escalation of the residue -> persist) and CONFIRM (create
one collection per logical unit -> enqueue run_collection_full per unit).

The frontier escalation goes through the real AI layer
(modules.intelligence.anthropic_adapter.call, module='ediscovery',
purpose='ingest_triage' -> claude-sonnet-4, haiku fallback), so routing, the
tenant BYOK key, budget caps and the cost ledger all apply. Only the small
deterministic-classifier RESIDUE is sent (filenames only).
"""
import os
import json
import logging

from sqlalchemy import text
from core.db.base import AsyncSessionLocal
from modules.ediscovery.guided_ingest.observe import (
    observe, classify_loose_files)

logger = logging.getLogger(__name__)

EDISCOVERY_ROOT = os.environ.get("CIFS_EDISCOVERY_MOUNT", "/mnt/ediscovery")

# bucket -> onboarding_clusters.cluster_type vocabulary (alignment w/ alerts build)
BUCKET_CLUSTER_TYPE = {
    "pst": "pst_mailstore",
    "loadfile": "production",
    "loose": "client_documents",
}

_TRIAGE_SYS = (
    "You are a senior eDiscovery onboarding attorney's assistant. A client "
    "DOCUMENTS folder for a commercial-litigation matter is being onboarded; "
    "client files go to the DMS first, eDiscovery second. A deterministic "
    "scanner already placed the obvious families. For each REMAINING file-pattern "
    "group decide:\n"
    "  dms = ordinary client/business record -> DMS.\n"
    "  promote = communication or likely evidence -> eDiscovery collection.\n"
    "  uncertain = cannot tell from the name; needs human review.\n"
    "Use domain knowledge. Output STRICT JSON ONLY: "
    '{"labels":{"<name_pattern>":"dms|promote|uncertain"},'
    '"notes":{"<name_pattern>":"one-line reason"},"summary":"..."}'
)


async def escalate_residue(tenant_id, matter_id, residue_groups, user_id, existing_summary=""):
    """Send the deterministic residue to the frontier model via the real AI
    layer. Returns {} when there is no residue. Never raises -- on any AI error
    every residue group falls back to 'uncertain' (human review)."""
    if not residue_groups:
        return {"labels": {}, "notes": {}, "summary": "", "escalated": False}
    from modules.intelligence.anthropic_adapter import (
        call, AICallContext, strip_markdown_fences)
    payload = [{"name_pattern": g["name_pattern"], "count": g["count"],
                "example": g["example"]} for g in residue_groups]
    user = ("Matter onboarding. Unmatched client-file pattern groups "
            "(name_pattern, count, example):\n"
            + json.dumps(payload, ensure_ascii=False)
            + ("\n\nAlready ingested for this matter (do NOT propose re-ingesting these; context only): " + existing_summary
               if existing_summary else "")
            + "\n\nReturn the labels JSON for every name_pattern.")
    ctx = AICallContext(tenant_id=tenant_id, module="ediscovery",
                        purpose="ingest_triage", matter_id=matter_id,
                        user_id=user_id, user_role="attorney")
    try:
        res = await call(ctx, raw_user_prompt=user, raw_system_prompt=_TRIAGE_SYS)
        parsed = json.loads(strip_markdown_fences(res.text))
        return {
            "labels": parsed.get("labels") or {},
            "notes": parsed.get("notes") or {},
            "summary": parsed.get("summary") or "",
            "escalated": True,
            "model_used": res.model_used,
            "cost_usd": str(res.cost_usd),
            "call_id": res.call_id,
        }
    except Exception as e:
        logger.warning("residue escalation failed (-> uncertain): %s", e)
        return {"labels": {}, "notes": {}, "summary": "",
                "escalated": False, "error": str(e)[:300]}


def _apply_split(collection, classified, escalation):
    """Attach the DMS-vs-promote split to a loose (client_files) collection.
    Deterministic guarantees: email/dms rules are locked; only the residue takes
    the frontier label, and anything the model omits/mislabels -> uncertain."""
    labels = escalation.get("labels", {})
    notes = escalation.get("notes", {})
    dms = list(classified["dms"])
    promote = list(classified["promote"])
    uncertain = []
    for g in classified["residue"]:
        lab = str(labels.get(g["name_pattern"], "uncertain")).lower()
        g = dict(g, frontier_reason=notes.get(g["name_pattern"], ""))
        if lab == "dms":
            dms.append(g)
        elif lab == "promote":
            promote.append(g)
        else:
            uncertain.append(g)

    def nfiles(groups):
        return sum(g["count"] for g in groups)
    collection["split"] = {
        "dms_groups": dms, "promote_groups": promote, "uncertain_groups": uncertain,
        "dms_files": nfiles(dms), "promote_files": nfiles(promote),
        "uncertain_files": nfiles(uncertain),
        "escalation": {k: escalation.get(k) for k in
                       ("escalated", "model_used", "cost_usd", "call_id", "summary")},
    }


async def _existing_collections(tenant_id, matter_id):
    """Collections already ingested / in-flight for this matter — the dedup
    substrate so a re-run does not duplicate work."""
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text("""
            SELECT id::text, COALESCE(collection_name, name) AS name, custodian,
                   bucket, dms_source_path, stated_bates_range, total_docs, status
              FROM ediscovery_collections
             WHERE TRIM(tenant_id) = :tid AND matter_id = CAST(:mid AS uuid)
               AND COALESCE(status, '') NOT IN ('failed', 'deleted')
        """), {"tid": tenant_id, "mid": matter_id})).mappings().all()
    return [dict(r) for r in rows]


def _norm(p):
    return (p or "").rstrip("/").lower()


def _annotate_duplicates(collections, existing):
    """Flag proposed units that match something already ingested (same custodian
    for PST, or overlapping source path otherwise). Matches default to excluded
    (_skip) so re-including is an explicit human choice in the confirm screen."""
    ex_paths = {_norm(e.get("dms_source_path")) for e in existing if e.get("dms_source_path")}
    ex_cust = {(e.get("custodian") or "").strip().lower()
               for e in existing if e.get("bucket") == "pst" and e.get("custodian")}
    for c in collections:
        match = None
        if c.get("bucket") == "pst" and (c.get("custodian") or "").strip().lower() in ex_cust:
            match = "custodian already ingested"
        else:
            for sp in c.get("source_paths", []):
                n = _norm(sp)
                if n in ex_paths or any(n.startswith(p + "/") or p.startswith(n + "/")
                                        for p in ex_paths if p):
                    match = "source path already ingested"
                    break
        if match:
            c["duplicate"] = True
            c["duplicate_reason"] = match
            c["_skip"] = True
    return collections


async def build_proposal(proposal_id, tenant_id, matter_id, source_paths, user_id):
    """rq job body (async). Runs the full propose pass and writes the proposal
    row. Sets status 'ready' on success, 'error' on failure."""
    try:
        obs = observe(source_paths)
        loose_files = obs.pop("loose_files_by_root", {})
        existing = await _existing_collections(tenant_id, matter_id)
        _existing_summary = "; ".join(
            (e.get("name") or e.get("custodian") or "?") for e in existing[:30]) or "none"
        for c in obs["collections"]:
            if c["bucket"] != "loose":
                continue
            files = []
            for root in c["source_paths"]:
                files += loose_files.get(root, [])
            if not files:
                continue
            classified = classify_loose_files(files)
            escalation = await escalate_residue(
                tenant_id, matter_id, classified["residue"], user_id,
                existing_summary=_existing_summary)
            _apply_split(c, classified, escalation)
            c["cluster_type"] = BUCKET_CLUSTER_TYPE.get(c["bucket"])
        for c in obs["collections"]:
            c.setdefault("cluster_type", BUCKET_CLUSTER_TYPE.get(c["bucket"]))

        proposal = {"collections": obs["collections"], "flagged": obs["flagged"]}
        _annotate_duplicates(proposal["collections"], existing)
        proposal["existing_ingestions"] = existing
        async with AsyncSessionLocal() as s:
            await s.execute(text("""
                UPDATE collection_proposals
                   SET proposal = CAST(:prop AS jsonb),
                       inventory = CAST(:inv AS jsonb),
                       status = 'ready', updated_at = now()
                 WHERE id = CAST(:pid AS uuid)
            """), {"prop": json.dumps(proposal), "inv": json.dumps(obs["inventory"]),
                   "pid": proposal_id})
            await s.commit()
        logger.info("guided proposal %s ready: %d collections, %d flagged",
                    proposal_id, len(obs["collections"]), len(obs["flagged"]))
        return {"proposal_id": proposal_id, "status": "ready"}
    except Exception as e:
        logger.exception("build_proposal %s failed", proposal_id)
        async with AsyncSessionLocal() as s:
            await s.execute(text("""
                UPDATE collection_proposals
                   SET status='error',
                       inventory = CAST(:inv AS jsonb), updated_at=now()
                 WHERE id = CAST(:pid AS uuid)
            """), {"inv": json.dumps({"error": str(e)[:500]}), "pid": proposal_id})
            await s.commit()
        return {"proposal_id": proposal_id, "status": "error", "error": str(e)[:300]}


def _stage_sources(tenant_id, matter_id, collection_id, source_paths):
    """Single dir source -> use it directly. Otherwise symlink each source into a
    per-collection staging dir (non-destructive) and return that dir. The DAG's
    intake copies from this path."""
    if len(source_paths) == 1 and os.path.isdir(source_paths[0]):
        return source_paths[0]
    staging = os.path.join(EDISCOVERY_ROOT, tenant_id, matter_id, "_guided",
                           str(collection_id), "as_received")
    os.makedirs(staging, exist_ok=True)
    for sp in source_paths:
        link = os.path.join(staging, os.path.basename(sp.rstrip("/")))
        try:
            if not os.path.lexists(link):
                os.symlink(sp, link)
        except OSError as e:
            logger.warning("stage symlink %s -> %s failed: %s", sp, link, e)
    return staging


async def _create_unit_collection(session, tenant_id, matter_id, proposal_id,
                                   col, user_id):
    storage = os.path.join(EDISCOVERY_ROOT, tenant_id, matter_id, "_guided",
                           "pending")
    # placeholder; reset to per-collection once id is known
    result = await session.execute(text("""
        INSERT INTO ediscovery_collections
            (tenant_id, matter_id, name, collection_name, status,
             source_type, source_party, custodian, bucket, proposal_id,
             received_by, created_at, updated_at)
        VALUES
            (:tid, CAST(:mid AS uuid), :name, :name, 'collecting',
             :src_type, :src_party, :cust, :bucket, CAST(:pid AS uuid),
             :uid, NOW(), NOW())
        RETURNING id::text
    """), {
        "tid": tenant_id, "mid": matter_id, "name": col["name"],
        "src_type": "opposing_production" if col["track"] == "ediscovery"
                    else "client_documents",
        "src_party": col.get("custodian"),
        "cust": col.get("custodian"), "bucket": col.get("bucket"),
        "pid": proposal_id, "uid": user_id,
    })
    cid = result.scalar()
    dms_source_path = _stage_sources(tenant_id, matter_id, cid, col["source_paths"])
    storage = os.path.join(EDISCOVERY_ROOT, tenant_id, matter_id, "_guided", cid)
    await session.execute(text("""
        UPDATE ediscovery_collections
           SET dms_source_path = :src, storage_path = :storage, updated_at = NOW()
         WHERE id = CAST(:cid AS uuid)
    """), {"src": dms_source_path, "storage": storage, "cid": cid})
    return cid


async def confirm_proposal(tenant_id, proposal_id, edited_proposal, user_id):
    """Create one collection per confirmed unit and enqueue the DAG for each.
    Client-files units are enqueued first, eDiscovery units second (files-first
    sequencing). Returns the created collection ids by track."""
    from modules.ediscovery.routes.upload import _enqueue_ingest

    collections = (edited_proposal or {}).get("collections", [])
    ordered = ([c for c in collections if c.get("track") == "client_files"]
               + [c for c in collections if c.get("track") != "client_files"])
    created = {"client_files": [], "ediscovery": []}
    async with AsyncSessionLocal() as s:
        for col in ordered:
            if col.get("_skip"):
                continue
            cid = await _create_unit_collection(
                s, tenant_id, matter_id_of(col, edited_proposal), proposal_id,
                col, user_id)
            created.setdefault(col.get("track", "ediscovery"), []).append(cid)
        await s.execute(text("""
            UPDATE collection_proposals SET status='executed', updated_at=now()
             WHERE id = CAST(:pid AS uuid)
        """), {"pid": proposal_id})
        await s.commit()
    # enqueue after commit so the rows are visible to the worker
    for track in ("client_files", "ediscovery"):
        for cid in created.get(track, []):
            _enqueue_ingest(tenant_id, str(cid), user_id)
    return created


def matter_id_of(col, proposal):
    return proposal.get("matter_id") or col.get("matter_id")
