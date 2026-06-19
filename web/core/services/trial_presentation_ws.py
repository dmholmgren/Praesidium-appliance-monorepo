"""
Praesidium Trial Presentation Relay (M14 TrialDesk) -- v2 multi-surface engine.
SIBLING of the depot core/services/presentation_ws.py (depot left untouched).

Model (Dennis, 2026-06-18):
  - A live **Program channel** holds the operator's composition: content
    (corpus+doc_id), page, view (zoom/pan), and annotation marks.
  - Each connected display has a **mode**: 'mirror' (renders the Program live)
    or 'independent' (its own channel). Set per-display from the console.
  - Bidirectional: any **interactive** (touchscreen) display can annotate / turn
    pages / zoom on the channel it shows. A mirror touchscreen edits the Program
    (everyone sees it); an independent touchscreen edits only itself. A projector
    is a passive display (interactive=false) driven by the operator's Program
    tools or any paired interactive surface.
  - Identify (flash a display's name), frameless auto-fullscreen, short /p/{code}
    tiny URL + QR. Real pixels come from the shared Page-Raster service,
    token-scoped (only staged docs are reachable by the unauthenticated display).

Live routing state is in-memory (single appliance / single web worker, same as
depot). The audit (trial_presentation_events) persists -- it is the record.
"""

import io
import os
import json
import uuid
import shutil
import secrets
from typing import Dict, Set, Optional, List

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

router = APIRouter()

# In-memory live state (token-keyed)
_sessions: Dict[str, dict] = {}
_codes: Dict[str, str] = {}                      # short code -> token
_display_ws: Dict[str, Dict[str, WebSocket]] = {}
_console_ws: Dict[str, Set[WebSocket]] = {}

ROLES = [
    "unassigned", "presenter", "counsel", "co_counsel", "witness",
    "jury", "tribunal", "court_reporter", "gallery",
]
_CORPUS_MAP = {"ediscovery": "ediscovery", "dms": "dms"}
UPLOAD_CORPUS = "upload"
UPLOAD_ROOT = os.environ.get("TRIAL_UPLOAD_DIR", "/tmp/trial_uploads")
_DEFAULT_VIEW = {"zoom": 1.0, "cx": 0.5, "cy": 0.5}


def _ok_corpus(c):
    return c in _CORPUS_MAP or c == UPLOAD_CORPUS


# auth + small helpers

def _uid(request: Request):
    u = getattr(request.state, "current_user", None)
    if not u:
        return None
    return getattr(u, "id", None) or getattr(u, "user_id", None)


def _auth(request: Request):
    uid = _uid(request)
    tid = (getattr(request.state, "tenant_id", "") or "").strip()
    if uid is None or not tid:
        return None, None
    return uid, tid


def _same_tenant(session: dict, tid: str) -> bool:
    st = (session.get("tenant_id") or "").strip()
    return bool(tid) and (not st or st == tid)


def _new_channel() -> dict:
    return {"corpus": None, "doc_id": None, "page": 1,
            "view": dict(_DEFAULT_VIEW), "marks": [],
            "exhibit_label": None, "exhibit_id": None}


def _page_url(token: str, ch: dict) -> Optional[str]:
    c = ch.get("corpus")
    if not _ok_corpus(c) or not ch.get("doc_id"):
        return None
    return "/trial-present/page/%s/%s/%s/%s" % (
        token, c, ch["doc_id"], int(ch.get("page") or 1))


def _apply_channel(ch: dict, body: dict) -> dict:
    """Merge an update into a channel. Loading new content (corpus+doc_id that
    differs) resets view+marks unless explicitly provided. Otherwise patch only
    the fields present (page / view / marks / addmark / clear / labels)."""
    corpus = body.get("corpus")
    doc_id = body.get("doc_id")
    new_content = (_ok_corpus(corpus) and doc_id and
                   (str(doc_id) != str(ch.get("doc_id")) or
                    corpus != ch.get("corpus")))
    if new_content:
        ch["corpus"] = corpus
        ch["doc_id"] = str(doc_id)
        ch["page"] = int(body.get("page") or 1)
        ch["view"] = body.get("view") or dict(_DEFAULT_VIEW)
        ch["marks"] = body.get("marks") or []
        ch["exhibit_label"] = body.get("exhibit_label")
        ch["exhibit_id"] = body.get("exhibit_id")
        return ch
    if "page" in body and body["page"] is not None:
        ch["page"] = max(1, int(body["page"]))
    if "page_delta" in body and body["page_delta"]:
        ch["page"] = max(1, int(ch.get("page") or 1) + int(body["page_delta"]))
    if "view" in body and body["view"] is not None:
        ch["view"] = body["view"]
    if "marks" in body and body["marks"] is not None:
        ch["marks"] = body["marks"]
    if "addmark" in body and body["addmark"]:
        ch.setdefault("marks", []).append(body["addmark"])
    if body.get("clear"):
        ch["marks"] = []
    if "exhibit_label" in body:
        ch["exhibit_label"] = body["exhibit_label"]
    if "exhibit_id" in body:
        ch["exhibit_id"] = body["exhibit_id"]
    return ch


def _effective_channel(session: dict, disp: dict) -> dict:
    return session["program"] if disp.get("mode") != "independent" else \
        disp.setdefault("current", _new_channel())


def _frame_msg(token: str, session: dict, disp: dict) -> dict:
    if disp.get("blank"):
        return {"type": "display_state", "frame": {"kind": "blank"},
                "role": disp.get("role"), "label": disp.get("label"),
                "mode": disp.get("mode"), "interactive": bool(disp.get("interactive"))}
    ch = _effective_channel(session, disp)
    url = _page_url(token, ch)
    frame = {
        "kind": "doc" if url else "idle",
        "page_url": url,
        "page": int(ch.get("page") or 1),
        "view": ch.get("view") or dict(_DEFAULT_VIEW),
        "marks": ch.get("marks") or [],
        "exhibit_label": ch.get("exhibit_label"),
    }
    return {"type": "display_state", "frame": frame,
            "role": disp.get("role"), "label": disp.get("label"),
            "mode": disp.get("mode"), "interactive": bool(disp.get("interactive"))}


