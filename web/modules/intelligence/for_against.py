"""for_against.py -- reusable "Evidence For & Against" engine (frontier opus-4-8).

A corpus-agnostic generalization of the appellate Record Fact-Element Classifier.
A SPINE of claim-items (elements of causes of action, or proposed findings of fact /
conclusions of law) is matched against a chosen CORPUS in the shared ModernBERT-768
space; for each item the top-K nearest corpus excerpts are handed to a FRONTIER
(claude-opus-4-8) call that labels each FOR / AGAINST / NEUTRAL with a pulled quote,
a record/Bates cite, and a one-sentence rationale.

Spine kinds:
  pleading_coa -- causes of action + elements (reuse coa_elements if present, else
                  detect from the operative pleading vs cause_of_action_library, then
                  seed causes_of_action/coa_elements -- the litigation spine).
  ffcl         -- a proposed Findings of Fact & Conclusions of Law document, parsed
                  into numbered finding/conclusion items.

Corpora:
  record     -- CR (document_sections.cr_para) + RR (transcript_qa_units).
  ediscovery -- ediscovery_chunk_embeddings.embedding_768 (scoped to the matter).

CLI (inside praesidium-web, cwd /app):
  python -m modules.intelligence.for_against --build pleading_coa --corpus record \
       --matter <uuid> --appeal <uuid> --tenant T [--run]
  python -m modules.intelligence.for_against --build ffcl --corpus ediscovery \
       --matter <uuid> --doc "/path/to/FoF.docx" --tenant T [--run]
  python -m modules.intelligence.for_against --run --spine <uuid> --tenant T
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import uuid

logger = logging.getLogger(__name__)

TOP_K = 18
CAND_CHARS = 700
ITEM_CHARS = 800


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def _embed_texts(texts):
    """ModernBERT-768 vectors for a list of texts (reuses the depo embed contract)."""
    from modules.depositions.jobs.embed_qa import _embed, EMBED_URL_DEFAULT
    out = []
    B = 64
    for i in range(0, len(texts), B):
        embs, _, _ = _embed(EMBED_URL_DEFAULT, [t[:8000] for t in texts[i:i + B]])
        out.extend(embs)
    return out


def _vec(v):
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


# --------------------------------------------------------------------------- #
#  spine row lifecycle                                                          #
# --------------------------------------------------------------------------- #

def _create_spine(cur, tenant, matter_id, spine_kind, corpus, label, source_ref,
                  appellate_case_id=None):
    """Idempotent: replace any spine of the same (matter, kind, corpus)."""
    cur.execute("SELECT id::text FROM evidence_spines WHERE matter_id=CAST(%s AS uuid) "
                "AND spine_kind=%s AND corpus=%s", (matter_id, spine_kind, corpus))
    for (sid,) in cur.fetchall():
        cur.execute("DELETE FROM evidence_spines WHERE id=CAST(%s AS uuid)", (sid,))
    sid = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO evidence_spines (id, tenant_id, matter_id, appellate_case_id, spine_kind, "
        "  corpus, label, source_ref, status) VALUES (CAST(%s AS uuid), %s, CAST(%s AS uuid), "
        "  %s, %s, %s, %s, CAST(%s AS jsonb), 'building')",
        (sid, tenant, matter_id, appellate_case_id, spine_kind, corpus, label,
         json.dumps(source_ref or {})))
    return sid


def _insert_items(cur, spine_id, items):
    """items: [{group_label, group_no, item_no, item_role, item_text, embedding(list),
               source_kind, source_ref_id, attributes}]."""
    from psycopg2.extras import execute_values
    rows = [(spine_id, it["group_label"], it["group_no"], it["item_no"], it["item_role"],
             it["item_text"], _vec(it["embedding"]), it.get("source_kind"),
             it.get("source_ref_id"), json.dumps(it.get("attributes") or {}))
            for it in items]
    execute_values(cur,
        "INSERT INTO evidence_spine_items (spine_id, group_label, group_no, item_no, item_role, "
        "  item_text, embedding, source_kind, source_ref_id, attributes) VALUES %s",
        rows,
        template="(CAST(%s AS uuid),%s,%s,%s,%s,%s,%s::vector,%s,CAST(%s AS uuid),CAST(%s AS jsonb))",
        page_size=200)


# --------------------------------------------------------------------------- #
#  spine builder: pleading -> causes of action + elements                      #
# --------------------------------------------------------------------------- #

def _coa_for_matter(cur, matter_id):
    """Existing causes_of_action + coa_elements for a matter (with embeddings)."""
    cur.execute("SELECT id::text, title, coalesce(count_number,0) FROM causes_of_action "
                "WHERE matter_id=CAST(%s AS uuid) ORDER BY count_number NULLS LAST, title", (matter_id,))
    causes = cur.fetchall()
    out = []
    for (cid, title, num) in causes:
        cur.execute("SELECT id::text, element_name, coalesce((attributes->>'n')::int, 0), "
                    "  embedding::text FROM coa_elements WHERE cause_of_action_id=CAST(%s AS uuid) "
                    "ORDER BY (attributes->>'n')::int NULLS LAST", (cid,))
        els = cur.fetchall()
        if els:
            out.append((cid, title, num, els))
    return out


def _pdf_text(path):
    import fitz
    doc = fitz.open(path)
    try:
        return "\n".join(p.get_text("text") for p in doc)
    finally:
        doc.close()


def _paragraphs(text, min_len=40):
    parts = re.split(r"\n\s*\n|(?<=\.)\s{2,}", text or "")
    return [re.sub(r"\s+", " ", p).strip() for p in parts if len(p.strip()) >= min_len]


# claim-bearing pleading taxonomy codes (Trial Center's documents.legal_category);
# answers / motions / responses are excluded -- they don't assert causes of action.
CLAIM_PLEADING_CATS = ('original_petition', 'amended_petition', 'complaint',
                       'counterclaim', 'crossclaim', 'third_party_petition', 'intervention')


def _trial_center_pleadings(cur, matter_id, max_chars=500000):
    """The matter's claim-bearing pleadings AS TRIAL CENTER sees them
    (documents.legal_category in the pleading taxonomy) -> concatenated extracted_text,
    longest first (the amended/operative pleadings carry the fullest claim set)."""
    cur.execute(
        "SELECT coalesce(extracted_text,'') FROM documents "
        "WHERE matter_id=CAST(%s AS uuid) AND legal_category = ANY(%s) "
        "  AND length(coalesce(extracted_text,'')) > 200 "
        "ORDER BY length(extracted_text) DESC", (matter_id, list(CLAIM_PLEADING_CATS)))
    texts, total = [], 0
    for (t,) in cur.fetchall():
        if total >= max_chars:
            break
        texts.append(t)
        total += len(t)
    return "\n\n".join(texts)


async def _gen_pleading_parties(tenant, matter_id, pleadings):
    """pleadings: [(i, legal_category, snippet)] -> {i: {suing, sued}} (one opus call).
    Petitions are filed by the plaintiff; counterclaims/crossclaims/third-party petitions by a
    defendant against another party -- so the filer (suing) varies per pleading."""
    from modules.intelligence import call, AICallContext
    try:
        from modules.intelligence import strip_markdown_fences
    except Exception:
        def strip_markdown_fences(s):
            return (s or "").strip()
    sys = ("You are a litigation analyst. For each numbered pleading excerpt, identify the party "
           "FILING it (the one asserting claims / suing) and the party it is filed AGAINST (being "
           "sued). Petitions/complaints are filed by the plaintiff; counterclaims, crossclaims, and "
           "third-party petitions are filed by a defendant against another party. Use short party "
           "names. Respond with ONLY JSON: {\"pleadings\":[{\"i\":<int>,\"suing\":\"...\",\"sued\":\"...\"}]}")
    user = "PLEADINGS:\n" + "\n".join("[%d] (%s) %s" % (i, cat, sn) for i, cat, sn in pleadings)
    ctx = AICallContext(tenant_id=tenant, module="intelligence",
                        purpose="evidence_for_against", matter_id=matter_id)
    res = await call(ctx, raw_user_prompt=user, raw_system_prompt=sys)
    m = re.search(r"\{.*\}", strip_markdown_fences(res.text or ""), re.S)
    if not m:
        return {}
    try:
        out = {}
        for p in json.loads(m.group(0)).get("pleadings", []):
            out[int(p.get("i"))] = {"suing": p.get("suing"), "sued": p.get("sued")}
        return out
    except Exception:
        return {}


# Precision gate: keep a COA only if it is actually NAMED in the pleadings (lexical) with at
# least a weak topical match, OR it has a strong cosine even without the name. Pure cosine-only
# matches below HIGH_COS are noise (e.g. "Securities Fraud" leaking into a construction suit).
LEX_MIN_COS = 0.30
HIGH_COS = 0.62


def _detect_in_text(cur, text, high_cos=HIGH_COS, lex_min=LEX_MIN_COS):
    """Library COAs asserted in `text` -> [(lid,code,dn,els,sali,sim,lex)] (uncapped, ranked)."""
    from modules.depositions.jobs.appellate_framelock import _COA_WORDS
    from psycopg2.extras import execute_values
    paras = sorted(_paragraphs(text), key=len, reverse=True)[:400]
    if not paras:
        return []
    embs = _embed_texts(paras)
    cur.execute("CREATE TEMP TABLE IF NOT EXISTS tmp_fa_para (embedding vector(768))")
    cur.execute("TRUNCATE tmp_fa_para")
    execute_values(cur, "INSERT INTO tmp_fa_para (embedding) VALUES %s",
                   [(_vec(e),) for e in embs], template="(%s::vector)", page_size=200)
    cur.execute("SELECT lib.id::text, lib.code, lib.display_name, lib.elements_json, lib.sali_iri, "
                "       max(1-(p.embedding <=> lib.embedding)) sim "
                "FROM cause_of_action_library lib, tmp_fa_para p "
                "WHERE lib.is_active AND lib.embedding IS NOT NULL GROUP BY 1,2,3,4,5 ORDER BY sim DESC")
    tl = (text or "").lower()
    out = []
    for (lid, code, dn, ej, sali, sim) in cur.fetchall():
        sim = float(sim)
        lexical = any(w in tl for w in _COA_WORDS.get(code, []))
        if (lexical and sim >= lex_min) or sim >= high_cos:
            els = ej if isinstance(ej, list) else json.loads(ej)
            out.append((lid, code, dn, els, sali, sim, lexical))
    out.sort(key=lambda r: (r[6], r[5]), reverse=True)
    return out


def _seed_cause(cur, tenant, matter_id, n, lid, code, dn, els, lex, sim, suing, sued):
    cause_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO causes_of_action (id, tenant_id, matter_id, cause_library_id, title, "
        "  count_number, status, confidence, attribution, attributes) "
        "VALUES (CAST(%s AS uuid), %s, CAST(%s AS uuid), CAST(%s AS uuid), %s, %s, 'seeded', "
        "  %s, 'for_against', CAST(%s AS jsonb))",
        (cause_id, tenant, matter_id, lid, dn, n, sim,
         json.dumps({"source": "for_against_framelock", "code": code, "lexical": lex,
                     "suing_party": suing, "sued_party": sued})))
    texts = [(e.get("text") if isinstance(e, dict) else str(e)) for e in els]
    embs = _embed_texts(texts)
    el_rows = []
    for e, et, emb in zip(els, texts, embs):
        eid = str(uuid.uuid4())
        en = e.get("n") if isinstance(e, dict) else None
        cur.execute(
            "INSERT INTO coa_elements (id, tenant_id, cause_of_action_id, element_name, status, "
            "  confidence, attribution, embedding, attributes) VALUES (CAST(%s AS uuid), %s, "
            "  CAST(%s AS uuid), %s, 'not_developed', %s, 'for_against', %s::vector, CAST(%s AS jsonb))",
            (eid, tenant, cause_id, et, sim, _vec(emb), json.dumps({"n": en})))
        el_rows.append((eid, et, en or 0, _vec(emb)))
    return (cause_id, dn, n, el_rows)


def _detect_and_seed_coa(cur, tenant, matter_id, pleading_text=None):
    """Seed causes_of_action + coa_elements (the litigation spine). Per-party when reading from
    Trial Center: each claim-bearing pleading is attributed to its filer, then each party's claims
    are detected and seeded separately (so EVERY party's causes are included, tagged suing/sued)."""
    PER_PARTY = 8
    seeded, n = [], 0

    if pleading_text:                       # single-document override (source_ref.doc_path)
        for (lid, code, dn, els, sali, sim, lex) in _detect_in_text(cur, pleading_text)[:PER_PARTY]:
            n += 1
            seeded.append(_seed_cause(cur, tenant, matter_id, n, lid, code, dn, els, lex, sim, None, None))
        cur.execute("DROP TABLE IF EXISTS tmp_fa_para")
        if not seeded:
            raise RuntimeError("no causes of action detected in the pleading")
        return seeded

    # per-party: claim-bearing pleadings -> attribute filer -> detect per party
    cur.execute("SELECT id::text, legal_category, extracted_text FROM documents "
                "WHERE matter_id=CAST(%s AS uuid) AND legal_category = ANY(%s) "
                "  AND length(coalesce(extracted_text,'')) > 200 "
                "ORDER BY length(extracted_text) DESC LIMIT 40", (matter_id, list(CLAIM_PLEADING_CATS)))
    pls = cur.fetchall()
    if not pls:
        raise RuntimeError("no claim-bearing pleadings in Trial Center for this matter")
    try:
        pp = asyncio.run(_gen_pleading_parties(
            tenant, matter_id, [(i, cat, (txt or "")[:700]) for i, (did, cat, txt) in enumerate(pls)]))
    except Exception as e:
        logger.warning("pleading-party identification failed: %s", e)
        pp = {}

    from collections import Counter
    groups = {}
    for i, (did, cat, txt) in enumerate(pls):
        info = pp.get(i, {})
        suing = ((info.get("suing") or "").strip() or "Unknown")
        sued = (info.get("sued") or "").strip()
        g = groups.setdefault(suing, {"texts": [], "sued": Counter()})
        g["texts"].append(txt)
        if sued:
            g["sued"][sued] += 1

    for suing, g in groups.items():
        sued = g["sued"].most_common(1)[0][0] if g["sued"] else None
        for (lid, code, dn, els, sali, sim, lex) in _detect_in_text(
                cur, "\n\n".join(g["texts"]))[:PER_PARTY]:
            n += 1
            seeded.append(_seed_cause(cur, tenant, matter_id, n, lid, code, dn, els, lex, sim, suing, sued))
    cur.execute("DROP TABLE IF EXISTS tmp_fa_para")
    if not seeded:
        raise RuntimeError("no causes of action detected in the pleadings")
    return seeded


def build_pleading_coa_spine(tenant, matter_id, corpus, source_ref=None,
                             appellate_case_id=None, rebuild=False) -> dict:
    """Build a pleading->causes/elements spine. Pleadings come from TRIAL CENTER
    (documents.legal_category, claim-bearing) unless source_ref.doc_path overrides.
    rebuild=True re-detects the claims/elements (clears the prior for_against spine)."""
    tenant = (tenant or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        if rebuild:
            cur.execute("DELETE FROM coa_elements WHERE cause_of_action_id IN "
                        "(SELECT id FROM causes_of_action WHERE matter_id=CAST(%s AS uuid) "
                        " AND attributes->>'source'='for_against_framelock')", (matter_id,))
            cur.execute("DELETE FROM causes_of_action WHERE matter_id=CAST(%s AS uuid) "
                        "AND attributes->>'source'='for_against_framelock'", (matter_id,))
        causes = _coa_for_matter(cur, matter_id)
        seeded = 0
        if not causes:
            # pleadings from Trial Center (per-party detection) or a single doc_path override
            src = source_ref or {}
            path = src.get("doc_path")
            if path and os.path.exists(path):
                text = _pdf_text(path) if path.lower().endswith(".pdf") else _docx_text(path)
                rows = _detect_and_seed_coa(cur, tenant, matter_id, pleading_text=text)
            else:
                rows = _detect_and_seed_coa(cur, tenant, matter_id, pleading_text=None)
            seeded = len(rows)
            # normalize to (cid,title,num,[(eid,name,n,emb_text)])
            causes = [(c[0], c[1], c[2], [(e[0], e[1], e[2], e[3]) for e in c[3]]) for c in rows]

        label = "%d cause%s -> %s" % (len(causes), "" if len(causes) == 1 else "s", corpus)
        sid = _create_spine(cur, tenant, matter_id, "pleading_coa", corpus, label,
                            source_ref, appellate_case_id)
        items = []
        for gi, (cid, title, num, els) in enumerate(causes, start=1):
            for (eid, name, n, emb_text) in els:
                emb = json.loads(emb_text) if emb_text else None
                if emb is None:
                    emb = _embed_texts([name])[0]
                items.append({"group_label": title, "group_no": num or gi, "item_no": n,
                              "item_role": "element", "item_text": name, "embedding": emb,
                              "source_kind": "coa_element", "source_ref_id": eid,
                              "attributes": {"n": n}})
        _insert_items(cur, sid, items)
        cur.execute("UPDATE evidence_spines SET status='seeded', updated_at=now() "
                    "WHERE id=CAST(%s AS uuid)", (sid,))
        conn.commit()
        return {"spine_id": sid, "spine_kind": "pleading_coa", "corpus": corpus,
                "causes": len(causes), "items": len(items), "seeded_causes": seeded}
    except Exception as e:
        conn.rollback()
        logger.exception("build_pleading_coa_spine failed")
        return {"error": str(e)}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  spine builder: proposed Findings of Fact & Conclusions of Law               #
# --------------------------------------------------------------------------- #

def _docx_text(path):
    import docx
    d = docx.Document(path)
    return "\n".join(p.text for p in d.paragraphs)


_FOF_HDR = re.compile(r"^findings of fact$", re.I)
_COL_HDR = re.compile(r"^conclusions? of law$", re.I)
_FFCL_STOP = re.compile(r"^(respectfully submitted|certificate of service|signed (this|on)|"
                        r"so ordered|judge presiding|/s/)", re.I)


def _docx_para_tuples(path):
    """[(text, style_name)] for each paragraph (preserves Word auto-numbering context
    via the style name; the list number itself is not in the run text)."""
    import docx
    return [(re.sub(r"\s+", " ", p.text).strip(), (p.style.name if p.style else "") or "")
            for p in docx.Document(path).paragraphs]


def _split_ffcl(paras):
    """paras: [(text, style)] -> [(group_label, item_role, item_no, item_text)].

    Sections are driven by the 'Findings of Fact' / 'Conclusions of Law' heading lines;
    each substantive paragraph under a section is one item (Word auto-numbered, so no
    literal number in the text). Heading-styled lines become the sub-group label."""
    section = None          # 'finding' | 'conclusion'
    sub = None
    items, ino = [], 0
    for (text, style) in paras:
        if not text:
            continue
        low = text.lower()
        if _FOF_HDR.match(low):
            section, sub, ino = "finding", None, 0
            continue
        if _COL_HDR.match(low):
            section, sub, ino = "conclusion", None, 0
            continue
        if not section:
            continue
        if _FFCL_STOP.match(low):
            section = None
            continue
        is_heading = style.startswith("Heading")
        if is_heading and len(text) < 120:
            sub = text
            continue
        if len(text) < 40:          # stray short label / page furniture
            continue
        ino += 1
        label = sub or ("Findings of Fact" if section == "finding" else "Conclusions of Law")
        items.append((label, section, ino, text))
    return items


def build_ffcl_spine(tenant, matter_id, corpus, doc_path, appellate_case_id=None) -> dict:
    tenant = (tenant or "").strip()
    if not os.path.exists(doc_path):
        return {"error": "FF/CL doc not found: %s" % doc_path}
    if doc_path.lower().endswith(".docx"):
        paras = _docx_para_tuples(doc_path)
    else:
        paras = [(re.sub(r"\s+", " ", ln).strip(), "") for ln in _pdf_text(doc_path).splitlines()]
    parsed = _split_ffcl(paras)
    if not parsed:
        return {"error": "no findings/conclusions parsed from the document"}
    conn = _connect()
    try:
        cur = conn.cursor()
        embs = _embed_texts([p[3] for p in parsed])
        label = os.path.basename(doc_path)
        sid = _create_spine(cur, tenant, matter_id, "ffcl", corpus, label,
                            {"doc_path": doc_path}, appellate_case_id)
        # group_no: Findings group = 1, Conclusions group = 2
        items = []
        for (glabel, role, ino, itext), emb in zip(parsed, embs):
            items.append({"group_label": glabel, "group_no": 1 if role == "finding" else 2,
                          "item_no": ino, "item_role": role, "item_text": itext,
                          "embedding": emb, "source_kind": "ffcl_para", "source_ref_id": None,
                          "attributes": {}})
        _insert_items(cur, sid, items)
        cur.execute("UPDATE evidence_spines SET status='seeded', updated_at=now() "
                    "WHERE id=CAST(%s AS uuid)", (sid,))
        conn.commit()
        n_f = sum(1 for p in parsed if p[1] == "finding")
        return {"spine_id": sid, "spine_kind": "ffcl", "corpus": corpus,
                "items": len(parsed), "findings": n_f, "conclusions": len(parsed) - n_f}
    except Exception as e:
        conn.rollback()
        logger.exception("build_ffcl_spine failed")
        return {"error": str(e)}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  LLM-as-input: per proposition, draft a FOR query (local) and an AGAINST      #
#  counter-proposition (frontier opus-4-8, aggressive). Vector-as-output: the   #
#  two queries run as HYBRID (semantic + keyword) search; the hits ARE the      #
#  for/against columns. Cheap local model proposes; the frontier owns AGAINST,  #
#  the hard, valuable side (a weak counter yields a useless "against" column).  #
# --------------------------------------------------------------------------- #

SIDE_K = 6                  # hits kept per side (for / against)
LOCAL_PURPOSE = "extraction_escalation"      # -> ai_model_routing: local ollama (qwen)
FRONTIER_PURPOSE = "evidence_for_against"    # -> ai_model_routing: claude-opus-4-8
STD_PURPOSE = "evidence_for_against_std"      # -> ai_model_routing: claude-sonnet-4-6


def _tier_purposes(tier):
    """(for_purpose, against_purpose) for a model tier.

      cascade  -- dashboard widget default: cheap LOCAL drafts FOR, the FRONTIER
                  (opus-4-8) owns the AGAINST counter-proposition (the hard side).
      frontier -- same as cascade (viewer 'Frontier' switch).
      regular  -- viewer 'Regular' switch: standard model (sonnet-4-6) both sides.
    """
    if tier == "regular":
        return STD_PURPOSE, STD_PURPOSE
    return LOCAL_PURPOSE, FRONTIER_PURPOSE

_FOR_SYS = (
    "You are a litigation evidence analyst. Given a PROPOSITION a party must prove, output the "
    "single best natural-language search query to find record or e-discovery evidence that "
    "SUPPORTS it, plus 3-7 salient keywords or short phrases the authoring party would use. "
    "Respond with ONLY JSON: {\"query\": \"...\", \"keywords\": [\"...\"]}")

_AGAINST_SYS = (
    "You are an adversarial litigation analyst. Given a PROPOSITION a party must prove, write the "
    "single best natural-language search query to surface record or e-discovery evidence that "
    "CONTRADICTS, REBUTS, or materially UNDERCUTS it -- the counter-proposition the opponent "
    "would prove. Attack the SUBTLE element most likely to fail: negate intent, knowledge, "
    "falsity, causation, notice, authorization, or timing rather than the headline claim. Also "
    "give 3-7 salient keywords or short phrases an opponent's documents would contain. "
    "Respond with ONLY JSON: {\"query\": \"...\", \"keywords\": [\"...\"]}")


async def _gen_query(tenant, matter_id, proposition, sys_prompt, purpose):
    """One LLM call -> {query, keywords}. purpose routes the model (local vs frontier)."""
    from modules.intelligence import call, AICallContext
    try:
        from modules.intelligence import strip_markdown_fences
    except Exception:
        def strip_markdown_fences(s):
            return re.sub(r"^```\w*\s*|\s*```$", "", (s or "").strip())
    fallback = {"query": proposition, "keywords": []}
    ctx = AICallContext(tenant_id=tenant, module="intelligence", purpose=purpose,
                        matter_id=matter_id)
    try:
        res = await call(ctx, raw_user_prompt="PROPOSITION:\n" + (proposition or "")[:ITEM_CHARS],
                         raw_system_prompt=sys_prompt)
    except Exception as e:
        logger.warning("gen_query(%s) failed: %s", purpose, e)
        return fallback
    m = re.search(r"\{.*\}", strip_markdown_fences(res.text or ""), re.S)
    if not m:
        return fallback
    try:
        d = json.loads(m.group(0))
        return {"query": (d.get("query") or proposition), "keywords": d.get("keywords") or []}
    except Exception:
        return fallback


def _ts_terms(keywords):
    kw = [re.sub(r'["\\():&|!*]', " ", str(t)).strip() for t in (keywords or [])]
    kw = [t for t in kw if len(t) > 2][:8]
    return " or ".join('"%s"' % t for t in kw) if kw else ""


def _rrf(lists, k, C=60):
    """Reciprocal-rank fusion of ranked candidate lists -> top-k fused."""
    score, meta = {}, {}
    for lst in lists:
        for rank, (kind, rid, cite, txt, _sc) in enumerate(lst):
            score[rid] = score.get(rid, 0.0) + 1.0 / (C + rank + 1)
            meta.setdefault(rid, (kind, cite, txt))
    ranked = sorted(score, key=lambda r: -score[r])[:k]
    return [(meta[r][0], r, meta[r][1], meta[r][2], round(score[r], 5)) for r in ranked]


def _resolve_ctx(cur, corpus, matter_id, appellate_case_id):
    """Doc-set scope for a corpus: record -> CR docs + RR transcripts; ediscovery -> matter."""
    ctx = {"matter": matter_id, "cr": [], "rr": []}
    if corpus == "record" and appellate_case_id:
        cur.execute("SELECT document_id::text FROM record_documents "
                    "WHERE appellate_case_id=CAST(%s AS uuid) AND record_kind IN ('CR','SUPP_CR')",
                    (appellate_case_id,))
        ctx["cr"] = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT t.id::text FROM deposition_transcripts t "
                    "JOIN appellate_cases a ON a.trial_id=t.trial_id WHERE a.id=CAST(%s AS uuid)",
                    (appellate_case_id,))
        ctx["rr"] = [r[0] for r in cur.fetchall()]
    elif corpus == "record":
        # trial-court matter (no appellate record): scope RR to the matter's own depositions
        cur.execute("SELECT id::text FROM deposition_transcripts "
                    "WHERE matter_id=CAST(%s AS uuid) AND transcript_kind='deposition' "
                    "AND coalesce(deponent,'') NOT ILIKE 'DEMO%%'", (matter_id,))
        ctx["rr"] = [r[0] for r in cur.fetchall()]
    return ctx


def _vec_search(cur, corpus, ctx, qlit, k):
    out = []
    if corpus == "record":
        if ctx["cr"]:
            cur.execute(
                "SELECT ds.id::text, coalesce(ds.attributes->>'cite','CR'), ds.content, "
                "       1-(ds.embedding <=> %s::vector) FROM document_sections ds "
                "WHERE ds.section_type='cr_para' AND ds.logical_document_id = ANY(CAST(%s AS uuid[])) "
                "  AND ds.embedding IS NOT NULL ORDER BY ds.embedding <=> %s::vector LIMIT %s",
                (qlit, ctx["cr"], qlit, k))
            out += [("cr_section", r[0], r[1], r[2], float(r[3])) for r in cur.fetchall()]
        if ctx["rr"]:
            cur.execute(
                "SELECT q.id::text, '1 RR '||q.q_start_page||':'||q.q_start_line, "
                "       coalesce(q.question_text,'')||' '||coalesce(q.answer_text,''), "
                "       1-(em.embedding <=> %s::vector) FROM transcript_qa_units q "
                "JOIN transcript_qa_embeddings em ON em.qa_unit_id=q.id AND em.chunk_number=0 "
                "WHERE q.transcript_id = ANY(CAST(%s AS uuid[])) AND coalesce(q.is_colloquy,false)=false "
                "ORDER BY em.embedding <=> %s::vector LIMIT %s", (qlit, ctx["rr"], qlit, k))
            out += [("rr_qa", r[0], r[1], r[2], float(r[3])) for r in cur.fetchall()]
    else:   # ediscovery
        cur.execute(
            "SELECT ch.id::text, coalesce(NULLIF(d.bates_begin,''),NULLIF(d.bates_start,''),d.file_name), "
            "       ch.content, 1-(ce.embedding_768 <=> %s::vector) "
            "FROM ediscovery_chunk_embeddings ce JOIN ediscovery_chunks ch ON ch.id=ce.chunk_id "
            "JOIN ediscovery_documents d ON d.id=ch.document_id "
            "JOIN ediscovery_collections col ON col.id=d.collection_id "
            "WHERE col.matter_id=CAST(%s AS uuid) AND ce.embedding_768 IS NOT NULL "
            "ORDER BY ce.embedding_768 <=> %s::vector LIMIT %s", (qlit, ctx["matter"], qlit, k))
        out += [("edisc_chunk", r[0], r[1] or "e-discovery", r[2], float(r[3])) for r in cur.fetchall()]
    return out


def _fts_search(cur, corpus, ctx, terms, k):
    out = []
    if not terms:
        return out
    if corpus == "record":
        if ctx["cr"]:
            cur.execute(
                "SELECT ds.id::text, coalesce(ds.attributes->>'cite','CR'), ds.content, "
                "       ts_rank(to_tsvector('english',ds.content), qq) "
                "FROM document_sections ds, websearch_to_tsquery('english',%s) qq "
                "WHERE ds.section_type='cr_para' AND ds.logical_document_id = ANY(CAST(%s AS uuid[])) "
                "  AND to_tsvector('english',ds.content) @@ qq ORDER BY 4 DESC LIMIT %s",
                (terms, ctx["cr"], k))
            out += [("cr_section", r[0], r[1], r[2], float(r[3])) for r in cur.fetchall()]
        if ctx["rr"]:
            cur.execute(
                "SELECT q.id::text, '1 RR '||q.q_start_page||':'||q.q_start_line, "
                "       coalesce(q.question_text,'')||' '||coalesce(q.answer_text,''), "
                "       ts_rank(to_tsvector('english',coalesce(q.question_text,'')||' '||"
                "               coalesce(q.answer_text,'')), qq) "
                "FROM transcript_qa_units q, websearch_to_tsquery('english',%s) qq "
                "WHERE q.transcript_id = ANY(CAST(%s AS uuid[])) AND coalesce(q.is_colloquy,false)=false "
                "  AND to_tsvector('english',coalesce(q.question_text,'')||' '||coalesce(q.answer_text,'')) @@ qq "
                "ORDER BY 4 DESC LIMIT %s", (terms, ctx["rr"], k))
            out += [("rr_qa", r[0], r[1], r[2], float(r[3])) for r in cur.fetchall()]
    else:   # ediscovery (GIN fts index ix_edr_chunks_fts)
        cur.execute(
            "SELECT ch.id::text, coalesce(NULLIF(d.bates_begin,''),NULLIF(d.bates_start,''),d.file_name), "
            "       ch.content, ts_rank(to_tsvector('english',ch.content), qq) "
            "FROM ediscovery_chunks ch JOIN ediscovery_documents d ON d.id=ch.document_id "
            "JOIN ediscovery_collections col ON col.id=d.collection_id, "
            "     websearch_to_tsquery('english',%s) qq "
            "WHERE col.matter_id=CAST(%s AS uuid) AND to_tsvector('english',ch.content) @@ qq "
            "ORDER BY 4 DESC LIMIT %s", (terms, ctx["matter"], k))
        out += [("edisc_chunk", r[0], r[1] or "e-discovery", r[2], float(r[3])) for r in cur.fetchall()]
    return out


def _hybrid(cur, corpus, ctx, qtext, keywords, k):
    """Hybrid retrieval = semantic (pgvector) + keyword (FTS), fused by RRF."""
    qlit = _vec(_embed_texts([qtext or ""])[0])
    vec = _vec_search(cur, corpus, ctx, qlit, k * 3)
    lex = _fts_search(cur, corpus, ctx, _ts_terms(keywords), k * 3)
    return _rrf([vec, lex], k)


def analyze_proposition(cur, tenant, matter_id, corpus, ctx, proposition, k=SIDE_K, tier="cascade"):
    """LLM (FOR / AGAINST) -> queries; hybrid search -> for/against hits.
    `tier` selects the model (cascade=local+opus, regular=sonnet, frontier=local+opus).
    Returns (for_query, against_query, for_hits, against_hits)."""
    fp, ap = _tier_purposes(tier)
    forq = asyncio.run(_gen_query(tenant, matter_id, proposition, _FOR_SYS, fp))
    agq = asyncio.run(_gen_query(tenant, matter_id, proposition, _AGAINST_SYS, ap))
    for_hits = _hybrid(cur, corpus, ctx, forq["query"], forq["keywords"], k)
    ag_hits = _hybrid(cur, corpus, ctx, agq["query"], agq["keywords"], k)
    return forq, agq, for_hits, ag_hits


# --------------------------------------------------------------------------- #
#  ad-hoc entry points for the right-click modals (selected text)              #
# --------------------------------------------------------------------------- #

def find_similar(tenant_id, matter_id, corpus, text, k=12, appellate_case_id=None) -> dict:
    """Right-click modal #1: hybrid 'find similar' on the selected text (no polarity)."""
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        ctx = _resolve_ctx(cur, corpus, matter_id, appellate_case_id)
        hits = _hybrid(cur, corpus, ctx, text, [], k)
        return {"corpus": corpus, "query": text[:300],
                "hits": [{"evidence_kind": h[0], "evidence_ref_id": h[1], "cite": h[2],
                          "snippet": re.sub(r"\s+", " ", h[3] or "")[:400], "score": h[4]}
                         for h in hits]}
    finally:
        conn.close()


def analyze_text(tenant_id, matter_id, corpus, text, k=SIDE_K, appellate_case_id=None,
                 tier="frontier") -> dict:
    """Right-click modal #2 ('AI Analyze'): full for/against on the selected text.
    `tier` is the viewer's Regular(sonnet) vs Frontier(opus) switch."""
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        ctx = _resolve_ctx(cur, corpus, matter_id, appellate_case_id)
        forq, agq, fh, ah = analyze_proposition(cur, tenant, matter_id, corpus, ctx, text, k, tier)

        def pack(hits):
            return [{"evidence_kind": h[0], "evidence_ref_id": h[1], "cite": h[2],
                     "snippet": re.sub(r"\s+", " ", h[3] or "")[:400], "score": h[4]} for h in hits]
        return {"corpus": corpus, "proposition": text[:600],
                "for_query": forq["query"], "against_query": agq["query"],
                "for": pack(fh), "against": pack(ah)}
    finally:
        conn.close()


def _persist_item_links(cur, tenant, corpus, item_id, forq, agq, for_hits, ag_hits, run_id):
    """Replace one item's links: FOR hits (local query, tier 1) + AGAINST hits
    (frontier counter-proposition, tier 3). A hit appearing on both sides keeps the
    stronger-scoring side only."""
    from psycopg2.extras import execute_values
    best = {}   # evidence_ref_id -> (relation, tier, query, kind, cite, txt, score)
    for kind, rid, cite, txt, score in for_hits:
        best[rid] = ("supports", 1, forq["query"], kind, cite, txt, score)
    for kind, rid, cite, txt, score in ag_hits:
        if rid in best:        # also matched the FOR query -> on-proposition, keep as support
            continue
        best[rid] = ("undermines", 3, agq["query"], kind, cite, txt, score)
    cur.execute("DELETE FROM evidence_links WHERE spine_item_id=CAST(%s AS uuid)", (item_id,))
    rows = [(tenant, item_id, corpus, kind, rid, rel,
             re.sub(r"\s+", " ", txt or "")[:240], q, cite,
             re.sub(r"\s+", " ", txt or "")[:300], round(score, 5), tier, "proposed", run_id)
            for rid, (rel, tier, q, kind, cite, txt, score) in best.items()]
    if rows:
        execute_values(cur,
            "INSERT INTO evidence_links (tenant_id, spine_item_id, corpus, evidence_kind, "
            "  evidence_ref_id, relation, quote, rationale, cite, snippet, cosine, tier, status, "
            "  frontier_run_id) VALUES %s",
            rows,
            template="(%s,CAST(%s AS uuid),%s,%s,CAST(%s AS uuid),%s,%s,%s,%s,%s,%s,%s,%s,CAST(%s AS uuid))",
            page_size=200)
    from collections import Counter
    return Counter(v[0] for v in best.values())


def run_one_item(tenant_id, item_id, tier="cascade") -> dict:
    """Lazy per-proposition run (right column on click / review). One spine item."""
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT i.item_text, s.matter_id::text, s.appellate_case_id::text, s.corpus, "
                    "       s.id::text FROM evidence_spine_items i JOIN evidence_spines s ON s.id=i.spine_id "
                    "WHERE i.id=CAST(%s AS uuid)", (item_id,))
        row = cur.fetchone()
        if not row:
            return {"error": "item not found"}
        item_text, matter_id, appellate_case_id, corpus, spine_id = row
        ctx = _resolve_ctx(cur, corpus, matter_id, appellate_case_id)
        run_id = str(uuid.uuid4())
        forq, agq, fh, ah = analyze_proposition(cur, tenant, matter_id, corpus, ctx, item_text,
                                                SIDE_K, tier)
        outcome = _persist_item_links(cur, tenant, corpus, item_id, forq, agq, fh, ah, run_id)
        conn.commit()
        return {"item_id": item_id, "for": int(outcome.get("supports", 0)),
                "against": int(outcome.get("undermines", 0)),
                "for_query": forq["query"], "against_query": agq["query"]}
    except Exception as e:
        conn.rollback()
        logger.exception("run_one_item failed")
        return {"error": str(e)}
    finally:
        conn.close()


