"""
modules/depositions/routes/trial_intake_api.py

AI-assisted Trial Center document intake. Drop ANY trial-related document —
an exhibit, a pleading, a motion, a transcript — and an AI assistant triages
it: classifies the document, proposes what to do with it, and (after a short
chat to resolve party / matter / kind) executes the action:

  exhibit   -> file into the matter DMS + create a marked trial_exhibits row
  pleading  -> file into the matter DMS (surfaces in the Pleadings tab)
  motion    -> file into the matter DMS (surfaces in the Motions tab)
  transcript-> file + register on the shared transcript substrate (depo DAG)
  file/other-> file into the matter DMS

Designed for speed mid-trial: decisive defaults, one drop -> a proposed action.
All AI goes through the mandated adapter (modules.intelligence.anthropic_adapter).
State for the multi-turn chat lives in trial_intake_sessions (migration 0109).
"""
import json
import logging
import os
import re
import shutil
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user
from modules.depositions.routes.depo_api import _tenant, _serialize

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/trial/intake", tags=["trial-intake-api"])

PRAESIDIUM_ROOT = "/mnt/praesidium"
_MAX_TEXT = 18000

_SYSTEM = (
    "You are the Trial Center intake assistant for a litigation practice. A user "
    "has dropped a document into the system — possibly in the middle of a trial — "
    "and needs to know what it is and what to do with it, fast.\n\n"
    "Classify the document into exactly one category:\n"
    "  exhibit    — documentary evidence to be marked/offered as a trial exhibit\n"
    "  pleading   — petition, answer, complaint, counterclaim, plea, cross-claim\n"
    "  motion     — motion, response, reply, or brief (motion practice)\n"
    "  transcript — a deposition, hearing, or trial transcript\n"
    "  file       — anything else; just file it to the matter\n\n"
    "Be decisive and brief — you are assisting a trial lawyer who needs speed. "
    "Propose the single most likely action. For an exhibit, infer the offering "
    "party (plaintiff or defendant) from the content when you can.\n\n"
    "Respond with ONLY a JSON object, no prose, no code fences:\n"
    '{"message": "<one or two sentences to the lawyer>", '
    '"action": {"type": "exhibit|pleading|motion|transcript|file", '
    '"party": "plaintiff|defendant|null", '
    '"exhibit_label": "<short human label, or null>", '
    '"transcript_kind": "deposition|hearing|trial|null", '
    '"dms_subfolder": "<folder name to file under, e.g. Exhibits/Pleadings/Motions>", '
    '"ready": true|false}}\n'
    "Set ready=true only when you have enough to act. If the offering party for an "
    "exhibit is genuinely ambiguous, ask one short question and set ready=false."
)


# --------------------------------------------------------------------------- #
#  helpers                                                                     #
# --------------------------------------------------------------------------- #
def _uid(user):
    return getattr(user, "id", None) if user is not None else None


def _extract(path: str, filename: str):
    """Best-effort text + page count from a staged file (PDF/docx/text)."""
    ext = os.path.splitext(filename or "")[1].lower()
    try:
        if ext == ".pdf":
            import fitz  # PyMuPDF
            d = fitz.open(path)
            n = len(d)
            txt = "\n".join(p.get_text("text") for p in d)
            return txt[:_MAX_TEXT], n
        if ext in (".txt", ".text", ".md", ".csv", ".log"):
            with open(path, "r", errors="ignore") as f:
                return f.read()[:_MAX_TEXT], None
        if ext == ".docx":
            import docx
            doc = docx.Document(path)
            return "\n".join(p.text for p in doc.paragraphs)[:_MAX_TEXT], None
    except Exception as e:
        logger.warning("intake extract failed (%s): %s", filename, e)
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "ignore")[:_MAX_TEXT], None
    except Exception:
        return "", None


