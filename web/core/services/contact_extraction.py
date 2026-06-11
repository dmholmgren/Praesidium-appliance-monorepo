"""
core/services/contact_extraction.py
=====================================
Shared contact extraction service — deterministic (Tier 1) and AI-assisted (Tier 2).

Consumers:
  - Matter People seed (scan DMS docs for contacts)
  - Email filing pipeline (extract sender/recipients)
  - AI reconciliation (match phone/calendar to matter via contacts)
  - eDiscovery (custodian identification, communication network)
  - Drafting auto-fill (party roles from contracts)
  - Conflict checking (adverse party database)

Tier 1: Deterministic regex extraction (emails, phones, name+email pairs, msg headers)
Tier 2: AI-assisted extraction (party roles, entities, relationships from contract text)

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations
import re
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Data Classes ──────────────────────────────────────────────────

@dataclass
class ExtractedContact:
    """A contact extracted from document text."""
    full_name: str = ""
    email: str = ""
    phone: str = ""
    company: str = ""
    role: str = "other"           # From contact_role_library codes
    category: str = "people"       # people | witness
    source_type: str = ""          # msg_header, email_signature, contract_party, loi_party, etc.
    confidence: float = 0.0        # 0.0 - 1.0
    raw_match: str = ""            # The original text that was matched
    address: str = ""
    bar_number: str = ""

    @property
    def key(self) -> str:
        """Dedup key — email if available, else normalized name (case-insensitive, whitespace-collapsed)."""
        if self.email:
            return self.email.lower().strip()
        import re as _re
        return _re.sub(r'\s+', ' ', self.full_name.lower().strip().rstrip('.,'))


@dataclass
class ExtractionResult:
    """Result of extracting contacts from a document."""
    contacts: list[ExtractedContact] = field(default_factory=list)
    source_file: str = ""
    source_type: str = ""          # msg, eml, pdf, docx, contract, loi
    matter_type: str = ""          # litigation, transactional
    raw_text_length: int = 0
    tier: str = "tier1"            # tier1 (deterministic) | tier2 (AI)
    errors: list[str] = field(default_factory=list)

    def deduped(self) -> list[ExtractedContact]:
        """Return contacts deduped by key, keeping highest confidence."""
        seen = {}
        for c in self.contacts:
            k = c.key
            if not k:
                continue
            if k not in seen or c.confidence > seen[k].confidence:
                seen[k] = c
        return list(seen.values())


# ─── Regex Patterns ────────────────────────────────────────────────

# Email address
EMAIL_RE = re.compile(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}')

# "Display Name" <email> or Display Name <email>
NAME_EMAIL_RE = re.compile(
    r'(?:"?([A-Z][a-zA-Z.\'-]+(?: [A-Z][a-zA-Z.\'-]+)+)"?\s*<([\w.+-]+@[\w.-]+\.[a-zA-Z]{2,})>)'
)

# From: / To: / Cc: header lines in .msg extracted text
MSG_HEADER_RE = re.compile(
    r'^(From|To|Cc|CC):\s*(.+?)$', re.MULTILINE
)

# Phone numbers — US format
PHONE_RE = re.compile(
    r'(?:(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4})'
)

# "Name | Title, Company" pattern common in email signatures
SIG_NAME_TITLE_RE = re.compile(
    r'^([A-Z][a-z]+ [A-Z][a-z]+)\s*\|\s*(.+?)(?:,\s*(.+))?$', re.MULTILINE
)

# Contract party pattern: "ENTITY NAME LLC, a State type (Role)"
CONTRACT_PARTY_RE = re.compile(
    r'([A-Z][A-Z\s&.,\'-]+(?:LLC|LP|Inc|Corp|Ltd|LLP|Company|Trust|Estate)\.?),?\s*'
    r'(?:a\s+)?([A-Za-z]+\s+(?:limited liability company|corporation|limited partnership'
    r'|general partnership|trust|company))\s*'
    r'\(\s*["\u201c]?(\w+)["\u201d]?\s*\)',
    re.IGNORECASE
)

# "Attn: Name" or "Attention: Name"
ATTN_RE = re.compile(
    r'(?:Attn|Attention|ATTN)\.?:\s*([A-Z][a-zA-Z.\'-]+(?: [A-Z][a-zA-Z.\'-]+)+)',
    re.IGNORECASE
)

# Address block: number street, city, state zip
ADDRESS_RE = re.compile(
    r'(\d+\s+[A-Za-z][\w\s]+(?:Lane|Drive|Street|Ave|Road|Blvd|Way|Court|Circle|Place|Pkwy|Dr|St|Rd|Ct|Ln|Pl)\.?'
    r'(?:\s*,\s*(?:Suite|Ste|Apt|Unit|#)\s*\w+)?)\s*\n\s*'
    r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*,\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)'
)

# Bar number
BAR_RE = re.compile(r'(?:Bar\s*(?:No\.?|Number|#)\s*:?\s*)(\d{5,8})', re.IGNORECASE)


# ─── Tier 1: Deterministic Extraction ──────────────────────────────

def extract_from_msg_headers(text: str) -> list[ExtractedContact]:
    """Extract contacts from .msg/.eml header lines (From/To/Cc)."""
    contacts = []
    for header_match in MSG_HEADER_RE.finditer(text[:2000]):  # Headers in first 2KB
        header_type = header_match.group(1)
        header_val = header_match.group(2)

        # Find name+email pairs in the header value
        for ne_match in NAME_EMAIL_RE.finditer(header_val):
            name = ne_match.group(1).strip().strip('"')
            email = ne_match.group(2).strip().lower()
            contacts.append(ExtractedContact(
                full_name=name, email=email,
                source_type="msg_header",
                confidence=0.95,
                raw_match=ne_match.group(0),
            ))

        # Also catch bare emails without names
        found_emails = {c.email for c in contacts}
        for em in EMAIL_RE.finditer(header_val):
            addr = em.group(0).lower()
            if addr not in found_emails:
                local = addr.split("@")[0].replace(".", " ").replace("_", " ").title()
                contacts.append(ExtractedContact(
                    full_name=local, email=addr,
                    source_type="msg_header",
                    confidence=0.7,
                    raw_match=em.group(0),
                ))
    return contacts


def extract_from_email_signature(text: str) -> list[ExtractedContact]:
    """Extract contact details from email signature blocks."""
    contacts = []
    # Look for signature patterns in last 1500 chars
    sig_text = text[-1500:] if len(text) > 1500 else text

    for sig_match in SIG_NAME_TITLE_RE.finditer(sig_text):
        name = sig_match.group(1).strip()
        title = sig_match.group(2).strip() if sig_match.group(2) else ""
        company = sig_match.group(3).strip() if sig_match.group(3) else ""

        # Find associated email and phone nearby
        context = sig_text[max(0, sig_match.start()-50):sig_match.end()+300]
        emails = EMAIL_RE.findall(context)
        phones = PHONE_RE.findall(context)

        contacts.append(ExtractedContact(
            full_name=name,
            email=emails[0].lower() if emails else "",
            phone=phones[0] if phones else "",
            company=company or title,
            source_type="email_signature",
            confidence=0.85,
            raw_match=sig_match.group(0),
        ))
    return contacts


def extract_contract_parties(text: str) -> list[ExtractedContact]:
    """Extract named parties from contract/LOI/PSA text."""
    contacts = []

    # Entity + role: "TA COLMENA CORNER LLC... ("Seller")"
    for pm in CONTRACT_PARTY_RE.finditer(text[:5000]):
        entity = pm.group(1).strip().rstrip(',')
        role_label = pm.group(3).strip().lower()

        # Map common contract role labels to our codes
        role_map = {
            'seller': 'seller', 'buyer': 'buyer', 'purchaser': 'buyer',
            'lender': 'lender', 'borrower': 'borrower',
            'landlord': 'landlord', 'tenant': 'tenant_party',
            'licensor': 'other', 'licensee': 'other',
            'escrow': 'escrow_agent', 'agent': 'broker',
        }
        role_code = role_map.get(role_label, 'other')

        contacts.append(ExtractedContact(
            full_name=entity,
            company=entity,
            role=role_code,
            source_type="contract_party",
            confidence=0.95,
            raw_match=pm.group(0)[:120],
        ))

    # Attn: Name patterns
    for attn_m in ATTN_RE.finditer(text[:5000]):
        name = attn_m.group(1).strip()
        # Try to find email/phone nearby
        context = text[max(0, attn_m.start()-20):attn_m.end()+500]
        emails = EMAIL_RE.findall(context)
        phones = PHONE_RE.findall(context)
        address_m = ADDRESS_RE.search(context)

        contacts.append(ExtractedContact(
            full_name=name,
            email=emails[0].lower() if emails else "",
            phone=phones[0] if phones else "",
            address=f"{address_m.group(1)}, {address_m.group(2)}, {address_m.group(3)} {address_m.group(4)}" if address_m else "",
            role="client_contact",  # Attn: is usually the client/party contact
            source_type="contract_attn",
            confidence=0.85,
            raw_match=attn_m.group(0),
        ))

    return contacts


def extract_all_emails(text: str) -> list[ExtractedContact]:
    """Extract all email addresses from text as low-confidence contacts."""
    contacts = []
    seen = set()
    for em in EMAIL_RE.finditer(text):
        addr = em.group(0).lower()
        if addr in seen:
            continue
        seen.add(addr)
        local = addr.split("@")[0].replace(".", " ").replace("_", " ").title()
        domain = addr.split("@")[1] if "@" in addr else ""
        contacts.append(ExtractedContact(
            full_name=local,
            email=addr,
            company=domain,
            source_type="email_in_text",
            confidence=0.5,
            raw_match=em.group(0),
        ))
    return contacts


# ─── Main Extraction Entry Points ─────────────────────────────────

def extract_tier1(text: str, filename: str = "", matter_type: str = "") -> ExtractionResult:
    """
    Tier 1 deterministic extraction — regex-based, no AI.

    Args:
        text: Document text content
        filename: Original filename (used to detect .msg, .pdf, etc.)
        matter_type: 'litigation' or 'transactional' (affects role assignment)

    Returns:
        ExtractionResult with deduped contacts
    """
    result = ExtractionResult(
        source_file=filename,
        source_type=_classify_doc(filename),
        matter_type=matter_type,
        raw_text_length=len(text),
        tier="tier1",
    )

    if not text or len(text.strip()) < 10:
        return result

    try:
        # 1. MSG/EML headers (highest confidence)
        if result.source_type in ("msg", "eml"):
            result.contacts.extend(extract_from_msg_headers(text))
            result.contacts.extend(extract_from_email_signature(text))

        # 2. Contract/LOI party extraction
        if result.source_type in ("contract", "pdf", "docx") or _looks_like_contract(text):
            result.contacts.extend(extract_contract_parties(text))

        # 3. All emails as fallback (lower confidence)
        result.contacts.extend(extract_all_emails(text))

    except Exception as exc:
        logger.warning("Tier 1 extraction error for %s: %s", filename, exc)
        result.errors.append(str(exc))

    return result


async def extract_tier2(text: str, filename: str = "", matter_type: str = "",
                        matter_name: str = "", tenant_id: str = "") -> ExtractionResult:
    """
    Tier 2 AI-assisted extraction — uses Claude to identify parties, roles, relationships.

    Feeds document text to Claude with a structured prompt asking for:
    - All named parties and their roles
    - Contact information (email, phone, address)
    - Entity type (individual, company, government)
    - Relationship to the matter

    Returns ExtractionResult with AI-identified contacts.
    """
    result = ExtractionResult(
        source_file=filename,
        source_type=_classify_doc(filename),
        matter_type=matter_type,
        raw_text_length=len(text),
        tier="tier2",
    )

    if not text or len(text.strip()) < 50:
        return result

    try:
        # Import here to avoid circular dependency
        from core.services.ai_client import get_ai_client

        # Truncate to avoid token limits — first 8K chars has the parties
        excerpt = text[:20000]

        prompt = f"""You are extracting contact information from a {matter_type} matter called "{matter_name}".

