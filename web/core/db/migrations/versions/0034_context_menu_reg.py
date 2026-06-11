"""Context menu registry — data-driven right-click actions.

Revision ID: 0034_context_menu_reg
Revises: 0033_portal_magic_lnk
Create Date: 2026-05-19

Every right-click action is a row.  The React <ContextMenu> component
calls GET /api/v1/context-menu/{object_type}/{object_id}, the backend
evaluates each action's condition_query, and returns the applicable items.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
import uuid

revision = '0034_context_menu_reg'
down_revision = '0033_portal_magic_lnk'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    op.create_table(
        'context_menu_actions',
        sa.Column('id', sa.dialects.postgresql.UUID(), primary_key=True),
        sa.Column('tenant_id', sa.CHAR(36), nullable=True,
                  comment='NULL = platform-standard, populated = tenant-custom'),
        sa.Column('object_type', sa.VARCHAR(50), nullable=False,
                  comment='document | folder | email | deposition | contact | exhibit | production | time_entry | matter | project'),
        sa.Column('action_slug', sa.VARCHAR(100), nullable=False,
                  comment='Unique per object_type: preview, download, email, rename, etc.'),
        sa.Column('label', sa.VARCHAR(200), nullable=False,
                  comment='Display text: "Preview", "Go to Witness Page", etc.'),
        sa.Column('icon', sa.VARCHAR(50), nullable=True,
                  comment='Lucide icon name or emoji'),
        sa.Column('action_type', sa.VARCHAR(30), nullable=False, server_default='navigate',
                  comment='navigate | api_call | js_action | submenu'),
        sa.Column('navigate_to_template', sa.VARCHAR(500), nullable=True,
                  comment='URL template with {placeholders} resolved from condition_query result'),
        sa.Column('api_endpoint', sa.VARCHAR(500), nullable=True,
                  comment='For api_call type: POST endpoint to call'),
        sa.Column('js_action', sa.VARCHAR(200), nullable=True,
                  comment='For js_action type: window function name, e.g. openOnlyOfficeEditor'),
        sa.Column('condition_query', sa.Text(), nullable=True,
                  comment='SQL SELECT returning one row if action applies. Bind params: :object_id, :tenant_id, :user_id. NULL = always show.'),
        sa.Column('condition_result_keys', sa.dialects.postgresql.JSONB(),
                  nullable=True, server_default='{}',
                  comment='Maps condition query columns to template placeholders'),
        sa.Column('requires_selection', sa.Boolean(), nullable=False, server_default='false',
                  comment='True = only show when object is selected/focused'),
        sa.Column('multi_select', sa.Boolean(), nullable=False, server_default='false',
                  comment='True = available in multi-select mode'),
        sa.Column('separator_before', sa.Boolean(), nullable=False, server_default='false',
                  comment='Render a divider line above this item'),
        sa.Column('keyboard_shortcut', sa.VARCHAR(30), nullable=True,
                  comment='e.g. Shift+W, Shift+D, Cmd+K'),
        sa.Column('display_order', sa.Integer(), nullable=False, server_default='50'),
        sa.Column('permission_level', sa.VARCHAR(30), nullable=False, server_default='attorney',
                  comment='attorney | partner | admin | superadmin'),
        sa.Column('feature_flag', sa.VARCHAR(100), nullable=True),
        sa.Column('scope', sa.VARCHAR(20), nullable=False, server_default='global',
                  comment='global | tenant | user'),
        sa.Column('is_platform_standard', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('submenu_parent_id', sa.dialects.postgresql.UUID(), nullable=True,
                  comment='For submenu items: FK to parent action'),
        sa.Column('confirm_message', sa.VARCHAR(500), nullable=True,
                  comment='If set, show confirmation dialog before executing'),
        sa.Column('config', sa.dialects.postgresql.JSONB(),
                  nullable=True, server_default='{}',
                  comment='Extensible config: css_class, badge, tooltip, etc.'),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
    )

    op.create_index(
        'ix_ctx_menu_slug_type_tenant',
        'context_menu_actions',
        [sa.text("action_slug"), sa.text("object_type"), sa.text("COALESCE(tenant_id, '')")],
        unique=True,
    )

    op.create_index(
        'ix_ctx_menu_object_type',
        'context_menu_actions',
        ['object_type', 'display_order'],
    )

    op.create_index(
        'ix_ctx_menu_parent',
        'context_menu_actions',
        ['submenu_parent_id'],
        postgresql_where=sa.text("submenu_parent_id IS NOT NULL"),
    )

    op.create_foreign_key(
        'fk_ctx_menu_parent',
        'context_menu_actions', 'context_menu_actions',
        ['submenu_parent_id'], ['id'],
        ondelete='CASCADE',
    )

    op.create_table(
        'user_context_menu_prefs',
        sa.Column('id', sa.dialects.postgresql.UUID(), primary_key=True),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('action_id', sa.dialects.postgresql.UUID(), nullable=False),
        sa.Column('is_hidden', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('custom_order', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
    )

    op.create_foreign_key(
        'fk_ctx_pref_action',
        'user_context_menu_prefs', 'context_menu_actions',
        ['action_id'], ['id'],
        ondelete='CASCADE',
    )

    op.create_index(
        'ix_ctx_pref_user_action',
        'user_context_menu_prefs',
        ['user_id', 'action_id'],
        unique=True,
    )

    # ── Seed: Platform-standard context menu actions ─────────

    def uid():
        return str(uuid.uuid4())

    send_to_project_id = uid()

    rows = [
        # ── DOCUMENT ─────────────────────────────────────────
        {'id': uid(), 'object_type': 'document', 'action_slug': 'preview',
         'label': 'Preview', 'icon': 'eye',
         'action_type': 'js_action', 'js_action': 'previewDocument',
         'display_order': 10},
        {'id': uid(), 'object_type': 'document', 'action_slug': 'open_editor',
         'label': 'Edit in OnlyOffice', 'icon': 'file-edit',
         'action_type': 'js_action', 'js_action': 'openOnlyOfficeEditor',
         'condition_query': "SELECT 1 FROM documents d WHERE d.id = CAST(:object_id AS uuid) AND (d.file_name ILIKE '%.docx' OR d.file_name ILIKE '%.xlsx' OR d.file_name ILIKE '%.pptx')",
         'display_order': 15},
        {'id': uid(), 'object_type': 'document', 'action_slug': 'download',
         'label': 'Download', 'icon': 'download',
         'action_type': 'js_action', 'js_action': 'downloadDocument',
         'display_order': 20},
        {'id': uid(), 'object_type': 'document', 'action_slug': 'email',
         'label': 'Email', 'icon': 'mail',
         'action_type': 'js_action', 'js_action': 'emailDocument',
         'display_order': 30},
        {'id': uid(), 'object_type': 'document', 'action_slug': 'copy_link',
         'label': 'Copy Link', 'icon': 'link',
         'action_type': 'js_action', 'js_action': 'copyDocumentLink',
         'display_order': 40},
        {'id': uid(), 'object_type': 'document', 'action_slug': 'check_out',
         'label': 'Check Out', 'icon': 'lock',
         'action_type': 'api_call', 'api_endpoint': '/api/v1/dms/matter/{matter_id}/checkout',
         'display_order': 50, 'separator_before': True},
        {'id': uid(), 'object_type': 'document', 'action_slug': 'rename',
         'label': 'Rename', 'icon': 'pencil',
         'action_type': 'js_action', 'js_action': 'renameDocument',
         'display_order': 55},
        {'id': uid(), 'object_type': 'document', 'action_slug': 'add_to_exhibits',
         'label': 'Add to Exhibits', 'icon': 'paperclip',
         'action_type': 'js_action', 'js_action': '_dmsAddExhibit',
         'display_order': 60},
        {'id': uid(), 'object_type': 'document', 'action_slug': 'save_new_version',
         'label': 'Save New Version', 'icon': 'save',
         'action_type': 'api_call', 'api_endpoint': '/api/v1/dms/matter/{matter_id}/oo-saveas',
         'display_order': 65},
        {'id': uid(), 'object_type': 'document', 'action_slug': 'key_document',
         'label': 'Toggle Key Document', 'icon': 'star',
         'action_type': 'api_call', 'api_endpoint': '/api/v1/dms/matter/{matter_id}/key-document',
         'display_order': 70},
        {'id': send_to_project_id, 'object_type': 'document',
         'action_slug': 'send_to_project',
         'label': 'Send to Project', 'icon': 'folder-kanban',
         'action_type': 'submenu',
         'display_order': 80, 'separator_before': True},
        {'id': uid(), 'object_type': 'document', 'action_slug': 'send_to_meeting',
         'label': 'Send to Meeting', 'icon': 'calendar',
         'action_type': 'submenu',
         'display_order': 85},
        {'id': uid(), 'object_type': 'document', 'action_slug': 'convert_to_pdf',
         'label': 'Convert to PDF', 'icon': 'file-type',
         'action_type': 'api_call', 'api_endpoint': '/api/v1/dms/matter/{matter_id}/convert-pdf',
         'condition_query': "SELECT 1 FROM documents d WHERE d.id = CAST(:object_id AS uuid) AND (d.file_name ILIKE '%.docx' OR d.file_name ILIKE '%.xlsx' OR d.file_name ILIKE '%.pptx' OR d.file_name ILIKE '%.html')",
         'display_order': 90},
        # Cross-module navigation
        {'id': uid(), 'object_type': 'document', 'action_slug': 'go_to_witness',
         'label': 'Go to Witness Page', 'icon': 'user',
         'action_type': 'navigate',
         'navigate_to_template': '/matters/{matter_id}/witnesses/{witness_contact_id}',
         'condition_query': "SELECT mc.contact_id AS witness_contact_id FROM matter_contacts mc JOIN documents d ON d.matter_id = mc.matter_id WHERE d.id = CAST(:object_id AS uuid) AND mc.role = 'witness' AND d.storage_path ILIKE '%' || mc.contact_id::text || '%' LIMIT 1",
         'condition_result_keys': '{"witness_contact_id": "witness_contact_id"}',
         'display_order': 100, 'separator_before': True,
         'keyboard_shortcut': 'Shift+W'},
        {'id': uid(), 'object_type': 'document', 'action_slug': 'reveal_in_dms',
         'label': 'Reveal in DMS', 'icon': 'folder-open',
         'action_type': 'navigate',
         'navigate_to_template': '/matters/{matter_id}/workspace?reveal={object_id}',
         'display_order': 105,
         'keyboard_shortcut': 'Shift+D'},
        {'id': uid(), 'object_type': 'document', 'action_slug': 'open_matter_dashboard',
         'label': 'Open Matter Dashboard', 'icon': 'layout-dashboard',
         'action_type': 'navigate',
         'navigate_to_template': '/matters/{matter_id}/dashboard',
         'display_order': 110,
         'keyboard_shortcut': 'Shift+M'},
        {'id': uid(), 'object_type': 'document', 'action_slug': 'delete',
         'label': 'Delete', 'icon': 'trash',
         'action_type': 'api_call', 'api_endpoint': '/api/v1/dms/matter/{matter_id}/delete',
         'display_order': 200, 'separator_before': True,
         'confirm_message': 'Are you sure you want to delete this document?',
         'multi_select': True,
         'config': '{"css_class": "text-red-600"}'},
        # ── FOLDER ───────────────────────────────────────────
        {'id': uid(), 'object_type': 'folder', 'action_slug': 'new_document',
         'label': 'New Document', 'icon': 'file-plus',
         'action_type': 'api_call', 'api_endpoint': '/api/v1/dms/matter/{matter_id}/create-blank-doc',
         'display_order': 10},
        {'id': uid(), 'object_type': 'folder', 'action_slug': 'new_email',
         'label': 'New Email', 'icon': 'mail-plus',
         'action_type': 'js_action', 'js_action': 'openEmailCompose',
         'display_order': 20},
        {'id': uid(), 'object_type': 'folder', 'action_slug': 'rename',
         'label': 'Rename', 'icon': 'pencil',
         'action_type': 'js_action', 'js_action': 'renameFolder',
         'display_order': 30},
        {'id': uid(), 'object_type': 'folder', 'action_slug': 'upload_files',
         'label': 'Upload Files', 'icon': 'upload',
         'action_type': 'js_action', 'js_action': 'uploadToFolder',
         'display_order': 40},
        # ── EMAIL ────────────────────────────────────────────
        {'id': uid(), 'object_type': 'email', 'action_slug': 'reply',
         'label': 'Reply', 'icon': 'reply',
         'action_type': 'js_action', 'js_action': 'replyEmail',
         'display_order': 10},
        {'id': uid(), 'object_type': 'email', 'action_slug': 'reply_all',
         'label': 'Reply All', 'icon': 'reply-all',
         'action_type': 'js_action', 'js_action': 'replyAllEmail',
         'display_order': 20},
        {'id': uid(), 'object_type': 'email', 'action_slug': 'forward',
         'label': 'Forward', 'icon': 'forward',
         'action_type': 'js_action', 'js_action': 'forwardEmail',
         'display_order': 30},
        {'id': uid(), 'object_type': 'email', 'action_slug': 'new_email',
         'label': 'New Email', 'icon': 'mail-plus',
         'action_type': 'js_action', 'js_action': 'openEmailCompose',
         'display_order': 40, 'separator_before': True},
        {'id': uid(), 'object_type': 'email', 'action_slug': 'new_document',
         'label': 'New Document', 'icon': 'file-plus',
         'action_type': 'api_call', 'api_endpoint': '/api/v1/dms/matter/{matter_id}/create-blank-doc',
         'display_order': 50},
        {'id': uid(), 'object_type': 'email', 'action_slug': 'file_to_matter',
         'label': 'File to Matter', 'icon': 'inbox',
         'action_type': 'js_action', 'js_action': 'fileEmailToMatter',
         'display_order': 60, 'separator_before': True},
        # ── MATTER ───────────────────────────────────────────
        {'id': uid(), 'object_type': 'matter', 'action_slug': 'open_dashboard',
         'label': 'Open Dashboard', 'icon': 'layout-dashboard',
         'action_type': 'navigate', 'navigate_to_template': '/matters/{object_id}/dashboard',
         'display_order': 10},
        {'id': uid(), 'object_type': 'matter', 'action_slug': 'open_workspace',
         'label': 'Open Workspace', 'icon': 'folder-open',
         'action_type': 'navigate', 'navigate_to_template': '/matters/{object_id}/workspace',
         'display_order': 20},
        {'id': uid(), 'object_type': 'matter', 'action_slug': 'open_billing',
         'label': 'Billing', 'icon': 'receipt',
         'action_type': 'navigate', 'navigate_to_template': '/billing/matters/{object_id}',
         'display_order': 30},
        {'id': uid(), 'object_type': 'matter', 'action_slug': 'open_ediscovery',
         'label': 'eDiscovery', 'icon': 'search',
         'action_type': 'navigate', 'navigate_to_template': '/ediscovery/matters/{object_id}',
         'display_order': 40,
         'condition_query': "SELECT 1 FROM ediscovery_collections ec WHERE ec.matter_id = CAST(:object_id AS uuid) LIMIT 1"},
        {'id': uid(), 'object_type': 'matter', 'action_slug': 'new_email',
         'label': 'New Email', 'icon': 'mail-plus',
         'action_type': 'js_action', 'js_action': 'openEmailCompose',
         'display_order': 50, 'separator_before': True},
        {'id': uid(), 'object_type': 'matter', 'action_slug': 'new_time_entry',
         'label': 'New Time Entry', 'icon': 'clock',
         'action_type': 'js_action', 'js_action': 'openTimeEntry',
         'display_order': 60},
    ]

    insert_sql = sa.text("""
        INSERT INTO context_menu_actions (
            id, object_type, action_slug, label, icon, action_type,
            navigate_to_template, api_endpoint, js_action,
            condition_query, condition_result_keys,
            requires_selection, multi_select, separator_before,
            keyboard_shortcut, display_order, permission_level,
            scope, is_platform_standard, is_active,
            submenu_parent_id, confirm_message, config
        ) VALUES (
            CAST(:id AS uuid), :object_type, :action_slug, :label, :icon, :action_type,
            :navigate_to_template, :api_endpoint, :js_action,
            :condition_query, CAST(:condition_result_keys AS jsonb),
            :requires_selection, :multi_select, :separator_before,
            :keyboard_shortcut, :display_order, :permission_level,
            :scope, :is_platform_standard, :is_active,
            CAST(:submenu_parent_id AS uuid), :confirm_message,
            CAST(:config AS jsonb)
        )
    """)

    defaults = {
        'icon': None, 'action_type': 'navigate',
        'navigate_to_template': None, 'api_endpoint': None, 'js_action': None,
        'condition_query': None, 'condition_result_keys': '{}',
        'requires_selection': False, 'multi_select': False, 'separator_before': False,
        'keyboard_shortcut': None, 'permission_level': 'attorney',
        'scope': 'global', 'is_platform_standard': True, 'is_active': True,
        'submenu_parent_id': None, 'confirm_message': None, 'config': '{}',
    }

    for row in rows:
        params = {**defaults, **row}
        conn.execute(insert_sql, params)


def downgrade():
    op.drop_table('user_context_menu_prefs')
    op.drop_table('context_menu_actions')