def _parse_json(txt: str):
    t = (txt or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*", "", t).strip()
        if t.endswith("```"):
            t = t[:-3].strip()
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        t = t[i:j + 1]
    try:
        return json.loads(t)
    except Exception:
        return None


async def _ai(tid, user_id, matter_id, user_prompt, max_tokens=1100):
    """Single text call through the mandated adapter; returns raw text."""
    from modules.intelligence import anthropic_adapter
    ctx = anthropic_adapter.AICallContext(
        tenant_id=tid, module="intelligence", purpose="chat",
        user_id=user_id, matter_id=matter_id)
    res = await anthropic_adapter.call(
        ctx, raw_user_prompt=user_prompt, raw_system_prompt=_SYSTEM,
        max_tokens_override=max_tokens)
    return res.text


def _doc_context(filename, category, matter_name, extracted_text):
    head = (extracted_text or "").strip()[:6000]
    parts = [
        f"Dropped file: {filename}",
        f"Matter: {matter_name}" if matter_name else "Matter: (not yet chosen)",
    ]
    if category:
        parts.append(f"Earlier read: looked like a {category}.")
    parts.append("Document text (truncated):\n" + (head or "(no extractable text)"))
    return "\n".join(parts)


async def _matter_name(tid, matter_id):
    if not matter_id:
        return None
    try:
        async with AsyncSessionLocal() as s:
            r = await s.execute(sa_text(
                "SELECT matter_name FROM matters "
                "WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
                {"mid": matter_id, "tid": tid})
            row = r.mappings().fetchone()
            return row["matter_name"] if row else None
    except Exception:
        return None


async def _file_into_dms(tid, matter_id, subfolder, src_path, filename,
                         user_id, extracted_text=""):
    """Copy a staged file into the matter's DMS tree and insert a documents row.
    Returns (document_id, dest_path)."""
    import hashlib
    import mimetypes
    async with AsyncSessionLocal() as s:
        r = await s.execute(sa_text(
            "SELECT m.matter_name, c.client_name FROM matters m "
            "LEFT JOIN clients c ON c.id = m.client_id AND TRIM(c.tenant_id) = TRIM(:tid) "
            "WHERE m.id = CAST(:mid AS uuid) AND TRIM(m.tenant_id) = TRIM(:tid)"),
            {"tid": tid, "mid": matter_id})
        row = r.mappings().fetchone()
    if not row:
        raise ValueError("matter not found")
    base = os.path.join(PRAESIDIUM_ROOT, tid.strip(), "matters",
                        row["client_name"] or "_", row["matter_name"] or "_",
                        subfolder or "Trial")
    os.makedirs(base, exist_ok=True)
    safe = os.path.basename(filename or "document")
    dest = os.path.join(base, safe)
    if os.path.exists(dest):
        stem, ext = os.path.splitext(safe)
        k = 2
        while os.path.exists(os.path.join(base, "%s (%d)%s" % (stem, k, ext))):
            k += 1
        dest = os.path.join(base, "%s (%d)%s" % (stem, k, ext))
    shutil.copyfile(src_path, dest)

    with open(dest, "rb") as f:
        content = f.read()
    checksum = hashlib.sha256(content).hexdigest()
    mime = mimetypes.guess_type(dest)[0] or "application/octet-stream"
    doc_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as s:
        await s.execute(sa_text("""
            INSERT INTO documents (
                id, tenant_id, matter_id, filename, original_filename,
                mime_type, file_size, storage_path, checksum,
                extracted_text, version_number, status, created_by,
                created_at, updated_at
            ) VALUES (
                CAST(:id AS uuid), :tid, CAST(:mid AS uuid), :fn, :ofn,
                :mime, :fs, :sp, :cs, :et, 1, 'active', :uid, NOW(), NOW()
            ) ON CONFLICT DO NOTHING
        """), {"id": doc_id, "tid": tid, "mid": matter_id, "fn": os.path.basename(dest),
               "ofn": safe, "mime": mime, "fs": len(content), "sp": dest,
               "cs": checksum, "et": (extracted_text or None),
               "uid": user_id})
        await s.commit()
    return doc_id, dest


async def _next_exhibit_number(session, tid, matter_id, party):
    r = await session.execute(sa_text(
        "SELECT exhibit_number FROM trial_exhibits "
        "WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = TRIM(:tid) "
        "  AND COALESCE(party,'') = COALESCE(:p,'')"),
        {"mid": matter_id, "tid": tid, "p": party})
    mx = 0
    for row in r.mappings().fetchall():
        m = re.search(r"(\d+)", row["exhibit_number"] or "")
        if m:
            mx = max(mx, int(m.group(1)))
    n = mx + 1
    prefix = {"plaintiff": "P", "defendant": "D"}.get((party or "").lower(), "")
    return ("%s-%d" % (prefix, n)) if prefix else str(n)


# --------------------------------------------------------------------------- #
#  analyze — drop a file, get a first read + proposed action                   #
# --------------------------------------------------------------------------- #
@router.post("/analyze")
async def analyze(request: Request, file: UploadFile = File(...),
                  matter_id: str = Form(""), user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        intake_id = str(uuid.uuid4())
        stage_dir = os.path.join(PRAESIDIUM_ROOT, tid.strip(), "_intake")
        os.makedirs(stage_dir, exist_ok=True)
        safe = os.path.basename(file.filename or "document")
        staged = os.path.join(stage_dir, intake_id + "__" + safe)
        with open(staged, "wb") as out:
            shutil.copyfileobj(file.file, out)

        text, pages = _extract(staged, safe)
        mname = await _matter_name(tid, matter_id) if matter_id else None

        prompt = ("A document was just dropped into the Trial Center.\n\n"
                  + _doc_context(safe, None, mname, text)
                  + "\n\nClassify it and propose what to do. Respond with the JSON object only.")
        raw = await _ai(tid, _uid(user), matter_id or None, prompt)
        parsed = _parse_json(raw) or {
            "message": "I couldn't read this one cleanly — tell me what it is and "
                       "I'll file it.",
            "action": {"type": "file", "party": None, "exhibit_label": None,
                       "transcript_kind": None, "dms_subfolder": "Trial", "ready": False},
        }
        action = parsed.get("action") or {}
        category = action.get("type") or "file"

        async with AsyncSessionLocal() as s:
            await s.execute(sa_text("""
                INSERT INTO trial_intake_sessions
                    (id, tenant_id, matter_id, file_path, filename, mime_type,
                     page_count, extracted_text, category, analysis, status, created_by)
                VALUES (CAST(:id AS uuid), :tid,
                        CASE WHEN :mid = '' THEN NULL ELSE CAST(:mid AS uuid) END,
                        :fp, :fn, :mime, :pc, :et, :cat, CAST(:an AS jsonb), 'open', :uid)
            """), {"id": intake_id, "tid": tid, "mid": matter_id or "",
                   "fp": staged, "fn": safe,
                   "mime": file.content_type or "application/octet-stream",
                   "pc": pages, "et": text, "cat": category,
                   "an": json.dumps(parsed),
                   "uid": str(_uid(user)) if _uid(user) is not None else None})
            await s.commit()

        return JSONResponse(_serialize({
            "intake_id": intake_id, "filename": safe, "page_count": pages,
            "matter_id": matter_id or None, "matter_name": mname,
            "message": parsed.get("message", ""), "action": action,
            "text_preview": (text or "")[:600],
        }))
    except Exception as e:
        logger.exception("intake analyze failed")
        return JSONResponse({"error": str(e)}, 500)


# --------------------------------------------------------------------------- #
#  chat — refine the plan                                                      #
# --------------------------------------------------------------------------- #
class ChatMsg(BaseModel):
    role: str
    content: str


class ChatBody(BaseModel):
    intake_id: str
    matter_id: Optional[str] = None
    messages: List[ChatMsg] = []


@router.post("/chat")
async def chat(request: Request, body: ChatBody, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as s:
            r = await s.execute(sa_text(
                "SELECT filename, category, extracted_text, matter_id::text "
                "FROM trial_intake_sessions "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
                {"id": body.intake_id, "tid": tid})
            row = r.mappings().fetchone()
        if not row:
            return JSONResponse({"error": "intake session not found"}, 404)

        matter_id = body.matter_id or row["matter_id"]
        mname = await _matter_name(tid, matter_id) if matter_id else None
        convo = "\n".join(
            ("Lawyer: " if m.role == "user" else "Assistant: ") + m.content
            for m in body.messages[-12:])
        prompt = (_doc_context(row["filename"], row["category"], mname, row["extracted_text"])
                  + "\n\nConversation so far:\n" + (convo or "(none)")
                  + "\n\nReply to the lawyer and update the proposed action. "
                    "Respond with the JSON object only.")
        raw = await _ai(tid, _uid(user), matter_id, prompt)
        parsed = _parse_json(raw) or {
            "message": raw[:500] if raw else "Sorry, try rephrasing.",
            "action": {"type": row["category"] or "file", "ready": False},
        }

        async with AsyncSessionLocal() as s:
            await s.execute(sa_text(
                "UPDATE trial_intake_sessions SET analysis = CAST(:an AS jsonb), "
                "category = :cat, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
                {"an": json.dumps(parsed),
                 "cat": (parsed.get("action") or {}).get("type") or row["category"],
                 "id": body.intake_id, "tid": tid})
            await s.commit()

        return JSONResponse(_serialize({
            "message": parsed.get("message", ""),
            "action": parsed.get("action") or {},
        }))
    except Exception as e:
        logger.exception("intake chat failed")
        return JSONResponse({"error": str(e)}, 500)


# --------------------------------------------------------------------------- #
#  execute — do the thing                                                      #
# --------------------------------------------------------------------------- #
class ExecAction(BaseModel):
    type: str
    party: Optional[str] = None
    exhibit_label: Optional[str] = None
    transcript_kind: Optional[str] = None
    dms_subfolder: Optional[str] = None


class ExecBody(BaseModel):
    intake_id: str
    matter_id: str
    action: ExecAction


@router.post("/execute")
async def execute(request: Request, body: ExecBody, user=Depends(get_current_user)):
    tid = _tenant(request)
    if not body.matter_id:
        return JSONResponse({"error": "matter_id required"}, 400)
    try:
        async with AsyncSessionLocal() as s:
            r = await s.execute(sa_text(
                "SELECT file_path, filename, extracted_text "
                "FROM trial_intake_sessions "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
                {"id": body.intake_id, "tid": tid})
            row = r.mappings().fetchone()
        if not row:
            return JSONResponse({"error": "intake session not found"}, 404)
        src, fname, etext = row["file_path"], row["filename"], row["extracted_text"]
        if not os.path.exists(src):
            return JSONResponse({"error": "staged file is gone"}, 410)

        a = body.action
        kind = (a.type or "file").lower()
        redirect = "/trial/home/" + body.matter_id
        summary = ""

        if kind == "transcript":
            # file into Depositions tree + seed the shared transcript substrate
            doc_id, dest = await _file_into_dms(
                tid, body.matter_id, "Depositions", src, fname, _uid(user), etext)
            deponent = os.path.splitext(os.path.basename(dest))[0]

            def _seed():
                from modules.depositions.jobs.deposition_alerts import register_pending
                from modules.depositions.routes.alerts_api import _ingest_alert_sync
                from modules.depositions.jobs.depo_dag import enqueue_pipeline
                res = register_pending(tid.strip(), dest, body.matter_id, None, deponent, False)
                alert_id = ((res or {}).get("alert") or {}).get("alert")
                if alert_id:
                    res["ingest"] = _ingest_alert_sync(tid.strip(), alert_id)
                res["pipeline_job"] = enqueue_pipeline(tid.strip(), body.matter_id)
                return res
            import asyncio
            await asyncio.get_event_loop().run_in_executor(None, _seed)
            summary = "Transcript filed and queued for ingest — it'll appear under Depositions once parsed."

        elif kind == "exhibit":
            party = (a.party or "").lower() or None
            doc_id, dest = await _file_into_dms(
                tid, body.matter_id, a.dms_subfolder or "Exhibits", src, fname,
                _uid(user), etext)
            async with AsyncSessionLocal() as s:
                tr = await s.execute(sa_text(
                    "SELECT trial_id::text FROM trial_exhibits "
                    "WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = TRIM(:tid) "
                    "  AND trial_id IS NOT NULL LIMIT 1"),
                    {"mid": body.matter_id, "tid": tid})
                trow = tr.mappings().fetchone()
                trial_id = trow["trial_id"] if trow else None
                exnum = await _next_exhibit_number(s, tid, body.matter_id, party)
                label = a.exhibit_label or os.path.splitext(fname)[0]
                await s.execute(sa_text("""
                    INSERT INTO trial_exhibits
                        (id, tenant_id, matter_id, trial_id, party, exhibit_number,
                         exhibit_label, document_id, document_source, status,
                         admitted, conditional, notes, created_at, updated_at)
                    VALUES (CAST(:id AS uuid), :tid, CAST(:mid AS uuid),
                        CASE WHEN :trid IS NULL THEN NULL ELSE CAST(:trid AS uuid) END,
                        :party, :num, :label, CAST(:doc AS uuid), 'dms',
                        'marked', false, false, :notes, now(), now())
                """), {"id": str(uuid.uuid4()), "tid": tid, "mid": body.matter_id,
                       "trid": trial_id, "party": party, "num": exnum, "label": label,
                       "doc": doc_id, "notes": "Marked via AI trial intake"})
                await s.commit()
            summary = "Marked as %s exhibit %s — open in the Trial Exhibits tab." % (
                party or "unassigned", exnum)
            redirect = "/trial/home/" + body.matter_id

        else:  # pleading | motion | file
            folder = a.dms_subfolder or {"pleading": "Pleadings",
                                         "motion": "Motions"}.get(kind, "Trial")
            doc_id, dest = await _file_into_dms(
                tid, body.matter_id, folder, src, fname, _uid(user), etext)
            where = {"pleading": "Pleadings", "motion": "Motions"}.get(kind, "the matter")
            summary = "Filed to %s — it'll surface in %s as it's classified." % (folder, where)

        async with AsyncSessionLocal() as s:
            await s.execute(sa_text(
                "UPDATE trial_intake_sessions SET status = 'done', "
                "matter_id = CAST(:mid AS uuid), updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
                {"mid": body.matter_id, "id": body.intake_id, "tid": tid})
            await s.commit()
        try:
            os.remove(src)
        except Exception:
            pass

        return JSONResponse({"ok": True, "summary": summary, "redirect": redirect})
    except Exception as e:
        logger.exception("intake execute failed")
        return JSONResponse({"error": str(e)}, 500)
