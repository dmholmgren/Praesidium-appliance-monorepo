"""
eDiscovery collection routes — HTMX endpoints on MAIN-PRD-WEB-01.

Includes the collection creation form with:
  - Source type dropdown
  - Checkbox to import client docs from DMS
  - DMS folder browser when checkbox is selected
"""

import os
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from rq import Queue

from core.auth import get_current_user
from core.audit import write_audit
from core.database import TenantSession, get_tenant_session
from core.services import get_storage_service, get_redis_connection
from modules.ediscovery.utils.brand_helper import get_brand
from modules.ediscovery.models.collections import (
    EdiscoveryCollection, CollectionStatus, SourceType,
)
from modules.ediscovery.models.documents import EdiscoveryDocument

router = APIRouter(prefix="/api/v1/ediscovery", tags=["ediscovery"])


# ----------------------------------------------------------------
# Pydantic schemas
# ----------------------------------------------------------------

class CollectionCreateRequest(BaseModel):
    matter_id: int
    collection_name: str
    source_type: str  # maps to SourceType enum
    source_party: Optional[str] = None
    received_date: Optional[date] = None
    received_method: Optional[str] = None
    stated_bates_range: Optional[str] = None
    import_from_dms: bool = False  # checkbox: get client docs from DMS
    dms_folder_path: Optional[str] = None  # DMS subfolder path
    dedicated_source_path: Optional[str] = None  # for dedicated storage uploads


class CollectionResponse(BaseModel):
    id: int
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
    req: CollectionCreateRequest,
    request: Request,
    session: TenantSession = Depends(get_tenant_session),
    user=Depends(get_current_user),
):
    """Create a new eDiscovery collection and dispatch ingestion job."""
    tenant_id = request.state.tenant_id
    brand = get_brand(request)

    # Resolve source type
    source_type = SourceType(req.source_type)

    # Build storage path on dedicated eDiscovery volume
    ediscovery_root = os.environ.get("EDISCOVERY_STORAGE_ROOT", "/mnt/ediscovery")
    storage_path = os.path.join(
        ediscovery_root, tenant_id, str(req.matter_id), req.collection_name.replace(" ", "_"),
    )

    # Determine DMS source path if importing from DMS
    dms_source_path = None
    if req.import_from_dms and req.dms_folder_path:
        dms_source_path = req.dms_folder_path
        if source_type == SourceType.client_collection_dedicated:
            source_type = SourceType.client_collection_dms
    elif req.dedicated_source_path:
        dms_source_path = req.dedicated_source_path

    collection = EdiscoveryCollection(
        tenant_id=tenant_id,
        matter_id=req.matter_id,
        collection_name=req.collection_name,
        storage_path=storage_path,
        source_type=source_type,
        source_party=req.source_party,
        received_date=req.received_date,
        received_method=req.received_method,
        received_by=user.id,
        stated_bates_range=req.stated_bates_range,
        dms_source_path=dms_source_path,
        status=CollectionStatus.collecting,
    )
    session.add(collection)
    session.commit()
    session.refresh(collection)

    # write_audit: Chat 0 signature is (session, action, table_name, record_id, details)
    try:
        write_audit(
            session,
            "ediscovery_collection_created",
            "ediscovery_collections",
            collection.id,
            {
                "tenant_id": tenant_id,
                "user_id": user.id,
                "source_type": source_type.value,
                "import_from_dms": req.import_from_dms,
                "dms_source_path": dms_source_path,
            },
        )
    except Exception:
        pass  # audit failure must not block collection creation

    # Dispatch ingestion job to PROC-01 RQ queue
    redis_conn = get_redis_connection()
    queue = Queue("ediscovery_proc", connection=redis_conn)
    queue.enqueue(
        "modules.ediscovery.jobs.ingest_collection.ingest_ediscovery_collection",
        tenant_id=tenant_id,
        collection_id=collection.id,
        user_id=user.id,
        job_timeout="24h",
    )

    return CollectionResponse(
        id=collection.id,
        collection_name=collection.collection_name,
        source_type=collection.source_type.value,
        source_party=collection.source_party,
        status=collection.status.value,
        total_docs=collection.total_docs or 0,
        processed_docs=collection.processed_docs or 0,
        reviewed_docs=collection.reviewed_docs or 0,
    )


@router.get("/collections", response_class=HTMLResponse)
async def list_collections(
    request: Request,
    matter_id: Optional[int] = None,
    session: TenantSession = Depends(get_tenant_session),
    user=Depends(get_current_user),
):
    """List collections — returns HTMX partial."""
    tenant_id = request.state.tenant_id
    query = session.query(EdiscoveryCollection).filter(
        EdiscoveryCollection.tenant_id == tenant_id,
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
            "brand": get_brand(request),
        },
    )


@router.get("/collections/{collection_id}")
async def get_collection(
    collection_id: int,
    request: Request,
    session: TenantSession = Depends(get_tenant_session),
    user=Depends(get_current_user),
):
    """Get collection details."""
    tenant_id = request.state.tenant_id
    collection = session.query(EdiscoveryCollection).filter(
        EdiscoveryCollection.id == collection_id,
        EdiscoveryCollection.tenant_id == tenant_id,
    ).first()
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    return CollectionResponse(
        id=collection.id,
        collection_name=collection.collection_name,
        source_type=collection.source_type.value,
        source_party=collection.source_party,
        status=collection.status.value,
        total_docs=collection.total_docs or 0,
        processed_docs=collection.processed_docs or 0,
        reviewed_docs=collection.reviewed_docs or 0,
    )


# ----------------------------------------------------------------
# DMS folder browser — for the "Import from DMS" dropdown
# ----------------------------------------------------------------

@router.get("/dms-folders/{matter_id}", response_class=HTMLResponse)
async def browse_dms_folders(
    matter_id: int,
    request: Request,
    path: Optional[str] = None,
    session: TenantSession = Depends(get_tenant_session),
    user=Depends(get_current_user),
):
    """
    Browse DMS matter folders — HTMX partial for folder picker.
    Returns a selectable folder tree rooted at the matter's DMS path.
    """
    tenant_id = request.state.tenant_id
    storage_service = get_storage_service(tenant_id)

    # Get matter's DMS root path
    from modules.dms.models import Matter
    matter = session.query(Matter).filter(
        Matter.id == matter_id,
        Matter.tenant_id == tenant_id,
    ).first()
    if not matter:
        raise HTTPException(status_code=404, detail="Matter not found")

    # Build base path from matter folder
    base_path = storage_service.get_matter_path(tenant_id, matter)
    browse_path = os.path.join(base_path, path) if path else base_path

    folders = []
    files_count = 0
    try:
        for entry in storage_service.list_directory(tenant_id, browse_path):
            if entry["is_dir"]:
                folders.append({
                    "name": entry["name"],
                    "path": os.path.relpath(
                        os.path.join(browse_path, entry["name"]),
                        base_path,
                    ),
                    "full_path": os.path.join(browse_path, entry["name"]),
                })
            else:
                files_count += 1
    except Exception as e:
        folders = []
        files_count = 0

    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory="modules/ediscovery/templates")
    return templates.TemplateResponse(
        "ediscovery/dms_folder_picker.html",
        {
            "request": request,
            "folders": folders,
            "files_count": files_count,
            "current_path": browse_path,
            "matter_id": matter_id,
            "brand": get_brand(request),
        },
    )
