"""ManicTime Server API adapter — polls activities, matches to matters."""
import re, json, logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional
import httpx
logger = logging.getLogger(__name__)

class ManicTimeAdapter:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0)

    async def get_activities(self, timeline_id: str, from_dt: datetime, to_dt: datetime) -> list:
        resp = await self._client.get(f"/api/timelines/{timeline_id}/activities", params={"fromUtc": from_dt.isoformat()+"Z", "toUtc": to_dt.isoformat()+"Z"})
        resp.raise_for_status()
        return resp.json().get("activities", [])

    def match_activity_to_matter(self, activity: dict, matter_patterns: dict) -> Optional[str]:
        combined = f"{activity.get('displayName','')} {activity.get('notes','')}".lower()
        for matter_id, patterns in matter_patterns.items():
            for pattern in patterns:
                if re.search(pattern.lower(), combined):
                    return matter_id
        return None

    async def poll_and_create_entries(self, user_email: str, user_id: int, matter_patterns: dict, from_dt: datetime, to_dt: datetime, min_duration_seconds: int = 300) -> list:
        timelines = (await self._client.get("/api/timelines", params={"userEmail": user_email})).json().get("timelines", [])
        drafts = []
        for tl in timelines:
            activities = await self.get_activities(tl["timelineId"], from_dt, to_dt)
            for act in activities:
                duration = act.get("durationSeconds", 0)
                if duration < min_duration_seconds:
                    continue
                matter_id = self.match_activity_to_matter(act, matter_patterns)
                if not matter_id:
                    continue
                hours = Decimal(str(duration / 3600))
                start = act.get("startUtc", "")
                entry_date = datetime.fromisoformat(start.replace("Z","+00:00")).date() if start else datetime.utcnow().date()
                drafts.append({"entry_data": {"matter_id": matter_id, "user_id": user_id, "entry_date": entry_date,
                    "description": f"[ManicTime] {act.get('displayName','Activity')}", "hours": hours,
                    "source": "manictime", "status": "draft"},
                    "source_data": {"source_type": "manictime", "source_id": act.get("activityId",""),
                        "raw_data": json.dumps(act), "duration_seconds": duration, "matter_confidence": Decimal("0.7")}})
        logger.info(f"ManicTime poll for {user_email}: {len(drafts)} entries")
        return drafts

    async def close(self):
        await self._client.aclose()
