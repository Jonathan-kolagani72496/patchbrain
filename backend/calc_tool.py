"""
PatchBrain structured row calculation engine - production v2.

Routes math/aggregation questions away from RAG and into deterministic table logic.
Handles CSV/XLSX row chunks in the parser format:
    Row 2: ticket_id=FT-9001 | date=6/2/25 | truck_id=T-03 | ...
"""

from __future__ import annotations

import re
import uuid
from typing import Any

VERSION_MARKER = "[structured-calc-v3]"
TABLE_SOURCE_TYPES = ("csv_rows", "sheet_rows")

FIELD_ALIASES: dict[str, str] = {
    "total revenue": "total_amount", "revenue": "total_amount",
    "total amount": "total_amount", "invoice total": "total_amount",
    "invoice amount": "total_amount", "total invoice": "total_amount",
    "total billed": "total_amount", "billed": "total_amount", "amount": "total_amount",
    "bbls hauled": "bbls_hauled", "barrels hauled": "bbls_hauled",
    "total hauled": "bbls_hauled", "bbls": "bbls_hauled",
    "bbl": "bbls_hauled", "barrels": "bbls_hauled", "barrel": "bbls_hauled",
    "disposed": "bbls_received", "disposal": "bbls_received", "disposal bbls": "bbls_received",
    "injected": "total_bbls", "injection": "total_bbls", "total injected": "total_bbls",
}

NUMERIC_FIELD_PRIORITY = (
    "total_amount", "bbls_hauled", "bbls_received", "total_bbls", "rate_per_bbl",
    "amount", "quantity", "qty", "volume", "barrels", "gallons", "hours", "miles", "count", "total",
)
ID_FIELDS = ("ticket_id", "invoice_id", "invoice_number", "job_id", "po_number", "load_id")

GROUP_FIELD_PRIORITY = {
    "client": ("client", "customer", "company", "operator", "trucking_company", "vendor"),
    "customer": ("client", "customer", "company", "operator", "vendor"),
    "vendor": ("vendor", "client", "customer", "company"),
    "lease": ("lease_location", "lease_origin", "lease", "well", "location", "site"),
    "location": ("lease_location", "lease_origin", "lease", "well", "location", "site"),
    "well": ("well", "lease_location", "lease_origin", "lease", "location", "site"),
    "site": ("site", "lease_location", "lease_origin", "lease", "well", "location"),
    "truck": ("truck_id", "truck_number", "truck", "unit"),
    "driver": ("driver", "driver_name"),
}

CALC_SUM_PATTERNS = (
    "total revenue", "total amount", "total bbls", "total barrels",
    "how many bbls", "how many barrels", "how many bbl", "how many barrel",
    "bbls were hauled", "barrels were hauled", "bbls hauled", "barrels hauled",
    "how much revenue", "how much did", "how much was", "how much were",
    "sum of", "total hauled", "total disposed", "total injected",
    "total invoice", "total billed", "invoice breakdown",
)
CALC_COUNT_PATTERNS = (
    "how many runs", "how many tickets", "how many loads", "how many trips", "how many hauls",
    "number of runs", "number of tickets", "number of loads", "count rows", "count tickets",
)
CALC_RANK_PATTERNS = (
    "highest", "lowest", "most", "least", "top client", "top lease", "top truck", "top driver",
    "ranked", "rank clients", "rank leases", "rank trucks", "rank by", "show all clients",
    "show all leases", "client breakdown", "lease breakdown", "truck breakdown", "driver breakdown",
    "breakdown by client", "breakdown by lease", "breakdown by truck", "breakdown by driver",
    "which client had", "which lease had", "which truck had", "compare clients", "compare leases", "compare trucks",
)
CALC_AVG_PATTERNS = ("average", "avg", "mean", "per run", "per ticket", "per load")
ALL_CALC_PATTERNS = CALC_SUM_PATTERNS + CALC_COUNT_PATTERNS + CALC_RANK_PATTERNS + CALC_AVG_PATTERNS
_ROW_RE = re.compile(r"^Row\s+(\d+):\s*", re.IGNORECASE)


def _normalize_key(key: str) -> str:
    key = (key or "").strip().lower()
    key = re.sub(r"[^a-z0-9]+", "_", key)
    return re.sub(r"_+", "_", key).strip("_")