def _board(session: dict) -> dict:
    token = session["token"]
    prog = session["program"]
    displays = []
    for did, d in session.get("displays", {}).items():
        ch = _effective_channel(session, d)
        cur = None
        if not d.get("blank") and ch.get("doc_id"):
            cur = {"corpus": ch.get("corpus"), "doc_id": ch.get("doc_id"),
                   "page": int(ch.get("page") or 1),
                   "exhibit_label": ch.get("exhibit_label"),
                   "exhibit_id": ch.get("exhibit_id"),
                   "view": ch.get("view"), "marks": ch.get("marks") or [],
                   "thumb_url": _page_url(token, ch)}
        displays.append({
            "display_id": did, "role": d.get("role"), "label": d.get("label"),
            "mode": d.get("mode"), "interactive": bool(d.get("interactive")),
            "hold": bool(d.get("hold")), "blank": bool(d.get("blank")),
            "connected": bool(_display_ws.get(token, {}).get(did)),
            "current": cur,
        })
    program = {
        "corpus": prog.get("corpus"), "doc_id": prog.get("doc_id"),
        "page": int(prog.get("page") or 1), "view": prog.get("view"),
        "marks": prog.get("marks") or [], "exhibit_label": prog.get("exhibit_label"),
        "exhibit_id": prog.get("exhibit_id"), "thumb_url": _page_url(token, prog),
    }
    return {"type": "board", "token": token, "code": session.get("code"),
            "session_id": session.get("session_id"), "title": session.get("title"),
            "status": session.get("status"), "roles": ROLES,
            "staged_count": len(session.get("staged", set())),
            "program": program, "displays": displays}


async def _send_display(token: str, display_id: str):
    ws = _display_ws.get(token, {}).get(display_id)
    sess = _sessions.get(token)
    if not ws or not sess:
        return
    disp = sess.get("displays", {}).get(display_id)
    if not disp:
        return
    try:
        await ws.send_text(json.dumps(_frame_msg(token, sess, disp)))
    except Exception:
        pass


async def _send_to_display_raw(token: str, display_id: str, msg: dict):
    ws = _display_ws.get(token, {}).get(display_id)
    if not ws:
        return
    try:
        await ws.send_text(json.dumps(msg))
    except Exception:
        pass


async def _push_program(token: str):
    """Send the Program frame to every mirror-mode display (that isn't blanked
    or held)."""
    sess = _sessions.get(token)
    if not sess:
        return
    for did, d in sess.get("displays", {}).items():
        if d.get("mode") != "independent" and not d.get("hold"):
            await _send_display(token, did)


async def _broadcast_board(token: str):
    sess = _sessions.get(token)
    if not sess:
        return
    msg = json.dumps(_board(sess))
    dead = set()
    for ws in _console_ws.get(token, set()):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _console_ws.get(token, set()).discard(ws)


async def _audit(session: dict, event_type: str, *, document_id=None,
                 exhibit_id=None, page=None, target_roles=None,
                 actor_id=None, actor_role=None, payload=None):
    roles_str = ",".join(target_roles) if target_roles else ""
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(text("""
                INSERT INTO trial_presentation_events
                  (id, session_id, tenant_id, event_type, document_id,
                   exhibit_id, page, target_roles, actor_id, actor_role, payload)
                VALUES
                  (gen_random_uuid(), CAST(:sid AS uuid), :tid, :etype,
                   CAST(NULLIF(:did, '') AS uuid), CAST(NULLIF(:eid, '') AS uuid),
                   :page,
                   CASE WHEN :roles = '' THEN NULL
                        ELSE string_to_array(:roles, ',') END,
                   :aid, :arole, CAST(:payload AS jsonb))
            """), {
                "sid": session["session_id"], "tid": session["tenant_id"],
                "etype": event_type,
                "did": str(document_id) if document_id else "",
                "eid": str(exhibit_id) if exhibit_id else "",
                "page": page, "roles": roles_str, "aid": actor_id,
                "arole": actor_role, "payload": json.dumps(payload or {}),
            })
            await s.commit()
    except Exception as e:  # noqa
        import logging
        logging.getLogger("trial_present").warning("audit write failed: %s", e)


def _targets(session: dict, body: dict) -> List[str]:
    displays = session.get("displays", {})
    t = body.get("targets")
    if t == "all" or t is None:
        return list(displays.keys())
    if isinstance(t, str):
        t = [t]
    return [d for d in t if d in displays]


def _roles_of(session: dict, display_ids: List[str]) -> List[str]:
    out = []
    for d in display_ids:
        r = session.get("displays", {}).get(d, {}).get("role")
        if r:
            out.append(r)
    return out


# REST: session lifecycle (authenticated)

@router.post("/api/v1/trial-present/sessions")
async def create_session(request: Request):
    uid, tid = _auth(request)
    if not tid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    token = secrets.token_urlsafe(24)
    code = secrets.token_hex(3).upper()  # 6-char tiny code
    while code in _codes:
        code = secrets.token_hex(3).upper()
    sid = str(uuid.uuid4())
    title = (body.get("title") or "Trial Presentation").strip()
    proceeding_id = body.get("proceeding_id")
    matter_id = body.get("matter_id")
    start_num = int(body.get("exhibit_numbering_start") or 1)
    config = body.get("config") or {}
    async with AsyncSessionLocal() as s:
        await s.execute(text("""
            INSERT INTO trial_presentation_sessions
              (id, tenant_id, proceeding_id, matter_id, token, title, status,
               exhibit_numbering_start, created_by, config)
            VALUES
              (CAST(:id AS uuid), :tid, CAST(NULLIF(:pid,'') AS uuid),
               CAST(NULLIF(:mid,'') AS uuid), :token, :title, 'active',
               :start, :uid, CAST(:cfg AS jsonb))
        """), {
            "id": sid, "tid": tid,
            "pid": str(proceeding_id) if proceeding_id else "",
            "mid": str(matter_id) if matter_id else "",
            "token": token, "title": title, "start": start_num,
            "uid": uid, "cfg": json.dumps(config),
        })
        await s.commit()
    _sessions[token] = {
        "token": token, "code": code, "session_id": sid, "tenant_id": tid,
        "proceeding_id": str(proceeding_id) if proceeding_id else None,
        "matter_id": str(matter_id) if matter_id else None,
        "title": title, "status": "active",
        "exhibit_numbering_start": start_num, "created_by": uid,
        "config": config, "staged": set(), "displays": {}, "display_seq": 0,
        "program": _new_channel(), "uploads": {},
    }
    _codes[code] = token
    _display_ws[token] = {}
    _console_ws[token] = set()
    await _audit(_sessions[token], "session_start", actor_id=uid)
    host = request.headers.get("host", "localhost")
    scheme = "https" if ("hjmmlegal" in host or "praesidium" in host) else "http"
    base = "%s://%s" % (scheme, host)
    return JSONResponse({
        "token": token, "session_id": sid, "code": code,
        "display_url": "%s/p/%s" % (base, code),
        "long_url": "%s/present/trial/%s" % (base, token),
        "qr_url": "/api/v1/trial-present/sessions/%s/qr" % token,
        "console_ws": "/ws/trial-console/%s" % token,
    })


