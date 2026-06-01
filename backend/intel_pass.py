"""
PatchBrain intelligence pass — deterministic only.
Called by worker.py after chunks are inserted and file_version is 'ready'.

Responsibilities:
1. Extract entity_mentions rows from all chunks (deterministic, no LLM).
2. Classify the file into category + document_type.
3. Build short_summary, key_entities, suggested_questions, risk_flags.
4. Upsert one file_intelligence row.

Design constraints:
- Uses the sync Supabase client (same as worker).
- Non-fatal: every exception is caught and logged; worker must not fail because of this.
- No LLM calls unless ENABLE_INTELLIGENCE_LLM=true (currently never set).
- Reuses _parse_chunk_rows from calc_tool so row parsing stays in one place.
"""

from __future__ import annotations

import re
import sys
import traceback
import json
from collections import defaultdict
from datetime import date
from typing import Any

# ── Field-to-entity-type mapping ──────────────────────────────────────────────
# Keys are normalised field names (lowercase, underscores).
# Values are entity_type strings stored in entity_mentions.entity_type.

FIELD_ENTITY_MAP: dict[str, str] = {
    "truck_id":        "truck",
    "truck_number":    "truck",
    "truck":           "truck",
    "unit":            "truck",
    "driver":          "driver",
    "driver_name":     "driver",
    "client":          "client",
    "customer":        "client",
    "vendor":          "client",
    "operator":        "client",
    "company":         "client",
    "trucking_company": "client",
    "lease_location":  "lease",
    "lease_origin":    "lease",
    "lease":           "lease",
    "well":            "lease",
    "site":            "lease",
    "location":        "lease",
    "ticket_id":       "ticket_id",
    "ticket_number":   "ticket_id",
    "invoice_id":      "invoice_id",
    "invoice_number":  "invoice_id",
    "invoice_no":      "invoice_id",
    "po_number":       "po_number",
    "po":              "po_number",
    "job_id":          "job_id",
    "job_number":      "job_id",
    "date":            "date",
    "service_date":    "date",
    "invoice_date":    "date",
    "ticket_date":     "date",
    "load_date":       "date",
    "run_date":        "date",
    "delivery_date":   "date",
    "job_date":        "date",
}

# Fields we intentionally skip — too noisy or purely numeric
SKIP_FIELDS = frozenset({
    "total_amount", "amount", "bbls_hauled", "bbls_received", "total_bbls",
    "rate_per_bbl", "rate", "quantity", "qty", "volume", "gallons", "hours",
    "miles", "price", "cost", "total", "count", "balance", "subtotal",
    "tax", "discount", "fee", "charge",
})

# ── Regex patterns for PDF/TXT/WhatsApp extraction ────────────────────────────
_RE_TRUCK   = re.compile(r"\bT-\d{2,4}\b")
_RE_TICKET  = re.compile(r"\bFT-\d{4,8}\b", re.I)
_RE_INVOICE = re.compile(r"\bINV[-\s]?\d{4,10}\b", re.I)
_RE_PO      = re.compile(r"\b(?:RO|PO)[-\s]?\d{2,4}[-\s]\d{2,6}\b", re.I)
_RE_DATE    = re.compile(
    r"\b(?:\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})\b"
)
# Lease/well pattern: word(s) followed by digits+letter suffix, e.g. "Reeves 14H", "Cimarron Unit 7", "Acme 7B"
_RE_LEASE   = re.compile(
    r"\b([A-Z][a-zA-Z]+(?:\s+[A-Za-z]+)*\s+\d+[A-Za-z]?)\b"
)

