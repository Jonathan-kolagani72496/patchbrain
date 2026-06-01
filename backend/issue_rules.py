"""
PatchBrain issue detection rules — Phase 2.

Runs deterministic, LLM-free rules over file_chunks and entity_mentions to
produce operational_issues and issue_evidence rows.

Design constraints:
- Uses the sync Supabase client (supabase.Client) — same pattern as worker.py.
- No async/await. No asyncpg. Called from a sync worker context.
- No LLM / Anthropic / Claude calls. All logic is pure Python + regex.
- Non-fatal: exceptions are caught at each rule boundary and logged.
"""

from __future__ import annotations

import hashlib
import re
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from supabase import Client


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class EvidenceItem:
    file_id: str | None
    file_version_id: str | None
    chunk_id: str | None
    source_filename: str
    source_label: str
    evidence_text: str
    evidence_json: dict


@dataclass
class IssueCandidate:
    rule_key: str
    rule_version: int
    issue_type: str
    compliance_framework: str
    severity: str
    title: str
    description: str
    related_entities: dict
    related_files: list
    raw_data: dict
    confidence_score: float
    suppression_hash: str
    evidence: list  # list of EvidenceItem


# ---------------------------------------------------------------------------
# Suppression + insertion helpers
# ---------------------------------------------------------------------------

def _suppression_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _insert_candidate(sb: Client, company_id: str, candidate: IssueCandidate) -> bool:
    """Insert candidate into operational_issues + issue_evidence.

    Returns True if inserted, False if suppressed (duplicate active issue).
    """
    try:
        existing = (
            sb.table("operational_issues")
            .select("id, status")
            .eq("company_id", company_id)
            .eq("rule_key", candidate.rule_key)
            .eq("suppression_hash", candidate.suppression_hash)
            .not_.in_("status", ["dismissed", "resolved", "ignored"])
            .execute()
            .data
        )
    except Exception:
        # If suppression check fails, be conservative and skip insertion
        print(
            f"[issue_rules] suppression check failed for {candidate.rule_key}:\n"
            + traceback.format_exc()
        )
        return False

    if existing:
        return False  # suppressed

    issue_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    issue_row = {
        "id": issue_id,
        "company_id": company_id,
        "issue_type": candidate.issue_type,
        "compliance_framework": candidate.compliance_framework,
        "status": "candidate",
        "severity": candidate.severity,
        "title": candidate.title,
        "description": candidate.description,
        "related_entities": candidate.related_entities,
        "related_files": candidate.related_files,
        "raw_data": candidate.raw_data,
        "confidence_score": candidate.confidence_score,
        "suppression_hash": candidate.suppression_hash,
        "rule_key": candidate.rule_key,
        "rule_version": candidate.rule_version,
        "created_at": now,
        "updated_at": now,
    }

    try:
        sb.table("operational_issues").insert(issue_row).execute()
    except Exception:
        print(
            f"[issue_rules] failed to insert operational_issue {issue_id}:\n"
            + traceback.format_exc()
        )
        return False

    for ev in candidate.evidence:
        ev_row: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "company_id": company_id,
            "issue_id": issue_id,
            "file_id": ev.file_id,
            "file_version_id": ev.file_version_id,
            "chunk_id": ev.chunk_id,
            "source_filename": ev.source_filename,
            "source_label": ev.source_label,
            "evidence_text": ev.evidence_text[:2000],
            "evidence_json": ev.evidence_json,
            "created_at": now,
        }
        try:
            sb.table("issue_evidence").insert(ev_row).execute()
        except Exception:
            print(
                f"[issue_rules] failed to insert evidence for issue {issue_id}:\n"
                + traceback.format_exc()
            )

    return True


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})")
_NAME_RE = re.compile(
    r"([A-Z][a-z]+\s+[A-Z][a-z]+).*?(?:cert|certification|H2S).*?expir"
    r"|expir.*?([A-Z][a-z]+\s+[A-Z][a-z]+)",
    re.IGNORECASE,
)