@router.get("/api/v1/trial-present/sessions/{token}")
async def get_session(token: str, request: Request):
    uid, tid = _auth(request)
    if not tid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sess = _sessions.get(token)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    if not _same_tenant(sess, tid):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    return JSONResponse(_board(sess))


@router.delete("/api/v1/trial-present/sessions/{token}")
async def end_session(token: str, request: Request):
    uid, tid = _auth(request)
    if not tid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sess = _sessions.get(token)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    if not _same_tenant(sess, tid):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    for ws in list(_display_ws.get(token, {}).values()):
        try:
            await ws.send_text(json.dumps({"type": "session_ended"}))
            await ws.close()
        except Exception:
            pass
    for ws in list(_console_ws.get(token, set())):
        try:
            await ws.send_text(json.dumps({"type": "session_ended"}))
            await ws.close()
        except Exception:
            pass
    async with AsyncSessionLocal() as s:
        await s.execute(text("""
            UPDATE trial_presentation_sessions
               SET status='ended', ended_at=now()
             WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = :tid
        """), {"id": sess["session_id"], "tid": tid})
        await s.commit()
    await _audit(sess, "session_end", actor_id=uid)
    _codes.pop(sess.get("code", ""), None)
    _display_ws.pop(token, None)
    _console_ws.pop(token, None)
    _sessions.pop(token, None)
    try:
        d = os.path.join(UPLOAD_ROOT, token)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass
    return JSONResponse({"ok": True})


# REST: stage documents (authorize for display fetch)

@router.post("/api/v1/trial-present/sessions/{token}/stage")
async def stage_documents(token: str, request: Request):
    uid, tid = _auth(request)
    if not tid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sess = _sessions.get(token)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    if not _same_tenant(sess, tid):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    body = await request.json()
    corpus = body.get("corpus")
    if not _ok_corpus(corpus):
        return JSONResponse({"error": "Unsupported corpus"}, status_code=400)
    staged = sess.setdefault("staged", set())
    doc_ids = [str(d) for d in (body.get("doc_ids") or []) if d]
    for d in doc_ids:
        staged.add((corpus, d))
    stid = (sess.get("tenant_id") or "").strip()
    if stid and doc_ids:
        from starlette.concurrency import run_in_threadpool
        from modules.render.page_raster import render_page
        import asyncio

        async def _warm():
            for d in doc_ids[:40]:
                try:
                    await run_in_threadpool(render_page, stid, corpus, d, 1)
                except Exception:
                    pass
        asyncio.create_task(_warm())
    return JSONResponse({"ok": True, "staged_now": len(staged),
                         "added": len(doc_ids)})


def _ensure_staged(sess: dict, corpus: str, doc_id: str):
    if _ok_corpus(corpus) and doc_id:
        sess.setdefault("staged", set()).add((corpus, str(doc_id)))


# REST: exhibit queue

@router.get("/api/v1/trial-present/sessions/{token}/exhibits")
async def session_exhibits(token: str, request: Request):
    uid, tid = _auth(request)
    if not tid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sess = _sessions.get(token)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    if not _same_tenant(sess, tid):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    where = ["TRIM(tenant_id) = :tid"]
    params = {"tid": tid}
    if sess.get("proceeding_id"):
        where.append("trial_id = CAST(:pid AS uuid)")
        params["pid"] = sess["proceeding_id"]
    elif sess.get("matter_id"):
        where.append("matter_id = CAST(:mid AS uuid)")
        params["mid"] = sess["matter_id"]
    sql = """
        SELECT id, party, exhibit_number, exhibit_label, document_id,
               document_source, sponsoring_witness, status, admitted
          FROM trial_exhibits
         WHERE %s
         ORDER BY party NULLS LAST, exhibit_number
    """ % " AND ".join(where)
    rows = []
    async with AsyncSessionLocal() as s:
        res = await s.execute(text(sql), params)
        for r in res.mappings():
            corpus = _CORPUS_MAP.get(r["document_source"])
            rows.append({
                "exhibit_id": str(r["id"]), "party": r["party"],
                "exhibit_number": r["exhibit_number"],
                "exhibit_label": r["exhibit_label"],
                "document_id": str(r["document_id"]) if r["document_id"] else None,
                "corpus": corpus,
                "renderable": bool(corpus and r["document_id"]),
                "sponsoring_witness": r["sponsoring_witness"],
                "status": r["status"], "admitted": r["admitted"],
            })
    return JSONResponse({"exhibits": rows})


# REST: program channel (drives all mirror displays)

@router.post("/api/v1/trial-present/sessions/{token}/program")
async def set_program(token: str, request: Request):
    uid, tid = _auth(request)
    if not tid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sess = _sessions.get(token)
    if not sess or not _same_tenant(sess, tid):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    body = await request.json()
    if body.get("corpus") and body.get("doc_id"):
        if not _ok_corpus(body["corpus"]):
            return JSONResponse({"error": "Unsupported corpus"}, status_code=400)
        _ensure_staged(sess, body["corpus"], body["doc_id"])
    _apply_channel(sess["program"], body)
    await _push_program(token)
    await _broadcast_board(token)
    prog = sess["program"]
    await _audit(sess, "program", actor_id=uid, document_id=prog.get("doc_id"),
                 exhibit_id=prog.get("exhibit_id"), page=prog.get("page"),
                 payload={"view": prog.get("view"),
                          "marks_n": len(prog.get("marks") or []),
                          "exhibit_label": prog.get("exhibit_label")})
    return JSONResponse({"ok": True})


# REST: per-display content (independent), role, mode, identify

