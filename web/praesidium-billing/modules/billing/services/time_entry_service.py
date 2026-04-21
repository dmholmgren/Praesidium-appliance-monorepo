"""Time entry service — uses Chat 0 columns (description, hours, rate, amount)."""
import math
from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List
from core.db.base import TenantSession
from core.audit import write_audit
from modules.billing.models.time_entry import TimeEntry, TimeEntrySource
from modules.billing.services.rate_service import RateService


def round_to_quarter_hour(hours: Decimal) -> Decimal:
    quarters = math.ceil(float(hours) * 4)
    return Decimal(str(quarters / 4))


class TimeEntryService:
    def __init__(self, db: TenantSession):
        self.db = db
        self.rate_service = RateService(db)

    async def list_entries(self, user_id=None, matter_id=None, status=None, date_from=None, date_to=None, offset=0, limit=100) -> dict:
        query = self.db.query(TimeEntry)
        if user_id:
            query = query.filter(TimeEntry.user_id == user_id)
        if matter_id:
            query = query.filter(TimeEntry.matter_id == matter_id)
        if status:
            query = query.filter(TimeEntry.status == status)
        if date_from:
            query = query.filter(TimeEntry.entry_date >= date_from)
        if date_to:
            query = query.filter(TimeEntry.entry_date <= date_to)
        total = query.count()
        entries = query.order_by(TimeEntry.entry_date.desc(), TimeEntry.created_at.desc()).offset(offset).limit(limit).all()
        return {"items": entries, "total": total, "offset": offset, "limit": limit}

    async def get_entry(self, entry_id: int) -> Optional[TimeEntry]:
        return self.db.query(TimeEntry).filter(TimeEntry.id == entry_id).first()

    async def create_entry(self, data: dict, user_id: int) -> TimeEntry:
        hours = Decimal(str(data.get("hours", 0)))
        hours_rounded = round_to_quarter_hour(hours)
        rate = data.get("rate")
        if rate is None:
            rate = await self.rate_service.resolve_rate(user_id=data.get("user_id", user_id), matter_id=data.get("matter_id"))
            rate = rate or Decimal("0")
        else:
            rate = Decimal(str(rate))
        amount = hours_rounded * rate

        entry = TimeEntry(
            tenant_id=self.db.tenant_id,
            matter_id=data["matter_id"],
            user_id=data.get("user_id", user_id),
            entry_date=data.get("entry_date", date.today()),
            hours=hours_rounded,
            rate=rate,
            amount=amount,
            description=data.get("description", ""),
            source=data.get("source", "manual"),
            status=data.get("status", "draft"),
            utbms_code=data.get("utbms_code"),
            source_metadata=data.get("source_metadata"),
        )
        self.db.add(entry)
        self.db.flush()
        write_audit(self.db, tenant_id=self.db.tenant_id, user_id=user_id,
            action="time_entry.create", entity_type="time_entry", entity_id=entry.id,
            new_value={"matter_id": data["matter_id"], "hours": str(hours_rounded)})
        self.db.commit()
        return entry

    async def create_from_source(self, data: dict, source_data: dict, user_id: int) -> TimeEntry:
        entry = await self.create_entry(data, user_id)
        source = TimeEntrySource(
            tenant_id=self.db.tenant_id, time_entry_id=entry.id,
            source_type=source_data.get("source_type", "unknown"),
            source_id=source_data.get("source_id", ""),
            source_data=source_data.get("raw_data"),
            duration_seconds=source_data.get("duration_seconds"),
            matter_confidence=source_data.get("matter_confidence"),
        )
        self.db.add(source)
        self.db.commit()
        return entry

    async def update_entry(self, entry_id: int, data: dict, user_id: int) -> Optional[TimeEntry]:
        entry = await self.get_entry(entry_id)
        if not entry:
            return None
        if entry.status == "billed":
            raise ValueError("Cannot edit a billed time entry")
        old_values = {k: getattr(entry, k) for k in data if hasattr(entry, k)}
        for key, value in data.items():
            if hasattr(entry, key):
                setattr(entry, key, value)
        if "hours" in data:
            entry.hours = round_to_quarter_hour(Decimal(str(data["hours"])))
            entry.amount = entry.hours * entry.rate
        if "rate" in data:
            entry.rate = Decimal(str(data["rate"]))
            entry.amount = entry.hours * entry.rate
        entry.updated_at = datetime.utcnow()
        write_audit(self.db, tenant_id=self.db.tenant_id, user_id=user_id,
            action="time_entry.update", entity_type="time_entry", entity_id=entry.id,
            old_value={k: str(v) for k, v in old_values.items()}, new_value={k: str(v) for k, v in data.items()})
        self.db.commit()
        return entry

    async def bulk_approve(self, entry_ids: List[int], user_id: int) -> int:
        count = 0
        for eid in entry_ids:
            entry = await self.get_entry(eid)
            if entry and entry.status in ("draft", "ai_suggested", "reviewed"):
                entry.status = "approved"
                entry.reviewed_by = user_id
                entry.reviewed_at = datetime.utcnow()
                entry.updated_at = datetime.utcnow()
                write_audit(self.db, tenant_id=self.db.tenant_id, user_id=user_id,
                    action="time_entry.approve", entity_type="time_entry", entity_id=eid)
                count += 1
        self.db.commit()
        return count

    async def reject_entry(self, entry_id: int, user_id: int, reason: str = "") -> Optional[TimeEntry]:
        entry = await self.get_entry(entry_id)
        if not entry:
            return None
        entry.status = "written_off"
        entry.reviewed_by = user_id
        entry.reviewed_at = datetime.utcnow()
        entry.updated_at = datetime.utcnow()
        write_audit(self.db, tenant_id=self.db.tenant_id, user_id=user_id,
            action="time_entry.reject", entity_type="time_entry", entity_id=entry_id, new_value={"reason": reason})
        self.db.commit()
        return entry

    async def get_timesheet_summary(self, user_id: int, date_from: date, date_to: date) -> dict:
        from sqlalchemy import func
        rows = (self.db.query(TimeEntry.entry_date, func.sum(TimeEntry.hours).label("total_hours"),
                func.sum(TimeEntry.amount).label("total_amount"), func.count(TimeEntry.id).label("entry_count"))
            .filter(TimeEntry.user_id == user_id, TimeEntry.entry_date >= date_from, TimeEntry.entry_date <= date_to)
            .group_by(TimeEntry.entry_date).order_by(TimeEntry.entry_date).all())
        return {
            "days": [{"date": str(r.entry_date), "total_hours": float(r.total_hours or 0),
                "total_amount": float(r.total_amount or 0), "entry_count": r.entry_count} for r in rows],
            "period_total_hours": sum(float(r.total_hours or 0) for r in rows),
            "period_total_amount": sum(float(r.total_amount or 0) for r in rows),
        }
