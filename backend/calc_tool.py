"""
PatchBrain structured row calculation engine - production v4.

Routes math/aggregation questions away from RAG and into deterministic table logic.
Handles CSV/XLSX row chunks in the parser format:
    Row 2: ticket_id=FT-9001 | date=6/2/25 | truck_id=T-03 | ...

v4 additions:
- date/day/week/month grouping
- two-entity comparisons
- stricter metric selection for revenue/barrels/loads
- friendlier field labels with source rows
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, date
from typing import Any

VERSION_MARKER = "[structured-calc-v4]"
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
DATE_FIELD_PRIORITY = (
    "date", "ticket_date", "invoice_date", "service_date", "created_at", "completed_at",
    "job_date", "run_date", "load_date", "delivery_date",
)
ENTITY_FIELDS = (
    "lease_location", "lease_origin", "lease", "well", "location", "site",
    "client", "customer", "operator", "vendor", "company",
    "truck_id", "truck_number", "truck", "unit", "driver", "driver_name",
)

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
    "total invoice", "total billed", "invoice breakdown", "revenue breakdown",
)
CALC_COUNT_PATTERNS = (
    "how many runs", "how many tickets", "how many loads", "how many trips", "how many hauls",
    "number of runs", "number of tickets", "number of loads", "count rows", "count tickets",
    "by loads", "by load count", "load count", "ticket count", "run count",
)
CALC_RANK_PATTERNS = (
    "highest", "lowest", "most", "least", "top client", "top lease", "top truck", "top driver",
    "ranked", "rank clients", "rank leases", "rank trucks", "rank by", "show all clients",
    "show all leases", "client breakdown", "lease breakdown", "truck breakdown", "driver breakdown",
    "breakdown by client", "breakdown by lease", "breakdown by truck", "breakdown by driver",
    "which client had", "which lease had", "which truck had", "which driver", "compare clients", "compare leases", "compare trucks",
)
CALC_AVG_PATTERNS = ("average", "avg", "mean", "per run", "per ticket", "per load")
CALC_DATE_PATTERNS = (
    "by month", "per month", "monthly", "month breakdown", "breakdown by month",
    "by week", "per week", "weekly", "week breakdown", "breakdown by week",
    "by date", "by day", "per day", "daily", "date breakdown", "day breakdown", "breakdown by date",
)
CALC_COMPARE_PATTERNS = (" compare ", " vs ", " versus ", "compare ", "compared to", "how does")
ALL_CALC_PATTERNS = (
    CALC_SUM_PATTERNS + CALC_COUNT_PATTERNS + CALC_RANK_PATTERNS +
    CALC_AVG_PATTERNS + CALC_DATE_PATTERNS + CALC_COMPARE_PATTERNS
)
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


def _parse_date_value(value: Any) -> date | None:
    s = str(value or "").strip()
    if not s:
        return None
    s = s.split("T", 1)[0].strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _resolve_date_field(records: list[dict]) -> str | None:
    fields = _available_fields(records)
    for f in DATE_FIELD_PRIORITY:
        if f in fields and any(_parse_date_value(r.get(f)) for r in records[:200]):
            return f
    for f in fields:
        if f.endswith("_date") or f in {"date", "created_at"}:
            if any(_parse_date_value(r.get(f)) for r in records[:200]):
                return f
    return None


def _date_grouping(q: str) -> str | None:
    ql = (q or "").lower()
    if any(p in ql for p in ("by month", "per month", "monthly", "month breakdown", "breakdown by month")):
        return "month"
    if any(p in ql for p in ("by week", "per week", "weekly", "week breakdown", "breakdown by week")):
        return "week"
    if any(p in ql for p in ("by date", "by day", "per day", "daily", "date breakdown", "day breakdown", "breakdown by date")):
        return "date"
    return None


def _date_group_key(d: date, mode: str) -> str:
    if mode == "month":
        return d.strftime("%Y-%m")
    if mode == "week":
        iso = d.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return d.isoformat()


def _strip_noise_from_entity(s: str) -> str:
    s = (s or "").strip(" ?.,:;!\"'“”‘’")
    s = re.sub(r"^(how does|compare|show|what was|what is|was|does)\s+", "", s, flags=re.I).strip()
    s = re.sub(r"\s+(by|for|using|on)\s+(revenue|total amount|amount|barrels hauled|barrels|bbls|loads|runs|tickets).*$", "", s, flags=re.I).strip()
    s = re.sub(r"\s+(revenue|total amount|amount|barrels hauled|barrels|bbls|loads|runs|tickets)\s+breakdown.*$", "", s, flags=re.I).strip()
    s = re.sub(r"\s+breakdown\s+by\s+(month|week|date|day).*$", "", s, flags=re.I).strip()
    return s.strip(" ?.,:;!\"'“”‘’")


def _known_entity_values(records: list[dict]) -> list[str]:
    values: set[str] = set()
    for r in records:
        for f in ENTITY_FIELDS:
            v = str(r.get(f) or "").strip()
            if v and not _is_unknown_group_value(v):
                values.add(v)
    return sorted(values, key=len, reverse=True)


def _extract_entities(q: str, records: list[dict] | None = None, max_entities: int = 2) -> list[str]:
    q = (q or "").strip()
    found: list[str] = []

    for pat in (r'"([^"]+)"', r"'([^']+)'", r"“([^”]+)”", r"‘([^’]+)’"):
        for m in re.finditer(pat, q):
            val = _strip_noise_from_entity(m.group(1))
            if val and val.lower() not in {x.lower() for x in found}:
                found.append(val)

    if records:
        ql = q.lower()
        matches: list[tuple[int, int, str]] = []
        for value in _known_entity_values(records):
            vl = value.lower()
            idx = ql.find(vl)
            if idx >= 0:
                matches.append((idx, -len(value), value))
        for _, __, value in sorted(matches):
            if value.lower() not in {x.lower() for x in found}:
                found.append(value)
                if len(found) >= max_entities:
                    return found[:max_entities]

    split_patterns = [
        r"compare\s+(.+?)\s+(?:and|to|with|versus|vs\.?)\s+(.+?)(?:\s+by\s+|$)",
        r"how\s+does\s+(.+?)\s+compare\s+(?:to|with)\s+(.+?)(?:\s+by\s+|$)",
        r"(.+?)\s+(?:vs\.?|versus)\s+(.+?)(?:\s+by\s+|$)",
    ]
    for pat in split_patterns:
        m = re.search(pat, q, flags=re.I)
        if m:
            for part in (m.group(1), m.group(2)):
                val = _strip_noise_from_entity(part)
                if val and val.lower() not in {x.lower() for x in found}:
                    found.append(val)
            if len(found) >= max_entities:
                return found[:max_entities]

    patterns = [
        r"\b(T-\d{1,4})\b",
        r"\b([A-Z][A-Za-z]+(?:\s+[A-Z0-9][A-Za-z0-9-]*)*\s+\d+[A-Z]{0,3})\b",
        r"\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,4}\s+(?:LLC|Inc\.?|Co\.?|Company|Operating|Pumping|Transport|Vacuum|Energy|Midstream|Services))\b",
    ]
    for pat in patterns:
        for m in re.finditer(pat, q):
            candidate = _strip_noise_from_entity(m.group(1))
            if candidate.lower() not in {"total revenue", "total amount", "which client", "which lease", "show all clients", "show all leases", "rank clients"}:
                if candidate and candidate.lower() not in {x.lower() for x in found}:
                    found.append(candidate)
                    if len(found) >= max_entities:
                        return found[:max_entities]
    return found[:max_entities]


def _extract_entity(q: str, records: list[dict] | None = None) -> str | None:
    entities = _extract_entities(q, records, max_entities=1)
    return entities[0] if entities else None


def _record_matches_entity(record: dict[str, Any], entity: str) -> bool:
    needle = entity.lower()
    return any(needle in str(v).lower() for k, v in record.items() if not k.startswith("_"))


def _resolve_metric(q: str, records: list[dict]) -> tuple[str, str | None]:
    """Return (metric_kind, numeric_field). metric_kind is sum/count/avg."""
    ql = (q or "").lower()
    fields = _available_fields(records)

    if any(w in ql for w in ("load", "loads", "run", "runs", "ticket", "tickets", "trip", "trips")) and not any(w in ql for w in ("barrel", "bbl", "revenue", "amount")):
        return "count", None

    numeric = _resolve_numeric_field(q, records)
    if any(p in ql for p in CALC_AVG_PATTERNS):
        return "avg", numeric
    if numeric:
        return "sum", numeric

    # If the wording asks for a count and no numeric metric is explicit, count rows.
    if any(p in ql for p in CALC_COUNT_PATTERNS):
        return "count", None

    # Safe fallback: do not invent a metric.
    for f in NUMERIC_FIELD_PRIORITY:
        if f in fields:
            return "sum", f
    return "count", None


def _resolve_numeric_field(q: str, records: list[dict]) -> str | None:
    ql = (q or "").lower()
    fields = _available_fields(records)
    for phrase, canonical in sorted(FIELD_ALIASES.items(), key=lambda x: len(x[0]), reverse=True):
        if phrase in ql and canonical in fields:
            return canonical
    normalized_q = _normalize_key(ql)
    for f in fields:
        if f in normalized_q and any(_to_float(r.get(f)) is not None for r in records[:50]):
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
    if _is_compare_question(q):
        return "compare"
    if _date_grouping(q):
        return "date_group"
    if any(p in ql for p in CALC_RANK_PATTERNS):
        return "rank"
    if any(p in ql for p in CALC_AVG_PATTERNS):
        return "avg"
    if re.search(r"how\s+many\s+(bbls?|barrels?)", ql):
        return "sum"
    if re.search(r"(bbls?|barrels?)\s+(were\s+)?hauled", ql):
        return "sum"
    if any(p in ql for p in CALC_COUNT_PATTERNS):
        return "count"
    return "sum"


def _is_compare_question(q: str) -> bool:
    ql = f" {(q or '').lower()} "
    return any(p in ql for p in CALC_COMPARE_PATTERNS)


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


def _is_unknown_group_value(value: Any) -> bool:
    s = str(value or "").strip().lower()
    return s in {"", "unknown", "none", "null", "n/a", "na", "-", "--"}


def _record_has_field_value(record: dict[str, Any], field: str) -> bool:
    if not field:
        return False
    return not _is_unknown_group_value(record.get(field))


def _filter_records_for_requested_shape(
    q: str,
    records: list[dict],
    numeric_field: str | None,
    group_field: str | None,
    entity: str | None,
) -> list[dict]:
    ql = (q or "").lower()
    filtered = records

    if group_field:
        filtered = [r for r in filtered if _record_has_field_value(r, group_field)]

    if numeric_field:
        filtered = [r for r in filtered if _to_float(r.get(numeric_field)) is not None]

    if any(w in ql for w in ("run", "runs", "ticket", "tickets", "load", "loads", "haul", "hauled", "truck", "driver")):
        preferred = [r for r in filtered if any(k in r for k in ("ticket_id", "truck_id", "driver", "lease_location", "bbls_hauled"))]
        if preferred:
            filtered = preferred

    if any(w in ql for w in ("client", "customer")):
        preferred = [r for r in filtered if any(_record_has_field_value(r, f) for f in ("client", "customer"))]
        if preferred:
            filtered = preferred

    if any(w in ql for w in ("lease", "site", "well", "location")) or entity:
        # For named well/lease questions, prefer rows with location-like fields, but only if that keeps data.
        preferred = [r for r in filtered if any(_record_has_field_value(r, f) for f in ("lease_location", "lease_origin", "lease", "well", "site", "location"))]
        if preferred:
            filtered = preferred

    return filtered or records


def _friendly_field_name(field: str | None) -> str:
    mapping = {
        "total_amount": "revenue",
        "bbls_hauled": "barrels hauled",
        "bbls_received": "barrels received",
        "total_bbls": "total barrels",
        "rate_per_bbl": "rate per barrel",
        "lease_location": "lease",
        "truck_id": "truck",
    }
    return mapping.get(field or "", field or "value")


def _format_value(field: str | None, value: float, metric_kind: str = "sum") -> str:
    if metric_kind == "count" or field is None:
        return f"{int(value):,}"
    if field and any(w in field for w in ("amount", "revenue", "invoice", "billed", "paid", "total")) and "bbl" not in field:
        return f"${value:,.2f}"
    return f"{value:,.2f}"


def _metric_label(metric_kind: str, numeric_field: str | None) -> str:
    if metric_kind == "count":
        return "loads"
    return _friendly_field_name(numeric_field)


def _aggregate(records: list[dict], metric_kind: str, numeric_field: str | None) -> tuple[float, list[dict]]:
    if metric_kind == "count":
        return float(len(records)), records
    if not numeric_field:
        return 0.0, []
    pairs = [(v, r) for r in records if (v := _to_float(r.get(numeric_field))) is not None]
    if metric_kind == "avg":
        return (sum(v for v, _ in pairs) / len(pairs), [r for _, r in pairs]) if pairs else (0.0, [])
    return sum(v for v, _ in pairs), [r for _, r in pairs]


def _run_date_group_calc(q: str, records: list[dict], entity: str | None, metric_kind: str, numeric_field: str | None) -> str:
    mode = _date_grouping(q) or "date"
    date_field = _resolve_date_field(records)
    if not date_field:
        return f"{VERSION_MARKER} Could not find a usable date field. Expected fields like date, ticket_date, invoice_date, service_date, or created_at."

    filtered = [r for r in records if _record_matches_entity(r, entity)] if entity else records
    if entity and not filtered:
        return f'{VERSION_MARKER} No rows matched "{entity}" in structured table data. Searched {len(records)} parsed rows.'
    filtered = _filter_records_for_requested_shape(q, filtered, numeric_field, None, entity)

    buckets: dict[str, list[dict]] = {}
    skipped = 0
    for r in filtered:
        d = _parse_date_value(r.get(date_field))
        if not d:
            skipped += 1
            continue
        if metric_kind != "count" and numeric_field and _to_float(r.get(numeric_field)) is None:
            continue
        buckets.setdefault(_date_group_key(d, mode), []).append(r)

    if not buckets:
        return f'{VERSION_MARKER} Found matching rows, but no rows had both a parseable "{date_field}" value and the requested metric.'

    label = _metric_label(metric_kind, numeric_field)
    entity_clause = f' for "{entity}"' if entity else ""
    lines = [f"{VERSION_MARKER} {label.capitalize()} breakdown by {mode}{entity_clause}:", ""]
    for key in sorted(buckets):
        total, used_rows = _aggregate(buckets[key], metric_kind, numeric_field)
        examples = "; ".join(_source_label(r) for r in used_rows[:3])
        lines.append(f"- {key}: {_format_value(numeric_field, total, metric_kind)} ({len(used_rows)} row{'s' if len(used_rows) != 1 else ''})" + (f" — sources: {examples}" if examples else ""))
    if skipped:
        lines.append(f"\nNote: skipped {skipped} row{'s' if skipped != 1 else ''} without a parseable {date_field} value.")
    return "\n".join(lines)


def _run_compare_calc(q: str, records: list[dict], entities: list[str], metric_kind: str, numeric_field: str | None) -> str:
    if len(entities) < 2:
        return f"{VERSION_MARKER} I could not safely identify two entities to compare. Try: 'Compare Reeves 14H and Cimarron Unit 7 by revenue.'"

    results = []
    for entity in entities[:2]:
        matched = [r for r in records if _record_matches_entity(r, entity)]
        matched = _filter_records_for_requested_shape(q, matched, numeric_field, None, entity)
        if metric_kind != "count" and numeric_field:
            matched = [r for r in matched if _to_float(r.get(numeric_field)) is not None]
        total, used_rows = _aggregate(matched, metric_kind, numeric_field)
        results.append({"entity": entity, "total": total, "rows": used_rows})

    label = _metric_label(metric_kind, numeric_field)
    a, b = results[0], results[1]
    diff = abs(a["total"] - b["total"])
    leader = a if a["total"] >= b["total"] else b

    lines = [f"{VERSION_MARKER} Comparison by {label}:", ""]
    for item in results:
        examples = "; ".join(_source_label(r) for r in item["rows"][:5])
        lines.append(f"- {item['entity']}: {_format_value(numeric_field, item['total'], metric_kind)} ({len(item['rows'])} row{'s' if len(item['rows']) != 1 else ''})" + (f" — sources: {examples}" if examples else ""))
    result_line = "Result: tie" if diff == 0 else f"Higher: {leader['entity']}"
    lines.extend(["", f"Difference: {_format_value(numeric_field, diff, metric_kind)}", result_line])
    return "\n".join(lines)


def _run_calc(q: str, records: list[dict], entity: str | None, numeric_field: str | None, group_field: str | None, intent: str) -> str:
    if not records:
        return f"{VERSION_MARKER} No structured table rows found."

    metric_kind, metric_field = _resolve_metric(q, records)
    numeric_field = metric_field if metric_kind != "count" else None

    if intent == "date_group":
        return _run_date_group_calc(q, records, entity, metric_kind, numeric_field)

    filtered = [r for r in records if _record_matches_entity(r, entity)] if entity else records
    if entity and not filtered:
        return f'{VERSION_MARKER} No rows matched "{entity}" in structured table data. Searched {len(records)} parsed rows.'
    filtered = _filter_records_for_requested_shape(q, filtered, numeric_field, group_field, entity)
    total_rows = len(filtered)

    if intent == "rank" or (not entity and group_field):
        gf = group_field
        if not gf:
            return f"{VERSION_MARKER} Could not determine a grouping field. Try 'by client', 'by lease', 'by truck', or 'by driver'."
        groups: dict[str, list[dict]] = {}
        excluded_unknown = 0
        for r in filtered:
            raw_key = r.get(gf)
            if _is_unknown_group_value(raw_key):
                excluded_unknown += 1
                continue
            if metric_kind != "count" and numeric_field and _to_float(r.get(numeric_field)) is None:
                continue
            groups.setdefault(str(raw_key).strip(), []).append(r)
        if not groups:
            return f'{VERSION_MARKER} Found {total_rows} rows, but no usable values for grouping field "{gf}".'
        totals = []
        for key, rows in groups.items():
            total, used_rows = _aggregate(rows, metric_kind, numeric_field)
            totals.append((key, total, used_rows))
        reverse = "lowest" not in (q or "").lower() and "least" not in (q or "").lower()
        totals.sort(key=lambda x: x[1], reverse=reverse)
        grand_total = sum(t[1] for t in totals) if metric_kind != "avg" else 0.0
        label = _metric_label(metric_kind, numeric_field)
        header_total = f", grand total: {_format_value(numeric_field, grand_total, metric_kind)}" if metric_kind != "avg" else ""
        lines = [f"{VERSION_MARKER} {label} by {_friendly_field_name(gf)} ({total_rows} rows{header_total}):", ""]
        for i, (key, total, used_rows) in enumerate(totals[:25], 1):
            sample = "; ".join(_source_label(r) for r in used_rows[:2])
            lines.append(f"{i}. {key} — {_format_value(numeric_field, total, metric_kind)} ({len(used_rows)} row{'s' if len(used_rows) != 1 else ''})" + (f" — examples: {sample}" if sample else ""))
        if excluded_unknown:
            lines.append(f"\nNote: excluded {excluded_unknown} Unknown/blank group row{'s' if excluded_unknown != 1 else ''}.")
        return "\n".join(lines)

    if intent == "count" or metric_kind == "count":
        entity_clause = f' for "{entity}"' if entity else ""
        examples = ", ".join(_source_label(r) for r in filtered[:10])
        return f"{VERSION_MARKER} Found {total_rows} matching load/ticket row{'s' if total_rows != 1 else ''}{entity_clause}." + (f"\nSource rows: {examples}" if examples else "")

    if not numeric_field:
        return f"{VERSION_MARKER} Found {total_rows} matching rows, but could not determine which numeric field to calculate. Try 'total revenue', 'barrels hauled', or 'loads'."

    values = [(v, r) for r in filtered if (v := _to_float(r.get(numeric_field))) is not None]
    if not values:
        return f'{VERSION_MARKER} Found {total_rows} rows, but field "{numeric_field}" had no numeric values.'
    total = sum(v for v, _ in values)
    entity_clause = f' for "{entity}"' if entity else ""
    if intent == "avg" or metric_kind == "avg":
        avg = total / len(values)
        lines = [f"{VERSION_MARKER} Average {_friendly_field_name(numeric_field)}{entity_clause}: {_format_value(numeric_field, avg, 'avg')}", f"Based on {len(values)} numeric row{'s' if len(values) != 1 else ''}; total {_format_value(numeric_field, total)}."]
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

        intent = _calc_intent(question)
        metric_kind, numeric_field = _resolve_metric(question, all_records)

        if intent == "compare":
            entities = _extract_entities(question, all_records, max_entities=2)
            return _run_compare_calc(question, all_records, entities, metric_kind, numeric_field)

        entity = _extract_entity(question, all_records)
        group_field = _resolve_group_field(question, all_records) if intent == "rank" or (not entity and intent != "date_group") else None
        return _run_calc(question, all_records, entity, numeric_field, group_field, intent)
    except Exception as e:
        import traceback
        print(f"structured_calc failed: {e}")
        print(traceback.format_exc())
        return f"{VERSION_MARKER} Calculation failed internally. Try rephrasing as 'total revenue from Reeves 14H', 'show revenue by date for Reeves 14H', or 'compare Reeves 14H and Cimarron Unit 7 by loads'."


def detect_calc_intent(q: str) -> bool:
    raw = (q or "").lower().replace("?", "").replace("'", "")
    ql = " ".join(raw.split())
    padded = f" {ql} "

    if any(p in ql for p in ALL_CALC_PATTERNS):
        return True
    if any(p.strip() in ql for p in CALC_DATE_PATTERNS):
        return True
    if any(p in padded for p in (" vs ", " versus ")):
        return True
    if re.search(r"compare\s+.+\s+(and|to|with|versus|vs\.?)+\s+.+\s+by\s+", ql):
        return True
    if re.search(r"how\s+does\s+.+\s+compare\s+(to|with)\s+.+", ql):
        return True
    if re.search(r"how\s+many\s+(bbls?|barrels?)", ql):
        return True
    if re.search(r"(bbls?|barrels?)\s+(were\s+)?hauled", ql):
        return True
    if re.search(r"(highest|lowest|most|least).*(revenue|amount|bbls?|barrels?|loads?|runs?|tickets?)", ql):
        return True
    if re.search(r"(total|sum).*(revenue|amount|bbls?|barrels?)", ql):
        return True

    return False
