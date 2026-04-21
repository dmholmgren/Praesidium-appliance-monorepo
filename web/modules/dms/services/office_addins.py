from modules.dms.brand_helper import get_brand
"""
COMP 13 — Office Web Add-In Backend
TypeScript manifest + backend API endpoints.
addin_display_name from tenant_branding — not hardcoded.

COMP 14 — COM Add-In Backend (Windows GPO deployment)
Python COM wrapper registration + ribbon XML generator.
"""

import os
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import text as sa_text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/addin", tags=["office-addin"])


# ═══════════════════════════════════════════════════════════════
# COMP 13 — OFFICE WEB ADD-IN
# ═══════════════════════════════════════════════════════════════

@router.get("/manifest.xml")
async def get_addin_manifest(request: Request):
    """
    Generate the Office Web Add-In manifest XML dynamically.
    addin_display_name comes from tenant_branding — never hardcoded.
    """
    brand = get_brand(request)
    tenant_id = request.state.tenant_id

    # All values from BrandingService
    display_name = brand.get("addin_display_name", brand.get("platform.matter_name", "Legal Platform"))
    base_url = brand.get("docs_url", "")
    support_url = brand.get("support_url", base_url)
    provider_name = brand.get("company_name", "")
    icon_url = brand.get("addin_icon_url", f"{base_url}/static/addin-icon.png")

    manifest = f"""<?xml version="1.0" encoding="UTF-8"?>
<OfficeApp xmlns="http://schemas.microsoft.com/office/appforoffice/1.1"
           xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
           xsi:type="TaskPaneApp">
  <Id>{uuid.uuid5(uuid.NAMESPACE_URL, f"addin-{tenant_id}")}</Id>
  <Version>1.0.0</Version>
  <ProviderName>{provider_name}</ProviderName>
  <DefaultLocale>en-US</DefaultLocale>
  <DisplayName DefaultValue="{display_name}"/>
  <Description DefaultValue="{display_name} — Document Management Integration"/>
  <IconUrl DefaultValue="{icon_url}"/>
  <SupportUrl DefaultValue="{support_url}"/>
  <Hosts>
    <Host Name="Document"/>
    <Host Name="Workbook"/>
    <Host Name="Presentation"/>
  </Hosts>
  <DefaultSettings>
    <SourceLocation DefaultValue="{base_url}/api/v1/addin/taskpane"/>
  </DefaultSettings>
  <Permissions>ReadWriteDocument</Permissions>
  <VersionOverrides xmlns="http://schemas.microsoft.com/office/taskpaneappversionoverrides"
                    xsi:type="VersionOverridesV1_0">
    <Hosts>
      <Host xsi:type="Document">
        <DesktopFormFactor>
          <ExtensionPoint xsi:type="PrimaryCommandSurface">
            <OfficeTab id="TabHome">
              <Group id="DmsGroup">
                <Label resid="GroupLabel"/>
                <Icon><bt:Image size="16" resid="Icon16"/></Icon>
                <Control xsi:type="Button" id="SaveToDms">
                  <Label resid="SaveLabel"/>
                  <Icon><bt:Image size="16" resid="Icon16"/></Icon>
                  <Action xsi:type="ShowTaskpane">
                    <TaskpaneId>DmsTaskpane</TaskpaneId>
                    <SourceLocation resid="TaskpaneUrl"/>
                  </Action>
                </Control>
                <Control xsi:type="Button" id="SearchDms">
                  <Label resid="SearchLabel"/>
                  <Icon><bt:Image size="16" resid="Icon16"/></Icon>
                  <Action xsi:type="ShowTaskpane">
                    <TaskpaneId>SearchTaskpane</TaskpaneId>
                    <SourceLocation resid="SearchUrl"/>
                  </Action>
                </Control>
              </Group>
            </OfficeTab>
          </ExtensionPoint>
        </DesktopFormFactor>
      </Host>
    </Hosts>
    <Resources>
      <bt:Urls>
        <bt:Url id="TaskpaneUrl" DefaultValue="{base_url}/api/v1/addin/taskpane"/>
        <bt:Url id="SearchUrl" DefaultValue="{base_url}/api/v1/addin/search"/>
      </bt:Urls>
      <bt:ShortStrings>
        <bt:String id="GroupLabel" DefaultValue="{display_name}"/>
        <bt:String id="SaveLabel" DefaultValue="Save to DMS"/>
        <bt:String id="SearchLabel" DefaultValue="Search DMS"/>
      </bt:ShortStrings>
      <bt:Images>
        <bt:Image id="Icon16" DefaultValue="{icon_url}"/>
      </bt:Images>
    </Resources>
  </VersionOverrides>
</OfficeApp>"""

    return Response(
        content=manifest,
        media_type="application/xml",
        headers={"Content-Disposition": "attachment; filename=manifest.xml"},
    )