@router.post("/api/v1/trial-present/sessions/{token}/displays/{display_id}/content")
async def display_content(token: str, display_id: str, request: Request):
    uid, tid = _auth(request)
    if not tid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sess = _sessions.get(token)
    if not sess or not _same_tenant(sess, tid):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    disp = sess.get("displays", {}).get(display_id)
    if not disp:
        return JSONResponse({"error": "Display not found"}, status_code=404)
    body = await request.json()
    if body.get("corpus") and body.get("doc_id"):
        if not _ok_corpus(body["corpus"]):
            return JSONResponse({"error": "Unsupported corpus"}, status_code=400)
        _ensure_staged(sess, body["corpus"], body["doc_id"])
    disp["mode"] = "independent"
    _apply_channel(disp.setdefault("current", _new_channel()), body)
    disp["blank"] = False
    if not disp.get("hold"):
        await _send_display(token, display_id)
    await _broadcast_board(token)
    return JSONResponse({"ok": True})


@router.post("/api/v1/trial-present/sessions/{token}/displays/{display_id}/role")
async def assign_role(token: str, display_id: str, request: Request):
    uid, tid = _auth(request)
    if not tid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sess = _sessions.get(token)
    if not sess or not _same_tenant(sess, tid):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    disp = sess.get("displays", {}).get(display_id)
    if not disp:
        return JSONResponse({"error": "Display not found"}, status_code=404)
    body = await request.json()
    role = body.get("role")
    if role and role not in ROLES:
        return JSONResponse({"error": "Unknown role"}, status_code=400)
    if role:
        disp["role"] = role
    if "label" in body and body["label"]:
        disp["label"] = str(body["label"])[:60]
    await _send_display(token, display_id)
    await _broadcast_board(token)
    await _audit(sess, "role_assign", actor_id=uid, actor_role=disp.get("role"),
                 payload={"display_id": display_id, "role": disp.get("role"),
                          "label": disp.get("label")})
    return JSONResponse({"ok": True})


@router.post("/api/v1/trial-present/sessions/{token}/displays/{display_id}/mode")
async def set_mode(token: str, display_id: str, request: Request):
    """mode = 'mirror' | 'independent'; interactive = bool (touch surface)."""
    uid, tid = _auth(request)
    if not tid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sess = _sessions.get(token)
    if not sess or not _same_tenant(sess, tid):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    disp = sess.get("displays", {}).get(display_id)
    if not disp:
        return JSONResponse({"error": "Display not found"}, status_code=404)
    body = await request.json()
    mode = body.get("mode")
    if mode in ("mirror", "independent"):
        disp["mode"] = mode
        if mode == "independent" and not disp.get("current"):
            disp["current"] = _new_channel()
    if "interactive" in body:
        disp["interactive"] = bool(body["interactive"])
    await _send_display(token, display_id)
    await _broadcast_board(token)
    await _audit(sess, "mode", actor_id=uid,
                 payload={"display_id": display_id, "mode": disp.get("mode"),
                          "interactive": bool(disp.get("interactive"))})
    return JSONResponse({"ok": True})


@router.post("/api/v1/trial-present/sessions/{token}/displays/{display_id}/identify")
async def identify_display(token: str, display_id: str, request: Request):
    uid, tid = _auth(request)
    if not tid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sess = _sessions.get(token)
    if not sess or not _same_tenant(sess, tid):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    disp = sess.get("displays", {}).get(display_id)
    if not disp:
        return JSONResponse({"error": "Display not found"}, status_code=404)
    await _send_to_display_raw(token, display_id, {
        "type": "identify", "label": disp.get("label"),
        "role": disp.get("role")})
    return JSONResponse({"ok": True})


@router.post("/api/v1/trial-present/sessions/{token}/identify-all")
async def identify_all(token: str, request: Request):
    uid, tid = _auth(request)
    if not tid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sess = _sessions.get(token)
    if not sess or not _same_tenant(sess, tid):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    for did, d in sess.get("displays", {}).items():
        await _send_to_display_raw(token, did, {
            "type": "identify", "label": d.get("label"), "role": d.get("role")})
    return JSONResponse({"ok": True})


# REST: blank / hold

@router.post("/api/v1/trial-present/sessions/{token}/blank")
async def blank(token: str, request: Request):
    uid, tid = _auth(request)
    if not tid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sess = _sessions.get(token)
    if not sess or not _same_tenant(sess, tid):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    body = await request.json()
    targets = _targets(sess, body)
    on = body.get("blank", True)
    for did in targets:
        sess["displays"][did]["blank"] = bool(on)
        await _send_display(token, did)
    await _broadcast_board(token)
    await _audit(sess, "blank", actor_id=uid,
                 target_roles=_roles_of(sess, targets),
                 payload={"display_ids": targets, "blank": bool(on)})
    return JSONResponse({"ok": True, "targets": targets})


@router.post("/api/v1/trial-present/sessions/{token}/hold")
async def hold(token: str, request: Request):
    uid, tid = _auth(request)
    if not tid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sess = _sessions.get(token)
    if not sess or not _same_tenant(sess, tid):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    body = await request.json()
    targets = _targets(sess, body)
    on = bool(body.get("hold", True))
    for did in targets:
        disp = sess["displays"][did]
        disp["hold"] = on
        if not on:
            await _send_display(token, did)
    await _broadcast_board(token)
    await _audit(sess, "hold", actor_id=uid,
                 target_roles=_roles_of(sess, targets),
                 payload={"display_ids": targets, "hold": on})
    return JSONResponse({"ok": True, "targets": targets})


# Back-compat: /push == push independent content to targets

@router.post("/api/v1/trial-present/sessions/{token}/push")
async def push(token: str, request: Request):
    uid, tid = _auth(request)
    if not tid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sess = _sessions.get(token)
    if not sess or not _same_tenant(sess, tid):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    body = await request.json()
    corpus = body.get("corpus")
    doc_id = body.get("doc_id")
    if not _ok_corpus(corpus) or not doc_id:
        return JSONResponse({"error": "corpus + doc_id required"}, status_code=400)
    _ensure_staged(sess, corpus, doc_id)
    targets = _targets(sess, body)
    for did in targets:
        disp = sess["displays"][did]
        disp["mode"] = "independent"
        _apply_channel(disp.setdefault("current", _new_channel()), body)
        disp["blank"] = False
        if not disp.get("hold"):
            await _send_display(token, did)
    await _broadcast_board(token)
    await _audit(sess, "push", actor_id=uid, document_id=doc_id,
                 exhibit_id=body.get("exhibit_id"), page=int(body.get("page") or 1),
                 target_roles=_roles_of(sess, targets),
                 payload={"display_ids": targets,
                          "exhibit_label": body.get("exhibit_label"),
                          "corpus": corpus})
    return JSONResponse({"ok": True, "targets": targets})