def _get_filename_for_fv(sb: Client, company_id: str, file_version_id: str) -> str:
    """Return original_filename for a file_version_id, or the id itself on failure."""
    try:
        rows = (
            sb.table("file_versions")
            .select("original_filename")
            .eq("company_id", company_id)
            .eq("id", file_version_id)
            .limit(1)
            .execute()
            .data
        )
        if rows:
            return rows[0].get("original_filename") or file_version_id
    except Exception:
        pass
    return file_version_id


def _fv_filename_map(sb: Client, company_id: str, fv_ids: list[str]) -> dict[str, str]:
    """Bulk fetch filenames for a list of file_version_ids."""
    if not fv_ids:
        return {}
    try:
        rows = (
            sb.table("file_versions")
            .select("id, original_filename")
            .eq("company_id", company_id)
            .in_("id", fv_ids)
            .execute()
            .data
        )
        return {r["id"]: (r.get("original_filename") or r["id"]) for r in rows}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Rule 1: expired_hazmat_or_safety_cert_driver_used
# ---------------------------------------------------------------------------

def _rule_expired_cert(
    sb: Client, company_id: str, file_version_id: str | None
) -> tuple[list[IssueCandidate], int, list[str]]:
    """Detect expired hazmat / safety certifications for active drivers."""
    RULE_KEY = "expired_hazmat_or_safety_cert_driver_used"
    RULE_VERSION = 1
    candidates: list[IssueCandidate] = []
    errors: list[str] = []

    try:
        # Build query for expiration-related chunks
        q = (
            sb.table("file_chunks")
            .select(
                "id, company_id, file_id, file_version_id, content, chunk_index, metadata_jsonb"
            )
            .eq("company_id", company_id)
        )
        if file_version_id:
            q = q.eq("file_version_id", file_version_id)

        # Use OR filters via ilike chain — Supabase py client supports .or_()
        try:
            chunks_data = (
                q.or_(
                    "content.ilike.%expired%,"
                    "content.ilike.%cert expired%,"
                    "content.ilike.%certification expired%"
                )
                .limit(200)
                .execute()
                .data
            )
        except Exception:
            # Fallback: fetch with single ilike
            chunks_data = (
                q.ilike("content", "%expired%")
                .limit(200)
                .execute()
                .data
            )
    except Exception:
        errors.append(f"[{RULE_KEY}] chunk fetch failed: " + traceback.format_exc())
        return candidates, 0, errors

    if not chunks_data:
        return candidates, 0, errors

    # Pre-fetch filenames
    fv_ids = list({c.get("file_version_id") for c in chunks_data if c.get("file_version_id")})
    fv_map = _fv_filename_map(sb, company_id, fv_ids)

    for chunk in chunks_data:
        content: str = chunk.get("content") or ""
        chunk_id = chunk.get("id")
        chunk_fv_id = chunk.get("file_version_id")
        chunk_file_id = chunk.get("file_id")
        source_filename = fv_map.get(chunk_fv_id or "", chunk_fv_id or "unknown")

        # Extract driver name
        driver_name: str | None = None
        m = _NAME_RE.search(content)
        if m:
            driver_name = m.group(1) or m.group(2)

        if not driver_name:
            # Fallback: look for any capitalized two-word name near "expired"
            vicinity = content
            fb = re.search(r"([A-Z][a-z]+\s+[A-Z][a-z]+)", vicinity)
            if fb:
                driver_name = fb.group(1)

        if not driver_name:
            continue

        driver_name_lower = driver_name.lower().strip()

        # Extract date if present
        date_match = _DATE_RE.search(content)
        expiry_date = date_match.group(1) if date_match else None

        # Check whether this driver appears in other files (entity_mentions)
        try:
            em_rows = (
                sb.table("entity_mentions")
                .select("file_id, file_version_id, canonical_value, source_field, source_row")
                .eq("company_id", company_id)
                .eq("entity_type", "driver")
                .ilike("canonical_value", f"%{driver_name_lower}%")
                .limit(50)
                .execute()
                .data
            )
        except Exception:
            errors.append(
                f"[{RULE_KEY}] entity_mentions fetch failed for '{driver_name_lower}': "
                + traceback.format_exc()
            )
            continue

        # Filter out the safety file itself
        other_files = [
            r for r in em_rows if r.get("file_version_id") != chunk_fv_id
        ]

        if not other_files:
            continue  # driver not used elsewhere — no actionable issue

        # Build evidence
        evidence: list[EvidenceItem] = [
            EvidenceItem(
                file_id=chunk_file_id,
                file_version_id=chunk_fv_id,
                chunk_id=chunk_id,
                source_filename=source_filename,
                source_label="Expiration notice",
                evidence_text=content[:400],
                evidence_json={
                    "driver_name": driver_name,
                    "expiry_date": expiry_date,
                },
            )
        ]

        # Add entity mention evidence from run tickets
        seen_fv: set[str] = set()
        for em in other_files:
            em_fv = em.get("file_version_id") or ""
            if em_fv in seen_fv:
                continue
            seen_fv.add(em_fv)
            em_filename = fv_map.get(em_fv) or _get_filename_for_fv(sb, company_id, em_fv)
            evidence.append(
                EvidenceItem(
                    file_id=em.get("file_id"),
                    file_version_id=em_fv or None,
                    chunk_id=None,
                    source_filename=em_filename,
                    source_label="Driver usage reference",
                    evidence_text=(
                        f"Driver '{driver_name}' referenced in {em_filename} "
                        f"(field: {em.get('source_field', 'unknown')}, "
                        f"row: {em.get('source_row', 'unknown')})"
                    ),
                    evidence_json=dict(em),
                )
            )

        related_fv_ids = list({em.get("file_version_id") for em in other_files if em.get("file_version_id")})

        candidate = IssueCandidate(
            rule_key=RULE_KEY,
            rule_version=RULE_VERSION,
            issue_type="driver_compliance",
            compliance_framework="internal_safety",
            severity="high",
            title=f"Driver with expired certification used: {driver_name}",
            description=(
                f"Driver '{driver_name}' has an expired hazmat or safety certification"
                + (f" (expired {expiry_date})" if expiry_date else "")
                + " but appears in active run tickets or dispatch records. "
                "Verify certification status and remove from active duty if unresolved."
            ),
            related_entities={"driver": driver_name, "expiry_date": expiry_date},
            related_files=[source_filename]
            + [fv_map.get(fv, fv) for fv in related_fv_ids],
            raw_data={
                "chunk_id": chunk_id,
                "driver_name": driver_name,
                "expiry_date": expiry_date,
                "source_filename": source_filename,
                "usage_file_version_ids": related_fv_ids,
            },
            confidence_score=0.85,
            suppression_hash=_suppression_hash(
                f"{company_id}:expired_cert:{driver_name_lower}"
            ),
            evidence=evidence,
        )
        candidates.append(candidate)

    return candidates, len(chunks_data), errors


