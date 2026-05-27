"""
CSV parser for PatchBrain.

Uses DuckDB read_csv_auto so oilfield exports with varied delimiters/quoting
are handled better than a naive csv.reader. Produces row-group chunks similar
to the Excel parser.

Output source_type: csv_rows
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb


ROWS_PER_CHUNK = 25
MIN_ROW_LENGTH = 2
MAX_CELL_CHARS = 300
MAX_COLUMNS = 100


def _sql_path(path: str) -> str:
    """Safely quote a local file path for DuckDB SQL."""
    return "'" + path.replace("'", "''") + "'"


def _cell_str(val: Any) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    if len(s) > MAX_CELL_CHARS:
        s = s[:MAX_CELL_CHARS] + "…"
    return s


def _clean_col_name(name: Any, idx: int) -> str:
    s = str(name or "").strip()
    if not s or s.lower().startswith("column"):
        return f"Column_{idx + 1}"
    if len(s) > 80:
        return s[:80] + "…"
    return s


def _dedupe_columns(cols: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []

    for col in cols:
        base = col or "Column"
        count = seen.get(base, 0)
        seen[base] = count + 1
        out.append(base if count == 0 else f"{base}_{count + 1}")

    return out


def _read_csv_table(con: duckdb.DuckDBPyConnection, path: str) -> tuple[list[str], list[tuple], int]:
    """Read CSV as all strings.

    First attempt assumes a header row. Fallback handles no-header or weird files.
    """
    qpath = _sql_path(path)

    attempts = [
        f"""
        select *
        from read_csv_auto(
            {qpath},
            header=true,
            all_varchar=true,
            ignore_errors=true,
            sample_size=20480
        )
        """,
        f"""
        select *
        from read_csv_auto(
            {qpath},
            header=false,
            all_varchar=true,
            ignore_errors=true,
            sample_size=20480
        )
        """,
    ]

    last_error: Exception | None = None

    for attempt_index, query in enumerate(attempts):
        try:
            rel = con.sql(query)
            rows = rel.fetchall()
            raw_cols = [d[0] for d in rel.description]

            if not rows and not raw_cols:
                continue

            cols = _dedupe_columns([
                _clean_col_name(c, i) for i, c in enumerate(raw_cols[:MAX_COLUMNS])
            ])

            trimmed_rows = [tuple(r[:MAX_COLUMNS]) for r in rows]
            header_row_number = 1 if attempt_index == 0 else 0
            return cols, trimmed_rows, header_row_number

        except Exception as e:
            last_error = e

    raise ValueError(f"Could not parse CSV file: {last_error}")


def parse_csv(path: str) -> list[dict]:
    chunks: list[dict] = []
    filename = Path(path).name

    con = duckdb.connect(database=":memory:", read_only=False)

    try:
        col_names, rows, header_row_number = _read_csv_table(con, path)
    finally:
        con.close()

    if not rows:
        return []

    total_rows = len(rows)

    for chunk_start in range(0, total_rows, ROWS_PER_CHUNK):
        chunk_rows = rows[chunk_start: chunk_start + ROWS_PER_CHUNK]

        meaningful: list[tuple[int, tuple]] = []
        for local_idx, row in enumerate(chunk_rows):
            non_empty = [c for c in row if _cell_str(c)]
            if len(non_empty) >= MIN_ROW_LENGTH:
                # Actual source row number. If header exists, data starts at row 2.
                source_row_num = chunk_start + local_idx + 1 + header_row_number
                meaningful.append((source_row_num, row))

        if not meaningful:
            continue

        lines = [
            f"CSV file: {filename}",
            "Columns: " + " | ".join(col_names),
            "",
        ]

        for source_row_num, row in meaningful:
            parts: list[str] = []
            for col_name, cell_val in zip(col_names, row):
                val = _cell_str(cell_val)
                if val:
                    parts.append(f"{col_name}={val}")
            if parts:
                lines.append(f"Row {source_row_num}: " + " | ".join(parts))

        row_start = meaningful[0][0]
        row_end = meaningful[-1][0]

        chunks.append({
            "content": "\n".join(lines),
            "source_type": "csv_rows",
            "page_number": None,
            "sheet_name": None,
            "row_start": row_start,
            "row_end": row_end,
            "bbox_json": None,
            "metadata_jsonb": {
                "parser": "duckdb_read_csv_auto",
                "filename": filename,
                "column_names": col_names,
                "column_count": len(col_names),
                "total_data_rows": total_rows,
                "header_row_number": header_row_number,
                "rows_per_chunk": ROWS_PER_CHUNK,
            },
        })

    return chunks
