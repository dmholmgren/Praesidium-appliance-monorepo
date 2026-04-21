"""
reimport_client_matters.py v2
Reimport clients and matters from ts_clients.ts_raw.

nickname1: "Client/Matter" — split on first "/" for name grouping
nickname2: "7020.361" — split on "." for client_number.matter_number
  If no dot, use same number for both.
  Client number conflicts: use lowest 4-digit number, name is primary grouping key.

Run on WEB-01:
  docker cp reimport_client_matters.py praesidium-web:/tmp/reimport4.py
  docker exec -it praesidium-web python3 /tmp/reimport4.py
"""

import asyncio
import uuid
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("reimport")

TENANT_ID = "ca4a6450-fe63-4fea-aeba-9faf2bb122d6"


async def main():
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal

    log.info("=== Reimport clients/matters from ts_raw for %s ===", TENANT_ID)
    tid = TENANT_ID

    async with AsyncSessionLocal() as db:
        log.info("Clearing dependent tables...")
        for tbl in ["timesheet_drafts", "timesheet_sessions",
                     "matter_contacts", "matter_folders", "contacts"]:
            await db.execute(text(f"DELETE FROM {tbl} WHERE trim(tenant_id) = :tid"), {"tid": tid})
        await db.execute(text("DELETE FROM matters WHERE trim(tenant_id) = :tid"), {"tid": tid})
        await db.execute(text("DELETE FROM clients WHERE trim(tenant_id) = :tid"), {"tid": tid})
        await db.commit()
        log.info("Tables cleared")

        r = await db.execute(text("""
            SELECT ts_client_id, ts_name, ts_raw->>'nickname1' AS nn1,
                   ts_raw->>'nickname2' AS nn2, phone1, email
            FROM ts_clients WHERE trim(tenant_id) = :tid
            ORDER BY ts_raw->>'nickname1'
        """), {"tid": tid})
        rows = r.mappings().all()
        log.info("Read %d ts_clients rows", len(rows))

        client_groups = {}
        for row in rows:
            nn1 = (row["nn1"] or "").strip()
            nn2 = (row["nn2"] or "").strip()
            ts_name = (row["ts_name"] or "").strip()

            if "/" in nn1:
                idx = nn1.index("/")
                client_key = nn1[:idx].strip()
                matter_name = nn1[idx + 1:].strip()
            else:
                client_key = nn1 or ts_name
                matter_name = nn1 or ts_name

            if not client_key:
                client_key = ts_name or f"Unknown-{row['ts_client_id']}"

            if "." in nn2:
                dot_idx = nn2.index(".")
                client_number = nn2[:dot_idx].strip()
                matter_number = nn2
            else:
                client_number = nn2
                matter_number = nn2

            if client_key not in client_groups:
                client_groups[client_key] = {"client_numbers": set(), "matters": []}
            if client_number:
                client_groups[client_key]["client_numbers"].add(client_number)
            client_groups[client_key]["matters"].append({
                "matter_name": matter_name,
                "matter_number": matter_number,
                "client_number": client_number,
                "ts_client_id": row["ts_client_id"],
            })

        log.info("Found %d unique client groups", len(client_groups))

        clients_created = 0
        matters_created = 0

        for client_key, group in client_groups.items():
            client_uuid = str(uuid.uuid4())
            client_nums = sorted(group["client_numbers"])
            four_digit = [n for n in client_nums if len(n) == 4 and n.isdigit()]
            client_number = four_digit[0] if four_digit else (client_nums[0] if client_nums else None)

            await db.execute(text("""
                INSERT INTO clients (id, tenant_id, client_name, client_number, is_active, created_at, updated_at)
                VALUES (CAST(:cid AS uuid), :tid, :cname, :cnum, true, NOW(), NOW())
            """), {"cid": client_uuid, "tid": tid, "cname": client_key, "cnum": client_number})
            clients_created += 1

            for m in group["matters"]:
                matter_uuid = str(uuid.uuid4())
                await db.execute(text("""
                    INSERT INTO matters
                        (id, tenant_id, client_id, matter_name, matter_number,
                         status, created_at, updated_at)
                    VALUES (CAST(:mid AS uuid), :tid, CAST(:cid AS uuid),
                            :mname, :mnum, 'active', NOW(), NOW())
                """), {
                    "mid": matter_uuid, "tid": tid, "cid": client_uuid,
                    "mname": m["matter_name"], "mnum": m["matter_number"],
                })
                matters_created += 1

                await db.execute(text("""
                    UPDATE ts_clients SET praesidium_client_id = :cid
                    WHERE ts_client_id = :tsid AND trim(tenant_id) = :tid
                """), {"cid": client_uuid, "tsid": m["ts_client_id"], "tid": tid})

        await db.commit()
        log.info("Created %d clients, %d matters", clients_created, matters_created)

        r = await db.execute(text("""
            SELECT c.client_name, c.client_number, m.matter_name, m.matter_number
            FROM matters m JOIN clients c ON c.id = m.client_id
            WHERE trim(m.tenant_id) = :tid
            ORDER BY c.client_name, m.matter_name LIMIT 30
        """), {"tid": tid})
        log.info("=== Sample client/matter nesting ===")
        prev = ""
        for row in r.mappings().all():
            cn = row["client_name"]
            if cn != prev:
                log.info("  %s [#%s]", cn, row["client_number"] or "-")
                prev = cn
            log.info("    └─ %s [%s]", row["matter_name"], row["matter_number"])

        r2 = await db.execute(text("SELECT COUNT(*) FROM clients WHERE trim(tenant_id) = :tid"), {"tid": tid})
        r3 = await db.execute(text("SELECT COUNT(*) FROM matters WHERE trim(tenant_id) = :tid"), {"tid": tid})
        log.info("=== DONE: %d clients, %d matters ===", r2.scalar(), r3.scalar())

if __name__ == "__main__":
    asyncio.run(main())
