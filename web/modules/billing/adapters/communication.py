"""CommunicationService time tracking adapters — Teams, Zoom, Webex, Google Meet."""
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class _BaseCommunicationTimeAdapter:
    """Base class for communication platform time capture."""

    source_name: str = "unknown"

    def _build_entry(
        self,
        meeting: dict,
        user_id: str,
        matter_id: str,
        hours: Decimal,
        narrative: str,
        source_id: str,
        confidence: Decimal,
    ) -> dict:
        entry_date = meeting.get("start_time")
        if isinstance(entry_date, str):
            try:
                entry_date = datetime.fromisoformat(entry_date.replace("Z", "+00:00")).date()
            except (ValueError, AttributeError):
                entry_date = datetime.utcnow().date()
        elif isinstance(entry_date, datetime):
            entry_date = entry_date.date()
        else:
            entry_date = datetime.utcnow().date()

        return {
            "entry_data": {
                "matter_id": matter_id,
                "user_id": user_id,
                "entry_date": entry_date,
                "narrative": narrative,
                "hours": hours,
                "source": self.source_name,
                "source_reference_id": source_id,
                "status": "draft",
            },
            "source_data": {
                "source_type": self.source_name,
                "source_id": source_id,
                "raw_data": json.dumps(meeting, default=str),
                "duration_seconds": int(float(hours) * 3600),
                "matter_confidence": confidence,
                "captured_at": datetime.utcnow(),
            },
        }


class TeamsTimeAdapter(_BaseCommunicationTimeAdapter):
    """Microsoft Teams meeting time capture via Graph API."""

    source_name = "teams"

    def __init__(self, graph_client):
        self.graph = graph_client

    async def get_meetings(
        self, user_id: str, from_dt: datetime, to_dt: datetime,
    ) -> list:
        """Fetch Teams meeting attendance via Graph API callRecords."""
        resp = await self.graph.get(
            f"/communications/callRecords",
            params={
                "$filter": (
                    f"startDateTime ge {from_dt.isoformat()}Z "
                    f"and startDateTime le {to_dt.isoformat()}Z"
                ),
                "$expand": "sessions($expand=segments)",
            },
        )
        return resp.get("value", [])

    async def poll_meetings(
        self, user_id: str, matter_matcher, from_dt: datetime, to_dt: datetime,
    ) -> list:
        meetings = await self.get_meetings(user_id, from_dt, to_dt)
        entries = []
        for m in meetings:
            duration_secs = 0
            participants = []
            for session in m.get("sessions", []):
                for seg in session.get("segments", []):
                    start = seg.get("startDateTime", "")
                    end = seg.get("endDateTime", "")
                    if start and end:
                        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
                        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
                        duration_secs += int((e - s).total_seconds())
                caller = session.get("caller", {}).get("identity", {}).get("user", {})
                if caller.get("displayName"):
                    participants.append(caller["displayName"])

            if duration_secs < 60:
                continue

            hours = Decimal(str(duration_secs / 3600))
            subject = m.get("organizer", {}).get("identity", {}).get("user", {}).get("displayName", "Teams Meeting")
            matter_id = matter_matcher(m) if callable(matter_matcher) else None
            if not matter_id:
                continue

            narrative = f"[Teams Meeting] {subject}"
            if participants:
                narrative += f" — Participants: {', '.join(participants[:5])}"

            entries.append(self._build_entry(
                meeting={"start_time": m.get("startDateTime", ""), **m},
                user_id=user_id, matter_id=matter_id, hours=hours,
                narrative=narrative, source_id=m.get("id", ""),
                confidence=Decimal("0.75"),
            ))
        return entries


class ZoomTimeAdapter(_BaseCommunicationTimeAdapter):
    """Zoom meeting time capture via Zoom API."""

    source_name = "zoom"

    def __init__(self, base_url: str, api_token: str):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=30.0,
        )

    async def get_meetings(self, user_email: str, from_dt: datetime, to_dt: datetime) -> list:
        resp = await self._client.get(
            f"/v2/users/{user_email}/meetings",
            params={"type": "previous_meetings", "from": from_dt.strftime("%Y-%m-%d"), "to": to_dt.strftime("%Y-%m-%d")},
        )
        resp.raise_for_status()
        return resp.json().get("meetings", [])

    async def poll_meetings(self, user_id: str, user_email: str, matter_matcher, from_dt: datetime, to_dt: datetime) -> list:
        meetings = await self.get_meetings(user_email, from_dt, to_dt)
        entries = []
        for m in meetings:
            duration_min = m.get("duration", 0)
            if duration_min < 1:
                continue
            hours = Decimal(str(duration_min / 60))
            topic = m.get("topic", "Zoom Meeting")
            matter_id = matter_matcher(m) if callable(matter_matcher) else None
            if not matter_id:
                continue

            entries.append(self._build_entry(
                meeting={"start_time": m.get("start_time", ""), **m},
                user_id=user_id, matter_id=matter_id, hours=hours,
                narrative=f"[Zoom Meeting] {topic}",
                source_id=str(m.get("id", "")),
                confidence=Decimal("0.70"),
            ))
        return entries

    async def close(self):
        await self._client.aclose()