def _parse_chunk_rows(content: str, chunk: dict | None = None) -> list[dict[str, Any]]:
    chunk = chunk or {}
    records: list[dict[str, Any]] = []
    for line in (content or "").splitlines():
        line = line.strip()
        m = _ROW_RE.match(line)
        if not m:
            continue
        source_row_number = int(m.group(1))
        line = _ROW_RE.sub("", line)
        record: dict[str, Any] = {}
        for part in [p.strip() for p in line.split("|")]:
            if "=" not in part:
                continue
            k, _, v = part.partition("=")
            k = _normalize_key(k)
            v = (v or "").strip()
            if k and v:
                record[k] = v
        if record:
            record["_source_row"] = source_row_number
            record["_chunk_id"] = str(chunk.get("chunk_id") or chunk.get("id") or "")
            record["_filename"] = chunk.get("original_filename") or chunk.get("filename") or "unknown"
            record["_source_type"] = chunk.get("source_type") or ""
            record["_row_start"] = chunk.get("row_start")
            record["_row_end"] = chunk.get("row_end")
            records.append(record)
    return records


def _to_float(val: Any) -> float | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1]
    cleaned = re.sub(r"[$,\s]", "", s)
    cleaned = re.sub(r"(?i)(bbls?|barrels?|gal|gallons?|hrs?|hours?|mi|miles)$", "", cleaned)
    try:
        out = float(cleaned)
        return -out if negative else out
    except (ValueError, TypeError):
        return None


def _available_fields(records: list[dict]) -> set[str]:
    out: set[str] = set()
    for r in records:
        out.update(k for k in r.keys() if not k.startswith("_"))
    return out


def _extract_entity(q: str) -> str | None:
    q = (q or "").strip()
    for pat in (r'"([^"]+)"', r"'([^']+)'", r"“([^”]+)”", r"‘([^’]+)’"):
        m = re.search(pat, q)
        if m and m.group(1).strip():
            return m.group(1).strip()
    patterns = [
        r"\b(T-\d{1,4})\b",
        r"\b([A-Z][A-Za-z]+(?:\s+[A-Z0-9][A-Za-z0-9-]*)*\s+\d+[A-Z]{0,3})\b",
        r"\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,4}\s+(?:LLC|Inc\.?|Co\.?|Company|Operating|Pumping|Transport|Vacuum|Energy|Midstream|Services))\b",
    ]
    for pat in patterns:
        m = re.search(pat, q)
        if m:
            candidate = m.group(1).strip()
            if candidate.lower() not in {"total revenue", "total amount", "which client", "which lease", "show all clients", "show all leases", "rank clients"}:
                return candidate
    return None


def _record_matches_entity(record: dict[str, Any], entity: str) -> bool:
    needle = entity.lower()
    return any(needle in str(v).lower() for k, v in record.items() if not k.startswith("_"))


def _resolve_numeric_field(q: str, records: list[dict]) -> str | None:
    ql = (q or "").lower()
    fields = _available_fields(records)
    for phrase, canonical in sorted(FIELD_ALIASES.items(), key=lambda x: len(x[0]), reverse=True):
        if phrase in ql and canonical in fields:
            return canonical
    normalized_q = _normalize_key(ql)
    for f in fields:
        if f in normalized_q:
            if any(_to_float(r.get(f)) is not None for r in records[:50]):
                return f
    if any(w in ql for w in ("revenue", "amount", "billed", "invoice", "paid")):
        for f in ("total_amount", "amount", "invoice_amount", "invoice_total"):
            if f in fields:
                return f
    if any(w in ql for w in ("bbl", "barrel", "haul", "volume")):
        for f in ("bbls_hauled", "bbls_received", "total_bbls", "barrels", "volume"):
            if f in fields:
                return f
    for f in NUMERIC_FIELD_PRIORITY:
        if f in fields:
            return f
    return None


def _resolve_group_field(q: str, records: list[dict]) -> str | None:
    ql = (q or "").lower()
    fields = _available_fields(records)
    for trigger, candidates in GROUP_FIELD_PRIORITY.items():
        if trigger in ql:
            for f in candidates:
                if f in fields:
                    return f
    for f in ("client", "customer", "vendor", "lease_location", "lease", "well", "truck_id", "driver"):
        if f in fields:
            return f
    return None


def _calc_intent(q: str) -> str:
    ql = (q or "").lower()

    if any(p in ql for p in CALC_RANK_PATTERNS):
        return "rank"
    if any(p in ql for p in CALC_AVG_PATTERNS):
        return "avg"

    # "How many barrels/bbls" means sum the volume field, not count rows.
    if re.search(r"how\s+many\s+(bbls?|barrels?)", ql):
        return "sum"
    if re.search(r"(bbls?|barrels?)\s+(were\s+)?hauled", ql):
        return "sum"

    if any(p in ql for p in CALC_COUNT_PATTERNS):
        return "count"

    return "sum"


def _source_label(record: dict[str, Any]) -> str:
    bits = []
    if record.get("_filename"):
        bits.append(str(record["_filename"]))
    if record.get("_source_row"):
        bits.append(f"row {record['_source_row']}")
    for f in ID_FIELDS:
        if record.get(f):
            bits.append(f"{f}={record[f]}")
            break
    return " — ".join(bits) if bits else "source row"


