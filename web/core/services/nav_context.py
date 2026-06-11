"""Nav Context — v3 (data-driven from ui_nav_items)

Replaces the hardcoded _NAV dict with a live query via nav_service.
The shell.html template already consumes nav_items with the correct
structure — this just changes the source from hardcode to database.

Fallback: if nav_service fails (table missing, DB down), returns
the v2 hardcoded dict so the UI never breaks.

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""
import logging
from fastapi import Request
logger = logging.getLogger(__name__)


# ── Hardcoded fallback — only used if DB query fails ──────────
_FALLBACK_NAV = {
    "main": [
        {"nav_key":"home","label":"Dashboard","rail_label":"Home",
         "icon_svg":'<path stroke-linecap="round" stroke-linejoin="round" d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6"/>',
         "url_path":"/dashboard","page_key":"dashboard","display_order":100},
        {"nav_key":"comms","label":"Comms","rail_label":"Comms",
         "icon_svg":'<path stroke-linecap="round" stroke-linejoin="round" d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>',
         "url_path":"/communications","page_key":"communications","display_order":200},
        {"nav_key":"calendar","label":"Calendar","rail_label":"Calendar",
         "icon_svg":'<path stroke-linecap="round" stroke-linejoin="round" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"/>',
         "url_path":"/calendar","page_key":"calendar","display_order":250},
        {"nav_key":"matters","label":"Matters","rail_label":"Matters",
         "icon_svg":'<path stroke-linecap="round" stroke-linejoin="round" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/>',
         "url_path":"/matters","page_key":"matters","display_order":300},
        {"nav_key":"projects","label":"Projects","rail_label":"Projects",
         "icon_svg":'<path stroke-linecap="round" stroke-linejoin="round" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01"/>',
         "url_path":"/projects","page_key":"projects","display_order":400},
        {"nav_key":"drafting","label":"Drafting","rail_label":"Draft",
         "icon_svg":'<path stroke-linecap="round" stroke-linejoin="round" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/>',
         "url_path":"/drafting","page_key":"drafting","display_order":500},
        {"nav_key":"documents","label":"Documents","rail_label":"Docs",
         "icon_svg":'<path stroke-linecap="round" stroke-linejoin="round" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/>',
         "url_path":"/dms","page_key":"dms","display_order":600},
        {"nav_key":"ediscovery","label":"eDiscovery","rail_label":"eDisc",
         "icon_svg":'<path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/>',
         "url_path":"/ediscovery","page_key":"ediscovery","display_order":700},
        {"nav_key":"billing","label":"Billing","rail_label":"Billing",
         "icon_svg":'<path stroke-linecap="round" stroke-linejoin="round" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>',
         "url_path":"/billing","page_key":"billing","display_order":800},
    ],
    "bottom": [
        {"nav_key":"files_panel","label":"Files","rail_label":"Files",
         "icon_svg":'<path stroke-linecap="round" stroke-linejoin="round" d="M5 19a2 2 0 01-2-2V7a2 2 0 012-2h4l2 2h4a2 2 0 012 2v1M5 19h14a2 2 0 002-2v-5a2 2 0 00-2-2H9a2 2 0 00-2 2v5a2 2 0 01-2 2z"/>',
         "url_path":"#panel:files","page_key":None,"display_order":1000},
        {"nav_key":"ai_assistant","label":"AI Assistant","rail_label":"AI",
         "icon_svg":'<path stroke-linecap="round" stroke-linejoin="round" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/>',
         "url_path":"#panel:ai","page_key":None,"display_order":1100},
        {"nav_key":"settings","label":"Settings","rail_label":"Settings",
         "icon_svg":'<path stroke-linecap="round" stroke-linejoin="round" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>',
         "url_path":"/tenant-admin/","page_key":"admin","display_order":1200},
    ],
    "divider_positions": [350, 550],
}


async def get_nav_context(request: Request) -> dict:
    """
    Return nav context for shell.html template rendering.

    Resolution: nav_service.get_nav_items() → ui_nav_items table
    with tenant override precedence, role gating, Redis cache.

    Falls back to hardcoded _FALLBACK_NAV if DB query fails,
    so the UI never breaks during migrations or outages.
    """
    try:
        from core.services.nav_service import get_nav_items

        tenant_id = getattr(request.state, "tenant_id", "") or ""
        current_user = getattr(request.state, "current_user", None)
        user_role = getattr(current_user, "role", None) if current_user else None

        nav_data = await get_nav_items(
            tenant_id=tenant_id.strip(),
            user_role=user_role,
        )

        # nav_service returns {"main": [...], "bottom": [...], "divider_positions": [...]}
        # shell.html expects nav_items with exactly that shape — verified match
        if nav_data and nav_data.get("main"):
            return {"nav_items": nav_data}
        else:
            # Empty result (no rows yet) — use fallback
            logger.info("nav_service returned empty — using fallback nav")
            return {"nav_items": _FALLBACK_NAV}

    except Exception as exc:
        logger.warning("nav_service failed, using fallback: %s", exc)
        return {"nav_items": _FALLBACK_NAV}
