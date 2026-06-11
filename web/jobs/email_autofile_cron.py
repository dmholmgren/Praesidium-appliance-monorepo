#!/usr/bin/env python3
"""
Email Autofile Cron Job
Runs periodically (e.g. every 15 minutes) to:
1. Auto-route read/pending emails using rule-based matcher
2. File matched emails to matter DMS folders

Usage:
  python3 /app/jobs/email_autofile_cron.py [tenant_id]

Or via cron inside praesidium-web container:
  */15 * * * * cd /app && python3 jobs/email_autofile_cron.py 986c0fee-1390-43bb-ad28-8cd1db6de53f >> /tmp/autofile.log 2>&1
"""
import sys, os, json, logging, asyncio
from datetime import datetime

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_URL", os.environ.get("DATABASE_URL", ""))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [autofile] %(message)s")
logger = logging.getLogger("autofile")

DEFAULT_TENANT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"


async def run_autofile(tenant_id: str):
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text

    tid = tenant_id.strip()
    logger.info("Starting autofile for tenant %s", tid)

    try:
        from modules.tenant_admin.email_sync_api import auto_route_emails

        class FakeState:
            tenant_id = tid
            current_user = type("U", (), {"id": 19, "user_id": 19})()

        class FakeRequest:
            state = FakeState()
            async def json(self):
                return {}

        route_resp = await auto_route_emails(FakeRequest(), user=FakeState.current_user)
        route_data = json.loads(route_resp.body)
        logger.info("Auto-route: %s", route_data.get("message", ""))
    except Exception as e:
        logger.exception("Auto-route failed: %s", e)
        return

    async with AsyncSessionLocal() as session:
        to_file = (await session.execute(text("""
            SELECT id::text, matched_matter_id::text
            FROM email_routing_queue
            WHERE TRIM(tenant_id) = :tid
              AND routing_status = 'matched'
              AND (filing_status IS NULL OR filing_status = 'pending')
              AND matched_matter_id IS NOT NULL
              AND is_read = true
              AND received_at >= NOW() - INTERVAL '60 days'
            ORDER BY received_at
            LIMIT 500
        """), {"tid": tid})).fetchall()

    if not to_file:
        logger.info("No matched emails to file")
        return

    logger.info("Filing %d matched emails", len(to_file))

    from modules.tenant_admin.email_sync_api import file_email_to_matter

    filed = 0
    errors = 0
    for row in to_file:
        try:
            class FileReq:
                state = FakeState()
                async def json(self):
                    return {"email_id": row[0], "matter_id": row[1]}

            resp = await file_email_to_matter(FileReq(), user=FakeState.current_user)
            data = json.loads(resp.body)
            if data.get("ok"):
                filed += 1
            else:
                errors += 1
        except Exception as exc:
            errors += 1
            logger.warning("File error %s: %s", row[0], exc)

    logger.info("Filed: %d, Errors: %d", filed, errors)


def main():
    tenant_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TENANT
    asyncio.run(run_autofile(tenant_id))


if __name__ == "__main__":
    main()