def run_for_against(tenant_id, spine_id, top_k=SIDE_K, limit_items=0, tier="cascade") -> dict:
    """Batch run over a whole spine: per proposition, LLM drafts FOR/AGAINST queries and
    hybrid search fills the columns. The frontier owns the AGAINST counter-proposition."""
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT matter_id::text, appellate_case_id::text, corpus FROM evidence_spines "
                    "WHERE id=CAST(%s AS uuid)", (spine_id,))
        meta = cur.fetchone()
        if not meta:
            return {"error": "spine not found"}
        matter_id, appellate_case_id, corpus = meta
        if corpus == "record" and not appellate_case_id:
            # trial-court matter: depositions are scoped by matter_id in _resolve_ctx
            pass
        ctx = _resolve_ctx(cur, corpus, matter_id, appellate_case_id)

        cur.execute("SELECT id::text, item_text FROM evidence_spine_items WHERE spine_id=CAST(%s AS uuid) "
                    "ORDER BY group_no, item_no" + (" LIMIT %d" % limit_items if limit_items else ""),
                    (spine_id,))
        items = cur.fetchall()
        if not items:
            return {"error": "spine has no items"}

        cur.execute("UPDATE evidence_spines SET status='running', updated_at=now() "
                    "WHERE id=CAST(%s AS uuid)", (spine_id,))
        conn.commit()

        run_id = str(uuid.uuid4())
        from collections import Counter
        outcome = Counter()
        for idx, (item_id, item_text) in enumerate(items, start=1):
            try:
                forq, agq, fh, ah = analyze_proposition(cur, tenant, matter_id, corpus, ctx,
                                                        item_text, top_k, tier)
                outcome += _persist_item_links(cur, tenant, corpus, item_id, forq, agq, fh, ah, run_id)
            except Exception as e:
                logger.warning("item %s failed: %s", item_id, e)
                outcome["failed"] += 1
            if idx % 5 == 0:
                conn.commit()
                logger.info("  analyzed %d/%d propositions", idx, len(items))

        cur.execute("UPDATE evidence_spines SET status='ready', last_run_id=CAST(%s AS uuid), "
                    "updated_at=now() WHERE id=CAST(%s AS uuid)", (run_id, spine_id))
        conn.commit()
        return {"spine_id": spine_id, "corpus": corpus, "items": len(items),
                "run_id": run_id, "by_relation": dict(outcome)}
    except Exception as e:
        conn.rollback()
        logger.exception("run_for_against failed")
        return {"error": str(e)}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  party alignment: who is suing on each claim, and who is being sued          #
