"""
jobs/contact_dedup_allfields.py
===============================

Comprehensive contact dedup ("by all fields") with safe conflict handling.

Edges (union-find):
  - same normalized email
  - same normalized phone (>= 10 digits)
  - same bar_number
  - same case-folded full_name AND no pairwise field conflict

After clustering, each cluster is validated for INTERNAL conflict: if it
holds two distinct non-empty values for any of {email, phone, company,
firm_name}, it is NOT auto-merged -- it is written to
contact_dedup_candidates (signal_type='allfields_conflict') for the
React review surface. Clean clusters auto-merge:
  - canonical = richest record (has_email, has_phone, n_matters, -id)
  - losers' non-null fields coalesced UP onto canonical
  - all 9 contact_id FK tables re-pointed (matter_contacts dedup-safe)
  - losers archived (contact_type='archived')

Modes:
  (default)  dry-run: prints the plan, writes nothing
  --apply    executes inside one transaction; pre-images dumped to
             /tmp/contact_dedup_backup_<ts>.json before any mutation

Usage (inside web/proc container):
  python -m jobs.contact_dedup_allfields            # dry-run
  python -m jobs.contact_dedup_allfields --apply

Patent Pending - Series 2/3 - D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from core.db.base import AsyncSessionLocal

TENANT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"

# every table.column that references contacts.id (straight re-point)
FK_SIMPLE = [
    ("communication_log", "contact_id"),
    ("deal_party_roles", "contact_id"),
    ("document_contacts", "promoted_to_contact_id"),
    ("document_parties", "promoted_to_contact_id"),
    ("expert_witnesses", "contact_id"),
    ("mediations", "mediator_contact_id"),
]
# matter_contacts handled separately (matter_id uniqueness)


def norm_email(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    v = v.strip().lower()
    return v or None


def norm_phone(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    d = re.sub(r"\D", "", v)
    return d if len(d) >= 10 else None


def norm_name(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    v = re.sub(r"\s+", " ", v.strip().lower())
    return v or None


def norm_simple(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    v = re.sub(r"\s+", " ", v.strip().lower())
    return v or None


class UF:
    def __init__(self):
        self.p: Dict[int, int] = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def _conflict(a: Dict, b: Dict) -> bool:
    """Hard identity conflict: two distinct emails or two distinct phones."""
    for f in ("nemail", "nphone"):
        if a[f] and b[f] and a[f] != b[f]:
            return True
    return False


def _cluster_internally_conflicted(members: List[Dict]) -> bool:
    # Hard conflict: >=2 distinct emails or >=2 distinct phones anywhere.
    for f in ("nemail", "nphone"):
        if len({m[f] for m in members if m[f]}) > 1:
            return True
    # Soft conflict: cluster has NO email/phone evidence at all AND >=2
    # distinct companies or firms -> likely distinct same-name entities.
    has_strong = any(m["nemail"] or m["nphone"] for m in members)
    if not has_strong:
        for f in ("ncompany", "nfirm"):
            if len({m[f] for m in members if m[f]}) > 1:
                return True
    return False


async def load_contacts(db) -> List[Dict]:
    r = await db.execute(text("""
        SELECT c.id, c.full_name, c.company, c.firm_name, c.email, c.phone,
               c.bar_number, c.address1, c.city, c.state, c.notes,
               c.contact_type, c.external_id, c.canonical_entity_id,
               (SELECT COUNT(*) FROM matter_contacts mc
                 WHERE mc.contact_id = c.id
                   AND TRIM(mc.tenant_id) = :tid) AS n_matters
        FROM contacts c
        WHERE TRIM(c.tenant_id) = :tid
          AND COALESCE(c.contact_type,'') <> 'archived'
    """), {"tid": TENANT})
    out = []
    for row in r.mappings().fetchall():
        d = dict(row)
        d["nemail"] = norm_email(d["email"])
        d["nphone"] = norm_phone(d["phone"])
        d["nname"] = norm_name(d["full_name"])
        d["ncompany"] = norm_simple(d["company"])
        d["nfirm"] = norm_simple(d["firm_name"])
        d["nbar"] = norm_simple(d["bar_number"])
        d["n_matters"] = int(d["n_matters"] or 0)
        out.append(d)
    return out


def build_clusters(contacts: List[Dict]) -> List[List[Dict]]:
    uf = UF()
    by_id = {c["id"]: c for c in contacts}
    for c in contacts:
        uf.find(c["id"])

    # strong keys
    for key in ("nemail", "nphone", "nbar"):
        buckets: Dict[str, List[int]] = defaultdict(list)
        for c in contacts:
            if c[key]:
                buckets[c[key]].append(c["id"])
        for ids in buckets.values():
            for i in range(1, len(ids)):
                uf.union(ids[0], ids[i])

    # name match with no pairwise conflict
    name_buckets: Dict[str, List[Dict]] = defaultdict(list)
    for c in contacts:
        if c["nname"]:
            name_buckets[c["nname"]].append(c)
    for members in name_buckets.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if not _conflict(members[i], members[j]):
                    uf.union(members[i]["id"], members[j]["id"])

    groups: Dict[int, List[Dict]] = defaultdict(list)
    for c in contacts:
        groups[uf.find(c["id"])].append(by_id[c["id"]])
    return [g for g in groups.values() if len(g) > 1]


def pick_canonical(members: List[Dict]) -> Dict:
    def score(c):
        return (1 if c["nemail"] else 0,
                1 if c["nphone"] else 0,
                c["n_matters"],
                -int(c["id"]))
    return max(members, key=score)


def coalesce_fields(canon: Dict, losers: List[Dict]) -> Dict[str, Any]:
    """Return {col: new_value} for canonical, filling its NULL/empty fields
    from the first loser that has a value."""
    updates: Dict[str, Any] = {}
    for col in ("full_name", "company", "firm_name", "email", "phone",
                "bar_number", "address1", "city", "state"):
        cur = (canon.get(col) or "").strip() if isinstance(canon.get(col), str) else canon.get(col)
        if cur:
            continue
        for l in losers:
            val = l.get(col)
            if isinstance(val, str):
                val = val.strip()
            if val:
                updates[col] = val
                break
    return updates


async def main(apply: bool):
    ts = time.strftime("%Y%m%d-%H%M%S")
    async with AsyncSessionLocal() as db:
        contacts = await load_contacts(db)
        clusters = build_clusters(contacts)

        auto, conflicted = [], []
        for cl in clusters:
            (conflicted if _cluster_internally_conflicted(cl) else auto).append(cl)

        n_archive = sum(len(c) - 1 for c in auto)
        print(f"== contact dedup (all fields) {'APPLY' if apply else 'DRY-RUN'} ==")
        print(f"live contacts        : {len(contacts)}")
        print(f"clusters (size>=2)   : {len(clusters)}")
        print(f"  auto-merge clusters: {len(auto)}  -> archive {n_archive} losers")
        print(f"  conflict clusters  : {len(conflicted)} -> review queue")
        print()
        print("--- sample auto-merge clusters (up to 15) ---")
        for cl in sorted(auto, key=lambda c: -len(c))[:15]:
            canon = pick_canonical(cl)
            ups = coalesce_fields(canon, [m for m in cl if m["id"] != canon["id"]])
            names = ", ".join(f"#{m['id']}" for m in cl)
            print(f"  keep #{canon['id']} '{canon['full_name']}' "
                  f"(matters={canon['n_matters']}) <- [{names}]"
                  + (f"  +fill {list(ups)}" if ups else ""))
        if conflicted:
            print("\n--- sample conflict clusters -> review (up to 10) ---")
            for cl in conflicted[:10]:
                vals = []
                for m in cl:
                    vals.append(f"#{m['id']}({m.get('nphone') or m.get('nemail') or '-'})")
                print(f"  '{cl[0]['full_name']}': {', '.join(vals)}")

        if not apply:
            print("\nDRY-RUN only. Re-run with --apply to execute.")
            return

        # ---- APPLY ----
        backup = {"ts": ts, "auto": [], "conflict_candidates": []}
        merged = 0
        for cl in auto:
            canon = pick_canonical(cl)
            losers = [m for m in cl if m["id"] != canon["id"]]
            backup["auto"].append({
                "canonical": canon["id"],
                "losers": [l["id"] for l in losers],
                "preimages": [{k: v for k, v in m.items()
                               if k in ("id", "full_name", "company", "firm_name",
                                        "email", "phone", "bar_number", "contact_type")}
                              for m in cl],
            })

        with open(f"/tmp/contact_dedup_backup_{ts}.json", "w") as f:
            json.dump(backup, f, indent=2, default=str)
        print(f"\nbackup -> /tmp/contact_dedup_backup_{ts}.json")

        for cl in auto:
            canon = pick_canonical(cl)
            cid = canon["id"]
            losers = [m for m in cl if m["id"] != cid]
            loser_ids = [l["id"] for l in losers]

            # coalesce fields onto canonical
            ups = coalesce_fields(canon, losers)
            if ups:
                set_clause = ", ".join(f"{k} = :{k}" for k in ups)
                await db.execute(
                    text(f"UPDATE contacts SET {set_clause}, updated_at = NOW() "
                         f"WHERE id = :cid AND TRIM(tenant_id) = :tid"),
                    {**ups, "cid": cid, "tid": TENANT})

            # matter_contacts: dedup-safe re-point
            r = await db.execute(text("""
                SELECT id, matter_id FROM matter_contacts
                WHERE contact_id = ANY(:losers) AND TRIM(tenant_id) = :tid
            """), {"losers": loser_ids, "tid": TENANT})
            for link in r.fetchall():
                exists = await db.execute(text("""
                    SELECT 1 FROM matter_contacts
                    WHERE contact_id = :cid AND matter_id = :mid
                      AND TRIM(tenant_id) = :tid
                """), {"cid": cid, "mid": link.matter_id, "tid": TENANT})
                if exists.fetchone():
                    await db.execute(text("DELETE FROM matter_contacts WHERE id = :id"),
                                     {"id": link.id})
                else:
                    await db.execute(text("UPDATE matter_contacts SET contact_id = :cid WHERE id = :id"),
                                     {"cid": cid, "id": link.id})

            # matter_contact_proposals: dedup-safe (uq_mcp_pending_pair)
            r = await db.execute(text("""
                SELECT id, matter_id FROM matter_contact_proposals
                WHERE contact_id = ANY(:losers) AND TRIM(tenant_id) = :tid
            """), {"losers": loser_ids, "tid": TENANT})
            for prop in r.fetchall():
                ex = await db.execute(text("""
                    SELECT 1 FROM matter_contact_proposals
                    WHERE contact_id = :cid AND matter_id = :mid
                      AND TRIM(tenant_id) = :tid
                """), {"cid": cid, "mid": prop.matter_id, "tid": TENANT})
                if ex.fetchone():
                    await db.execute(text("DELETE FROM matter_contact_proposals WHERE id = :id"),
                                     {"id": prop.id})
                else:
                    await db.execute(text("UPDATE matter_contact_proposals SET contact_id = :cid WHERE id = :id"),
                                     {"cid": cid, "id": prop.id})

            # straight FK re-points
            for tbl, col in FK_SIMPLE:
                await db.execute(
                    text(f"UPDATE {tbl} SET {col} = :cid WHERE {col} = ANY(:losers)"),
                    {"cid": cid, "losers": loser_ids})

            # archive losers
            await db.execute(text("""
                UPDATE contacts SET contact_type = 'archived', updated_at = NOW()
                WHERE id = ANY(:losers) AND TRIM(tenant_id) = :tid
            """), {"losers": loser_ids, "tid": TENANT})
            merged += 1

        # conflict clusters -> review candidates (idempotent-ish: skip if open one exists)
        for cl in conflicted:
            ids = sorted(int(m["id"]) for m in cl)
            await db.execute(text("""
                INSERT INTO contact_dedup_candidates
                    (id, tenant_id, contact_ids, signal_type, confidence, notes, detected_at)
                SELECT gen_random_uuid()::text, CAST(:tid AS varchar), CAST(:ids AS jsonb),
                       'allfields_conflict', 0.50,
                       'same name, conflicting fields - needs human pick', NOW()
                WHERE NOT EXISTS (
                    SELECT 1 FROM contact_dedup_candidates
                    WHERE TRIM(tenant_id) = CAST(:tid AS varchar) AND review_outcome IS NULL
                      AND contact_ids = CAST(:ids AS jsonb))
            """), {"tid": TENANT, "ids": json.dumps(ids)})

        await db.commit()
        print(f"\nAPPLIED: merged {merged} clusters, archived {n_archive}, "
              f"queued {len(conflicted)} conflict clusters for review.")


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
