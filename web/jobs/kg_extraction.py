"""
jobs/kg_extraction.py

M5i-C — Knowledge Graph Extraction Engine (S2-003)

RQ job that processes a single document or all documents for a matter:
  1. Entity extraction — persons, orgs, contracts, events, facts (all doc types)
  2. Relationship mapping — pairs of entities with typed relationships
  3. Admission detection — deposition transcripts only
  4. Contradiction detection — deposition transcripts, cross-referenced with prior KG

Queue: ediscovery
Entry points:
  jobs.kg_extraction.run_document(doc_id, matter_id, tenant_id)
  jobs.kg_extraction.run_matter(matter_id, tenant_id)
Timeout: 600 seconds

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Document type routing                                               #
# ------------------------------------------------------------------ #
DEPOSITION_TYPES = {"deposition_transcript", "deposition", "transcript"}

VALID_ENTITY_TYPES = {
    "person", "organization", "contract",
    "event", "fact", "admission", "contradiction",
}

VALID_RELATIONSHIP_TYPES = {
    "employed_by", "contradicts", "supports", "admitted_in",
    "party_to", "signed", "testified_about", "affiliated_with",
    "authored", "received", "referenced_in", "expert_for",
    "adverse_to", "counsel_for",
}


# ================================================================== #
# DB helpers                                                          #
# ================================================================== #

def _get_conn():
    raw = os.environ.get("DATABASE_URL", "")
    raw = raw.replace("postgresql+asyncpg://", "postgresql://")
    at = raw.rfind("@")
    creds = raw[len("postgresql://"):at]
    rest = raw[at + 1:]
    user, password = creds.split(":", 1)
    host_port, dbname = rest.rsplit("/", 1)
    host = host_port.split(":")[0]
    return psycopg2.connect(
        host=host, port=5432,
        dbname=dbname, user=user, password=password,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def _get_ai_client():
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=api_key)


# ================================================================== #
# Entity extraction (S2-003 §162)                                    #
# ================================================================== #

ENTITY_EXTRACTION_PROMPT = """You are a legal entity extraction engine for Praesidium.
Extract all named entities from the legal document below.

Return ONLY a JSON object with this exact structure — no preamble, no markdown:
{
  "entities": [
    {
      "entity_type": "person|organization|contract|event|fact",
      "canonical_name": "string — standardized full name",
      "confidence": 0.0-1.0,
      "properties": {
        "role": "string — e.g. plaintiff, defendant, witness, expert, attorney",
        "organization": "string or null",
        "dates": ["string"],
        "notes": "string or null"
      }
    }
  ],
  "relationships": [
    {
      "entity_a": "canonical name of first entity",
      "entity_b": "canonical name of second entity",
      "relationship_type": "employed_by|party_to|signed|testified_about|affiliated_with|authored|received|referenced_in|expert_for|adverse_to|counsel_for|supports",
      "confidence": 0.0-1.0
    }
  ]
}

Rules:
- Only extract entities explicitly named in the document
- canonical_name must be consistent (use full legal names)
- Only use relationship_type values from the allowed list
- Confidence reflects how certain you are the entity/relationship is correctly identified
- Do not hallucinate entities not present in the text
- Return empty arrays if no entities/relationships found"""


def _extract_entities_and_relationships(
    doc_id: str,
    doc_type: str,
    file_name: str,
    custodian: str | None,
    doc_summary: str,
) -> tuple[list[dict], list[dict]]:
    """
    Call AIService to extract entities and relationships from a document.
    Returns (entities, relationships).
    """
    user_content = (
        f"Document Type: {doc_type}\n"
        f"File: {file_name}\n"
        f"Custodian: {custodian or 'unknown'}\n\n"
        f"Document content summary:\n{doc_summary[:4000]}"
    )

    try:
        client = _get_ai_client()
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            system=ENTITY_EXTRACTION_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        entities = result.get("entities", [])
        relationships = result.get("relationships", [])
        return entities, relationships
    except Exception as exc:
        log.error("kg_extraction: entity extraction failed for doc %s: %s", doc_id, exc)
        return [], []


# ================================================================== #
# Admission detection (S2-003 §166)                                  #
# Deposition transcripts only                                        #
# ================================================================== #

ADMISSION_DETECTION_PROMPT = """You are a litigation intelligence engine analyzing a deposition transcript.
Identify admissions — statements by the witness that:
  (a) tend to support a claim or defense asserted by the ADVERSE party, or
  (b) tend to undermine a claim or defense asserted by the witness's OWN party.

