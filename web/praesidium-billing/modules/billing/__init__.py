"""Billing module — registers API + view routes."""
from fastapi import FastAPI
from modules.billing.api.routes import router as api_router
from modules.billing.api.views import views as view_router

def register_billing_module(app: FastAPI):
    app.include_router(api_router)
    app.include_router(view_router)