class WebexTimeAdapter(_BaseCommunicationTimeAdapter):
    """Webex meeting time capture via Webex API."""

    source_name = "webex"

    def __init__(self, base_url: str, api_token: str):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=30.0,
        )

    async def get_meetings(self, from_dt: datetime, to_dt: datetime) -> list:
        resp = await self._client.get(
            "/v1/meetings",
            params={"from": from_dt.isoformat() + "Z", "to": to_dt.isoformat() + "Z", "meetingType": "meeting"},
        )
        resp.raise_for_status()
        return resp.json().get("items", [])

    async def poll_meetings(self, user_id: str, matter_matcher, from_dt: datetime, to_dt: datetime) -> list:
        meetings = await self.get_meetings(from_dt, to_dt)
        entries = []
        for m in meetings:
            start = m.get("start", "")
            end = m.get("end", "")
            if not start or not end:
                continue
            s = datetime.fromisoformat(start.replace("Z", "+00:00"))
            e = datetime.fromisoformat(end.replace("Z", "+00:00"))
            duration_secs = int((e - s).total_seconds())
            if duration_secs < 60:
                continue

            hours = Decimal(str(duration_secs / 3600))
            matter_id = matter_matcher(m) if callable(matter_matcher) else None
            if not matter_id:
                continue

            entries.append(self._build_entry(
                meeting={"start_time": start, **m},
                user_id=user_id, matter_id=matter_id, hours=hours,
                narrative=f"[Webex Meeting] {m.get('title', 'Webex Meeting')}",
                source_id=m.get("id", ""),
                confidence=Decimal("0.70"),
            ))
        return entries

    async def close(self):
        await self._client.aclose()


class MeetTimeAdapter(_BaseCommunicationTimeAdapter):
    """Google Meet time capture via Google Workspace API."""

    source_name = "meet"

    def __init__(self, credentials):
        self.credentials = credentials

    async def get_meetings(self, user_email: str, from_dt: datetime, to_dt: datetime) -> list:
        """Fetch calendar events that are Google Meet meetings."""
        # Uses Google Calendar API to find events with conferenceData
        from googleapiclient.discovery import build
        service = build("calendar", "v3", credentials=self.credentials)
        events_result = service.events().list(
            calendarId=user_email,
            timeMin=from_dt.isoformat() + "Z",
            timeMax=to_dt.isoformat() + "Z",
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = events_result.get("items", [])
        return [e for e in events if e.get("conferenceData", {}).get("conferenceSolution", {}).get("name") == "Google Meet"]

    async def poll_meetings(self, user_id: str, user_email: str, matter_matcher, from_dt: datetime, to_dt: datetime) -> list:
        meetings = await self.get_meetings(user_email, from_dt, to_dt)
        entries = []
        for m in meetings:
            start_str = m.get("start", {}).get("dateTime", "")
            end_str = m.get("end", {}).get("dateTime", "")
            if not start_str or not end_str:
                continue
            s = datetime.fromisoformat(start_str)
            e = datetime.fromisoformat(end_str)
            duration_secs = int((e - s).total_seconds())
            if duration_secs < 60:
                continue

            hours = Decimal(str(duration_secs / 3600))
            matter_id = matter_matcher(m) if callable(matter_matcher) else None
            if not matter_id:
                continue

            attendees = [a.get("email", "") for a in m.get("attendees", [])[:5]]
            narrative = f"[Google Meet] {m.get('summary', 'Meeting')}"
            if attendees:
                narrative += f" — Attendees: {', '.join(attendees)}"

            entries.append(self._build_entry(
                meeting={"start_time": start_str, **m},
                user_id=user_id, matter_id=matter_id, hours=hours,
                narrative=narrative,
                source_id=m.get("id", ""),
                confidence=Decimal("0.70"),
            ))
        return entries
