"""COMP 4 — Gantt Charts (inline SVG).

DB pattern: get_session_factory() — matches working eDiscovery pattern.
Panel dispatch signature: async def generate_matter_gantt(tenant_id, matter_id)
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from core.db.base import AsyncSessionLocal

CHART_LEFT = 220
CHART_RIGHT = 50
ROW_HEIGHT = 32
HEADER_HEIGHT = 60
MIN_WIDTH = 900


async def generate_matter_gantt(tenant_id: str, matter_id: str) -> str:
    """Panel registry dispatch — SVG Gantt for a single matter."""
    async with AsyncSessionLocal() as session:
        dl_result = await session.execute(
            text("""
                SELECT id, description, due_date, status, priority FROM deadlines
                WHERE tenant_id = :tid AND matter_id = :mid AND due_date IS NOT NULL
                ORDER BY due_date ASC
            """),
            {"tid": tenant_id, "mid": matter_id},
        )
        deadlines = [dict(r._mapping) for r in dl_result.fetchall()]

        matter_result = await session.execute(
            text("SELECT matter_name FROM matters WHERE tenant_id = :tid AND id = :mid"),
            {"tid": tenant_id, "mid": matter_id},
        )
        matter_row = matter_result.fetchone()
        title = matter_row[0] if matter_row else f"Matter {matter_id[:8]}..."

    return _render_gantt(deadlines, title)


async def generate_firm_gantt(tenant_id: str) -> str:
    """Firm-level Gantt — all active matters."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT d.id, m.matter_name as description, d.due_date, d.status, d.priority
                FROM deadlines d
                JOIN matters m ON d.matter_id = m.id AND d.tenant_id = m.tenant_id
                WHERE d.tenant_id = :tid AND m.status = 'active' AND d.due_date IS NOT NULL
                ORDER BY d.due_date ASC LIMIT 100
            """),
            {"tid": tenant_id},
        )
        deadlines = [dict(r._mapping) for r in result.fetchall()]
    return _render_gantt(deadlines, "Firm Deadlines — All Active Matters")


async def generate_my_matters_gantt(tenant_id: str, user_id: int) -> str:
    """Gantt for a specific timekeeper's matters."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT d.id, m.matter_name as description, d.due_date, d.status, d.priority
                FROM deadlines d
                JOIN matters m ON d.matter_id = m.id AND d.tenant_id = m.tenant_id
                JOIN matter_timekeepers mt ON m.id = mt.matter_id AND m.tenant_id = mt.tenant_id
                WHERE d.tenant_id = :tid AND mt.user_id = :uid
                AND m.status = 'active' AND d.due_date IS NOT NULL
                ORDER BY d.due_date ASC LIMIT 100
            """),
            {"tid": tenant_id, "uid": user_id},
        )
        deadlines = [dict(r._mapping) for r in result.fetchall()]
    return _render_gantt(deadlines, "My Matters — Deadlines")


def _render_gantt(deadlines: list[dict[str, Any]], title: str) -> str:
    if not deadlines:
        return _empty_gantt(title)

    dates = [d["due_date"] for d in deadlines if d["due_date"]]
    if not dates:
        return _empty_gantt(title)

    min_date = min(dates) - timedelta(days=7)
    max_date = max(dates) + timedelta(days=14)
    total_days = max((max_date - min_date).days, 1)

    chart_width = max(MIN_WIDTH, total_days * 4 + CHART_LEFT + CHART_RIGHT)
    data_width = chart_width - CHART_LEFT - CHART_RIGHT
    svg_height = HEADER_HEIGHT + len(deadlines) * ROW_HEIGHT + 30

    lines: list[str] = []
    lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {chart_width} {svg_height}" '
        f'class="w-full" style="font-family: DM Sans, sans-serif; font-size: 12px;">'
    )
    lines.append(f'<rect width="{chart_width}" height="{svg_height}" fill="#fafafa" rx="4"/>')
    lines.append(
        f'<text x="{chart_width // 2}" y="24" text-anchor="middle" '
        f'font-weight="600" font-size="14" fill="#111827">{html.escape(title)}</text>'
    )
    lines.extend(_month_markers(min_date, max_date, data_width, svg_height))

    today = datetime.now().date()
    min_d = min_date.date() if hasattr(min_date, 'date') else min_date
    max_d = max_date.date() if hasattr(max_date, 'date') else max_date
    if min_d <= today <= max_d:
        tx = CHART_LEFT + int((today - min_d).days / total_days * data_width)
        lines.append(
            f'<line x1="{tx}" y1="{HEADER_HEIGHT}" x2="{tx}" y2="{svg_height - 10}" '
            f'stroke="#EF4444" stroke-width="2" stroke-dasharray="4,2"/>'
        )
        lines.append(
            f'<text x="{tx}" y="{HEADER_HEIGHT - 4}" text-anchor="middle" '
            f'font-size="10" fill="#EF4444">Today</text>'
        )

    for i, dl in enumerate(deadlines):
        y = HEADER_HEIGHT + i * ROW_HEIGHT
        if i % 2 == 0:
            lines.append(f'<rect x="0" y="{y}" width="{chart_width}" height="{ROW_HEIGHT}" fill="#F9FAFB"/>')

        label = html.escape(str(dl.get("description", ""))[:35])
        lines.append(f'<text x="8" y="{y + 20}" fill="#374151" font-size="11">{label}</text>')

        due = dl["due_date"]
        due_d = due.date() if hasattr(due, 'date') else due
        day_offset = (due_d - min_d).days
        bx = CHART_LEFT + int(day_offset / total_days * data_width)
        color = _priority_color(dl.get("priority", "medium"), dl.get("status", "pending"))
        lines.append(
            f'<circle cx="{bx}" cy="{y + 16}" r="6" fill="{color}" stroke="#fff" stroke-width="1.5"/>'
        )
        date_str = due_d.strftime("%b %d") if hasattr(due_d, 'strftime') else str(due_d)
        lines.append(
            f'<text x="{bx + 10}" y="{y + 20}" fill="#6B7280" font-size="10">{date_str}</text>'
        )

    lines.append("</svg>")
    return "\n".join(lines)


def _month_markers(min_date, max_date, data_width: int, svg_height: int) -> list[str]:
    lines: list[str] = []
    min_d = min_date.date() if hasattr(min_date, 'date') else min_date
    max_d = max_date.date() if hasattr(max_date, 'date') else max_date
    total_days = max((max_d - min_d).days, 1)
    current = min_d.replace(day=1)
    while current <= max_d:
        if current >= min_d:
            offset = (current - min_d).days
            x = CHART_LEFT + int(offset / total_days * data_width)
            lines.append(
                f'<line x1="{x}" y1="{HEADER_HEIGHT}" x2="{x}" y2="{svg_height - 10}" '
                f'stroke="#E5E7EB" stroke-width="1"/>'
            )
            lines.append(
                f'<text x="{x + 4}" y="{HEADER_HEIGHT - 4}" font-size="10" '
                f'fill="#9CA3AF">{current.strftime("%b %Y")}</text>'
            )
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return lines


def _priority_color(priority: str, status: str) -> str:
    if status in ("complete", "met"):
        return "#22C55E"
    return {"critical": "#EF4444", "high": "#F97316", "medium": "#3B82F6", "low": "#9CA3AF"}.get(priority, "#3B82F6")


def _empty_gantt(title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 100" style="width:100%;">'
        f'<rect width="600" height="100" fill="#fafafa" rx="4"/>'
        f'<text x="300" y="35" text-anchor="middle" font-size="14" font-weight="600" fill="#111827">{html.escape(title)}</text>'
        f'<text x="300" y="60" text-anchor="middle" font-size="12" fill="#9CA3AF">No deadlines to display</text>'
        f'</svg>'
    )
