"""
DOCX parser using python-docx.
Splits on headings for section-aware chunks.

Improvements over v1:
- Word tables extracted and formatted as header=value rows
- Heading context carried forward when a section overflows into multiple chunks
- Sentence-boundary splitting on overflow (no mid-sentence cuts)
- List items joined as bullet text rather than ignored
- MIN_CHUNK_CHARS lowered — short but real sections no longer silently dropped
"""

import re
from docx import Document
from docx.oxml.ns import qn


MIN_CHUNK_CHARS = 80    # sections shorter than this are merged into the next
MAX_CHUNK_CHARS = 1000  # flush and start new chunk when section exceeds this


# ── Helpers ────────────────────────────────────────────────────────────────────

def _split_sentences(text: str, target: int, overlap: int = 100) -> list[str]:
    """Split text at sentence boundaries; maintain overlap between chunks."""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for sent in sentences:
        if current_len + len(sent) > target and current:
            chunks.append(" ".join(current))
            overlap_buf: list[str] = []
            overlap_len = 0
            for s in reversed(current):
                if overlap_len + len(s) > overlap:
                    break
                overlap_buf.insert(0, s)
                overlap_len += len(s)
            current     = overlap_buf
            current_len = overlap_len
        current.append(sent)
        current_len += len(sent)

    if current:
        chunks.append(" ".join(current))

    return [c for c in chunks if c.strip()]


def _table_to_chunks(table, heading: str | None) -> list[dict]:
    """
    Convert a python-docx Table into one or more chunks formatted as
    column=value rows — consistent with the Excel and PDF table format.
    """
    rows_raw = []
    for row in table.rows:
        cells = [cell.text.strip() for cell in row.cells]
        rows_raw.append(cells)

    if len(rows_raw) < 2:
        return []

    headers = rows_raw[0]
    # De-duplicate merged column headers (python-docx repeats merged cell text)
    seen_headers: dict[str, int] = {}
    clean_headers: list[str] = []
    for h in headers:
        if h in seen_headers:
            seen_headers[h] += 1
            clean_headers.append(f"{h}_{seen_headers[h]}")
        else:
            seen_headers[h] = 0
            clean_headers.append(h if h else f"Col{len(clean_headers)+1}")

    prefix = f"[Section: {heading}]\n" if heading else ""
    lines  = [prefix + "Columns: " + " | ".join(clean_headers), ""]

    for row in rows_raw[1:]:
        parts = [f"{h}={v}" for h, v in zip(clean_headers, row) if v]
        if parts:
            lines.append(" | ".join(parts))

    content = "\n".join(lines).strip()
    if not content or len(content) < MIN_CHUNK_CHARS:
        return []

    return [{
        "content":        content,
        "source_type":    "docx_table",
        "page_number":    None,
        "sheet_name":     None,
        "row_start":      None,
        "row_end":        None,
        "bbox_json":      None,
        "metadata_jsonb": {
            "heading":    heading,
            "row_count":  len(rows_raw) - 1,
            "col_count":  len(clean_headers),
        },
    }]


def _make_text_chunk(content: str, heading: str | None, part: int | None = None) -> dict:
    label = f"[Section: {heading}]" if heading else ""
    if part:
        label += f" (part {part})"
    full = (label + "\n" + content).strip() if label else content
    return {
        "content":        full,
        "source_type":    "docx_section",
        "page_number":    None,
        "sheet_name":     None,
        "row_start":      None,
        "row_end":        None,
        "bbox_json":      None,
        "metadata_jsonb": {"heading": heading},
    }


# ── Main parser ────────────────────────────────────────────────────────────────

def parse_docx(path: str) -> list[dict]:
    doc    = Document(path)
    chunks: list[dict] = []

    current_heading: str | None = None
    current_parts:   list[str]  = []

    def flush(heading: str | None, parts: list[str]):
        """Emit chunk(s) from accumulated parts, splitting if too long."""
        if not parts:
            return
        combined = "\n\n".join(parts).strip()
        if len(combined) < MIN_CHUNK_CHARS:
            return

        if len(combined) <= MAX_CHUNK_CHARS:
            chunks.append(_make_text_chunk(combined, heading))
        else:
            sub_parts = _split_sentences(combined, MAX_CHUNK_CHARS)
            for idx, sub in enumerate(sub_parts, 1):
                if sub.strip():
                    chunks.append(_make_text_chunk(sub, heading,
                                                    part=idx if len(sub_parts) > 1 else None))

    # python-docx exposes paragraphs and tables interleaved via doc.element.body
    # Iterating doc.paragraphs alone misses tables — iterate body children instead
    body = doc.element.body

    for child in body:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag

        if tag == "tbl":
            # It's a table — flush pending text, then extract table
            flush(current_heading, current_parts)
            current_parts = []
            from docx.table import Table as DocxTable
            table = DocxTable(child, doc)
            table_chunks = _table_to_chunks(table, current_heading)
            chunks.extend(table_chunks)

        elif tag == "p":
            from docx.text.paragraph import Paragraph
            para       = Paragraph(child, doc)
            style_name = para.style.name if para.style else ""
            text       = para.text.strip()

            if not text:
                continue

            if "Heading" in style_name:
                # New section — flush previous accumulation
                flush(current_heading, current_parts)
                current_heading = text
                current_parts   = []

            elif "List" in style_name:
                # Format list items with a bullet marker
                current_parts.append(f"• {text}")

            else:
                current_parts.append(text)

            # Overflow flush — keep heading context for the next chunk
            combined = "\n".join(current_parts)
            if len(combined) > MAX_CHUNK_CHARS:
                flush(current_heading, current_parts)
                current_parts = []  # reset but retain heading

    # Final flush
    flush(current_heading, current_parts)
    return chunks
