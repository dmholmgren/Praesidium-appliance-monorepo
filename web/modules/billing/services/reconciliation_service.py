"""Cross-timekeeper reconciliation — gap/overlap detection, BigInteger IDs."""
import uuid
from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import Optional
from core.db.base import TenantSession
from core.audit import write_audit
from core.models.billing import TimeEntry
from modules.billing.models.time_entry_source import TimeEntrySource
from core.models.matter import Matter, MatterTimekeeper


class ReconciliationService:
    def __init__(self, db: TenantSession):
        self.db = db

    async def find_gaps(self, user_id: int, date_from: date, date_to: date, min_gap_hours: float = 1.0) -> list:
        entries = self.db.query(TimeEntry).filter(TimeEntry.user_id == user_id, TimeEntry.entry_date >= date_from, TimeEntry.entry_date <= date_to).order_by(TimeEntry.entry_date).all()
        entries_by_date = {}
        for e in entries:
            entries_by_date.setdefault(e.entry_date, []).append(e)
        gaps = []
        current = date_from
        while current <= date_to:
            if current.weekday() < 5:
                day = entries_by_date.get(current, [])
                total = sum(float(e.hours) for e in day)
                if total < min_gap_hours:
                    gaps.append({"date": str(current), "total_hours_logged": total, "gap_hours": max(0, 8 - total), "severity": "high" if total == 0 else "medium"})
            current += timedelta(days=1)
        return gaps

    async def find_overlaps(self, matter_id: int, date_from: date, date_to: date) -> list:
        from sqlalchemy import func
        rows = (self.db.query(TimeEntry.entry_date, TimeEntry.user_id, func.count(TimeEntry.id).label("cnt"), func.sum(TimeEntry.hours).label("hrs"))
            .filter(TimeEntry.matter_id == matter_id, TimeEntry.entry_date >= date_from, TimeEntry.entry_date <= date_to)
            .group_by(TimeEntry.entry_date, TimeEntry.user_id).all())
        date_users = {}
        for r in rows:
            d = str(r.entry_date)
            date_users.setdefault(d, []).append({"user_id": r.user_id, "hours": float(r.hrs)})
        return [{"date": d, "timekeepers": users, "combined_hours": sum(u["hours"] for u in users)} for d, users in date_users.items() if len(users) > 1]

    async def run_full_reconciliation(self, date_from: date, date_to: date) -> dict:
        tks = self.db.query(MatterTimekeeper.user_id).filter(MatterTimekeeper.is_active == True).distinct().all()
        results = {"period": {"from": str(date_from), "to": str(date_to)}, "timekeepers_checked": len(tks), "gaps": [], "overlaps": []}
        for (tk_id,) in tks:
            gaps = await self.find_gaps(tk_id, date_from, date_to)
            if gaps:
                results["gaps"].append({"user_id": tk_id, "gap_days": len(gaps), "details": gaps})
        for (mid,) in self.db.query(Matter.id).filter(Matter.status == "active").all():
            overlaps = await self.find_overlaps(mid, date_from, date_to)
            if overlaps:
                results["overlaps"].append({"matter_id": mid, "details": overlaps})
        write_audit(self.db, "reconciliation.run", "reconciliation", str(uuid.uuid4()), user_id=0,
            new_values={"timekeepers": len(tks), "gap_issues": len(results["gaps"])})
        self.db.commit()
        return results