# ── Classification hints ──────────────────────────────────────────────────────
CATEGORY_RULES: list[tuple[str, str, list[str]]] = [
    # (category, document_type, keyword_hints)
    ("field_data",    "run_tickets",        ["ticket_id", "truck_id", "bbls_hauled", "lease_location", "rate_per_bbl"]),
    ("financials",    "invoices",           ["invoice_id", "invoice_number", "total_amount", "due_date", "vendor", "bill_to"]),
    ("safety",        "safety_meeting",     ["safety", "h2s", "incident", "ppe", "hazard", "meeting", "cert", "expired", "msds", "sds"]),
    ("maintenance",   "maintenance_log",    ["maintenance", "repair", "pump", "inspection", "oil change", "filter", "truck issue", "downtime"]),
    ("compliance",    "compliance_report",  ["rrc", "compliance", "permit", "audit", "violation", "regulatory", "railroad commission"]),
    ("communications","email_thread",       ["from:", "to:", "subject:", "re:", "fw:", "forwarded", "sent from"]),
    ("communications","whatsapp_export",    ["whatsapp", "omitted", "end-to-end"]),
]

SUGGESTED_QUESTIONS: dict[str, list[str]] = {
    "run_tickets": [
        "Which lease had the most revenue?",
        "Which truck hauled the most barrels?",
        "Show revenue by client.",
        "Compare Reeves 14H and Cimarron Unit 7 by barrels hauled.",
        "Show revenue by month.",
    ],
    "invoices": [
        "What is the total invoiced amount?",
        "Which vendor had the highest invoice total?",
        "Show invoices by month.",
        "Which invoices are unpaid?",
    ],
    "safety_meeting": [
        "What safety topics were covered?",
        "Were any incidents reported?",
        "Which employees attended safety meetings?",
    ],
    "maintenance_log": [
        "Which trucks had maintenance issues?",
        "What was the most common repair type?",
        "Show maintenance events by date.",
    ],
    "compliance_report": [
        "Were any violations noted?",
        "What permits are referenced?",
        "What is the compliance status?",
    ],
    "email_thread": [
        "Who are the main participants in this thread?",
        "What was the primary topic discussed?",
        "Were any action items mentioned?",
    ],
    "whatsapp_export": [
        "Who are the participants in this chat?",
        "What was the main topic discussed?",
        "Were any job details or instructions shared?",
    ],
    "unknown": [
        "What is this document about?",
        "What key entities are mentioned?",
        "What date range does this file cover?",
    ],
}

RISK_KEYWORDS = {
    "expired_cert":  ["expired", "expiration", "past due", "overdue cert"],
    "spill_incident": ["spill", "leak", "blowout", "incident", "emergency"],
    "missing_po":    ["missing po", "no valid po", "no purchase order", "po missing"],
}


def _normalize_key(key: str) -> str:
    key = (key or "").strip().lower()
    key = re.sub(r"[^a-z0-9]+", "_", key)
    return re.sub(r"_+", "_", key).strip("_")


def _canonical(value: str) -> str:
    """Lowercase stripped canonical form for deduplication."""
    return " ".join((value or "").strip().lower().split())


def _parse_chunk_rows_local(content: str, chunk_id: str) -> list[dict]:
    """
    Inline row parser — same logic as calc_tool._parse_chunk_rows but
    standalone so intelligence.py has no import dependency on calc_tool.
    Returns list of {field, value, source_row, chunk_id}.
    """
    _ROW_RE = re.compile(r"^Row\s+(\d+):\s*", re.IGNORECASE)
    records = []
    for line in (content or "").splitlines():
        line = line.strip()
        m = _ROW_RE.match(line)
        if not m:
            continue
        source_row = int(m.group(1))
        line = _ROW_RE.sub("", line)
        for part in [p.strip() for p in line.split("|")]:
            if "=" not in part:
                continue
            k, _, v = part.partition("=")
            k = _normalize_key(k)
            v = (v or "").strip()
            if k and v and k not in SKIP_FIELDS:
                records.append({
                    "field": k,
                    "value": v,
                    "source_row": source_row,
                    "chunk_id": chunk_id,
                })
    return records


# ── Main extraction functions ─────────────────────────────────────────────────

