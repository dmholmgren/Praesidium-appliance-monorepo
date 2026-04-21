"""FreePBX CDR adapter — ingests call records, matches to matters via contacts."""
import json, logging
from datetime import datetime
from decimal import Decimal
from typing import Optional
import httpx
logger = logging.getLogger(__name__)

class FreePBXAdapter:
    def __init__(self, base_url: str, api_token: str):
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {api_token}"}, timeout=30.0)

    def match_phone_to_contact(self, phone_number: str, contact_phone_map: dict) -> Optional[dict]:
        normalized = "".join(c for c in phone_number if c.isdigit())
        if len(normalized) > 10:
            normalized = normalized[-10:]
        return contact_phone_map.get(normalized)

    async def poll_and_create_entries(self, extension: str, user_id: int, contact_phone_map: dict, from_dt: datetime, to_dt: datetime, min_duration_seconds: int = 60) -> list:
        resp = await self._client.get("/api/cdr", params={"start": from_dt.strftime("%Y-%m-%d %H:%M:%S"), "end": to_dt.strftime("%Y-%m-%d %H:%M:%S"), "extension": extension})
        resp.raise_for_status()
        records = resp.json().get("records", [])
        drafts = []
        for cdr in records:
            duration = int(cdr.get("billsec", 0) or cdr.get("duration", 0))
            if duration < min_duration_seconds or cdr.get("disposition") != "ANSWERED":
                continue
            src, dst = cdr.get("src",""), cdr.get("dst","")
            remote = dst if src == extension else src
            contact = self.match_phone_to_contact(remote, contact_phone_map)
            if not contact or not contact.get("matter_ids"):
                continue
            matter_id = contact["matter_ids"][0]
            hours = Decimal(str(duration / 3600))
            direction = "outbound" if src == extension else "inbound"
            call_time = cdr.get("calldate","")
            entry_date = datetime.strptime(call_time, "%Y-%m-%d %H:%M:%S").date() if call_time else datetime.utcnow().date()
            drafts.append({"entry_data": {"matter_id": matter_id, "user_id": user_id, "entry_date": entry_date,
                "description": f"[Phone - {direction}] Call with {remote} ({duration//60}m {duration%60}s)",
                "hours": hours, "source": "phone", "status": "draft"},
                "source_data": {"source_type": "freepbx", "source_id": cdr.get("uniqueid",""),
                    "raw_data": json.dumps(cdr), "duration_seconds": duration, "matter_confidence": Decimal("0.85")}})
        logger.info(f"FreePBX poll ext {extension}: {len(drafts)} entries")
        return drafts

    async def close(self):
        await self._client.aclose()
