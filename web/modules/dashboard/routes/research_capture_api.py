"""Research Capture API — serves the widget partial and handles AI extraction."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path

router = APIRouter(tags=["research"])

WIDGET_PATH = Path("/app/modules/widgets/templates/widgets/research_capture_widget.html")


@router.get("/api/research-capture-partial", response_class=HTMLResponse)
async def research_capture_partial(request: Request):
    """Serve the research capture widget HTML for the right panel."""
    if WIDGET_PATH.exists():
        return HTMLResponse(WIDGET_PATH.read_text())
    return HTMLResponse("<div style='padding:24px;color:var(--muted);text-align:center;'>Research capture widget not found.</div>")


@router.post("/api/v1/research/extract")
async def extract_fields(request: Request):
    """AI extraction from pasted text — maps unstructured text to record fields.
    
    In production, this calls Claude with the pasted text + record schema.
    For now, returns a simple heuristic extraction.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    
    text = body.get("text", "")
    record_type = body.get("record_type", "property")
    
    # Schema definitions (mirrors the widget JS)
    SCHEMAS = {
        "witness": ["employer", "license", "education", "priorTestimony", "publications", "background"],
        "property": ["parcel", "owner", "legalDesc", "appraised", "landValue", "improvValue", "acres", "yearBuilt", "zoning", "taxStatus"],
        "party": ["entityType", "stateOfFormation", "registeredAgent", "officers", "status", "filingHistory"],
        "case_law": ["citation", "court", "date", "shepards", "holdingSummary", "relevance"],
    }
    
    fields_list = SCHEMAS.get(record_type, [])
    
    # Simple heuristic: split text into lines, assign to fields
    lines = [l.strip() for l in text.split("\n") if l.strip() and len(l.strip()) > 3]
    result = {}
    for i, key in enumerate(fields_list):
        if i < len(lines):
            result[key] = lines[i][:300]
    
    # TODO: Replace with Claude API call:
    # prompt = f"Extract {record_type} fields from this text: {text}"
    # response = await call_claude(prompt, schema=fields_list)
    # result = response.fields
    
    return JSONResponse({"fields": result, "record_type": record_type, "source": "heuristic"})