def extract_entities_from_table_chunks(chunks: list[dict]) -> list[dict]:
    """
    Deterministic entity extraction from CSV/sheet chunks.
    Returns list of entity dicts ready for bulk insert into entity_mentions.
    """
    entities = []
    TABLE_SOURCE_TYPES = ("csv_rows", "sheet_rows")

    for chunk in chunks:
        if chunk.get("source_type") not in TABLE_SOURCE_TYPES:
            continue

        content  = chunk.get("content") or ""
        chunk_id = str(chunk.get("chunk_id") or chunk.get("id") or "")

        for field_rec in _parse_chunk_rows_local(content, chunk_id):
            field = field_rec["field"]
            entity_type = FIELD_ENTITY_MAP.get(field)
            if not entity_type:
                continue

            value = str(field_rec["value"]).strip()
            if not value or value.lower() in ("none", "null", "n/a", "", "unknown"):
                continue

            entities.append({
                "entity_type":       entity_type,
                "value":             value,
                "canonical_value":   _canonical(value),
                "source_field":      field,
                "source_row":        field_rec["source_row"],
                "source_text":       None,
                "chunk_id":          chunk_id or None,
                "confidence_score":  1.0,
                "extraction_method": "deterministic",
            })

    return entities


def extract_entities_from_text_chunks(chunks: list[dict]) -> list[dict]:
    """
    Regex entity extraction from PDF/TXT/WhatsApp/docx chunks.
    Keeps it simple: trucks, tickets, invoices, POs, dates, leases.
    """
    entities = []
    TEXT_SOURCE_TYPES = ("pdf_text", "pdf_table", "txt_chunk", "docx_section")

    for chunk in chunks:
        if chunk.get("source_type") not in TEXT_SOURCE_TYPES:
            continue

        content  = chunk.get("content") or ""
        chunk_id = str(chunk.get("chunk_id") or chunk.get("id") or "")

        def _add(entity_type: str, matches: list[str]):
            for m in set(matches):
                m = m.strip()
                if m:
                    entities.append({
                        "entity_type":       entity_type,
                        "value":             m,
                        "canonical_value":   _canonical(m),
                        "source_field":      None,
                        "source_row":        None,
                        "source_text":       (content[:200] if content else None),
                        "chunk_id":          chunk_id or None,
                        "confidence_score":  0.85,
                        "extraction_method": "regex",
                    })

        _add("truck",      _RE_TRUCK.findall(content))
        _add("ticket_id",  _RE_TICKET.findall(content))
        _add("invoice_id", _RE_INVOICE.findall(content))
        _add("po_number",  _RE_PO.findall(content))
        _add("date",       _RE_DATE.findall(content))

        # Lease matching: only keep values that look like oilfield lease names
        # (contain a digit, at least one uppercase word, not all-numeric)
        lease_hits = []
        for m in _RE_LEASE.findall(content):
            m = m.strip()
            if re.search(r'\d', m) and re.search(r'[A-Z]', m):
                lease_hits.append(m)
        _add("lease", lease_hits)

    return entities


def _collect_all_text(chunks: list[dict]) -> str:
    """Concatenate content from all chunks for classification/risk scanning."""
    return "\n".join((c.get("content") or "") for c in chunks).lower()


def classify_file(
    chunks: list[dict],
    original_filename: str,
    entities: list[dict],
) -> tuple[str, str]:
    """
    Returns (category, document_type).
    Uses source_type of chunks + keyword hints + filename.
    """
    fn_lower = (original_filename or "").lower()
    all_text = _collect_all_text(chunks)

    # Source type hints — strongest signal for structured files
    source_types = {c.get("source_type") for c in chunks}
    has_table_source = bool(source_types & {"csv_rows", "sheet_rows"})
    has_pdf = bool(source_types & {"pdf_text", "pdf_table"})

    # Filename hints
    if "whatsapp" in fn_lower:
        return ("communications", "whatsapp_export")
    if any(x in fn_lower for x in ("email", "client_email", "_emails")):
        return ("communications", "email_thread")

    # Check entity fields present for table files
    if has_table_source:
        entity_fields = {e["source_field"] for e in entities if e.get("source_field")}
        if entity_fields & {"ticket_id", "ticket_number"}:
            return ("field_data", "run_tickets")
        if entity_fields & {"invoice_id", "invoice_number", "invoice_no"}:
            return ("financials", "invoices")

    # Keyword scan (ordered by specificity)
    for category, document_type, keywords in CATEGORY_RULES:
        if any(kw in all_text for kw in keywords):
            return (category, document_type)

    # Generic text file fallback
    if has_pdf or "pdf" in fn_lower:
        return ("unknown", "unknown")
    if has_table_source:
        return ("field_data", "unknown")

    return ("unknown", "unknown")


