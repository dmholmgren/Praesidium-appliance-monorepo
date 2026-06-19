"""court_routes.py — Court homepage API. GET /api/v1/court/home (aggregate)."""
import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from modules.dashboard.services.auth_helper import get_current_user
from modules.depositions.routes.depo_api import _tenant, _serialize
from modules.intelligence import court_home as ch

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/court", tags=["court"])


@router.get("/home")
async def home(request: Request, user=Depends(get_current_user)):
    try:
        return JSONResponse(_serialize(await ch.court_home(_tenant(request))))
    except Exception as e:
        logger.exception("court_home failed")
        return JSONResponse({"error": str(e)}, 500)
