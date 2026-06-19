"""
Praesidium Presentation WebSocket Relay
- In-memory session store (Redis-backed in production)
- WebSocket endpoint at /ws/present/{session_token}
- REST endpoints to create/manage sessions
- Dumb terminal display page at /present/{session_token}
"""

import json
import uuid
import time
import secrets
from typing import Dict, Set
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()

# In-memory presentation sessions — production: move to Redis pub/sub
_sessions: Dict[str, dict] = {}  # token -> {state, clients, created_at, matter_id, ...}
_ws_clients: Dict[str, Set[WebSocket]] = {}  # token -> set of connected WebSockets


# ─── REST: Session Management ───

@router.post("/api/v1/present/sessions")
async def create_presentation_session(request: Request):
    """Create a new presentation session. Returns session token + display URL."""
    tenant_id = getattr(request.state, "tenant_id", None)
    user = getattr(request.state, "current_user", None)
    body = {}
    try:
        body = await request.json()
    except:
        pass
    
    token = secrets.token_urlsafe(24)  # Cryptographic, URL-safe
    _sessions[token] = {
        "token": token,
        "tenant_id": tenant_id,
        "created_by": getattr(user, "username", None) if user else None,
        "created_at": datetime.utcnow().isoformat(),
        "matter_id": body.get("matter_id"),
        "matter_name": body.get("matter_name"),
        "session_name": body.get("session_name", "Conference Space"),
        "state": {
            "document": None,  # {name, url, bates, pages, source}
            "page": 1,
            "zoom": 100,
            "annotations": [],
            "exhibit_label": None,
            "presenting": False,
        },
        "client_count": 0,
        # (corpus, doc_id) pairs an attorney has authorized for token-scoped
        # display fetch. The unauthenticated display can reach ONLY these (PR-3).
        "staged": set(),
    }
    _ws_clients[token] = set()
    
    # Build display URL
    host = request.headers.get("host", "localhost")
    scheme = "https" if "hjmmlegal" in host or "praesidium" in host else "http"
    display_url = f"{scheme}://{host}/present/{token}"
    
    return JSONResponse({
        "token": token,
        "display_url": display_url,
        "qr_data": display_url,  # Client generates QR from this
        "created_at": _sessions[token]["created_at"],
    })


@router.get("/api/v1/present/sessions/{token}")
async def get_session_info(token: str):
    """Get session info and current state."""
    session = _sessions.get(token)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return JSONResponse({
        "token": token,
        "state": session["state"],
        "client_count": len(_ws_clients.get(token, set())),
        "created_at": session["created_at"],
        "session_name": session["session_name"],
    })


@router.post("/api/v1/present/sessions/{token}/push")
async def push_state(token: str, request: Request):
    """Push presentation state update from the presenter. Broadcasts to all connected displays."""
    session = _sessions.get(token)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    
    body = await request.json()
    
    # Update session state
    state = session["state"]
    if "document" in body:
        state["document"] = body["document"]
    if "page" in body:
        state["page"] = body["page"]
    if "zoom" in body:
        state["zoom"] = body["zoom"]
    if "annotations" in body:
        state["annotations"] = body["annotations"]
    if "exhibit_label" in body:
        state["exhibit_label"] = body["exhibit_label"]
    if "presenting" in body:
        state["presenting"] = body["presenting"]
    
    # Broadcast to all connected WebSocket clients
    msg = json.dumps({"type": "state_update", "state": state})
    clients = _ws_clients.get(token, set())
    dead = set()
    for ws in clients:
        try:
            await ws.send_text(msg)
        except:
            dead.add(ws)
    for ws in dead:
        clients.discard(ws)
    
    return JSONResponse({
        "ok": True,
        "clients_notified": len(clients),
    })


@router.delete("/api/v1/present/sessions/{token}")
async def end_session(token: str):
    """End a presentation session. Disconnects all clients."""
    clients = _ws_clients.pop(token, set())
    for ws in clients:
        try:
            await ws.send_text(json.dumps({"type": "session_ended"}))
            await ws.close()
        except:
            pass
    _sessions.pop(token, None)
    return JSONResponse({"ok": True})


# ─── WebSocket: Display Client Connection ───

@router.websocket("/ws/present/{token}")
async def presentation_ws(websocket: WebSocket, token: str):
    """WebSocket endpoint for display terminals. Read-only — receives state updates."""
    session = _sessions.get(token)
    if not session:
        await websocket.close(code=4004, reason="Session not found")
        return
    
    await websocket.accept()
    clients = _ws_clients.setdefault(token, set())
    clients.add(websocket)
    
    # Send current state immediately on connect
    try:
        await websocket.send_text(json.dumps({
            "type": "state_update",
            "state": session["state"],
            "session_name": session["session_name"],
            "matter_name": session.get("matter_name"),
        }))
    except:
        clients.discard(websocket)
        return
    
    # Keep alive — listen for pings, ignore other messages
    try:
        while True:
            data = await websocket.receive_text()
            # Display clients are read-only; only accept pings
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(websocket)


# ─── Display Page: Dumb Terminal / Statio ───