# Token-scoped page raster (unauthenticated; token + staged are the bound)

# upload: ad-hoc file dropped in the console -> stage + render
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}


def _render_upload_bytes(meta, page, w):
    path = meta["path"]
    kind = meta["kind"]
    w = max(400, min(4000, int(w)))
    from PIL import Image
    if kind == "image":
        img = Image.open(path).convert("RGB")
        if img.width > w:
            h = max(1, int(img.height * (w / float(img.width))))
            img = img.resize((w, h))
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=82)
        return "image/webp", buf.getvalue(), (img.width, img.height)
    import fitz
    doc = fitz.open(path)
    try:
        n = doc.page_count
        if page < 1 or page > n:
            raise ValueError("page out of range")
        pg = doc.load_page(page - 1)
        rect = pg.rect
        scale = (w / rect.width) if rect.width else 1.0
        pix = pg.get_pixmap(matrix=fitz.Matrix(scale, scale),
                            colorspace=fitz.csRGB, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=82)
        return "image/webp", buf.getvalue(), (pix.width, pix.height)
    finally:
        doc.close()


async def _render_upload_response(sess, upload_id, page, w):
    meta = sess.get("uploads", {}).get(upload_id)
    if not meta or not os.path.exists(meta.get("path", "")):
        return JSONResponse({"error": "Upload not found"}, status_code=404)
    from starlette.concurrency import run_in_threadpool
    try:
        ctype, data, dims = await run_in_threadpool(
            _render_upload_bytes, meta, int(page), int(w))
    except ValueError:
        return JSONResponse({"error": "Page out of range"}, status_code=404)
    except Exception:
        return JSONResponse({"error": "Render failed"}, status_code=500)
    return Response(content=data, media_type=ctype, headers={
        "Cache-Control": "private, max-age=3600",
        "X-Page-Dims": "%dx%d" % (dims[0], dims[1]),
    })


@router.post("/api/v1/trial-present/sessions/{token}/upload")
async def upload_file(token: str, request: Request):
    uid, tid = _auth(request)
    if not tid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sess = _sessions.get(token)
    if not sess or not _same_tenant(sess, tid):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "multipart form required"}, status_code=400)
    up = form.get("file")
    if up is None or not hasattr(up, "read"):
        return JSONResponse({"error": "no file"}, status_code=400)
    name = getattr(up, "filename", "upload") or "upload"
    ext = os.path.splitext(name)[1].lower()
    if ext in _IMAGE_EXT:
        kind = "image"
    elif ext == ".pdf":
        kind = "pdf"
    else:
        return JSONResponse({"error": "Unsupported file type", "ext": ext},
                            status_code=415)
    data = await up.read()
    if not data:
        return JSONResponse({"error": "empty file"}, status_code=400)
    if len(data) > 80 * 1024 * 1024:
        return JSONResponse({"error": "file too large"}, status_code=413)
    # Store the upload as a real DMS document under the matter's
    # "Trial Uploads" folder so it is durable and renders via the dms corpus.
    matter_id = sess.get("matter_id")
    disk_root = None
    if matter_id:
        async with AsyncSessionLocal() as s2:
            disk_root = (await s2.execute(text(
                "SELECT disk_root FROM matter_folders "
                "WHERE matter_id = CAST(:m AS uuid) "
                "AND COALESCE(disk_root,'') <> '' "
                "ORDER BY length(disk_root) LIMIT 1"), {"m": matter_id})).scalar()
    if not disk_root:
        disk_root = "/mnt/praesidium/%s/matters/_Trial Uploads" % tid
    folder = os.path.join(disk_root, "Trial Uploads")
    safe = "".join(c for c in os.path.basename(name) if c not in '/:*?"<>|').strip() or "upload"
    stem, e2 = os.path.splitext(safe)
    fname = "%s__%s%s" % (stem, secrets.token_hex(4), e2 or ext)
    abs_path = os.path.join(folder, fname)
    try:
        os.makedirs(folder, exist_ok=True)
        with open(abs_path, "wb") as f:
            f.write(data)
    except Exception:
        return JSONResponse({"error": "save failed"}, status_code=500)
    doc_id = str(uuid.uuid4())
    try:
        async with AsyncSessionLocal() as s2:
            await s2.execute(text(
                "INSERT INTO dms_documents (id, tenant_id, file_path, "
                "folder_root, file_size_bytes, modified_at, updated_at, source) "
                "VALUES (CAST(:id AS uuid), :tid, :fp, :root, :sz, now(), now(), "
                "'trial_upload')"),
                {"id": doc_id, "tid": tid, "fp": abs_path, "root": disk_root,
                 "sz": len(data)})
            await s2.commit()
    except Exception as e:  # noqa
        import logging
        logging.getLogger("trial_present").warning("dms insert failed: %s", e)
        return JSONResponse({"error": "register failed"}, status_code=500)
    sess.setdefault("staged", set()).add(("dms", doc_id))
    return JSONResponse({"ok": True, "document_id": doc_id, "corpus": "dms",
                         "kind": kind, "label": name, "renderable": True,
                         "dms_path": abs_path})


@router.get("/trial-present/page/{token}/{corpus}/{doc_id}/{page}")
async def present_page(token: str, corpus: str, doc_id: str, page: int,
                       w: int = 2000):
    sess = _sessions.get(token)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    if (corpus, str(doc_id)) not in sess.get("staged", set()):
        return JSONResponse({"error": "Not authorized for this display"},
                            status_code=404)
    if corpus == UPLOAD_CORPUS:
        return await _render_upload_response(sess, str(doc_id), int(page), int(w))
    tid = (sess.get("tenant_id") or "").strip()
    if not tid:
        return JSONResponse({"error": "Session has no tenant"}, status_code=409)
    from starlette.concurrency import run_in_threadpool
    from modules.render.page_raster import render_page
    try:
        res = await run_in_threadpool(render_page, tid, corpus, str(doc_id),
                                      int(page), "native_pdf", int(w))
    except ValueError:
        return JSONResponse({"error": "Page out of range"}, status_code=404)
    except Exception:
        return JSONResponse({"error": "Render failed"}, status_code=500)
    if res is None:
        return JSONResponse({"error": "Document not found"}, status_code=404)
    return Response(content=res.image_bytes, media_type="image/webp", headers={
        "Cache-Control": "private, max-age=86400, immutable",
        "X-Page-Dims": "%dx%d" % (res.width, res.height),
        "X-Cache": "HIT" if res.cached else "MISS",
    })