@router.get("/taskpane")
async def addin_taskpane(request: Request):
    """Render the Add-In task pane for save/browse operations."""
    brand = get_brand(request)
    display_name = brand.get("addin_display_name", brand.get("platform.matter_name", "Legal Platform"))
    base_url = brand.get("docs_url", "")

    html = f"""<!DOCTYPE html>
<html><head>
<title>{display_name}</title>
<script src="https://appsforoffice.microsoft.com/lib/1.1/hosted/office.js"></script>
<script src="https://cdn.tailwindcss.com"></script>
</head><body class="p-4 bg-gray-50">
<h1 class="text-lg font-bold text-gray-800 mb-4">{display_name}</h1>
<div id="app">
  <div class="mb-4">
    <label class="block text-sm font-medium text-gray-700 mb-1">Matter</label>
    <select id="matter-select" class="w-full border rounded px-3 py-2 text-sm">
      <option>Loading matters...</option>
    </select>
  </div>
  <div class="mb-4">
    <label class="block text-sm font-medium text-gray-700 mb-1">Folder</label>
    <input id="folder-input" type="text" placeholder="e.g. Pleadings" class="w-full border rounded px-3 py-2 text-sm">
  </div>
  <button onclick="saveDocument()" class="w-full bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700 mb-2">
    Save to DMS
  </button>
  <button onclick="searchDms()" class="w-full bg-gray-100 text-gray-700 px-4 py-2 rounded text-sm hover:bg-gray-200">
    Search Documents
  </button>
  <div id="status" class="mt-4 text-sm"></div>
</div>
<script>
Office.onReady(function(info) {{
  loadMatters();
}});
async function loadMatters() {{
  const resp = await fetch('{base_url}/api/v1/dms/matters');
  const data = await resp.json();
  const sel = document.getElementById('matter-select');
  sel.innerHTML = data.matters.map(m => '<option value="'+m.id+'">'+m.matter_name+'</option>').join('');
}}
async function saveDocument() {{
  document.getElementById('status').innerText = 'Saving...';
  Office.context.document.getFileAsync(Office.FileType.Compressed, {{sliceSize: 4096}}, function(result) {{
    if (result.status === 'succeeded') {{
      const file = result.value;
      file.getSliceAsync(0, async function(sliceResult) {{
        const blob = new Blob([sliceResult.value.data]);
        const form = new FormData();
        form.append('file', blob, 'document.docx');
        form.append('matter_id', document.getElementById('matter-select').value);
        form.append('folder_path', document.getElementById('folder-input').value);
        const resp = await fetch('{base_url}/dms/upload', {{method: 'POST', body: form}});
        document.getElementById('status').innerText = resp.ok ? 'Saved!' : 'Failed';
        file.closeAsync();
      }});
    }}
  }});
}}
</script>
</body></html>"""
    return Response(content=html, media_type="text/html")


@router.get("/search")
async def addin_search(request: Request, q: str = ""):
    """Search endpoint for the Add-In task pane."""
    from modules.dms.services.search_service import MeilisearchService

    tenant_id = request.state.tenant_id
    results = {"hits": []}
    if q:
        meili = MeilisearchService()
        results = await meili.search(tenant_id, q, limit=10)
        await meili.close()

    return results


