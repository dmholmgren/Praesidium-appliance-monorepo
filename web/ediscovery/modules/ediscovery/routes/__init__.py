"""eDiscovery route registration."""

from fastapi import APIRouter
from modules.ediscovery.routes.collections import router as collections_router

router = APIRouter()
router.include_router(collections_router)