def build_date_range(entities: list[dict]) -> tuple[date | None, date | None]:
    """Parse date entities and return (min_date, max_date)."""
    from datetime import datetime

    DATE_FMTS = ["%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%m-%d-%y"]
    parsed: list[date] = []

    for e in entities:
        if e["entity_type"] != "date":
            continue
        val = e["value"].strip()
        for fmt in DATE_FMTS:
            try:
                parsed.append(datetime.strptime(val, fmt).date())
                break
            except ValueError:
                continue

    if not parsed:
        return (None, None)
    return (min(parsed), max(parsed))


def build_key_entities(entities: list[dict]) -> dict:
    """
    Group top unique canonical values per entity type.
    Returns jsonb-ready dict like:
    {"trucks": ["T-03"], "drivers": ["Carlos Mendez"], ...}
    """
    grouped: dict[str, set[str]] = defaultdict(set)
    for e in entities:
        grouped[e["entity_type"]].add(e["canonical_value"])

    label_map = {
        "truck":      "trucks",
        "driver":     "drivers",
        "client":     "clients",
        "lease":      "leases",
        "ticket_id":  "ticket_ids",
        "invoice_id": "invoice_ids",
        "po_number":  "po_numbers",
        "job_id":     "job_ids",
    }
    out: dict[str, list[str]] = {}
    for etype, label in label_map.items():
        vals = sorted(grouped.get(etype, set()))
        if vals:
            out[label] = vals[:50]  # cap per type
    return out


def build_short_summary(
    document_type: str,
    chunks: list[dict],
    entities: list[dict],
    date_start: date | None,
    date_end: date | None,
    original_filename: str,
) -> str:
    """
    40–80 word deterministic summary. No LLM.
    """
    n_chunks = len(chunks)

    # Count source rows for table files
    TABLE_SOURCE_TYPES = ("csv_rows", "sheet_rows")
    table_chunks = [c for c in chunks if c.get("source_type") in TABLE_SOURCE_TYPES]

    row_count = 0
    if table_chunks:
        import re as _re
        ROW_RE = _re.compile(r"^Row\s+\d+:", _re.IGNORECASE | _re.MULTILINE)
        for c in table_chunks:
            row_count += len(ROW_RE.findall(c.get("content") or ""))

    # Date range string
    date_str = ""
    if date_start and date_end and date_start == date_end:
        date_str = f"{date_start.strftime('%B %-d, %Y')}"
    elif date_start and date_end:
        if date_start.year == date_end.year and date_start.month == date_end.month:
            date_str = f"{date_start.strftime('%B %Y')}"
        else:
            date_str = f"{date_start.strftime('%b %Y')} – {date_end.strftime('%b %Y')}"
    elif date_start:
        date_str = f"{date_start.strftime('%B %Y')}"

    # Top entity values
    key = build_key_entities(entities)
    leases  = key.get("leases", [])[:5]
    trucks  = key.get("trucks", [])[:5]
    clients = key.get("clients", [])[:3]

    if document_type == "run_tickets":
        parts = []
        if date_str:
            parts.append(f"{date_str} run-ticket file")
        else:
            parts.append("Run-ticket file")
        if row_count:
            parts.append(f"containing {row_count} hauling records")
        if trucks:
            parts.append(f"with trucks {', '.join(trucks)}")
        if leases:
            parts.append(f"covering leases {', '.join(leases)}")
        if clients:
            parts.append(f"for clients {', '.join(clients)}")
        return ". ".join(parts) + "." if parts else f"Run-ticket file ({n_chunks} chunks)."

    if document_type == "invoices":
        invoice_ids = key.get("invoice_ids", [])[:3]
        parts = []
        if date_str:
            parts.append(f"{date_str} invoice file")
        else:
            parts.append("Invoice file")
        if row_count:
            parts.append(f"with {row_count} line items")
        if invoice_ids:
            parts.append(f"invoices {', '.join(invoice_ids)}")
        if clients:
            parts.append(f"clients {', '.join(clients)}")
        return ". ".join(parts) + "." if parts else f"Invoice file ({n_chunks} chunks)."

    if document_type == "safety_meeting":
        return f"Safety-related document{' — ' + date_str if date_str else ''} ({n_chunks} chunk{'s' if n_chunks != 1 else ''})."

    if document_type == "maintenance_log":
        truck_str = f" Trucks mentioned: {', '.join(trucks)}." if trucks else ""
        return f"Maintenance log{' — ' + date_str if date_str else ''} ({n_chunks} chunk{'s' if n_chunks != 1 else ''}).{truck_str}"

    if document_type == "compliance_report":
        return f"Compliance or regulatory document{' — ' + date_str if date_str else ''} ({n_chunks} chunk{'s' if n_chunks != 1 else ''})."

    if document_type == "email_thread":
        return f"Email thread{' — ' + date_str if date_str else ''} ({n_chunks} chunk{'s' if n_chunks != 1 else ''})."

    if document_type == "whatsapp_export":
        return f"WhatsApp export{' — ' + date_str if date_str else ''} ({n_chunks} chunk{'s' if n_chunks != 1 else ''})."

    # Generic fallback
    entity_hint = ""
    if leases:
        entity_hint = f" Mentions {', '.join(leases[:3])}."
    elif trucks:
        entity_hint = f" Mentions trucks {', '.join(trucks[:3])}."
    return f"Document with {n_chunks} chunk{'s' if n_chunks != 1 else ''}{' — ' + date_str if date_str else ''}.{entity_hint}"