# ---------------------------------------------------------------------------
# Rule 2: missing_po_reference
# ---------------------------------------------------------------------------

def _rule_missing_po(
    sb: Client, company_id: str, file_version_id: str | None
) -> tuple[list[IssueCandidate], int, list[str]]:
    """Detect documents that reference missing or invalid PO numbers."""
    RULE_KEY = "missing_po_reference"
    RULE_VERSION = 1
    candidates: list[IssueCandidate] = []
    errors: list[str] = []

    try:
        q = (
            sb.table("file_chunks")
            .select("id, file_id, file_version_id, content, chunk_index, metadata_jsonb")
            .eq("company_id", company_id)
        )
        if file_version_id:
            q = q.eq("file_version_id", file_version_id)

        try:
            chunks_data = (
                q.or_(
                    "content.ilike.%missing po%,"
                    "content.ilike.%no valid po%,"
                    "content.ilike.%no po%,"
                    "content.ilike.%wrong po%,"
                    "content.ilike.%invalid po%,"
                    "content.ilike.%old po%,"
                    "content.ilike.%new po%"
                )
                .limit(200)
                .execute()
                .data
            )
        except Exception:
            chunks_data = (
                q.ilike("content", "%missing po%")
                .limit(200)
                .execute()
                .data
            )
    except Exception:
        errors.append(f"[{RULE_KEY}] chunk fetch failed: " + traceback.format_exc())
        return candidates, 0, errors

    if not chunks_data:
        return candidates, 0, errors

    # Group by file_version_id
    fv_ids = list({c.get("file_version_id") for c in chunks_data if c.get("file_version_id")})
    fv_map = _fv_filename_map(sb, company_id, fv_ids)

    # Group chunks by file_version_id
    groups: dict[str, list[dict]] = {}
    for chunk in chunks_data:
        fv = chunk.get("file_version_id") or "__unknown__"
        groups.setdefault(fv, []).append(chunk)

    for fv, fv_chunks in groups.items():
        source_filename = fv_map.get(fv, fv)
        first_chunk = fv_chunks[0]

        evidence: list[EvidenceItem] = []
        for ch in fv_chunks:
            excerpt = (ch.get("content") or "")[:400]
            evidence.append(
                EvidenceItem(
                    file_id=ch.get("file_id"),
                    file_version_id=fv if fv != "__unknown__" else None,
                    chunk_id=ch.get("id"),
                    source_filename=source_filename,
                    source_label="PO reference issue",
                    evidence_text=excerpt,
                    evidence_json={
                        "chunk_index": ch.get("chunk_index"),
                        "metadata": ch.get("metadata_jsonb"),
                    },
                )
            )

        candidate = IssueCandidate(
            rule_key=RULE_KEY,
            rule_version=RULE_VERSION,
            issue_type="billing_compliance",
            compliance_framework="internal",
            severity="medium",
            title="Missing or invalid PO reference detected",
            description=(
                "One or more documents reference a missing, invalid, or revised "
                "purchase order. Review required."
            ),
            related_entities={},
            related_files=[source_filename],
            raw_data={
                "file_version_id": fv if fv != "__unknown__" else None,
                "source_filename": source_filename,
                "chunk_count": len(fv_chunks),
            },
            confidence_score=0.8,
            suppression_hash=_suppression_hash(
                f"{company_id}:missing_po:{fv}"
            ),
            evidence=evidence,
        )
        candidates.append(candidate)

    return candidates, len(chunks_data), errors


