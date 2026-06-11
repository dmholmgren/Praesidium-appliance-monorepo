"""Seed tenant_branding with design token system.

Revision ID: 0013_design_tokens
Revises: 0012_projects_tasks
Create Date: 2026-05-10
"""
from alembic import op
import json

revision = "0013_design_tokens"
down_revision = "0012_projects_tasks"
branch_labels = None
depends_on = None

PLATFORM_TOKENS = {
    "primary":"#0D1F3C","primary-hover":"#162D52","primary-light":"#EFF6FF",
    "accent":"#C8923A","accent-hover":"#B07D2E",
    "surface":"#FFFFFF","surface-hover":"#F8FAFC","bg":"#F8FAFC","bg-muted":"#F1F5F9",
    "border":"#E2E8F0","border-subtle":"#F1F5F9","border-focus":"rgba(13,31,60,0.15)",
    "text":"#0F172A","text-secondary":"#475569","text-muted":"#94A3B8","text-inverse":"#FFFFFF",
    "success":"#065F46","success-bg":"#D1FAE5","warning":"#92400E","warning-bg":"#FEF3C7",
    "danger":"#991B1B","danger-bg":"#FEE2E2","info":"#1E40AF","info-bg":"#DBEAFE",
    "font-sans":"'Inter', system-ui, -apple-system, sans-serif",
    "font-serif":"'Lora', Georgia, serif","font-mono":"'JetBrains Mono', monospace",
    "rail-width":"64px","header-height":"56px","panel-width":"340px",
    "radius-sm":"4px","radius-md":"6px","radius-lg":"8px","radius-xl":"10px","radius-full":"9999px",
    "shadow-sm":"0 1px 2px rgba(0,0,0,0.05)","shadow-md":"0 4px 12px rgba(0,0,0,0.06)",
    "shadow-lg":"0 8px 30px rgba(0,0,0,0.08), 0 2px 8px rgba(0,0,0,0.04)",
    "transition-fast":"80ms ease","transition-base":"120ms ease","transition-slow":"200ms ease",
}

def upgrade():
    tokens_json = json.dumps(PLATFORM_TOKENS).replace("'", "''")
    op.execute(f"""
        INSERT INTO tenant_branding (
            tenant_id, firm_name, platform_name, platform_short_name,
            base_domain, primary_color, secondary_color, css_vars,
            email_from_name, pwa_name, pwa_short_name, pwa_theme_color,
            addin_display_name, suppress_attribution
        ) VALUES (
            '986c0fee-1390-43bb-ad28-8cd1db6de53f',
            'Holmgren, Johnson, Manickam & Mitchell PLLC',
            'Praesidium', 'Praesidium', 'hjmmlegal.com',
            '#0D1F3C', '#C8923A', '{tokens_json}'::jsonb,
            'Praesidium', 'Praesidium', 'Praesidium', '#0D1F3C',
            'Praesidium', FALSE
        )
        ON CONFLICT (tenant_id) DO UPDATE SET
            css_vars = '{tokens_json}'::jsonb,
            primary_color = '#0D1F3C', secondary_color = '#C8923A',
            updated_at = NOW()
    """)

def downgrade():
    op.execute("""
        UPDATE tenant_branding SET css_vars = '{{}}'::jsonb
        WHERE TRIM(tenant_id) = '986c0fee-1390-43bb-ad28-8cd1db6de53f'
    """)