def _format_value(field: str, value: float) -> str:
    if field and any(w in field for w in ("amount", "revenue", "invoice", "billed", "paid", "total")):
        return f"${value:,.2f}"
    return f"{value:,.2f}"


def _run_calc(q: str, records: list[dict], entity: str | None, numeric_field: str | None, group_field: str | None, intent: str) -> str:
    if not records:
        return f"{VERSION_MARKER} No structured table rows found."
    filtered = [r for r in records if _record_matches_entity(r, entity)] if entity else records
    if entity and not filtered:
        return f'{VERSION_MARKER} No rows matched "{entity}" in structured table data. Searched {len(records)} parsed rows.'
    filtered = _filter_records_for_requested_shape(q, filtered, numeric_field, group_field, entity)
    total_rows = len(filtered)

    if intent == "rank" or (not entity and group_field):
        gf = group_field
        if not gf:
            return f"{VERSION_MARKER} Could not determine a grouping field. Try 'by client', 'by lease', 'by truck', or 'by driver'."
        if not numeric_field:
            groups: dict[str, int] = {}
            for r in filtered:
                key = str(r.get(gf) or "Unknown")
                groups[key] = groups.get(key, 0) + 1
            ranked = sorted(groups.items(), key=lambda x: x[1], reverse=True)
            lines = [f"{VERSION_MARKER} Row count by {gf} ({total_rows} rows):", ""]
            lines += [f"{i}. {key} — {cnt} row{'s' if cnt != 1 else ''}" for i, (key, cnt) in enumerate(ranked[:25], 1)]
            return "\n".join(lines)
        groups_num: dict[str, float] = {}
        groups_cnt: dict[str, int] = {}
        examples: dict[str, list[str]] = {}
        excluded_unknown = 0
        for r in filtered:
            raw_key = r.get(gf)
            if _is_unknown_group_value(raw_key):
                excluded_unknown += 1
                continue
            key = str(raw_key).strip()
            val = _to_float(r.get(numeric_field))
            if val is None:
                continue
            groups_num[key] = groups_num.get(key, 0.0) + val
            groups_cnt[key] = groups_cnt.get(key, 0) + 1
            examples.setdefault(key, [])
            if len(examples[key]) < 2:
                examples[key].append(_source_label(r))
        if not groups_num:
            return f'{VERSION_MARKER} Found {total_rows} rows, but field "{numeric_field}" had no numeric values.'
        reverse = "lowest" not in (q or "").lower() and "least" not in (q or "").lower()
        ranked_num = sorted(groups_num.items(), key=lambda x: x[1], reverse=reverse)
        grand_total = sum(groups_num.values())
        lines = [f"{VERSION_MARKER} {numeric_field} by {gf} ({total_rows} rows, grand total: {_format_value(numeric_field, grand_total)}):", ""]
        for i, (key, total) in enumerate(ranked_num[:25], 1):
            sample = "; ".join(examples.get(key, [])[:2])
            lines.append(f"{i}. {key} — {_format_value(numeric_field, total)} ({groups_cnt.get(key, 0)} row{'s' if groups_cnt.get(key, 0) != 1 else ''})" + (f" — examples: {sample}" if sample else ""))
        return "\n".join(lines)

    if intent == "count":
        entity_clause = f' for "{entity}"' if entity else ""
        examples = ", ".join(_source_label(r) for r in filtered[:10])
        return f"{VERSION_MARKER} Found {total_rows} matching row{'s' if total_rows != 1 else ''}{entity_clause}." + (f"\nSource rows: {examples}" if examples else "")

    if not numeric_field:
        return f"{VERSION_MARKER} Found {total_rows} matching rows, but could not determine which numeric field to calculate. Try 'total revenue', 'total amount', or 'total bbls hauled'."
    values = [(v, r) for r in filtered if (v := _to_float(r.get(numeric_field))) is not None]
    if not values:
        return f'{VERSION_MARKER} Found {total_rows} rows, but field "{numeric_field}" had no numeric values.'
    total = sum(v for v, _ in values)
    entity_clause = f' for "{entity}"' if entity else ""
    if intent == "avg":
        avg = total / len(values)
        lines = [f"{VERSION_MARKER} Average {_friendly_field_name(numeric_field)}{entity_clause}: {_format_value(numeric_field, avg)}", f"Based on {len(values)} numeric row{'s' if len(values) != 1 else ''}; total {_format_value(numeric_field, total)}."]
    else:
        lines = [f"{VERSION_MARKER} Total {_friendly_field_name(numeric_field)}{entity_clause}: {_format_value(numeric_field, total)}", f"Based on {len(values)} numeric row{'s' if len(values) != 1 else ''}."]
    examples = ", ".join(_source_label(r) for _, r in values[:10])
    if examples:
        lines.append("Source rows: " + examples + ("…" if len(values) > 10 else ""))
    return "\n".join(lines)