# --------------------------------------------------------------------------- #

_PARTY_SYS = (
    "You are a litigation analyst. Given a case caption / pleading excerpt and a list of causes "
    "of action asserted in the matter, identify for EACH cause the party ASSERTING it (the party "
    "suing on that claim) and the party it is asserted AGAINST (the party being sued). Use the "
    "actual short party names from the caption. Counterclaims and crossclaims reverse the usual "
    "alignment, so read carefully. Respond with ONLY JSON: "
    "{\"parties\": [{\"cause\": \"<exact title>\", \"suing\": \"...\", \"sued\": \"...\"}]}")


async def _gen_parties(tenant, matter_id, caption, titles):
    from modules.intelligence import call, AICallContext
    try:
        from modules.intelligence import strip_markdown_fences
    except Exception:
        def strip_markdown_fences(s):
            return (s or "").strip()
    ctx = AICallContext(tenant_id=tenant, module="intelligence",
                        purpose="evidence_for_against", matter_id=matter_id)
    user = ("PLEADING EXCERPTS (one per pleading type — note who files each):\n" + (caption or "")[:9000] +
            "\n\nCAUSES OF ACTION asserted in this matter:\n" + "\n".join("- " + t for t in titles) +
            "\n\nFor each cause, who asserts it (suing) and against whom (sued)?")
    res = await call(ctx, raw_user_prompt=user, raw_system_prompt=_PARTY_SYS)
    m = re.search(r"\{.*\}", strip_markdown_fences(res.text or ""), re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(0)).get("parties", [])
    except Exception:
        return []