INSTRUCTIONS:
1. Find ALL named people and organizations in the document.
2. For each, extract as much contact detail as possible:
   - Look in NOTICE sections for mailing addresses (these are usually near the end of the contract)
   - Look in signature blocks for phone, fax, email, bar numbers
   - Look in party definitions for entity names and roles
   - Look for "Attn:" lines to identify contact persons at entities
   - Look for addresses after party names in the opening paragraphs
3. For attorneys, look for "Bar No." or "State Bar" references
4. Distinguish between the entity (e.g. "S2 Land Development, LLC") and the individual contact person

Return ONLY a valid JSON array. No markdown, no commentary. Each object:
{{"full_name": "person or entity name", "email": "or empty", "phone": "with area code or empty", "company": "employer/entity or empty", "firm_name": "law firm if attorney or empty", "role": "one of: client_contact, opposing_counsel, co_counsel, judge, title_company, escrow_agent, lender, borrower, buyer, seller, broker, surveyor, inspector, appraiser, accountant, financial_advisor, insurer, vendor, other", "category": "people", "address": "full mailing address or empty", "city": "or empty", "state": "2-letter or empty", "bar_number": "or empty", "contact_type": "individual or entity", "confidence_note": "brief explanation"}}

IMPORTANT: Do NOT list the same person/entity twice.

