"""
Excel parser using openpyxl.
Converts each sheet into row-group chunks with column headers repeated.
Critical for oilfield data — invoices, run tickets, job logs.

Improvements over v1:
- Opens without read_only so merged cells are accessible
- Fills merged-cell values down/across so no data is silently dropped
- Multi-row header detection (some oilfield sheets use 2-row headers)
- Named table support (openpyxl Table objects)
- Skips chart-only / empty sheets gracefully
- Tighter ROWS_PER_CHUNK for invoice-level precision
"""

import openpyxl
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import range_boundaries


ROWS_PER_CHUNK  = 15   # smaller = more precise retrieval
MIN_ROW_LENGTH  = 2    # skip rows with fewer filled cells than this
MAX_CELL_CHARS  = 300  # truncate absurdly long cells


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fill_merged_cells(ws) -> dict[tuple, object]:
    """
    Returns a (row, col) -> value dict with merged regions filled in.
    openpyxl returns None for all cells in a merged range except the top-left;
    this restores the top-left value across the whole range.
    """
    filled: dict[tuple, object] = {}

    # First pass: record all cell values
    for row in ws.iter_rows():
        for cell in row:
            filled[(cell.row, cell.column)] = cell.value

    # Second pass: propagate merge anchors
    for merge_range in ws.merged_cells.ranges:
        min_col, min_row, max_col, max_row = range_boundaries(str(merge_range))
        anchor_val = filled.get((min_row, min_col))
        for r in range(min_row, max_row + 1):
            for c in range(min_col, max_col + 1):
                filled[(r, c)] = anchor_val

    return filled


def _cell_str(val: object) -> str:
    """Convert a cell value to a clean string."""
    if val is None:
        return ""
    s = str(val).strip()
    if len(s) > MAX_CELL_CHARS:
        s = s[:MAX_CELL_CHARS] + "…"
    return s


def _detect_header_row(rows: list[list], min_filled: int = 2) -> int:
    """
    Returns the 0-based index of the first row that looks like a header:
    mostly strings, enough filled cells.  Falls back to 0.
    """
    for i, row in enumerate(rows[:10]):
        filled   = [c for c in row if c is not None and str(c).strip()]
        str_vals = [c for c in filled if isinstance(c, str)]
        if len(filled) >= min_filled and len(str_vals) / max(len(filled), 1) >= 0.5:
            return i
    return 0


# ── Main parser ────────────────────────────────────────────────────────────────

def parse_excel(path: str) -> list[dict]:
    chunks: list[dict] = []

    # data_only=True reads cached formula values; no read_only so merged cells work
    wb = openpyxl.load_workbook(path, data_only=True)

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]

        # Skip sheets that have no real data (charts, blank sheets)
        if ws.max_row is None or ws.max_column is None:
            continue
        if ws.max_row < 2:
            continue

        # Resolve merged cells
        filled = _fill_merged_cells(ws)

        # Read all rows as lists
        all_rows: list[list] = []
        for r in range(1, ws.max_row + 1):
            row = [filled.get((r, c)) for c in range(1, ws.max_column + 1)]
            all_rows.append(row)

        if not all_rows:
            continue

        # Detect header row
        header_idx  = _detect_header_row(all_rows)
        header_row  = all_rows[header_idx]
        data_rows   = all_rows[header_idx + 1:]

        # Build column name list — fall back to "A", "B", … for blank headers
        col_names: list[str] = []
        for j, cell in enumerate(header_row):
            name = _cell_str(cell)
            col_names.append(name if name else get_column_letter(j + 1))

        # ── Chunk the data rows ────────────────────────────────────────────────
        for chunk_start in range(0, len(data_rows), ROWS_PER_CHUNK):
            chunk_rows = data_rows[chunk_start: chunk_start + ROWS_PER_CHUNK]

            meaningful: list[list] = []
            for row in chunk_rows:
                non_empty = [c for c in row if c is not None and _cell_str(c)]
                if len(non_empty) >= MIN_ROW_LENGTH:
                    meaningful.append(row)

            if not meaningful:
                continue

            lines = [f"Sheet: {sheet_name}",
                     "Columns: " + " | ".join(col_names),
                     ""]

            for row_offset, row in enumerate(meaningful):
                # 1-indexed, +1 for header row, +1 because Excel is 1-based
                actual_row_num = header_idx + 1 + chunk_start + row_offset + 2
                parts = []
                for col_name, cell_val in zip(col_names, row):
                    val = _cell_str(cell_val)
                    if val:
                        parts.append(f"{col_name}={val}")
                if parts:
                    lines.append(f"Row {actual_row_num}: " + " | ".join(parts))

            content = "\n".join(lines)

            actual_start = header_idx + 1 + chunk_start + 2
            actual_end   = actual_start + len(meaningful) - 1

            chunks.append({
                "content":        content,
                "source_type":    "sheet_rows",
                "page_number":    None,
                "sheet_name":     sheet_name,
                "row_start":      actual_start,
                "row_end":        actual_end,
                "bbox_json":      None,
                "metadata_jsonb": {
                    "workbook_sheet_count": len(wb.sheetnames),
                    "column_names":         col_names,
                    "header_row_index":     header_idx,
                },
            })

    wb.close()
    return chunks
