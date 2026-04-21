"""
scripts/seed_litigation_folders.py

Seeds standard HJMM litigation folder structure on the Praesidium share
via the file bridge API.

Structure:
  praesidium/
    {client_name}/
      {matter_number} - {matter_name}/
        01 - Pleadings/
        02 - Discovery/
        03 - Correspondence/
        04 - Research/
        05 - Court Filings/
        06 - Depositions/
        07 - Experts/
        08 - Mediation/
        09 - Orders/
        10 - Working Docs/
        11 - eDiscovery/
            Productions/
                Received/
                Outgoing/
            Collections/
            Review/
            Search Terms/
        12 - Billing/

Usage (seed all active matters):
  docker exec praesidium-web python3 /app/scripts/seed_litigation_folders.py

Usage (seed single matter):
  docker exec praesidium-web python3 /app/scripts/seed_litigation_folders.py --matter-id <uuid>
"""
import asyncio
import logging
import os
import sys
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

FILE_BRIDGE_URL = os.environ.get("FILE_BRIDGE_URL", "http://10.10.60.13:8080")

LITIGATION_SUBFOLDERS = [
    "01 - Pleadings",
    "02 - Discovery",
    "03 - Correspondence",
    "04 - Research",
    "05 - Court Filings",
    "06 - Depositions",
    "07 - Experts",
    "08 - Mediation",
    "09 - Orders",
    "10 - Working Docs",
    "11 - eDiscovery/Productions/Received",
    "11 - eDiscovery/Productions/Outgoing",
    "11 - eDiscovery/Collections",
    "11 - eDiscovery/Review",
    "11 - eDiscovery/Search Terms",
    "12 - Billing",
]


def _safe_name(s: str) -> str:
    """Strip characters unsafe for folder names."""
    for ch in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        s = s.replace(ch, '-')
    return s.strip()


def _matter_path(client_name: str, matter_number: str, matter_name: str) -> str:
    """Build the praesidium-relative path for a matter root folder."""
    client = _safe_name(client_name or "Unknown Client")
    matter_folder = _safe_name(f"{matter_number} - {matter_name}" if matter_number else matter_name)
    return f"praesidium/{client}/{matter_folder}"


async def create_folder(client: httpx.AsyncClient, tenant_id: str, path: str) -> bool:
    """Create a folder via file bridge. Returns True if created or already exists."""
    try:
        resp = await client.post(
            f"{FILE_BRIDGE_URL}/api/v1/files/mkdir",
            json={"tenant_id": tenant_id, "path": path},
            timeout=30.0,
        )
        if resp.status_code in (200, 201):
            return True
        # 409 or similar = already exists, that's fine
        if resp.status_code == 409:
            return True
        logger.warning(f"mkdir {path}: {resp.status_code} {resp.text[:100]}")
        return False
    except Exception as e:
        logger.error(f"mkdir error {path}: {e}")
        return False


async def seed_matter(
    tenant_id: str,
    matter_id: str,
    matter_number: str,
    matter_name: str,
    client_name: str,
) -> str:
    """
    Seed folder structure for one matter.
    Returns the matter root path on the Praesidium share.
    """
    matter_root = _matter_path(client_name, matter_number, matter_name)

    async with httpx.AsyncClient() as client:
        # Create client folder
        await create_folder(client, tenant_id, f"praesidium/{_safe_name(client_name or 'Unknown Client')}")

        # Create matter root
        await create_folder(client, tenant_id, matter_root)

        # Create all subfolders
        created = 0
        for subfolder in LITIGATION_SUBFOLDERS:
            path = f"{matter_root}/{subfolder}"
            ok = await create_folder(client, tenant_id, path)
            if ok:
                created += 1

    logger.info(f"Seeded {created}/{len(LITIGATION_SUBFOLDERS)} folders for {matter_root}")

    # Register in matter_folders table
    try:
        from core.db.base import AsyncSessionLocal
        from sqlalchemy import text as sa_text
        async with AsyncSessionLocal() as session:
            # Register the eDiscovery path specifically
            ediscovery_path = f"{matter_root}/11 - eDiscovery"
            await session.execute(sa_text("""
                INSERT INTO matter_folders
                    (tenant_id, matter_id, folder_path, disk_root, added_at)
                VALUES
                    (:tid, :mid::uuid, :path, :root, NOW())
                ON CONFLICT DO NOTHING
            """), {
                "tid":  tenant_id.strip(),
                "mid":  matter_id,
                "path": ediscovery_path,
                "root": "praesidium",
            })
            await session.commit()
            logger.info(f"Registered matter_folders: {ediscovery_path}")
    except Exception as e:
        logger.warning(f"matter_folders registration failed (non-blocking): {e}")

    return matter_root


async def seed_all_matters(tenant_id: str, matter_id_filter: str = None):
    """Seed folders for all active matters in a tenant."""
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text as sa_text

    async with AsyncSessionLocal() as session:
        params = {"tid": tenant_id.strip()}
        filter_clause = ""
        if matter_id_filter:
            filter_clause = " AND m.id::text = :mid"
            params["mid"] = matter_id_filter

        result = await session.execute(sa_text(f"""
            SELECT
                m.id::text as matter_id,
                m.matter_number,
                m.matter_name,
                COALESCE(
                    ts.raw_data->>'name',
                    ts.raw_data->>'client_name',
                    'Unknown Client'
                ) as client_name
            FROM matters m
            LEFT JOIN ts_clients ts
                ON ts.raw_data->>'nickname2' = m.matter_number
            WHERE TRIM(m.tenant_id) = :tid
              {filter_clause}
            ORDER BY m.matter_number
        """), params)
        matters = result.mappings().fetchall()

    logger.info(f"Seeding folders for {len(matters)} matters...")

    for m in matters:
        try:
            path = await seed_matter(
                tenant_id=tenant_id,
                matter_id=m["matter_id"],
                matter_number=m["matter_number"] or "",
                matter_name=m["matter_name"] or "Unknown Matter",
                client_name=m["client_name"] or "Unknown Client",
            )
            print(f"  ✓ {path}")
        except Exception as e:
            logger.error(f"Failed to seed {m['matter_number']}: {e}")

    print(f"\nDone. {len(matters)} matters processed.")


async def copy_to_praesidium(
    tenant_id: str,
    src_clients_path: str,
    dest_praesidium_path: str,
) -> bool:
    """
    Copy a file from Clients share to Praesidium share via FBRG-01.
    Both shares are mounted on FBRG-01 so this is a direct copy via bridge mkdir+upload.
    For large files, copy directly on FBRG-01 instead.
    """
    logger.info(f"Copy {src_clients_path} → {dest_praesidium_path}")
    # For large files this should be done directly on FBRG-01
    # This is a placeholder — use direct SSH copy for productions
    return False


if __name__ == "__main__":
    import argparse
    from core.db.base import AsyncSessionLocal

    parser = argparse.ArgumentParser(description="Seed HJMM matter folders on Praesidium share")
    parser.add_argument("--tenant-id", required=True, help="Tenant ID (UUID)")
    parser.add_argument("--matter-id", default=None, help="Seed single matter by ID")
    args = parser.parse_args()

    asyncio.run(seed_all_matters(args.tenant_id, args.matter_id))
