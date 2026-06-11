"""
DMS & Auth Module Router Registration
Wire all component routes into the FastAPI application.
Import this in main.py to register all DMS endpoints.
"""

from fastapi import FastAPI
from sqlalchemy import text as sa_text


def register_dms_routes(app: FastAPI):
    """Register all DMS & Auth module routes."""

    # Auth routes (login/logout) — must be first
    from core.auth.routes import router as auth_router
    app.include_router(auth_router)

    # COMP 5: Document time tracking
    from modules.dms.services.time_tracking import router as time_router
    app.include_router(time_router)

    # COMP 6: DMS web UI
    from modules.dms.services.dms_routes import router as dms_router
    app.include_router(dms_router)

    # COMP 7: WebDAV endpoint
    from modules.dms.services.webdav_endpoint import router as webdav_router
    app.include_router(webdav_router)

    # COMP 8: Document comparison
    from modules.dms.services.comparison_engine import router as compare_router
    app.include_router(compare_router)

    # COMP 10: Research portal
    from modules.dms.services.research_portal import router as research_router
    app.include_router(research_router)

    # COMP 11: Document generation + sanity check
    from modules.dms.services.generation_engine import router as gen_router
    app.include_router(gen_router)

    # COMP 12: Citation suite
    from modules.dms.services.citation_suite import router as cite_router
    app.include_router(cite_router)

    # COMP 13+14: Office add-ins
    from modules.dms.services.office_addins import router as addin_router
    app.include_router(addin_router)

    # COMP 15: Scanning portal
    from modules.dms.services.scanning_portal import router as scan_router
    app.include_router(scan_router)

    # DMS upload + versioning + email
    from modules.dms.services.dms_upload_route import router as upload_router
    app.include_router(upload_router)

    # Admin: Crawl exclusion rules
    from modules.dms.services.crawl_exclusion_rules import router as crawl_rules_router
    app.include_router(crawl_rules_router)

    # OnlyOffice editor (oo-config, oo-download, oo-callback)
    from modules.dms.services.onlyoffice_route import router as oo_router
    app.include_router(oo_router)

    # Tags API (matter-level issue + project tag management)
    from modules.dms.services.tags_api import router as tags_api_router
    app.include_router(tags_api_router)

    # Folder mapping review API (file-import legacy onboarding)
    from modules.dms.services.folder_mapping_api import router as folder_map_router
    app.include_router(folder_map_router)
