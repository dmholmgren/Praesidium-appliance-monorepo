"""Widget API — unified JSON endpoint for all widget registry widgets.
GET /api/v1/widgets/{slug}
Returns JSON data for any registered widget. The React WidgetRenderer calls this."""
from __future__ import annotations
import logging, importlib
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/widgets", tags=["widget-api"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()

@router.get("/{slug}")
async def widget_data(request: Request, slug: str):
    """Fetch data for a widget by slug. Looks up data_source in widget_registry,
    imports and calls the function, returns JSON."""
    tid = _tid(request)
    # Look up widget in registry
    try:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(sa_text(
                "SELECT data_source FROM widget_registry WHERE widget_slug=:s LIMIT 1"
            ), {"s": slug})).fetchone()
    except Exception as e:
        logger.error("widget lookup %s: %s", slug, e)
        return JSONResponse({"error": str(e)}, status_code=500)

    if not row or not row[0]:
        # No data_source — return empty (widget is static / client-only)
        return JSONResponse({"slug": slug, "data": None})

    # Import and call the data_source function
    ds = row[0]  # e.g. "modules.dms.services.widget_service.get_scan_queue"
    try:
        mod_path, func_name = ds.rsplit(".", 1)
        mod = importlib.import_module(mod_path)
        func = getattr(mod, func_name)
        data = await func(tid) if _is_async(func) else func(tid)
        return JSONResponse({"slug": slug, "data": data})
    except Exception as e:
        logger.error("widget dispatch %s (%s): %s", slug, ds, e)
        return JSONResponse({"slug": slug, "data": None, "error": str(e)})

def _is_async(func):
    import asyncio
    return asyncio.iscoroutinefunction(func)
