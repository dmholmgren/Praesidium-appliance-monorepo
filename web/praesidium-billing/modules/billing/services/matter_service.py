"""Matter CRUD — uses Chat 0 column names (matter_name, matter_number)."""
from datetime import datetime, date
from typing import Optional
from core.db.base import TenantSession
from core.audit import write_audit
from modules.billing.models.matter import Matter, MatterTimekeeper
from modules.billing.models.client import Client


class MatterService:
    def __init__(self, db: TenantSession):
        self.db = db

    def _generate_matter_number(self) -> str:
        year = date.today().year
        prefix = f"{year}-"
        last = self.db.query(Matter).filter(Matter.matter_number.like(f"{prefix}%")).order_by(Matter.matter_number.desc()).first()
        seq = int(last.matter_number.split("-")[1]) + 1 if last else 1
        return f"{year}-{seq:04d}"

    async def list_matters(self, client_id=None, status=None, attorney_id=None, search=None, offset=0, limit=50) -> dict:
        query = self.db.query(Matter)
        if client_id:
            query = query.filter(Matter.client_id == client_id)
        if status:
            query = query.filter(Matter.status == status)
        if attorney_id:
            query = query.filter((Matter.originating_attorney_id == attorney_id) | (Matter.responsible_attorney_id == attorney_id))
        if search:
            query = query.filter((Matter.matter_name.ilike(f"%{search}%")) | (Matter.matter_number.ilike(f"%{search}%")))
        total = query.count()
        matters = query.order_by(Matter.open_date.desc()).offset(offset).limit(limit).all()
        return {"items": matters, "total": total, "offset": offset, "limit": limit}

    async def get_matter(self, matter_id: int) -> Optional[Matter]:
        return self.db.query(Matter).filter(Matter.id == matter_id).first()

    async def create_matter(self, data: dict, user_id: int) -> Matter:
        if "matter_number" not in data or not data["matter_number"]:
            data["matter_number"] = self._generate_matter_number()
        matter = Matter(tenant_id=self.db.tenant_id, **data)
        self.db.add(matter)
        self.db.flush()
        write_audit(self.db, tenant_id=self.db.tenant_id, user_id=user_id,
            action="matter.create", entity_type="matter", entity_id=matter.id, new_value=data)
        self.db.commit()
        return matter

    async def quick_create_matter(self, client_id: int, name: str, user_id: int) -> Matter:
        client = self.db.query(Client).filter(Client.id == client_id).first()
        if not client:
            raise ValueError("Client not found")
        return await self.create_matter({"client_id": client_id, "matter_name": name, "billing_type": "hourly"}, user_id)

    async def assign_timekeeper(self, matter_id: int, user_id: int, role: str, assigned_by: int) -> MatterTimekeeper:
        tk = MatterTimekeeper(tenant_id=self.db.tenant_id, matter_id=matter_id, user_id=user_id, role=role)
        self.db.add(tk)
        write_audit(self.db, tenant_id=self.db.tenant_id, user_id=assigned_by,
            action="matter.assign_timekeeper", entity_type="matter_timekeeper", entity_id=tk.id,
            new_value={"matter_id": matter_id, "user_id": user_id, "role": role})
        self.db.commit()
        return tk