def _matter_caption(cur, matter_id):
    """One (longest) pleading per legal_category, captioned — so the model sees the petition
    AND the counterclaim/crossclaim alignments, not just whichever pleading is biggest."""
    cur.execute("SELECT DISTINCT ON (legal_category) legal_category, left(extracted_text, 1800) "
                "FROM documents WHERE matter_id=CAST(%s AS uuid) AND legal_category = ANY(%s) "
                "  AND length(coalesce(extracted_text,'')) > 200 "
                "ORDER BY legal_category, length(extracted_text) DESC",
                (matter_id, list(CLAIM_PLEADING_CATS)))
    return "\n\n".join("[%s]\n%s" % (cat, txt) for cat, txt in cur.fetchall())[:9000]


def _resolve_parties(cur, tenant, matter_id, causes):
    """causes: [(cause_id, title)] -> writes suing_party/sued_party onto each cause (opus)."""
    if not causes:
        return 0
    caption = _matter_caption(cur, matter_id)
    try:
        plist = asyncio.run(_gen_parties(tenant, matter_id, caption, [t for _, t in causes]))
    except Exception as e:
        logger.warning("party resolution failed: %s", e)
        return 0
    pm = {str(p.get("cause", "")).strip().lower(): p for p in plist}
    n = 0
    for (cid, title) in causes:
        p = pm.get(title.strip().lower())
        if not p:
            continue
        cur.execute("UPDATE causes_of_action SET attributes = attributes || CAST(%s AS jsonb), "
                    "updated_at=now() WHERE id=CAST(%s AS uuid)",
                    (json.dumps({"suing_party": p.get("suing"), "sued_party": p.get("sued")}), cid))
        n += 1
    return n


