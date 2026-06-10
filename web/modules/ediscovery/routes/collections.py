"""
eDiscovery collection routes — HTMX endpoints on MAIN-PRD-WEB-01.

Fixed: matter_id str (UUID), collection_id str (UUID)
"""
import os
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Form
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from rq import Queue
from redis import Redis

from modules.dashboard.services.auth_helper import get_current_user
from core.audit import write_audit
from core.db.base import TenantSession, get_tenant_session, get_session_factory
from modules.ediscovery.models.collections import (
    EdiscoveryCollection, CollectionStatus, SourceType,
)
from modules.ediscovery.models.documents import EdiscoveryDocument

router = APIRouter(prefix="/api/v1/ediscovery", tags=["ediscovery"])

REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")


def get_rq_queue(name: str = "ediscovery") -> Queue:
    return Queue(name, connection=Redis.from_url(REDIS_URL))


# ----------------------------------------------------------------
# Pydantic schemas
# ----------------------------------------------------------------

class CollectionCreateRequest(BaseModel):
    matter_id: str
    collection_name: str
    source_type: str
    source_party: Optional[str] = None
    received_date: Optional[date] = None
    received_method: Optional[str] = None
    stated_bates_range: Optional[str] = None
    import_from_dms: bool = False
    dms_folder_path: Optional[str] = None
    dedicated_source_path: Optional[str] = None


class CollectionResponse(BaseModel):
    id: str
    collection_name: str
    source_type: str
    source_party: Optional[str]
    status: str
    total_docs: int
    processed_docs: int
    reviewed_docs: int


# ----------------------------------------------------------------
# Collection CRUD
# ----------------------------------------------------------------

