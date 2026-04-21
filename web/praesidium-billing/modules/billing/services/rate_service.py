"""Rate card service — hierarchical resolution: firm > timekeeper > client > matter."""
from datetime import datetime, date
from decimal import Decimal
from typing import Optional
from core.db.base import TenantSession
from core.audit import write_audit
from modules.billing.models.rate_card import RateCard, RateChangeLog


class RateService:
    def __init__(self, db: TenantSession):
        self.db = db

    async def resolve_rate(self, user_id: int, client_id: Optional[int] = None,
                           matter_id: Optional[int] = None, as_of_date: Optional[date] = None) -> Optional[Decimal]:
        if as_of_date is None:
            as_of_date = date.today()
        scopes = []
        if matter_id:
            scopes.append(("matter", {"user_id": user_id, "matter_id": matter_id}))
        if client_id:
            scopes.append(("client", {"user_id": user_id, "client_id": client_id}))
        scopes.append(("timekeeper", {"user_id": user_id}))
        scopes.append(("firm", {}))

        for scope_name, filters in scopes:
            query = (self.db.query(RateCard)
                .filter(RateCard.scope == scope_name, RateCard.is_active == True, RateCard.effective_date <= as_of_date))
            for key, val in filters.items():
                query = query.filter(getattr(RateCard, key) == val)
            query = query.filter((RateCard.end_date.is_(None)) | (RateCard.end_date >= as_of_date))
            rate_card = query.order_by(RateCard.effective_date.desc()).first()
            if rate_card:
                return rate_card.hourly_rate
        return None

    async def set_rate(self, scope: str, hourly_rate: Decimal, changed_by_id: int,
                       user_id=None, client_id=None, matter_id=None,
                       effective_date=None, reason=None, apply_to_unbilled_wip=False) -> RateCard:
        if effective_date is None:
            effective_date = date.today()
        query = self.db.query(RateCard).filter(RateCard.scope == scope, RateCard.is_active == True)
        if user_id:
            query = query.filter(RateCard.user_id == user_id)
        if client_id:
            query = query.filter(RateCard.client_id == client_id)
        if matter_id:
            query = query.filter(RateCard.matter_id == matter_id)
        existing = query.first()
        old_rate = existing.hourly_rate if existing else Decimal("0")
        if existing:
            existing.end_date = effective_date
            existing.is_active = False
        rate_card = RateCard(tenant_id=self.db.tenant_id, scope=scope, user_id=user_id,
            client_id=client_id, matter_id=matter_id, hourly_rate=hourly_rate, effective_date=effective_date)
        self.db.add(rate_card)
        change_log = RateChangeLog(tenant_id=self.db.tenant_id, rate_card_id=rate_card.id,
            old_rate=old_rate, new_rate=hourly_rate, effective_date=effective_date, scope=scope,
            changed_by_id=changed_by_id, reason=reason, apply_to_unbilled_wip=apply_to_unbilled_wip)
        self.db.add(change_log)
        write_audit(self.db, tenant_id=self.db.tenant_id, user_id=changed_by_id,
            action="rate.change", entity_type="rate_card", entity_id=rate_card.id,
            old_value={"rate": str(old_rate)}, new_value={"rate": str(hourly_rate), "scope": scope})
        self.db.commit()
        return rate_card

    async def get_rate_history(self, scope=None, user_id=None, limit=50) -> list:
        query = self.db.query(RateChangeLog)
        if scope:
            query = query.filter(RateChangeLog.scope == scope)
        return query.order_by(RateChangeLog.created_at.desc()).limit(limit).all()