def resolve_parties(tenant_id, matter_id) -> dict:
    """Standalone: set suing/sued on the matter's for_against causes (no spine rebuild)."""
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id::text, title FROM causes_of_action WHERE matter_id=CAST(%s AS uuid) "
                    "AND attributes->>'source'='for_against_framelock' ORDER BY count_number", (matter_id,))
        causes = cur.fetchall()
        n = _resolve_parties(cur, tenant, matter_id, causes)
        conn.commit()
        return {"matter_id": matter_id, "causes": len(causes), "resolved": n}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  read entry points (for the API / dashboard widget)                          #
# --------------------------------------------------------------------------- #

def list_spines(tenant_id, matter_id) -> dict:
    """Spine instances available for a matter (which spine sources are built/run)."""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT s.id::text, s.spine_kind, s.corpus, s.label, s.status, s.updated_at, "
            "  (SELECT count(*) FROM evidence_spine_items i WHERE i.spine_id=s.id), "
            "  (SELECT count(*) FROM evidence_links l JOIN evidence_spine_items i ON i.id=l.spine_item_id "
            "     WHERE i.spine_id=s.id AND l.relation='supports'), "
            "  (SELECT count(*) FROM evidence_links l JOIN evidence_spine_items i ON i.id=l.spine_item_id "
            "     WHERE i.spine_id=s.id AND l.relation='undermines') "
            "FROM evidence_spines s WHERE s.matter_id=CAST(%s AS uuid) ORDER BY s.created_at",
            (matter_id,))
        return {"matter_id": matter_id, "spines": [
            {"spine_id": r[0], "spine_kind": r[1], "corpus": r[2], "label": r[3], "status": r[4],
             "updated_at": r[5].isoformat() if r[5] else None,
             "items": r[6], "for": r[7], "against": r[8]} for r in cur.fetchall()]}
    finally:
        conn.close()