DISPLAY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Praesidium Display</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@600;700&family=DM+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400&display=swap');
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{width:100%;height:100%;overflow:hidden;background:#000;font-family:'DM Sans',sans-serif}
  #waiting{display:flex;align-items:center;justify-content:center;height:100%;flex-direction:column;color:#94a3b8}
  #waiting .logo{font-family:'Cormorant Garamond',Georgia,serif;font-size:28px;font-weight:700;color:#D4A843;letter-spacing:2px;margin-bottom:8px}
  #waiting .sub{font-size:13px;opacity:.6}
  #waiting .pulse{width:12px;height:12px;border-radius:50%;background:#D4A843;margin-top:20px;animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.3;transform:scale(.8)}}
  #doc-viewer{display:none;width:100%;height:100%;background:#1a1a1a;align-items:flex-start;justify-content:center;overflow:auto;padding:24px}
  #doc-viewer.active{display:flex}
  #doc-page{background:#fff;border-radius:3px;box-shadow:0 4px 30px rgba(0,0,0,.5);padding:48px;max-width:800px;width:100%;min-height:90vh;position:relative;font-family:Georgia,'Times New Roman',serif;font-size:13px;color:#1a1a1a;line-height:1.7}
  #exhibit-sticker{display:none;position:absolute;top:14px;right:14px;border:2px solid #DC2626;padding:3px 14px;font-family:'DM Sans',sans-serif;font-weight:800;font-size:13px;color:#DC2626;letter-spacing:1.5px}
  #exhibit-sticker.visible{display:block}
  #doc-title{font-weight:700;font-size:16px;text-align:center;margin-bottom:20px;font-family:'Cormorant Garamond',Georgia,serif;letter-spacing:.3px}
  #bates-stamp{position:absolute;bottom:10px;right:14px;font-family:'JetBrains Mono',monospace;font-size:9px;color:#94a3b8}
  #status-bar{position:fixed;bottom:0;left:0;right:0;height:28px;background:rgba(13,31,60,.9);display:flex;align-items:center;justify-content:space-between;padding:0 16px;font-size:10px;color:rgba(255,255,255,.5)}
  #status-bar .dot{width:6px;height:6px;border-radius:50%;background:#4ade80;margin-right:6px}
  #status-bar.disconnected .dot{background:#ef4444}
  .session-ended{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);align-items:center;justify-content:center;color:#94a3b8;font-size:16px;flex-direction:column;gap:8px}
  .session-ended.active{display:flex}
</style>
</head>
<body>
<div id="waiting">
  <div class="logo">PRAESIDIUM</div>
  <div class="sub">Display — Waiting for presentation</div>
  <div class="pulse"></div>
</div>
<div id="doc-viewer">
  <div id="doc-page">
    <div id="exhibit-sticker"></div>
    <div id="doc-title"></div>
    <div id="doc-body"></div>
    <div id="bates-stamp"></div>
  </div>
</div>
<div id="status-bar">
  <div style="display:flex;align-items:center"><span class="dot"></span><span id="conn-status">Connecting...</span></div>
  <span id="session-info">SESSION_NAME</span>
</div>
<div class="session-ended" id="ended">
  <div style="font-family:'Cormorant Garamond',Georgia,serif;font-size:24px;color:#D4A843">PRAESIDIUM</div>
  <div>Session ended</div>
</div>

<script>
const TOKEN = "SESSION_TOKEN";
const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
let ws;
let reconnectTimer;

function connect() {
  ws = new WebSocket(wsProto + '//' + location.host + '/ws/present/' + TOKEN);
  ws.onopen = () => {
    document.getElementById('conn-status').textContent = 'Connected';
    document.getElementById('status-bar').classList.remove('disconnected');
    // Ping every 30s
    setInterval(() => { try { ws.send(JSON.stringify({type:'ping'})); } catch{} }, 30000);
  };
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'state_update') applyState(msg.state, msg.session_name, msg.matter_name);
      if (msg.type === 'session_ended') {
        document.getElementById('ended').classList.add('active');
        document.getElementById('waiting').style.display = 'none';
        document.getElementById('doc-viewer').classList.remove('active');
      }
    } catch(e) { console.error(e); }
  };
  ws.onclose = () => {
    document.getElementById('conn-status').textContent = 'Disconnected — reconnecting...';
    document.getElementById('status-bar').classList.add('disconnected');
    reconnectTimer = setTimeout(connect, 3000);
  };
  ws.onerror = () => ws.close();
}

