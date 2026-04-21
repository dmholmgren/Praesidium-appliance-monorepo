"""DMS time tracking — aggregate document edit/view time into entries."""
import json, logging
from datetime import datetime, date
from decimal import Decimal
from core.db.base import TenantSession
logger = logging.getLogger(__name__)

class DMSTimeAdapter:
    def __init__(self, db: TenantSession):
        self.db = db

    async def aggregate_and_create_entries(self, user_id: int, date_from: date, date_to: date) -> list:
        from sqlalchemy import text
        sql = text("SELECT dtt.id, dtt.document_id, dtt.user_id, dtt.matter_id, dtt.tracking_type, dtt.start_time, dtt.duration_seconds, d.name AS document_name FROM document_time_tracking dtt JOIN documents d ON dtt.document_id = d.id AND d.tenant_id = :tenant_id WHERE dtt.tenant_id = :tenant_id AND dtt.user_id = :user_id AND DATE(dtt.start_time) BETWEEN :date_from AND :date_to AND dtt.duration_seconds >= 300 ORDER BY dtt.start_time")
        result = self.db.execute(sql, {"tenant_id": self.db.tenant_id, "user_id": user_id, "date_from": date_from, "date_to": date_to})
        raw = [dict(row._mapping) for row in result]
        agg = {}
        for rec in raw:
            key = (rec["matter_id"], str(rec["start_time"].date() if isinstance(rec["start_time"], datetime) else rec["start_time"]), rec["tracking_type"])
            if key not in agg:
                agg[key] = {"matter_id": rec["matter_id"], "entry_date": rec["start_time"].date() if isinstance(rec["start_time"], datetime) else rec["start_time"], "tracking_type": rec["tracking_type"], "total_seconds": 0, "documents": [], "source_ids": []}
            agg[key]["total_seconds"] += rec["duration_seconds"]
            agg[key]["documents"].append(rec.get("document_name", "Document"))
            agg[key]["source_ids"].append(str(rec["id"]))
        drafts = []
        for key, a in agg.items():
            hours = Decimal(str(a["total_seconds"] / 3600))
            src_type = "document"
            action = "Drafting/editing" if a["tracking_type"] == "active" else "Reviewing"
            doc_list = ", ".join(set(a["documents"][:5]))
            drafts.append({"entry_data": {"matter_id": a["matter_id"], "user_id": user_id, "entry_date": a["entry_date"],
                "description": f"[DMS] {action}: {doc_list}", "hours": hours, "source": src_type, "status": "draft"},
                "source_data": {"source_type": "dms", "source_id": a["source_ids"][0],
                    "raw_data": json.dumps({"source_ids": a["source_ids"]}), "duration_seconds": a["total_seconds"], "matter_confidence": Decimal("1.0")}})
        logger.info(f"DMS time for user {user_id}: {len(drafts)} entries")
        return drafts