def read_spine(tenant_id, spine_id) -> dict:
    """The two-pane matrix: groups -> items -> {for[], against[]}."""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id::text, spine_kind, corpus, label, status, matter_id::text, "
                    "  appellate_case_id::text FROM evidence_spines WHERE id=CAST(%s AS uuid)", (spine_id,))
        s = cur.fetchone()
        if not s:
            return {"error": "spine not found"}
        cur.execute("SELECT id::text, group_label, group_no, item_no, item_role, item_text "
                    "FROM evidence_spine_items WHERE spine_id=CAST(%s AS uuid) ORDER BY group_no, item_no",
                    (spine_id,))
        items = cur.fetchall()
        cur.execute("SELECT l.spine_item_id::text, l.relation, l.evidence_kind, l.evidence_ref_id::text, "
                    "  l.cite, l.quote, l.rationale, l.snippet, l.cosine, l.tier "
                    "FROM evidence_links l JOIN evidence_spine_items i ON i.id=l.spine_item_id "
                    "WHERE i.spine_id=CAST(%s AS uuid) ORDER BY l.relation, l.cosine DESC", (spine_id,))
        by_item = {}
        for (iid, rel, kind, ref, cite, quote, rat, snip, cos, tier) in cur.fetchall():
            d = by_item.setdefault(iid, {"for": [], "against": []})
            rec = {"evidence_kind": kind, "evidence_ref_id": ref, "cite": cite, "quote": quote,
                   "rationale": rat, "snippet": snip, "score": cos, "tier": tier}
            (d["for"] if rel == "supports" else d["against"]).append(rec)
        groups, gmap = [], {}
        for (iid, glabel, gno, ino, role, text) in items:
            key = (gno, glabel)
            g = gmap.get(key)
            if not g:
                g = {"group_label": glabel, "group_no": gno, "items": []}
                gmap[key] = g
                groups.append(g)
            li = by_item.get(iid, {"for": [], "against": []})
            g["items"].append({"item_id": iid, "item_no": ino, "item_role": role, "item_text": text,
                               "for": li["for"], "against": li["against"],
                               "for_count": len(li["for"]), "against_count": len(li["against"])})
        # party alignment per group (suing v. sued) from the cause attributes
        cur.execute("SELECT DISTINCT i.group_label, co.attributes->>'suing_party', "
                    "  co.attributes->>'sued_party' FROM evidence_spine_items i "
                    "JOIN coa_elements e ON e.id=i.source_ref_id "
                    "JOIN causes_of_action co ON co.id=e.cause_of_action_id "
                    "WHERE i.spine_id=CAST(%s AS uuid) AND i.source_kind='coa_element'", (spine_id,))
        pmap = {gl: (su, sd) for gl, su, sd in cur.fetchall()}
        for g in groups:
            su, sd = pmap.get(g["group_label"], (None, None))
            g["suing_party"] = su
            g["sued_party"] = sd
        return {"spine_id": spine_id, "spine_kind": s[1], "corpus": s[2], "label": s[3],
                "status": s[4], "matter_id": s[5], "appellate_case_id": s[6], "groups": groups}
    finally:
        conn.close()


