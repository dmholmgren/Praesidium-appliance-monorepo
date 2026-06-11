"""
jobs/go_live_flips.py
=====================

Component 2 - "go live subject to review". Two mutations, one transaction:

A) SPINE: bless live proposed allegations + case_topics -> status='accepted'
   (Marcus matter). 'accepted' is the writer's protected state: non-accepted
   rows are superseded on a re-seed, so blessing also protects them. The AI
   defaults become the working set; the review surface demotes later.

B) CONTACTS: promote pending matter_contact_proposals -> matter_contacts
   (status default 'confirmed', source='go_live_promote', confidence carried),
   marking each proposal approved/promoted. Upsert-safe on (matter, contact).

PII: no-op. No consumer/flag gates document_entities; rows are live as
extracted. The review surface (component 3) reads them directly.

Modes:  (default) dry-run, writes nothing | --apply
Pre-images dumped to /tmp/go_live_backup_<ts>.json before mutation.

Patent Pending - Series 2/3 - D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any, Dict, List

from sqlalchemy import text
from core.db.base import AsyncSessionLocal

TENANT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
MARCUS = "d389d889-4fe9-41c9-b74e-622d93d244f8"


async def spine_plan(db) -> Dict[str, Any]:
    a_ids = [r.id for r in (await db.execute(text("""
        SELECT id FROM allegations
        WHERE matter_id = CAST(:m AS uuid) AND superseded_by_run_id IS NULL
          AND status = 'proposed'
    """), {"m": MARCUS})).fetchall()]
    t_ids = [r.id for r in (await db.execute(text("""
        SELECT id FROM case_topics
        WHERE matter_id = CAST(:m AS uuid) AND superseded_by_run_id IS NULL
          AND status = 'proposed'
    """), {"m": MARCUS})).fetchall()]
    conf = (await db.execute(text("""
        SELECT
          count(*) FILTER (WHERE confidence >= 0.8) AS hi,
          count(*) FILTER (WHERE confidence >= 0.5 AND confidence < 0.8) AS mid,
          count(*) FILTER (WHERE confidence < 0.5) AS lo,
          count(*) - count(DISTINCT (source_doc_id, source_char_start,
                    source_char_end, allegation_text)) AS exact_dupes
        FROM allegations
        WHERE matter_id = CAST(:m AS uuid) AND superseded_by_run_id IS NULL
          AND status = 'proposed'
    """), {"m": MARCUS})).mappings().fetchone()
    return {"alleg_ids": a_ids, "topic_ids": t_ids, "conf": dict(conf)}


async def contacts_plan(db) -> List[Dict[str, Any]]:
    rows = (await db.execute(text("""
        SELECT p.id, p.contact_id, p.matter_id, p.proposed_role,
               p.proposed_is_primary, p.confidence
        FROM matter_contact_proposals p
        JOIN contacts c ON c.id = p.contact_id
        WHERE TRIM(p.tenant_id) = :tid
          AND p.review_status = 'pending'
          AND COALESCE(c.contact_type,'') <> 'archived'
        ORDER BY p.matter_id, p.contact_id
    """), {"tid": TENANT})).mappings().fetchall()
    return [dict(r) for r in rows]


async def main(apply: bool):
    ts = time.strftime("%Y%m%d-%H%M%S")
    async with AsyncSessionLocal() as db:
        sp = await spine_plan(db)
        cp = await contacts_plan(db)

        print(f"== go-live flips {'APPLY' if apply else 'DRY-RUN'} ==\n")
        print("A) SPINE (Marcus) proposed -> accepted")
        print(f"   allegations: {len(sp['alleg_ids'])}   topics: {len(sp['topic_ids'])}")
        c = sp["conf"]
        print(f"   allegation confidence: hi(>=.8)={c['hi']} "
              f"mid={c['mid']} lo(<.5)={c['lo']}  exact-dupes={c['exact_dupes']}")
        print(f"\nB) CONTACTS promote pending proposals -> matter_contacts")
        print(f"   pending (live contact): {len(cp)}")
        print(f"\nPII: no-op (already live; no gating flag/consumer).")

        if not apply:
            print("\nDRY-RUN only. Re-run with --apply to execute.")
            return

        backup = {"ts": ts, "alleg_ids": [str(i) for i in sp["alleg_ids"]],
                  "topic_ids": [str(i) for i in sp["topic_ids"]],
                  "promoted_proposal_ids": [str(p["id"]) for p in cp]}
        with open(f"/tmp/go_live_backup_{ts}.json", "w") as f:
            json.dump(backup, f, indent=2, default=str)
        print(f"\nbackup -> /tmp/go_live_backup_{ts}.json")

        # A) spine
        await db.execute(text("""
            UPDATE allegations SET status='accepted', updated_at=NOW()
            WHERE matter_id = CAST(:m AS uuid) AND superseded_by_run_id IS NULL
              AND status = 'proposed'
        """), {"m": MARCUS})
        await db.execute(text("""
            UPDATE case_topics SET status='accepted'
            WHERE matter_id = CAST(:m AS uuid) AND superseded_by_run_id IS NULL
              AND status = 'proposed'
        """), {"m": MARCUS})

        # B) contacts promote
        promoted = 0
        for p in cp:
            ex = (await db.execute(text("""
                SELECT id FROM matter_contacts
                WHERE contact_id = :cid AND matter_id = :mid
                  AND TRIM(tenant_id) = :tid
            """), {"cid": p["contact_id"], "mid": p["matter_id"], "tid": TENANT})).fetchone()
            if ex:
                link_id = ex.id
            else:
                link_id = (await db.execute(text("""
                    INSERT INTO matter_contacts
                        (tenant_id, matter_id, contact_id, role, is_primary,
                         source, confidence)
                    VALUES (CAST(:tid AS varchar), :mid, :cid, :role, :prim,
                            'go_live_promote', :conf)
                    RETURNING id
                """), {"tid": TENANT, "mid": p["matter_id"], "cid": p["contact_id"],
                       "role": p["proposed_role"] or "party",
                       "prim": p["proposed_is_primary"] or "N",
                       "conf": p["confidence"]})).scalar()
                promoted += 1
            await db.execute(text("""
                UPDATE matter_contact_proposals
                SET review_status='approved', reviewed_at=NOW(),
                    promoted_matter_contact_id=:lid, promoted_at=NOW(),
                    review_notes='go-live bulk promote (AI default, subject to review)'
                WHERE id = :id
            """), {"lid": link_id, "id": p["id"]})

        await db.commit()
        print(f"\nAPPLIED: blessed {len(sp['alleg_ids'])} allegations + "
              f"{len(sp['topic_ids'])} topics; promoted {len(cp)} proposals "
              f"({promoted} new matter_contacts links).")


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