function applyState(state, sessionName, matterName) {
  if (sessionName) document.getElementById('session-info').textContent = sessionName + (matterName ? ' — ' + matterName : '');
  
  if (!state.document || !state.presenting) {
    document.getElementById('waiting').style.display = 'flex';
    document.getElementById('doc-viewer').classList.remove('active');
    return;
  }
  
  document.getElementById('waiting').style.display = 'none';
  document.getElementById('doc-viewer').classList.add('active');
  
  const doc = state.document;
  document.getElementById('doc-title').textContent = (doc.name || '').replace('.pdf','').toUpperCase();
  
  // Exhibit sticker
  const sticker = document.getElementById('exhibit-sticker');
  if (state.exhibit_label) {
    sticker.textContent = 'EXHIBIT ' + state.exhibit_label;
    sticker.classList.add('visible');
  } else {
    sticker.classList.remove('visible');
  }
  
  // Bates stamp
  const bates = document.getElementById('bates-stamp');
  bates.textContent = doc.bates ? doc.bates + '-0001' : '';
  
  // Simulated document body (in production: PDF.js render or iframe to viewer endpoint)
  const body = document.getElementById('doc-body');
  const pages = doc.pages || 3;
  let html = '';
  for (let i = 0; i < Math.min(pages * 3, 18); i++) {
    html += '<p style="margin-bottom:10px;color:#374151">Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris.</p>';
  }
  body.innerHTML = html;
}

connect();
</script>
</body>
</html>"""

@router.get("/present/{token}", response_class=HTMLResponse)
async def display_page(token: str):
    """Dumb terminal display page — no auth, session-scoped, read-only."""
    session = _sessions.get(token)
    if not session:
        return HTMLResponse(
            "<html><body style='background:#000;color:#94a3b8;display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif'>"
            "<div style='text-align:center'><h2 style='color:#D4A843'>Session Not Found</h2><p>This display link has expired or the session has ended.</p></div>"
            "</body></html>",
            status_code=404
        )
    
    html = DISPLAY_HTML.replace("SESSION_TOKEN", token).replace("SESSION_NAME", session.get("session_name", "Conference Space"))
    return HTMLResponse(html)


# ─── Trial display: stage exhibits + token-scoped page raster (Page-Raster PR-3) ──

@router.post("/api/v1/present/sessions/{token}/stage")
async def stage_documents(token: str, request: Request):
    """AUTHENTICATED. Authorize documents for token-scoped display fetch.
    Body: {corpus: dms|ediscovery, doc_ids: [...]}. Only staged (corpus, doc_id)
    pairs become reachable by the unauthenticated display via GET /present/page/.
    Also warms the Page-Raster cache for page 1 so the first push is instant."""
    # Staging authorizes documents for an unauthenticated display to render as the
    # session tenant -- it MUST be an authenticated, same-tenant user (the auth
    # middleware passes API requests through, so enforce here, not by route).
    if not getattr(request.state, "current_user", None):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    session = _sessions.get(token)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    user_tid = (getattr(request.state, "tenant_id", "") or "").strip()
    sess_tid = (session.get("tenant_id") or "").strip()
    if not user_tid or (sess_tid and user_tid != sess_tid):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    body = await request.json()
    corpus = body.get("corpus")
    if corpus not in ("dms", "ediscovery"):
        return JSONResponse({"error": "Unsupported corpus"}, status_code=400)
    staged = session.setdefault("staged", set())
    doc_ids = [str(d) for d in (body.get("doc_ids") or []) if d]
    for d in doc_ids:
        staged.add((corpus, d))
    # best-effort page-1 warm via the shared service (session tenant)
    tid = (session.get("tenant_id") or "").strip()
    if tid and doc_ids:
        from starlette.concurrency import run_in_threadpool
        from modules.render.page_raster import render_page
        import asyncio
        async def _warm():
            for d in doc_ids[:40]:
                try:
                    await run_in_threadpool(render_page, tid, corpus, d, 1)
                except Exception:
                    pass
        asyncio.create_task(_warm())
    return JSONResponse({"ok": True, "staged_now": len(staged), "added": len(doc_ids)})


@router.get("/present/page/{token}/{corpus}/{doc_id}/{page}")
async def present_page(token: str, corpus: str, doc_id: str, page: int, w: int = 1600):
    """UNAUTHENTICATED, token-scoped. The presentation session token is the
    credential (from the QR); only documents staged into THIS session are
    reachable. Renders through the shared Page-Raster cache as the session's
    tenant -- an arbitrary doc_id that was never staged returns 404."""
    session = _sessions.get(token)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    if (corpus, str(doc_id)) not in session.get("staged", set()):
        return JSONResponse({"error": "Not authorized for this display"}, status_code=404)
    tid = (session.get("tenant_id") or "").strip()
    if not tid:
        return JSONResponse({"error": "Session has no tenant"}, status_code=409)
    from starlette.concurrency import run_in_threadpool
    from modules.render.page_raster import render_page
    try:
        res = await run_in_threadpool(render_page, tid, corpus, str(doc_id), int(page), "native_pdf", int(w))
    except ValueError:
        return JSONResponse({"error": "Page out of range"}, status_code=404)
    except Exception:
        return JSONResponse({"error": "Render failed"}, status_code=500)
    if res is None:
        return JSONResponse({"error": "Document not found"}, status_code=404)
    from fastapi.responses import Response
    return Response(content=res.image_bytes, media_type="image/webp", headers={
        "Cache-Control": "private, max-age=86400, immutable",
        "X-Page-Dims": "%dx%d" % (res.width, res.height),
        "X-Cache": "HIT" if res.cached else "MISS",
    })