# ---------------------------------------------------------------------------
# Rule 3: spill_incident_followup
# ---------------------------------------------------------------------------

_SPILL_FALSE_POSITIVE_RE = re.compile(
    r"no spill|no leak|spill kit|spill prevention plan",
    re.IGNORECASE,
)


def _rule_spill_incident(
    sb: Client, company_id: str, file_version_id: str | None
) -> tuple[list[IssueCandidate], int, list[str]]:
    """Detect spill or incident mentions requiring follow-up."""
    RULE_KEY = "spill_incident_followup"
    RULE_VERSION = 1
    candidates: list[IssueCandidate] = []
    errors: list[str] = []

    try:
        q = (
            sb.table("file_chunks")
            .select("id, file_id, file_version_id, content, chunk_index, metadata_jsonb")
            .eq("company_id", company_id)
        )
        if file_version_id:
            q = q.eq("file_version_id", file_version_id)

        try:
            chunks_data = (
                q.or_(
                    "content.ilike.%spill%,"
                    "content.ilike.%spill incident%,"
                    "content.ilike.%leak%,"
                    "content.ilike.%blowout%"
                )
                .limit(200)
                .execute()
                .data
            )
        except Exception:
            chunks_data = (
                q.ilike("content", "%spill%")
                .limit(200)
                .execute()
                .data
            )
    except Exception:
        errors.append(f"[{RULE_KEY}] chunk fetch failed: " + traceback.format_exc())
        return candidates, 0, errors

    if not chunks_data:
        return candidates, 0, errors

    # Filter false positives
    real_chunks = [
        c for c in chunks_data
        if not _SPILL_FALSE_POSITIVE_RE.search(c.get("content") or "")
    ]

    if not real_chunks:
        return candidates, len(chunks_data), errors

    fv_ids = list({c.get("file_version_id") for c in real_chunks if c.get("file_version_id")})
    fv_map = _fv_filename_map(sb, company_id, fv_ids)

    # Group by file_version_id
    groups: dict[str, list[dict]] = {}
    for chunk in real_chunks:
        fv = chunk.get("file_version_id") or "__unknown__"
        groups.setdefault(fv, []).append(chunk)

    for fv, fv_chunks in groups.items():
        source_filename = fv_map.get(fv, fv)

        evidence: list[EvidenceItem] = []
        for ch in fv_chunks:
            excerpt = (ch.get("content") or "")[:400]
            evidence.append(
                EvidenceItem(
                    file_id=ch.get("file_id"),
                    file_version_id=fv if fv != "__unknown__" else None,
                    chunk_id=ch.get("id"),
                    source_filename=source_filename,
                    source_label="Spill/incident reference",
                    evidence_text=excerpt,
                    evidence_json={
                        "chunk_index": ch.get("chunk_index"),
                        "metadata": ch.get("metadata_jsonb"),
                    },
                )
            )

        candidate = IssueCandidate(
            rule_key=RULE_KEY,
            rule_version=RULE_VERSION,
            issue_type="safety_incident",
            compliance_framework="internal_safety",
            severity="medium",
            title="Spill incident requires follow-up review",
            description=(
                "A spill, leak, or incident was mentioned in documentation. "
                "Review whether follow-up or closure documentation exists."
            ),
            related_entities={},
            related_files=[source_filename],
            raw_data={
                "file_version_id": fv if fv != "__unknown__" else None,
                "source_filename": source_filename,
                "chunk_count": len(fv_chunks),
            },
            confidence_score=0.75,
            suppression_hash=_suppression_hash(
                f"{company_id}:spill_incident:{fv}"
            ),
            evidence=evidence,
        )
        candidates.append(candidate)

    return candidates, len(chunks_data), errors