def evidence_locator(tenant_id, kind, ref_id) -> dict:
    """Resolve an evidence link (kind, ref_id) to a pinpoint viewer target:
    {file_url, page, search, snippet}. record/transcript carry a page; e-discovery
    chunks have no stored page, so the viewer text-pinpoints on `search`."""
    conn = _connect()
    try:
        cur = conn.cursor()
        if kind == "edisc_chunk":
            cur.execute("SELECT ch.document_id::text, ch.content FROM ediscovery_chunks ch "
                        "WHERE ch.id = CAST(%s AS uuid)", (ref_id,))
            r = cur.fetchone()
            if r:
                return {"file_url": "/ediscovery/documents/%s/file" % r[0], "page": None,
                        "search": (r[1] or "")[:120], "snippet": (r[1] or "")[:400]}
        elif kind == "cr_section":
            cur.execute("SELECT logical_document_id::text, page_start, content "
                        "FROM document_sections WHERE id = CAST(%s AS uuid)", (ref_id,))
            r = cur.fetchone()
            if r:
                return {"file_url": ("/dms/document/%s/stream" % r[0]) if r[0] else None,
                        "page": r[1], "search": (r[2] or "")[:120], "snippet": (r[2] or "")[:400]}
        elif kind == "rr_qa":
            cur.execute("SELECT transcript_id::text, q_start_page, "
                        "coalesce(question_text,'') || ' ' || coalesce(answer_text,'') "
                        "FROM transcript_qa_units WHERE id = CAST(%s AS uuid)", (ref_id,))
            r = cur.fetchone()
            if r:
                return {"file_url": None, "page": r[1], "search": (r[2] or "")[:120],
                        "snippet": (r[2] or "")[:400]}
        return {}
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Evidence For & Against engine (frontier opus-4-8)")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", ""))
    ap.add_argument("--build", choices=["pleading_coa", "ffcl"])
    ap.add_argument("--corpus", choices=["record", "ediscovery"])
    ap.add_argument("--matter")
    ap.add_argument("--appeal", default=None)
    ap.add_argument("--doc", default=None, help="FF/CL or pleading file path")
    ap.add_argument("--spine", default=None)
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--top-k", type=int, default=SIDE_K)
    ap.add_argument("--limit-items", type=int, default=0)
    ap.add_argument("--tier", default="cascade", choices=["cascade", "regular", "frontier"])
    ap.add_argument("--rebuild", action="store_true", help="re-detect claims/elements")
    args = ap.parse_args()

    spine_id = args.spine
    if args.build == "pleading_coa":
        out = build_pleading_coa_spine(args.tenant, args.matter, args.corpus,
                                       source_ref={"doc_path": args.doc} if args.doc else None,
                                       appellate_case_id=args.appeal, rebuild=args.rebuild)
        logger.info("BUILD %s", json.dumps(out, default=str))
        spine_id = out.get("spine_id", spine_id)
    elif args.build == "ffcl":
        out = build_ffcl_spine(args.tenant, args.matter, args.corpus, args.doc,
                               appellate_case_id=args.appeal)
        logger.info("BUILD %s", json.dumps(out, default=str))
        spine_id = out.get("spine_id", spine_id)

    if args.run and spine_id:
        out = run_for_against(args.tenant, spine_id, top_k=args.top_k,
                              limit_items=args.limit_items, tier=args.tier)
        logger.info("RUN %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