# QR code for the tiny display URL (server-side, offline)

@router.get("/api/v1/trial-present/sessions/{token}/qr")
async def session_qr(token: str, request: Request):
    uid, tid = _auth(request)
    if not tid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sess = _sessions.get(token)
    if not sess or not _same_tenant(sess, tid):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    host = request.headers.get("host", "localhost")
    scheme = "https" if ("hjmmlegal" in host or "praesidium" in host) else "http"
    url = "%s://%s/p/%s" % (scheme, host, sess.get("code"))
    try:
        import qrcode
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png",
                        headers={"Cache-Control": "private, max-age=3600"})
    except Exception:
        return JSONResponse({"error": "QR unavailable", "url": url},
                            status_code=503)


# WebSocket: display client (unauthenticated, token-scoped, bidirectional)

@router.websocket("/ws/trial-present/{token}")
async def display_ws(websocket: WebSocket, token: str):
    sess = _sessions.get(token)
    if not sess:
        await websocket.close(code=4004, reason="Session not found")
        return
    await websocket.accept()
    sess["display_seq"] = sess.get("display_seq", 0) + 1
    seq = sess["display_seq"]
    display_id = secrets.token_hex(6)
    sess.setdefault("displays", {})[display_id] = {
        "role": "unassigned", "label": "Display %d" % seq,
        "mode": "mirror", "interactive": False,
        "hold": False, "blank": False, "current": _new_channel(),
    }
    _display_ws.setdefault(token, {})[display_id] = websocket
    await _audit(sess, "display_join",
                 payload={"display_id": display_id, "seq": seq})
    try:
        await websocket.send_text(json.dumps({
            "type": "registered", "display_id": display_id,
            "role": "unassigned", "label": "Display %d" % seq,
            "title": sess.get("title")}))
        await websocket.send_text(json.dumps(
            _frame_msg(token, sess, sess["displays"][display_id])))
    except Exception:
        pass
    await _broadcast_board(token)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except Exception:
                continue
            mtype = msg.get("type")
            if mtype == "ping":
                try:
                    await websocket.send_text(json.dumps({"type": "pong"}))
                except Exception:
                    pass
                continue
            if mtype == "edit":
                disp = sess.get("displays", {}).get(display_id)
                if not disp or not disp.get("interactive"):
                    continue  # only interactive (touch) surfaces may drive
                ch = _effective_channel(sess, disp)
                _apply_channel(ch, msg)
                if disp.get("mode") != "independent":
                    await _push_program(token)
                else:
                    await _send_display(token, display_id)
                await _broadcast_board(token)
                await _audit(sess, "edit", actor_role=disp.get("role"),
                             page=ch.get("page"),
                             payload={"display_id": display_id,
                                      "from": "display",
                                      "op": msg.get("op"),
                                      "marks_n": len(ch.get("marks") or [])})
    except WebSocketDisconnect:
        pass
    finally:
        _display_ws.get(token, {}).pop(display_id, None)
        if sess.get("displays"):
            sess["displays"].pop(display_id, None)
        await _audit(sess, "display_leave", payload={"display_id": display_id})
        await _broadcast_board(token)


# WebSocket: operator console (receives live board)

@router.websocket("/ws/trial-console/{token}")
async def console_ws(websocket: WebSocket, token: str):
    sess = _sessions.get(token)
    if not sess:
        await websocket.close(code=4004, reason="Session not found")
        return
    await websocket.accept()
    _console_ws.setdefault(token, set()).add(websocket)
    try:
        await websocket.send_text(json.dumps(_board(sess)))
    except Exception:
        _console_ws.get(token, set()).discard(websocket)
        return
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        _console_ws.get(token, set()).discard(websocket)


# Role-aware display client (zoom + marks + interactive + identify + FS)

