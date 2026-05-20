"""
Excel parser using openpyxl.
Converts each sheet into row-group chunks with column headers repeated.
This is critical for oilfield data — invoices, run tickets, job logs.
"""

import openpyxl
from openpyxl.utils import get_column_letter


ROWS_PER_CHUNK = 20  # rows per chunk — smaller = more precise retrieval for invoices/tickets
MIN_ROW_LENGTH = 3   # skip rows where fewer than this many cells have values


def parse_excel(path: str) -> list[dict]:
    chunks = []

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]

        # Read all rows into memory (openpyxl read_only is forward-only)
        all_rows = []
        for row in ws.iter_rows(values_only=True):
            all_rows.append(row)

        if not all_rows:
            continue

        # Detect header row (first non-empty row)
        header_row = None
        data_start = 0
        for i, row in enumerate(all_rows):
            non_empty = [c for c in row if c is not None and str(c).strip()]
            if len(non_empty) >= MIN_ROW_LENGTH:
                header_row = row
                data_start = i + 1
                break

        if header_row is None:
            continue

        # Build column names
        col_names = []
        for j, cell in enumerate(header_row):
            name = str(cell).strip() if cell is not None else get_column_letter(j + 1)
            col_names.append(name or get_column_letter(j + 1))

        # Chunk the data rows
        data_rows = all_rows[data_start:]

        for chunk_start in range(0, len(data_rows), ROWS_PER_CHUNK):
            chunk_rows = data_rows[chunk_start : chunk_start + ROWS_PER_CHUNK]

            # Filter out nearly empty rows
            meaningful_rows = []
            for row in chunk_rows:
                non_empty = [c for c in row if c is not None and str(c).strip()]
                if len(non_empty) >= MIN_ROW_LENGTH:
                    meaningful_rows.append(row)

            if not meaningful_rows:
                continue

            # Format as text: "col1=val | col2=val | col3=val"
            lines = [f"Sheet: {sheet_name}"]
            lines.append("Columns: " + " | ".join(col_names))
            lines.append("")

            for row_offset, row in enumerate(meaningful_rows):
                actual_row_num = data_start + chunk_start + row_offset + 2  # 1-indexed, +1 for header
                parts = []
                for col_name, cell_val in zip(col_names, row):
                    if cell_val is not None and str(cell_val).strip():
                        parts.append(f"{col_name}={cell_val}")
                if parts:
                    lines.append(f"Row {actual_row_num}: " + " | ".join(parts))

            content = "\n".join(lines)

            actual_row_start = data_start + chunk_start + 2
            actual_row_end = actual_row_start + len(meaningful_rows) - 1

            chunks.append({
                "content": content,
                "source_type": "sheet_rows",
                "page_number": None,
                "sheet_name": sheet_name,
                "row_start": actual_row_start,
                "row_end": actual_row_end,
                "bbox_json": None,
                "metadata_jsonb": {
                    "workbook_sheet_count": len(wb.sheetnames),
                    "column_names": col_names,
                },
            })

    wb.close()
    return chunks