# ---------------------------------------------------------------------------
# Rule runner registry
# ---------------------------------------------------------------------------

_RULES: list[tuple[str, int, Any]] = [
    ("expired_hazmat_or_safety_cert_driver_used", 1, _rule_expired_cert),
    ("missing_po_reference", 1, _rule_missing_po),
    ("spill_incident_followup", 1, _rule_spill_incident),
]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_issue_detection(
    sb: Client,
    company_id: str,
    file_version_id: str | None = None,
) -> dict:
    """Run all issue detection rules for a company (optionally scoped to one file version).

    Returns a summary dict:
        {
            "rules_run": int,
            "candidates_found": int,
            "issues_created": int,
            "issues_suppressed": int,
            "errors": list[str],
        }
    """
    total_candidates = 0
    total_created = 0
    total_suppressed = 0
    all_errors: list[str] = []
    rules_run = 0

    for rule_key, rule_version, rule_fn in _RULES:
        started_at = datetime.now(timezone.utc).isoformat()
        rule_candidates_found = 0
        rule_issues_created = 0
        rule_issues_suppressed = 0
        rule_errors: list[str] = []

        try:
            candidates, candidates_found, errors = rule_fn(sb, company_id, file_version_id)
            rule_candidates_found = candidates_found
            rule_errors.extend(errors)

            for candidate in candidates:
                try:
                    inserted = _insert_candidate(sb, company_id, candidate)
                    if inserted:
                        rule_issues_created += 1
                    else:
                        rule_issues_suppressed += 1
                except Exception:
                    rule_errors.append(
                        f"[{rule_key}] insert_candidate error: " + traceback.format_exc()
                    )

        except Exception:
            rule_errors.append(
                f"[{rule_key}] rule execution error: " + traceback.format_exc()
            )

        finished_at = datetime.now(timezone.utc).isoformat()
        rules_run += 1

        # Log the rule run
        try:
            sb.table("issue_rule_runs").insert(
                {
                    "id": str(uuid.uuid4()),
                    "company_id": company_id,
                    "rule_key": rule_key,
                    "rule_version": rule_version,
                    "candidates_found": rule_candidates_found,
                    "issues_created": rule_issues_created,
                    "issues_suppressed": rule_issues_suppressed,
                    "errors": rule_errors,
                    "started_at": started_at,
                    "finished_at": finished_at,
                }
            ).execute()
        except Exception:
            all_errors.append(
                f"[{rule_key}] rule_run log insert failed: " + traceback.format_exc()
            )

        total_candidates += rule_candidates_found
        total_created += rule_issues_created
        total_suppressed += rule_issues_suppressed
        all_errors.extend(rule_errors)

    return {
        "rules_run": rules_run,
        "candidates_found": total_candidates,
        "issues_created": total_created,
        "issues_suppressed": total_suppressed,
        "errors": all_errors,
    }
