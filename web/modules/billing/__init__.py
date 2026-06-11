"""Billing module - registers API + view routes."""
from fastapi import FastAPI
from modules.billing.api.routes import router as api_router
from modules.billing.api.views import views as view_router
from modules.billing.api.intake_api import router as intake_router
from modules.billing.api.billing_home_api import router as billing_home_api_router
from modules.billing.api.billing_client_dashboard_api import router as billing_client_dashboard_router
from modules.billing.api.billing_matter_dashboard_api import router as billing_matter_dashboard_router
from modules.billing.api.billing_trust_reports_api import router as billing_trust_reports_router
from modules.billing.api.billing_bill_runs_api import router as billing_bill_runs_api_router
from modules.billing.api.billing_timekeepers_api import router as billing_tk_router
from modules.billing.api.bill_run_selector_api import router as bill_run_selector_router
from modules.billing.api.bill_templates_api import router as bill_templates_api_router
from modules.billing.api.billing_templates_routes import router as billing_settings_views_router
from modules.billing.api.billing_settings_api import router as billing_settings_api_router
from modules.billing.api.time_entry_ops_api import router as time_entry_ops_router
from modules.billing.api.invoice_pdf_api import router as invoice_pdf_router
from modules.billing.api.bill_run_statement_api import router as statement_pdf_router
from modules.billing.api.billing_dashboard_widgets_api import router as dashboard_widgets_router


def register_billing_module(app: FastAPI):
    app.include_router(api_router)
    app.include_router(view_router)
    app.include_router(intake_router)
    app.include_router(billing_home_api_router)
    app.include_router(billing_client_dashboard_router)
    app.include_router(billing_matter_dashboard_router)
    app.include_router(billing_trust_reports_router)
    app.include_router(billing_bill_runs_api_router)
    app.include_router(billing_tk_router)
    app.include_router(bill_run_selector_router)
    app.include_router(bill_templates_api_router)
    app.include_router(billing_settings_views_router)
    from modules.billing.api.client_portal_api import router as client_portal_router
    app.include_router(client_portal_router)
    app.include_router(billing_settings_api_router)
    app.include_router(time_entry_ops_router)
    app.include_router(invoice_pdf_router)
    app.include_router(statement_pdf_router)
    app.include_router(dashboard_widgets_router)

    try:
        from modules.billing.api.billing_routes import router as billing_routes_router
        app.include_router(billing_routes_router)
        from modules.billing.api.billing_routes import billing_chat_router
        app.include_router(billing_chat_router)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("billing_routes not loaded: %s", e)

    try:
        from modules.billing.api.bill_run_routes import router as bill_run_router
        app.include_router(bill_run_router)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("bill_run_routes not loaded: %s", e)

    try:
        from modules.billing.api.connector_api import router as connector_router
        from modules.billing.api.connector_api import webhook_router
        app.include_router(connector_router)
        app.include_router(webhook_router)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("connector_api not loaded: %s", e)