DISPLAY_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Praesidium Trial Display</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@700&family=DM+Sans:wght@400;600&display=swap');
  *{margin:0;padding:0;box-sizing:border-box;-webkit-user-select:none;user-select:none}
  html,body{width:100%;height:100%;overflow:hidden;background:#000;font-family:'DM Sans',sans-serif;touch-action:none}
  #stage{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;overflow:hidden}
  #frame{position:relative;transition:transform .12s ease-out;transform-origin:50% 50%}
  #docimg{display:block;max-width:100vw;max-height:100vh;object-fit:contain}
  #marks{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
  #idle,#blank,#ended{position:absolute;inset:0;display:none;flex-direction:column;align-items:center;justify-content:center;color:#94a3b8;gap:14px;background:#000}
  #idle.on,#blank.on,#ended.on{display:flex}
  .logo{font-family:'Cormorant Garamond',Georgia,serif;font-size:34px;font-weight:700;color:#D4A843;letter-spacing:3px}
  .sub{font-size:13px;opacity:.55}
  .pulse{width:12px;height:12px;border-radius:50%;background:#D4A843;animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
  #exhibit{position:fixed;top:18px;right:18px;border:2px solid #DC2626;padding:4px 16px;font-weight:700;font-size:15px;color:#fff;background:rgba(220,38,38,.85);letter-spacing:1px;border-radius:2px;display:none;z-index:5}
  #rolechip{position:fixed;bottom:12px;left:14px;font-size:10px;color:rgba(255,255,255,.32);letter-spacing:1px;text-transform:uppercase;z-index:5}
  #conn{position:fixed;bottom:12px;right:14px;width:7px;height:7px;border-radius:50%;background:#4ade80;z-index:5}
  #conn.off{background:#ef4444}
  #fsbtn{position:fixed;top:14px;left:14px;z-index:6;background:rgba(15,23,42,.7);color:#cbd5e1;border:1px solid rgba(148,163,184,.3);border-radius:6px;padding:6px 10px;font-size:11px;cursor:pointer;font-family:inherit}
  #ident{position:fixed;inset:0;display:none;align-items:center;justify-content:center;flex-direction:column;background:rgba(13,31,60,.92);z-index:20;gap:10px}
  #ident.on{display:flex}
  #ident .big{font-family:'Cormorant Garamond',Georgia,serif;font-size:13vw;font-weight:700;color:#D4A843;line-height:1}
  #ident .r{font-size:3vw;color:#fff;text-transform:uppercase;letter-spacing:3px}
  #tools{position:fixed;bottom:0;left:0;right:0;display:none;align-items:center;justify-content:center;gap:8px;padding:8px;background:rgba(15,23,42,.82);z-index:8;flex-wrap:wrap}
  #tools.on{display:flex}
  #tools button{background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:7px 11px;font-size:13px;cursor:pointer;font-family:inherit}
  #tools button.sel{background:#D4A843;color:#1a1a1a;border-color:#D4A843;font-weight:700}
  #tools .sw{width:22px;height:22px;border-radius:50%;cursor:pointer;border:2px solid #fff}
</style></head>
<body>
<div id="stage">
  <div id="frame"><img id="docimg" alt=""><svg id="marks" viewBox="0 0 1000 1000" preserveAspectRatio="none"></svg></div>
</div>
<div id="idle" class="on"><div class="logo">PRAESIDIUM</div><div class="sub">Trial Display &mdash; waiting</div><div class="pulse"></div></div>
<div id="blank"></div>
<div id="ended"><div class="logo">PRAESIDIUM</div><div class="sub">Session ended</div></div>
<div id="exhibit"></div>
<button id="fsbtn">&#9974; Full screen</button>
<div id="rolechip">connecting&hellip;</div>
<div id="conn" class="off"></div>
<div id="ident"><div class="big" id="identName">1</div><div class="r" id="identRole"></div></div>
<div id="tools">
  <button data-tool="pan" class="sel">&#9995;</button>
  <button data-tool="pen">&#9998; Pen</button>
  <button data-tool="hl">&#9646; Hi-lite</button>
  <button data-tool="arrow">&#8599; Arrow</button>
  <button data-tool="box">&#9634; Box</button>
  <span class="sw" data-col="#dc2626" style="background:#dc2626"></span>
  <span class="sw" data-col="#facc15" style="background:#facc15"></span>
  <span class="sw" data-col="#22d3ee" style="background:#22d3ee"></span>
  <button id="clr">Clear</button>
  <button data-z="in">&#65291;</button><button data-z="out">&#65293;</button><button data-z="fit">Fit</button>
  <button data-pg="-1">&#8249; pg</button><button data-pg="1">pg &#8250;</button>
</div>
<script>
const TOKEN="__TOKEN__";
const wsProto=location.protocol==='https:'?'wss:':'ws:';
const $=id=>document.getElementById(id);
let ws,timer,displayId=null,interactive=false,curFrame=null,identTimer=null;
let tool='pan',color='#dc2626',drawing=null;
function show(which){for(const k of ['idle','blank','ended'])$(k).classList.toggle('on',k===which);
  $('frame').style.display=(which==='doc')?'block':'none';}
function applyView(v){v=v||{zoom:1,cx:.5,cy:.5};const f=$('frame');
  f.style.transformOrigin=(v.cx*100)+'% '+(v.cy*100)+'%';
  f.style.transform='scale('+(v.zoom||1)+')';}
function drawMarks(marks){const svg=$('marks');while(svg.firstChild)svg.removeChild(svg.firstChild);
  (marks||[]).forEach(m=>{let el;const NS='http://www.w3.org/2000/svg';const S=1000;
    if(m.type==='hl'){el=document.createElementNS(NS,'rect');el.setAttribute('x',m.x*S);el.setAttribute('y',m.y*S);el.setAttribute('width',m.w*S);el.setAttribute('height',m.h*S);el.setAttribute('fill',(m.color||'#facc15'));el.setAttribute('fill-opacity','.32');}
    else if(m.type==='box'){el=document.createElementNS(NS,'rect');el.setAttribute('x',m.x*S);el.setAttribute('y',m.y*S);el.setAttribute('width',m.w*S);el.setAttribute('height',m.h*S);el.setAttribute('fill','none');el.setAttribute('stroke',(m.color||'#dc2626'));el.setAttribute('stroke-width','4');}
    else if(m.type==='arrow'){el=document.createElementNS(NS,'line');el.setAttribute('x1',m.x1*S);el.setAttribute('y1',m.y1*S);el.setAttribute('x2',m.x2*S);el.setAttribute('y2',m.y2*S);el.setAttribute('stroke',(m.color||'#dc2626'));el.setAttribute('stroke-width','5');el.setAttribute('marker-end','url(#ah)');}
    else if(m.type==='pen'&&m.points&&m.points.length){el=document.createElementNS(NS,'polyline');el.setAttribute('points',m.points.map(p=>(p[0]*S)+','+(p[1]*S)).join(' '));el.setAttribute('fill','none');el.setAttribute('stroke',(m.color||'#dc2626'));el.setAttribute('stroke-width','5');el.setAttribute('stroke-linejoin','round');el.setAttribute('stroke-linecap','round');}
    if(el)svg.appendChild(el);});
  let defs=document.createElementNS('http://www.w3.org/2000/svg','defs');
  defs.innerHTML='<marker id="ah" markerWidth="10" markerHeight="10" refX="7" refY="3" orient="auto"><path d="M0,0 L7,3 L0,6 Z" fill="'+color+'"/></marker>';
  svg.appendChild(defs);}
function render(f){curFrame=f;
  if(!f){show('idle');$('exhibit').style.display='none';return;}
  if(f.kind==='blank'){show('blank');$('exhibit').style.display='none';return;}
  if(f.kind==='idle'){show('idle');$('exhibit').style.display='none';return;}
  if(f.kind==='doc'&&f.page_url){
    const img=$('docimg');const src=location.origin+f.page_url+'?w=2400';
    if(img.getAttribute('src')!==src){img.onload=()=>show('doc');img.src=src;}else show('doc');
    applyView(f.view);drawMarks(f.marks);
    if(f.exhibit_label){$('exhibit').textContent='EXHIBIT '+f.exhibit_label;$('exhibit').style.display='block';}
    else $('exhibit').style.display='none';
  }}
function setRole(role,label){$('rolechip').textContent=(label||'')+(role&&role!=='unassigned'?(' . '+role.replace('_',' ')):'');
  if(label)$('identName').textContent=label;$('identRole').textContent=(role&&role!=='unassigned')?role.replace('_',' '):'';}
function setInteractive(on){interactive=!!on;$('tools').classList.toggle('on',interactive);}
function sendEdit(op,extra){if(!interactive||!ws||ws.readyState!==1)return;
  ws.send(JSON.stringify(Object.assign({type:'edit',op:op},extra)));}
function norm(ev){const img=$('docimg');const r=img.getBoundingClientRect();
  let x=(ev.clientX-r.left)/r.width,y=(ev.clientY-r.top)/r.height;
  return [Math.max(0,Math.min(1,x)),Math.max(0,Math.min(1,y))];}
function connect(){
  ws=new WebSocket(wsProto+'//'+location.host+'/ws/trial-present/'+TOKEN);
  ws.onopen=()=>{$('conn').classList.remove('off');timer=setInterval(()=>{try{ws.send(JSON.stringify({type:'ping'}))}catch(e){}},30000);};
  ws.onmessage=ev=>{try{const m=JSON.parse(ev.data);
    if(m.type==='registered'){displayId=m.display_id;setRole(m.role,m.label);}
    if(m.type==='display_state'){if(m.role!==undefined)setRole(m.role,m.label);if(m.interactive!==undefined)setInteractive(m.interactive);render(m.frame);}
    if(m.type==='identify'){setRole(m.role,m.label);$('ident').classList.add('on');clearTimeout(identTimer);identTimer=setTimeout(()=>$('ident').classList.remove('on'),5000);}
    if(m.type==='session_ended'){show('ended');$('exhibit').style.display='none';}
  }catch(e){}};
  ws.onclose=()=>{$('conn').classList.add('off');clearInterval(timer);setTimeout(connect,3000);};
  ws.onerror=()=>ws.close();}
function goFS(){const el=document.documentElement;if(!document.fullscreenElement){(el.requestFullscreen||el.webkitRequestFullscreen||function(){}).call(el);}}
$('fsbtn').onclick=goFS;
document.body.addEventListener('click',function once(){goFS();},{once:true});
$('tools').addEventListener('click',e=>{
  const t=e.target.closest('[data-tool]');if(t){tool=t.getAttribute('data-tool');document.querySelectorAll('#tools [data-tool]').forEach(b=>b.classList.toggle('sel',b===t));return;}
  const sw=e.target.closest('[data-col]');if(sw){color=sw.getAttribute('data-col');return;}
  const z=e.target.closest('[data-z]');if(z){const v=(curFrame&&curFrame.view)?Object.assign({},curFrame.view):{zoom:1,cx:.5,cy:.5};const d=z.getAttribute('data-z');v.zoom=d==='in'?Math.min(5,(v.zoom||1)+.5):d==='out'?Math.max(1,(v.zoom||1)-.5):1;if(d==='fit'){v.cx=.5;v.cy=.5;}sendEdit('view',{view:v});return;}
  const pg=e.target.closest('[data-pg]');if(pg){sendEdit('page',{page_delta:parseInt(pg.getAttribute('data-pg'),10)});return;}
  if(e.target.id==='clr'){sendEdit('clear',{clear:true});return;}
});
const svg=$('marks');
function startDraw(ev){if(!interactive||tool==='pan')return;ev.preventDefault();const p=norm(ev);const x=p[0],y=p[1];
  if(tool==='pen')drawing={type:'pen',color:color,points:[[x,y]]};
  else if(tool==='hl')drawing={type:'hl',color:color,x:x,y:y,w:0,h:0,_ox:x,_oy:y};
  else if(tool==='box')drawing={type:'box',color:color,x:x,y:y,w:0,h:0,_ox:x,_oy:y};
  else if(tool==='arrow')drawing={type:'arrow',color:color,x1:x,y1:y,x2:x,y2:y};}
function moveDraw(ev){if(!drawing)return;ev.preventDefault();const p=norm(ev);const x=p[0],y=p[1];
  if(drawing.type==='pen')drawing.points.push([x,y]);
  else if(drawing.type==='arrow'){drawing.x2=x;drawing.y2=y;}
  else{drawing.x=Math.min(drawing._ox,x);drawing.y=Math.min(drawing._oy,y);drawing.w=Math.abs(x-drawing._ox);drawing.h=Math.abs(y-drawing._oy);}
  const prev=(curFrame&&curFrame.marks)?curFrame.marks:[];drawMarks(prev.concat([drawing]));}
function endDraw(ev){if(!drawing)return;const m=drawing;drawing=null;
  if(m._ox!==undefined){delete m._ox;delete m._oy;}
  sendEdit('addmark',{addmark:m});}
const istage=$('stage');
istage.addEventListener('pointerdown',e=>{if(interactive&&tool!=='pan')startDraw(e);});
istage.addEventListener('pointermove',e=>{if(drawing)moveDraw(e);});
istage.addEventListener('pointerup',e=>{if(drawing)endDraw(e);});
istage.addEventListener('pointercancel',e=>{if(drawing)endDraw(e);});
connect();
</script>
</body></html>"""


def _render_display(token: str) -> HTMLResponse:
    sess = _sessions.get(token)
    if not sess:
        return HTMLResponse(
            "<html><body style='background:#000;color:#94a3b8;display:flex;"
            "align-items:center;justify-content:center;height:100vh;"
            "font-family:sans-serif'><div style='text-align:center'>"
            "<h2 style='color:#D4A843'>Session Not Found</h2>"
            "<p>This display link has expired or the session has ended.</p>"
            "</div></body></html>", status_code=404)
    return HTMLResponse(DISPLAY_HTML.replace("__TOKEN__", token))


@router.get("/present/trial/{token}", response_class=HTMLResponse)
async def trial_display_page(token: str):
    return _render_display(token)


@router.get("/p/{code}", response_class=HTMLResponse)
async def trial_display_tiny(code: str):
    token = _codes.get((code or "").upper())
    if not token:
        return HTMLResponse(
            "<html><body style='background:#000;color:#94a3b8;display:flex;"
            "align-items:center;justify-content:center;height:100vh;"
            "font-family:sans-serif'><div style='text-align:center'>"
            "<h2 style='color:#D4A843'>Display Not Found</h2>"
            "<p>Check the code, or the session has ended.</p>"
            "</div></body></html>", status_code=404)
    return _render_display(token)