Return ONLY a JSON array — no preamble, no markdown:
[
  {
    "canonical_name": "Deponent full name",
    "admission_text": "verbatim or near-verbatim statement",
    "adverse_to": "which party this admission hurts",
    "element_affected": "which claim or defense element this affects",
    "confidence": 0.0-1.0,
    "properties": {
      "page": "page number if available",
      "line": "line number if available",
      "context": "brief surrounding context"
    }
  }
]

Return [] if no admissions are identified. Only surface genuine admissions — do not hallucinate."""


def _detect_admissions(
    doc_id: str,
    file_name: str,
    custodian: str | None,
    doc_summary: str,
) -> list[dict]:
    """
    Detect admissions in a deposition transcript.
    Returns list of admission entity dicts.
    """
    user_content = (
        f"Deponent: {custodian or 'unknown'}\n"
        f"Transcript: {file_name}\n\n"
        f"Transcript content:\n{doc_summary[:4000]}"
    )

    try:
        client = _get_ai_client()
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            system=ADMISSION_DETECTION_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        admissions = json.loads(raw)
        if not isinstance(admissions, list):
            return []
        return admissions
    except Exception as exc:
        log.error("kg_extraction: admission detection failed for doc %s: %s", doc_id, exc)
        return []


# ================================================================== #
# Contradiction detection (S2-003 §168)                              #
# Cross-references new content against prior KG statements           #
# ================================================================== #

CONTRADICTION_PROMPT = """You are a litigation intelligence engine.
Below are prior statements attributed to a witness from the case knowledge graph,
followed by new content from a document or transcript.

Identify contradictions — statements in the new content that are inconsistent with
prior statements attributed to the same person.

Return ONLY a JSON array — no preamble, no markdown:
[
  {
    "person_name": "full name of the person",
    "prior_statement": "the earlier statement that is contradicted",
    "new_statement": "the new contradicting statement",
    "contradiction_description": "plain English explanation of the inconsistency",
    "confidence": 0.0-1.0
  }
]

Return [] if no contradictions are found. Only surface genuine contradictions."""


def _detect_contradictions(
    doc_id: str,
    matter_id: str,
    tenant_id: str,
    doc_summary: str,
    conn,
) -> list[dict]:
    """
    Load prior statements from KG and detect contradictions with new content.
    Returns list of contradiction dicts.
    """
    # Load prior person entities and their facts from the KG
    with conn.cursor() as cur:
        cur.execute("""
            SELECT canonical_name, properties
            FROM kg_entities
            WHERE matter_id = %s AND tenant_id = %s
              AND entity_type IN ('person', 'fact', 'admission')
            ORDER BY created_at ASC
            LIMIT 50
        """, (matter_id, tenant_id))
        prior_entities = cur.fetchall() or []

    if not prior_entities:
        return []

    prior_summary = "\n".join([
        f"- {e['canonical_name']}: {json.dumps(e['properties'])}"
        for e in prior_entities
    ])

    user_content = (
        f"Prior statements from case knowledge graph:\n{prior_summary}\n\n"
        f"New document content:\n{doc_summary[:3000]}"
    )

    try:
        client = _get_ai_client()
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=CONTRADICTION_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        contradictions = json.loads(raw)
        if not isinstance(contradictions, list):
            return []
        return contradictions
    except Exception as exc:
        log.error("kg_extraction: contradiction detection failed for doc %s: %s", doc_id, exc)
        return []


# ================================================================== #
# Entity upsert helpers                                               #
# ================================================================== #

def _upsert_entity(
    conn,
    matter_id: str,
    tenant_id: str,
    entity_type: str,
    canonical_name: str,
    properties: dict,
    source_doc_id: str,
    confidence: float,
    attribution: str = "ai",
) -> str:
    """
    Insert entity if canonical_name not already in KG for this matter.
    Returns entity_id (existing or new).
    """
    if entity_type not in VALID_ENTITY_TYPES:
        entity_type = "fact"

    canonical_name = canonical_name.strip()[:512]
    if not canonical_name:
        return ""

    with conn.cursor() as cur:
        # Check for existing entity by canonical name
        cur.execute("""
            SELECT id FROM kg_entities
            WHERE matter_id = %s AND tenant_id = %s
              AND canonical_name = %s AND entity_type = %s
        """, (matter_id, tenant_id, canonical_name, entity_type))
        existing = cur.fetchone()

        if existing:
            return str(existing["id"])

        entity_id = str(uuid.uuid4())
        cur.execute("""
            INSERT INTO kg_entities
                (id, matter_id, tenant_id, entity_type, canonical_name,
                 properties, source_doc_id, confidence, attribution)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            entity_id, matter_id, tenant_id,
            entity_type, canonical_name,
            json.dumps(properties),
            source_doc_id,
            min(max(float(confidence), 0.0), 1.0),
            attribution,
        ))
        return entity_id


