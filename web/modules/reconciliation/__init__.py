"""Reconciliation module — registers routes."""
from fastapi import FastAPI


def register_reconciliation_module(app: FastAPI):
    from modules.reconciliation.views import views as recon_views
    from modules.reconciliation.recon_home_api import router as recon_home_api
    from modules.reconciliation.recon_timesheet_api import router as recon_timesheet_api
    app.include_router(recon_views)
    app.include_router(recon_home_api)
    app.include_router(recon_timesheet_api)
    from modules.reconciliation.recon_task_api import router as recon_task_api
    app.include_router(recon_task_api)
