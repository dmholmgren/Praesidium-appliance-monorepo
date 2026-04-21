"""Client CRUD service — uses Chat 0 column names (client_name, address1, etc.)."""
from datetime import datetime
from typing import Optional
from core.db.base import TenantSession
from core.audit import write_audit
from core.models.client import Client


class ClientService:
    def __init__(self, db: TenantSession):
        self.db = db

    async def list_clients(self, search: Optional[str] = None, is_active: Optional[bool] = True, offset: int = 0, limit: int = 50) -> dict:
        query = self.db.query(Client)
        if is_active is not None:
            query = query.filter(Client.is_active == is_active)
        if search:
            query = query.filter(Client.client_name.ilike(f"%{search}%"))
        total = query.count()
        clients = query.order_by(Client.client_name).offset(offset).limit(limit).all()
        return {"items": clients, "total": total, "offset": offset, "limit": limit}

    async def get_client(self, client_id: int) -> Optional[Client]:
        return self.db.query(Client).filter(Client.id == client_id).first()

    async def create_client(self, data: dict, user_id: int) -> Client:
        client = Client(tenant_id=self.db.tenant_id, **data)
        self.db.add(client)
        self.db.flush()
        write_audit(self.db, "client.create", "client", client.id, new_values={k: str(v) for k, v in data.items()}, user_id=user_id)
        self.db.commit()
        return client

    async def update_client(self, client_id: int, data: dict, user_id: int) -> Optional[Client]:
        client = await self.get_client(client_id)
        if not client:
            return None
        old_values = {k: getattr(client, k) for k in data if hasattr(client, k)}
        for key, value in data.items():
            if hasattr(client, key):
                setattr(client, key, value)
        client.updated_at = datetime.utcnow()
        write_audit(self.db, "client.update", "client", client.id, old_values=old_values, new_values={k: str(v) for k, v in data.items()}, user_id=user_id)
        self.db.commit()
        return client