def build_risk_flags(
    chunks: list[dict],
    category: str,
    version_status: str,
) -> list[str]:
    flags: list[str] = []
    all_text = _collect_all_text(chunks)

    for flag, keywords in RISK_KEYWORDS.items():
        if any(kw in all_text for kw in keywords):
            flags.append(flag)

    if version_status == "failed":
        flags.append("failed_parser")

    if category == "unknown":
        flags.append("unknown_category")

    return flags


def _deduplicate_entities(entities: list[dict]) -> list[dict]:
    """
    Remove exact duplicates per (entity_type, canonical_value, source_row, chunk_id).
    Keeps first occurrence.
    """
    seen: set[tuple] = set()
    out = []
    for e in entities:
        key = (
            e["entity_type"],
            e["canonical_value"],
            e.get("source_row"),
            e.get("chunk_id"),
        )
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


# ── Main entry point ──────────────────────────────────────────────────────────

def run_intelligence_pass(
    sb,
    version_id: str,
    file_id: str,
    company_id: str,
    chunks: list[dict],
    original_filename: str = "",
    version_status: str = "ready",
) -> None:
    """
    Called by worker.py after chunks are inserted and version is 'ready'.

    sb              — sync Supabase client (service role)
    version_id      — file_versions.id
    file_id         — files.id
    company_id      — companies.id
    chunks          — the full list of chunk dicts from process_job (pre-DB format)
                      Each chunk has: content, source_type, row_start, row_end, etc.
                      We also accept post-DB chunks that carry chunk_id.
    original_filename — from file_versions.original_filename
    version_status  — file_versions.status at call time (should be 'ready')

    All exceptions are caught — this pass must never fail the worker job.
    """
    try:
        print(f"  [intelligence] starting pass for version {version_id}")

        if not chunks:
            _upsert_intelligence(sb, version_id, file_id, company_id, {
                "category": "unknown",
                "document_type": "unknown",
                "short_summary": "File produced no parseable chunks.",
                "needs_review": True,
                "risk_flags": ["unknown_category"],
                "key_entities": {},
                "suggested_questions": SUGGESTED_QUESTIONS["unknown"],
                "confidence_score": 0.0,
            })
            return

        # --- Entity extraction ---
        table_entities = extract_entities_from_table_chunks(chunks)
        text_entities  = extract_entities_from_text_chunks(chunks)
        all_entities   = _deduplicate_entities(table_entities + text_entities)

        # --- Classification ---
        category, document_type = classify_file(chunks, original_filename, all_entities)

        # --- Date range ---
        date_start, date_end = build_date_range(all_entities)

        # --- Key entities summary ---
        key_entities = build_key_entities(all_entities)

        # --- Short summary ---
        short_summary = build_short_summary(
            document_type, chunks, all_entities, date_start, date_end, original_filename
        )

        # --- Suggested questions ---
        suggested_questions = SUGGESTED_QUESTIONS.get(document_type, SUGGESTED_QUESTIONS["unknown"])

        # --- Risk flags ---
        risk_flags = build_risk_flags(chunks, category, version_status)

        # --- needs_review ---
        needs_review = bool(
            risk_flags
            or category == "unknown"
            or not all_entities
        )

        confidence_score = 1.0 if category != "unknown" else 0.5

        # --- Upsert file_intelligence ---
        intel_payload: dict[str, Any] = {
            "category":           category,
            "document_type":      document_type,
            "short_summary":      short_summary,
            "detailed_summary":   None,
            "detected_date_start": date_start.isoformat() if date_start else None,
            "detected_date_end":   date_end.isoformat()   if date_end   else None,
            "key_entities":        key_entities,
            "suggested_questions": suggested_questions,
            "risk_flags":          risk_flags,
            "confidence_score":    confidence_score,
            "needs_review":        needs_review,
        }

        _upsert_intelligence(sb, version_id, file_id, company_id, intel_payload)
        print(f"  [intelligence] upserted file_intelligence: category={category}, document_type={document_type}")

        # --- Bulk insert entity_mentions ---
        if all_entities:
            _insert_entity_mentions(sb, version_id, file_id, company_id, all_entities)
            print(f"  [intelligence] inserted {len(all_entities)} entity_mentions")
        else:
            print("  [intelligence] no entities extracted")

        print(f"  [intelligence] pass complete for version {version_id}")

    except Exception:
        # Non-fatal — log and return. Worker job must not be affected.
        print(f"  [intelligence] ERROR in pass for version {version_id} (non-fatal):")
        traceback.print_exc(file=sys.stdout)