def _insert_relationship(
    conn,
    matter_id: str,
    tenant_id: str,
    entity_a_id: str,
    entity_b_id: str,
    relationship_type: str,
    source_doc_id: str,
    confidence: float,
) -> None:
    """Insert a relationship if the pair doesn't already exist."""
    if not entity_a_id or not entity_b_id:
        return
    if entity_a_id == entity_b_id:
        return
    if relationship_type not in VALID_RELATIONSHIP_TYPES:
        relationship_type = "referenced_in"

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id FROM kg_relationships
            WHERE matter_id = %s AND tenant_id = %s
              AND entity_a_id = %s AND entity_b_id = %s
              AND relationship_type = %s
        """, (matter_id, tenant_id, entity_a_id, entity_b_id, relationship_type))
        if cur.fetchone():
            return

        cur.execute("""
            INSERT INTO kg_relationships
                (id, matter_id, tenant_id, entity_a_id, entity_b_id,
                 relationship_type, source_doc_id, confidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            str(uuid.uuid4()), matter_id, tenant_id,
            entity_a_id, entity_b_id,
            relationship_type, source_doc_id,
            min(max(float(confidence), 0.0), 1.0),
        ))


# ================================================================== #
# Core document processor                                             #
# ================================================================== #

def _process_document(doc: dict, matter_id: str, tenant_id: str, conn) -> dict:
    """
    Process a single document through the full KG extraction pipeline.
    Returns summary counts.
    """
    doc_id      = str(doc["id"])
    doc_type    = (doc.get("doc_type") or "unknown").lower()
    file_name   = doc.get("file_name") or "untitled"
    custodian   = doc.get("custodian")
    is_depo     = doc_type in DEPOSITION_TYPES

    # Build a text summary for the AI (we have no extracted text column)
    doc_summary = (
        f"File: {file_name}\n"
        f"Type: {doc_type}\n"
        f"Custodian: {custodian or 'unknown'}\n"
        f"Date: {doc.get('doc_date') or 'unknown'}\n"
        f"Email subject: {doc.get('email_subject') or ''}\n"
        f"Email from: {doc.get('email_from') or ''}\n"
    )

    entity_count       = 0
    relationship_count = 0
    admission_count    = 0
    contradiction_count = 0

    # ---- Entity extraction + relationship mapping (all doc types) ----
    raw_entities, raw_relationships = _extract_entities_and_relationships(
        doc_id, doc_type, file_name, custodian, doc_summary
    )

    # Map canonical_name → entity_id for relationship resolution
    name_to_id: dict[str, str] = {}

    for ent in raw_entities:
        etype  = ent.get("entity_type", "fact")
        name   = ent.get("canonical_name", "")
        conf   = float(ent.get("confidence", 0.8))
        props  = ent.get("properties", {})
        if not name:
            continue
        eid = _upsert_entity(
            conn, matter_id, tenant_id,
            etype, name, props, doc_id, conf, "ai",
        )
        if eid:
            name_to_id[name] = eid
            entity_count += 1

    conn.commit()

    for rel in raw_relationships:
        a_name = rel.get("entity_a", "")
        b_name = rel.get("entity_b", "")
        rtype  = rel.get("relationship_type", "referenced_in")
        conf   = float(rel.get("confidence", 0.7))
        a_id   = name_to_id.get(a_name)
        b_id   = name_to_id.get(b_name)
        if a_id and b_id:
            _insert_relationship(
                conn, matter_id, tenant_id,
                a_id, b_id, rtype, doc_id, conf,
            )
            relationship_count += 1

    conn.commit()

    # ---- Admission detection (deposition transcripts only) ----
    if is_depo:
        admissions = _detect_admissions(doc_id, file_name, custodian, doc_summary)
        for adm in admissions:
            name  = adm.get("canonical_name", custodian or "Unknown Deponent")
            text  = adm.get("admission_text", "")
            props = {
                "admission_text": text,
                "adverse_to": adm.get("adverse_to", ""),
                "element_affected": adm.get("element_affected", ""),
                **(adm.get("properties") or {}),
            }
            # Store admission as kg_entity of type 'admission'
            adm_id = _upsert_entity(
                conn, matter_id, tenant_id,
                "admission",
                f"ADMISSION: {name} — {text[:80]}",
                props, doc_id,
                float(adm.get("confidence", 0.8)),
                "ai",
            )
            # Link admission → deponent via 'admitted_in'
            deponent_id = name_to_id.get(name)
            if adm_id and deponent_id:
                _insert_relationship(
                    conn, matter_id, tenant_id,
                    adm_id, deponent_id, "admitted_in", doc_id, 0.9,
                )
            if adm_id:
                admission_count += 1

        conn.commit()

        # ---- Contradiction detection ----
        contradictions = _detect_contradictions(
            doc_id, matter_id, tenant_id, doc_summary, conn
        )
        for con in contradictions:
            person  = con.get("person_name", "Unknown")
            desc    = con.get("contradiction_description", "")
            prior   = con.get("prior_statement", "")
            new_s   = con.get("new_statement", "")
            props   = {
                "prior_statement": prior,
                "new_statement": new_s,
                "description": desc,
            }
            con_id = _upsert_entity(
                conn, matter_id, tenant_id,
                "contradiction",
                f"CONTRADICTION: {person} — {desc[:80]}",
                props, doc_id,
                float(con.get("confidence", 0.75)),
                "ai",
            )
            # Link contradiction → person via 'contradicts'
            person_id = name_to_id.get(person)
            if con_id and person_id:
                _insert_relationship(
                    conn, matter_id, tenant_id,
                    con_id, person_id, "contradicts", doc_id, 0.85,
                )
            if con_id:
                contradiction_count += 1

        conn.commit()

    return {
        "doc_id": doc_id,
        "entities": entity_count,
        "relationships": relationship_count,
        "admissions": admission_count,
        "contradictions": contradiction_count,
    }