Document ({filename}):
{excerpt}"""

        client = await get_ai_client(tenant_id)
        if not client:
            result.errors.append("No AI client available")
            return result

        import json
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )

        resp_text = response.content[0].text if response.content else ""
        # Strip markdown fences if present
        resp_text = resp_text.strip()
        if resp_text.startswith("```"):
            resp_text = resp_text.split("\n", 1)[1] if "\n" in resp_text else resp_text
            if resp_text.endswith("```"):
                resp_text = resp_text[:-3]
            resp_text = resp_text.strip()

        parsed = json.loads(resp_text)
        for item in parsed:
            result.contacts.append(ExtractedContact(
                full_name=item.get("full_name", ""),
                email=item.get("email", ""),
                phone=item.get("phone", ""),
                company=item.get("company", ""),
                role=item.get("role", "other"),
                category=item.get("category", "people"),
                address=item.get("address", ""),
                source_type="ai_extraction",
                confidence=0.80,
                raw_match=item.get("confidence_note", ""),
            ))

    except Exception as exc:
        logger.warning("Tier 2 extraction error for %s: %s", filename, exc)
        result.errors.append(str(exc))

    return result


# ─── Helpers ───────────────────────────────────────────────────────

def _classify_doc(filename: str) -> str:
    """Classify document type from filename."""
    fn = (filename or "").lower()
    if fn.endswith(".msg"):
        return "msg"
    if fn.endswith(".eml"):
        return "eml"
    if fn.endswith(".pdf"):
        return "pdf"
    if fn.endswith((".docx", ".doc")):
        return "docx"
    return "unknown"


def _looks_like_contract(text: str) -> bool:
    """Heuristic — does this text look like a contract/LOI/PSA?"""
    indicators = [
        "purchase and sale agreement", "letter of intent",
        "hereby agrees", "witnesseth", "effective date",
        "seller", "buyer", "purchaser", "lender", "borrower",
        "in consideration of", "terms and conditions",
        "escrow", "closing date", "earnest money",
    ]
    lower = text[:5000].lower()
    matches = sum(1 for ind in indicators if ind in lower)
    return matches >= 3


def filter_internal_emails(contacts: list[ExtractedContact],
                           internal_domains: list[str] = None) -> list[ExtractedContact]:
    """Remove contacts with internal firm email domains."""
    if internal_domains is None:
        internal_domains = ["hjmmlegal.com"]
    return [
        c for c in contacts
        if not c.email or not any(c.email.endswith(f"@{d}") for d in internal_domains)
    ]


def merge_results(*results: ExtractionResult) -> list[ExtractedContact]:
    """Merge multiple extraction results, dedup by key, keep highest confidence."""
    seen = {}
    for r in results:
        for c in r.contacts:
            k = c.key
            if not k:
                continue
            if k not in seen or c.confidence > seen[k].confidence:
                # Merge fields — keep the richer record
                if k in seen:
                    existing = seen[k]
                    if not c.phone and existing.phone:
                        c.phone = existing.phone
                    if not c.company and existing.company:
                        c.company = existing.company
                    if not c.address and existing.address:
                        c.address = existing.address
                    if c.role == "other" and existing.role != "other":
                        c.role = existing.role
                seen[k] = c
    return list(seen.values())