def _upsert_intelligence(
    sb,
    version_id: str,
    file_id: str,
    company_id: str,
    payload: dict,
) -> None:
    """
    Insert or update file_intelligence row.
    Uses upsert on unique(file_version_id).
    """
    row = {
        "company_id":      company_id,
        "file_id":         file_id,
        "file_version_id": version_id,
        "updated_at":      "now()",
        **payload,
    }

    # Supabase Python client upsert — on_conflict targets the unique constraint column
    sb.table("file_intelligence").upsert(
        row,
        on_conflict="file_version_id",
    ).execute()


def _insert_entity_mentions(
    sb,
    version_id: str,
    file_id: str,
    company_id: str,
    entities: list[dict],
) -> None:
    """
    Delete existing entity_mentions for this version and bulk-insert fresh ones.
    Delete-then-insert is simpler than upsert for a variable-length list.
    """
    # Delete old records for this version first
    sb.table("entity_mentions").delete().eq("file_version_id", version_id).execute()

    rows = []
    for e in entities:
        rows.append({
            "company_id":        company_id,
            "file_id":           file_id,
            "file_version_id":   version_id,
            "chunk_id":          e.get("chunk_id") or None,
            "entity_type":       e["entity_type"],
            "value":             e["value"],
            "canonical_value":   e["canonical_value"],
            "source_field":      e.get("source_field"),
            "source_row":        e.get("source_row"),
            "source_text":       e.get("source_text"),
            "confidence_score":  e.get("confidence_score", 1.0),
            "extraction_method": e.get("extraction_method", "deterministic"),
        })

    # Batch insert in groups of 200 to stay well under Supabase request size limits
    for i in range(0, len(rows), 200):
        sb.table("entity_mentions").insert(rows[i: i + 200]).execute()