# ================================================================== #
# Entry points                                                        #
# ================================================================== #

def run_document(
    doc_id: str,
    matter_id: str,
    tenant_id: str,
) -> dict:
    """
    Process a single document through KG extraction.
    Called when a new document is ingested.
    """
    tenant_id = tenant_id.strip()
    log.info("kg_extraction.run_document: doc=%s matter=%s", doc_id, matter_id)

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT d.id, d.file_name, d.doc_type, d.custodian,
                       d.doc_date, d.email_subject, d.email_from
                FROM ediscovery_documents d
                JOIN ediscovery_collections c ON d.collection_id = c.id
                WHERE d.id = %s AND d.tenant_id = %s
            """, (doc_id, tenant_id))
            doc = cur.fetchone()

        if not doc:
            log.warning("kg_extraction.run_document: doc %s not found", doc_id)
            return {"status": "not_found", "doc_id": doc_id}

        result = _process_document(dict(doc), matter_id, tenant_id, conn)
        result["status"] = "complete"
        return result

    except Exception as exc:
        log.exception("kg_extraction.run_document: fatal error doc=%s: %s", doc_id, exc)
        conn.rollback()
        raise
    finally:
        conn.close()


def run_matter(
    matter_id: str,
    tenant_id: str,
) -> dict:
    """
    Process ALL documents for a matter through KG extraction.
    Called on-demand or after issue map refresh.
    """
    tenant_id = tenant_id.strip()
    log.info("kg_extraction.run_matter: matter=%s", matter_id)

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT d.id, d.file_name, d.doc_type, d.custodian,
                       d.doc_date, d.email_subject, d.email_from
                FROM ediscovery_documents d
                JOIN ediscovery_collections c ON d.collection_id = c.id
                WHERE c.matter_id = %s AND d.tenant_id = %s
                ORDER BY d.created_at ASC
                LIMIT 100
            """, (matter_id, tenant_id))
            docs = cur.fetchall() or []

        log.info("kg_extraction.run_matter: processing %d documents", len(docs))

        totals = {
            "entities": 0,
            "relationships": 0,
            "admissions": 0,
            "contradictions": 0,
            "docs_processed": 0,
            "docs_failed": 0,
        }

        for doc in docs:
            try:
                result = _process_document(dict(doc), matter_id, tenant_id, conn)
                totals["entities"]       += result["entities"]
                totals["relationships"]  += result["relationships"]
                totals["admissions"]     += result["admissions"]
                totals["contradictions"] += result["contradictions"]
                totals["docs_processed"] += 1
            except Exception as exc:
                log.error(
                    "kg_extraction.run_matter: doc %s failed: %s",
                    doc["id"], exc
                )
                conn.rollback()
                totals["docs_failed"] += 1

        totals["status"] = "complete"
        return totals

    except Exception as exc:
        log.exception("kg_extraction.run_matter: fatal error matter=%s: %s", matter_id, exc)
        conn.rollback()
        raise
    finally:
        conn.close()
