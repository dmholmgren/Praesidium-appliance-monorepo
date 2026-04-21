# modules/widgets/service.py
"""
Widget data source resolver.
Dynamically imports and calls service functions registered in widget_registry.data_source.
data_source format: "modules.dms.widget_service.get_widget_recent_documents"
"""

import asyncio
import importlib
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class WidgetDataSourceError(Exception):
    """Raised when a widget's data_source cannot be resolved or called."""
    pass


async def resolve_data_source(
    data_source: str,
    tenant_id: str,
    user_id: int,
    matter_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict[str, Any]:
    """
    Resolve a dot-path data_source string and call it with scoped parameters.

    Args:
        data_source: Dot-path to service function, e.g.
                     "modules.dms.widget_service.get_widget_recent_documents"
        tenant_id:   Tenant from session (already stripped by caller).
        user_id:     User from session.
        matter_id:   Optional matter scope from query param.
        date_from:   Optional ISO date string.
        date_to:     Optional ISO date string.

    Returns:
        Dict passed directly to the widget's Jinja2 template context.

    Raises:
        WidgetDataSourceError: on import failure, missing attribute, or call failure.
    """
    if not data_source:
        raise WidgetDataSourceError("data_source is empty")

    parts = data_source.rsplit(".", 1)
    if len(parts) != 2:
        raise WidgetDataSourceError(
            f"data_source must be 'module.path.function', got: {data_source}"
        )

    module_path, func_name = parts

    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        raise WidgetDataSourceError(
            f"Cannot import module '{module_path}': {exc}"
        ) from exc

    func = getattr(module, func_name, None)
    if func is None:
        raise WidgetDataSourceError(
            f"Function '{func_name}' not found in module '{module_path}'"
        )

    scope = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "matter_id": matter_id,
        "date_from": date_from,
        "date_to": date_to,
    }

    try:
        if asyncio.iscoroutinefunction(func):
            result = await func(**scope)
        else:
            result = func(**scope)
    except Exception as exc:
        logger.exception(
            "Widget data source call failed: %s — %s", data_source, exc
        )
        raise WidgetDataSourceError(
            f"Data source '{data_source}' raised an error: {exc}"
        ) from exc

    if not isinstance(result, dict):
        raise WidgetDataSourceError(
            f"Data source '{data_source}' must return a dict, got {type(result).__name__}"
        )

    return result