# ═══════════════════════════════════════════════════════════════
# COMP 14 — COM ADD-IN BACKEND (Windows GPO deployment)
# ═══════════════════════════════════════════════════════════════

@router.get("/com/ribbon.xml")
async def get_com_ribbon_xml(request: Request):
    """
    Generate the COM Add-In ribbon XML for Word/Excel/Outlook.
    Used in Windows GPO deployment.
    Display name from tenant_branding.
    """
    brand = get_brand(request)
    display_name = brand.get("addin_display_name", brand.get("platform.matter_name", "Legal Platform"))
    base_url = brand.get("docs_url", "")

    ribbon_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<customUI xmlns="http://schemas.microsoft.com/office/2009/07/customui">
  <ribbon>
    <tabs>
      <tab id="dmsTab" label="{display_name}">
        <group id="dmsGroup" label="Documents">
          <button id="saveToDms" label="Save to DMS"
                  imageMso="FileSaveAs" size="large"
                  onAction="SaveToDmsHandler"/>
          <button id="searchDms" label="Search DMS"
                  imageMso="FindDialog" size="large"
                  onAction="SearchDmsHandler"/>
          <separator id="sep1"/>
          <button id="compareDoc" label="Compare"
                  imageMso="ReviewCompareDocuments" size="large"
                  onAction="CompareHandler"/>
          <button id="sanityCheck" label="Sanity Check"
                  imageMso="ReviewTrackChanges" size="large"
                  onAction="SanityCheckHandler"/>
        </group>
        <group id="researchGroup" label="Research">
          <button id="citationCheck" label="Check Citations"
                  imageMso="SpellingMenu" size="large"
                  onAction="CitationCheckHandler"/>
          <button id="generateToa" label="Generate TOA"
                  imageMso="TableOfContentsInsert" size="large"
                  onAction="GenerateToaHandler"/>
        </group>
        <group id="timeGroup" label="Time">
          <toggleButton id="trackTime" label="Track Time"
                        imageMso="Clock" size="large"
                        onAction="ToggleTimeTracker"
                        getPressed="GetTimeTrackerState"/>
        </group>
      </tab>
    </tabs>
  </ribbon>
</customUI>"""

    return Response(content=ribbon_xml, media_type="application/xml")


@router.get("/com/config")
async def get_com_config(request: Request):
    """
    Configuration endpoint for COM Add-In.
    Returns URLs and settings the COM client needs.
    """
    brand = get_brand(request)
    return {
        "base_url": brand.get("docs_url", ""),
        "display_name": brand.get("addin_display_name", brand.get("platform.matter_name", "")),
        "api_base": f"{brand.get('docs_url', '')}/api/v1",
        "webdav_url": f"{brand.get('docs_url', '')}/webdav/",
        "version": "1.0.0",
    }


@router.get("/com/installer")
async def get_com_installer_info(request: Request):
    """
    Returns GPO deployment information for the COM Add-In.
    IT admins use this to configure Group Policy.
    """
    brand = get_brand(request)
    return {
        "display_name": brand.get("addin_display_name", brand.get("platform.matter_name", "")),
        "progid": "Praesidium.DmsAddin",
        "clsid": "{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}",
        "dll_path": "C:\\Program Files\\PraesidiumAddin\\DmsAddin.dll",
        "registry_keys": [
            {
                "path": "HKLM\\SOFTWARE\\Microsoft\\Office\\Word\\Addins\\Praesidium.DmsAddin",
                "values": {
                    "FriendlyName": brand.get("addin_display_name", ""),
                    "Description": f"{brand.get('platform.matter_name', '')} DMS Integration",
                    "LoadBehavior": 3,
                },
            },
        ],
        "gpo_template": f"{brand.get('docs_url', '')}/api/v1/addin/com/gpo-template.admx",
    }
