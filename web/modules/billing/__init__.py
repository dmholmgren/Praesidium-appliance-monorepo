"""Billing module — registers API + view routes."""
from fastapi import FastAPI
from modules.billing.api.routes import router as api_router
from modules.billing.api.views import views as view_router
from modules.billing.api.intake_api import router as intake_router


def register_billing_module(app: FastAPI):
    app.include_router(api_router)
    app.include_router(view_router)
    app.include_router(intake_router)

    # Register billing_routes.py endpoints (timekeepers, client-dashboard, etc.)
    try:
        from modules.billing.api.billing_routes import router as billing_routes_router
        app.include_router(billing_routes_router)
        from modules.billing.api.billing_routes import billing_chat_router
        app.include_router(billing_chat_router)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("billing_routes not loaded: %s", e)