@router.post("/collections", response_model=CollectionResponse)
async def create_collection(
    request: Request,
    user=Depends(get_current_user),
    matter_id: str = Form(...),
    collection_name: str = Form(...),
    source_type: str = Form(...),
    source_party: Optional[str] = Form(None),
    received_date: Optional[str] = Form(None),
    received_method: Optional[str] = Form(None),
    stated_bates_range: Optional[str] = Form(None),
    import_from_dms: Optional[str] = Form(None),
    dms_folder_path: Optional[str] = Form(None),
    dedicated_source_path: Optional[str] = Form(None),
):
    class _Req:
        pass
    req = _Req()
    req.matter_id = matter_id
    req.collection_name = collection_name
    req.source_type = source_type
    req.source_party = source_party
    req.received_date = None
    req.received_method = received_method
    req.stated_bates_range = stated_bates_range
    req.import_from_dms = import_from_dms == 'on' or import_from_dms == 'true'
    req.dms_folder_path = dms_folder_path
    req.dedicated_source_path = dedicated_source_path
    """Create a new eDiscovery collection and dispatch ingestion job."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=400, detail="No tenant resolved")

    source_type = SourceType(req.source_type)

    ediscovery_root = os.environ.get("EDISCOVERY_STORAGE_ROOT", "/mnt/ediscovery")
    storage_path = os.path.join(
        ediscovery_root, tenant_id, req.matter_id, req.collection_name.replace(" ", "_"),
    )

    dms_source_path = None
    if req.import_from_dms and req.dms_folder_path:
        dms_source_path = req.dms_folder_path
        if source_type == SourceType.client_collection_dedicated:
            source_type = SourceType.client_collection_dms
    elif req.dedicated_source_path:
        dms_source_path = req.dedicated_source_path

    factory = get_session_factory()
    session = factory()
    try:
        collection = EdiscoveryCollection(
            tenant_id=tenant_id,
            matter_id=req.matter_id,
            collection_name=req.collection_name,
            storage_path=storage_path,
            source_type=source_type,
            source_party=req.source_party,
            received_date=req.received_date,
            received_method=req.received_method,
            received_by=getattr(user, "id", None),
            stated_bates_range=req.stated_bates_range,
            dms_source_path=dms_source_path,
            status=CollectionStatus.collecting,
        )
        session.add(collection)
        session.commit()
        session.refresh(collection)

        try:
            write_audit(
                session,
                "ediscovery_collection_created",
                "ediscovery_collections",
                str(collection.id),
                {
                    "tenant_id": tenant_id,
                    "source_type": source_type.value,
                    "import_from_dms": req.import_from_dms,
                },
            )
        except Exception:
            pass

        q = get_rq_queue("ediscovery")
        q.enqueue(
            "modules.ediscovery.jobs.ledger_dag.run_collection_full",
            tenant_id,
            str(collection.id),
            getattr(user, "id", None),
            spine_workers=6,
            ocr_workers=2,
            embed_workers=1,
            job_timeout="24h",
            result_ttl=3600,
        )

        return CollectionResponse(
            id=str(collection.id),
            collection_name=collection.collection_name,
            source_type=collection.source_type.value,
            source_party=collection.source_party,
            status=collection.status.value,
            total_docs=collection.total_docs or 0,
            processed_docs=collection.processed_docs or 0,
            reviewed_docs=collection.reviewed_docs or 0,
        )
    finally:
        session.close()


@router.get("/collections", response_class=HTMLResponse)
async def list_collections(
    request: Request,
    matter_id: Optional[str] = None,
    user=Depends(get_current_user),
):
    """List collections — returns HTMX partial."""
    tenant_id = getattr(request.state, "tenant_id", None)
    factory = get_session_factory()
    session = factory()
    try:
        query = session.query(EdiscoveryCollection).filter(
            EdiscoveryCollection.tenant_id == tenant_id,
            EdiscoveryCollection.parent_collection_id == None,  # top-level only
        )
        # Hide internal decomposition children
        if hasattr(EdiscoveryCollection, 'is_internal'):
            from sqlalchemy import or_
            query = query.filter(
                or_(EdiscoveryCollection.is_internal == False,
                    EdiscoveryCollection.is_internal == None)
            )
        if matter_id:
            query = query.filter(EdiscoveryCollection.matter_id == matter_id)

        collections = query.order_by(EdiscoveryCollection.created_at.desc()).all()

        from fastapi.templating import Jinja2Templates
        templates = Jinja2Templates(directory="modules/ediscovery/templates")
        return templates.TemplateResponse(
            "ediscovery/collection_list.html",
            {
                "request": request,
                "collections": collections,
                "brand": getattr(request.state, "branding", None),
                
            },
        )
    finally:
        session.close()


@router.get("/collections/{collection_id}")
async def get_collection(
    collection_id: str,
    request: Request,
    user=Depends(get_current_user),
):
    """Get collection details."""
    tenant_id = getattr(request.state, "tenant_id", None)
    factory = get_session_factory()
    session = factory()
    try:
        collection = session.query(EdiscoveryCollection).filter(
            EdiscoveryCollection.id == collection_id,
            EdiscoveryCollection.tenant_id == tenant_id,
        ).first()
        if not collection:
            raise HTTPException(status_code=404, detail="Collection not found")

        return CollectionResponse(
            id=str(collection.id),
            collection_name=collection.collection_name,
            source_type=collection.source_type.value,
            source_party=collection.source_party,
            status=collection.status.value,
            total_docs=collection.total_docs or 0,
            processed_docs=collection.processed_docs or 0,
            reviewed_docs=collection.reviewed_docs or 0,
        )
    finally:
        session.close()


# ----------------------------------------------------------------
# DMS folder browser
# ----------------------------------------------------------------

@router.get("/dms-folders/{matter_id}", response_class=HTMLResponse)
async def browse_dms_folders(
    matter_id: str,
    request: Request,
    path: Optional[str] = None,
    user=Depends(get_current_user),
):
    """Browse DMS matter folders — HTMX partial for folder picker."""
    tenant_id = getattr(request.state, "tenant_id", None)

    factory = get_session_factory()
    session = factory()
    try:
        from sqlalchemy import text as sa_text
        result = session.execute(
            sa_text("SELECT matter_number, matter_name FROM matters WHERE id = :mid AND tenant_id = :tid"),
            {"mid": matter_id, "tid": tenant_id},
        ).fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Matter not found")

        from fastapi.templating import Jinja2Templates
        templates = Jinja2Templates(directory="modules/ediscovery/templates")
        return templates.TemplateResponse(
            "ediscovery/dms_folder_picker.html",
            {
                "request": request,
                "folders": [],
                "files_count": 0,
                "current_path": f"Matter: {result[0]} - {result[1]}",
                "matter_id": matter_id,
                "brand": getattr(request.state, "branding", None),
            },
        )
    finally:
        session.close()