async def load_table_chunks(company_id: str, db_pool, sb_client, limit: int = 5000) -> list[dict]:
    source_types = list(TABLE_SOURCE_TYPES)
    if db_pool:
        try:
            async with db_pool.acquire() as c:
                rows = await c.fetch(
                    """
                    select fc.id as chunk_id, fc.content, fc.source_type, fc.row_start, fc.row_end,
                           fc.file_id, fc.file_version_id, fv.original_filename
                    from file_chunks fc
                    join file_versions fv on fv.id = fc.file_version_id
                    join files f on f.id = fc.file_id
                    where fc.company_id = $1
                      and fv.company_id = $1
                      and f.company_id = $1
                      and fv.status = 'ready'
                      and f.deleted_at is null
                      and fc.source_type = any($2::text[])
                    order by fv.created_at desc, fc.chunk_index asc
                    limit $3
                    """,
                    uuid.UUID(str(company_id)), source_types, limit,
                )
            return [dict(r) for r in rows]
        except Exception as e:
            print(f"calc_tool asyncpg failed, falling back to Supabase: {e}")
    try:
        res = sb_client.table("file_chunks").select("id,content,source_type,row_start,row_end,file_id,file_version_id,chunk_index").eq("company_id", str(company_id)).in_("source_type", source_types).order("chunk_index", desc=False).limit(limit).execute()
        chunks = res.data or []
    except Exception as e:
        print(f"calc_tool Supabase chunks query failed: {e}")
        return []
    if not chunks:
        return []
    fv_ids = sorted({str(c.get("file_version_id")) for c in chunks if c.get("file_version_id")})
    file_ids = sorted({str(c.get("file_id")) for c in chunks if c.get("file_id")})
    try:
        versions = {str(v["id"]): v for v in (sb_client.table("file_versions").select("id,original_filename,status,company_id").eq("company_id", str(company_id)).in_("id", fv_ids).eq("status", "ready").execute().data or [])}
        active_files = {str(f["id"]) for f in (sb_client.table("files").select("id,deleted_at,company_id").eq("company_id", str(company_id)).in_("id", file_ids).is_("deleted_at", "null").execute().data or [])}
    except Exception as e:
        print(f"calc_tool related lookup failed: {e}")
        return []
    enriched = []
    for c in chunks:
        vid = str(c.get("file_version_id")); fid = str(c.get("file_id"))
        if vid not in versions or fid not in active_files:
            continue
        enriched.append({"chunk_id": c.get("id"), "content": c.get("content"), "source_type": c.get("source_type"), "row_start": c.get("row_start"), "row_end": c.get("row_end"), "file_id": c.get("file_id"), "file_version_id": c.get("file_version_id"), "original_filename": versions[vid].get("original_filename")})
    return enriched


async def run_structured_calc(question: str, company_id: str, db_pool, sb_client) -> str:
    try:
        chunks = await load_table_chunks(company_id, db_pool, sb_client)
        if not chunks:
            return f"{VERSION_MARKER} No structured table data found. Upload CSV or Excel files to enable calculation queries."
        all_records: list[dict[str, Any]] = []
        for chunk in chunks:
            all_records.extend(_parse_chunk_rows(chunk.get("content") or "", chunk))
        if not all_records:
            return f"{VERSION_MARKER} Found table chunks but could not parse any row data."
        entity = _extract_entity(question)
        intent = _calc_intent(question)
        numeric_field = _resolve_numeric_field(question, all_records)
        group_field = _resolve_group_field(question, all_records) if intent == "rank" or not entity else None
        return _run_calc(question, all_records, entity, numeric_field, group_field, intent)
    except Exception as e:
        import traceback
        print(f"structured_calc failed: {e}")
        print(traceback.format_exc())
        return f"{VERSION_MARKER} Calculation failed internally. Try rephrasing as 'total revenue from Reeves 14H' or 'show all clients ranked by total amount'."


def detect_calc_intent(q: str) -> bool:
    raw = (q or "").lower().replace("?", "").replace("'", "")
    ql = " ".join(raw.split())

    if any(p in ql for p in ALL_CALC_PATTERNS):
        return True

    if re.search(r"how\s+many\s+(bbls?|barrels?)", ql):
        return True
    if re.search(r"(bbls?|barrels?)\s+(were\s+)?hauled", ql):
        return True
    if re.search(r"(highest|lowest|most|least).*(revenue|amount|bbls?|barrels?)", ql):
        return True
    if re.search(r"(total|sum).*(revenue|amount|bbls?|barrels?)", ql):
        return True

    return False
