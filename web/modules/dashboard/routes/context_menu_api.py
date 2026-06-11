"""
Context Menu Registry API
GET /api/v1/context-menu/{object_type}/{object_id}
Returns applicable context menu actions for the given object.
"""

from fastapi import APIRouter, Depends, Request
from typing import Optional
from sqlalchemy import text
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/context-menu", tags=["context-menu"])


async def _get_session(request: Request):
    from core.db.base import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        yield session


async def _get_current_user(request: Request):
    return getattr(request.state, 'user', None)


@router.get("/{object_type}/{object_id}")
async def get_context_menu(
    object_type: str,
    object_id: str,
    request: Request,
    matter_id: Optional[str] = None,
    session=Depends(_get_session),
    user=Depends(_get_current_user),
):
    tenant_id = getattr(request.state, 'tenant_id', '').strip() if hasattr(request.state, 'tenant_id') else ''
    user_id = getattr(user, 'id', None) if user else None

    actions_sql = text("""
        SELECT
            a.id, a.action_slug, a.label, a.icon,
            a.action_type, a.navigate_to_template,
            a.api_endpoint, a.js_action,
            a.condition_query, a.condition_result_keys,
            a.requires_selection, a.multi_select,
            a.separator_before, a.keyboard_shortcut,
            a.display_order, a.permission_level,
            a.submenu_parent_id, a.confirm_message, a.config,
            COALESCE(p.is_hidden, false) AS user_hidden,
            p.custom_order
        FROM context_menu_actions a
        LEFT JOIN user_context_menu_prefs p
            ON p.action_id = a.id AND p.user_id = :user_id
        WHERE a.object_type = :object_type
          AND a.is_active = true
          AND (a.tenant_id IS NULL OR TRIM(a.tenant_id) = :tenant_id)
        ORDER BY COALESCE(p.custom_order, a.display_order)
    """)

    result = await session.execute(actions_sql, {
        'object_type': object_type,
        'tenant_id': tenant_id,
        'user_id': user_id or 0,
    })
    all_actions = result.mappings().all()

    applicable = []
    for action in all_actions:
        if action['user_hidden']:
            continue

        condition = action['condition_query']
        resolved_data = {}

        if condition:
            try:
                cond_result = await session.execute(
                    text(condition),
                    {'object_id': object_id, 'tenant_id': tenant_id, 'user_id': user_id or 0}
                )
                row = cond_result.mappings().first()
                if row is None:
                    continue
                resolved_data = dict(row)
            except Exception as e:
                logger.warning(f"Context menu condition failed: {action['action_slug']} for {object_type}/{object_id}: {e}")
                continue

        template_vars = {'object_id': object_id, 'matter_id': matter_id or '', **resolved_data}

        nav_url = action['navigate_to_template']
        if nav_url:
            for key, val in template_vars.items():
                nav_url = nav_url.replace(f'{{{key}}}', str(val))

        api_url = action['api_endpoint']
        if api_url:
            for key, val in template_vars.items():
                api_url = api_url.replace(f'{{{key}}}', str(val))

        applicable.append({
            'id': str(action['id']),
            'action_slug': action['action_slug'],
            'label': action['label'],
            'icon': action['icon'],
            'action_type': action['action_type'],
            'navigate_to': nav_url,
            'api_endpoint': api_url,
            'js_action': action['js_action'],
            'requires_selection': action['requires_selection'],
            'multi_select': action['multi_select'],
            'separator_before': action['separator_before'],
            'keyboard_shortcut': action['keyboard_shortcut'],
            'submenu_parent_id': str(action['submenu_parent_id']) if action['submenu_parent_id'] else None,
            'confirm_message': action['confirm_message'],
            'config': action['config'] or {},
            'resolved_data': resolved_data,
        })

    top_level = []
    children_by_parent = {}
    for item in applicable:
        parent = item['submenu_parent_id']
        if parent:
            children_by_parent.setdefault(parent, []).append(item)
        else:
            top_level.append(item)

    for item in top_level:
        item['children'] = children_by_parent.get(item['id'], [])

    return {
        'object_type': object_type,
        'object_id': object_id,
        'actions': top_level,
    }
