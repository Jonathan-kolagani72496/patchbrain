"""
PDF parser using PyMuPDF (fitz).

Improvements over v1:
- Table extraction via page.find_tables() — critical for invoices / run tickets
- Scanned-page detection with pytesseract OCR fallback
- Sentence-boundary splitting (no more mid-word cuts)
- Multi-column layout handled via reading-order sort on blocks
- Repeated header/footer strings suppressed across pages
- Richer metadata per chunk
"""

import re
import io

import fitz  # PyMuPDF >= 1.23 required for find_tables()

# Optional OCR — gracefully disabled if pytesseract / Pillow not installed
try:
    import pytesseract
    from PIL import Image
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

CHUNK_TARGET   = 900   # target chars per text chunk
CHUNK_OVERLAP  = 120   # overlap between consecutive text chunks
MIN_TEXT_LEN   = 15    # ignore blocks shorter than this (noise / stray characters)
MIN_PAGE_CHARS = 30    # if a page yields fewer chars, treat as scanned


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_header_footer(text: str, page_height: float, y0: float, y1: float) -> bool:
    """True if block sits in the top/bottom 7% margin — likely a running header or footer."""
    margin = page_height * 0.07
    return y1 < margin or y0 > page_height - margin


def _sort_reading_order(blocks: list) -> list:
    """
    Sort text blocks into natural reading order for two-column layouts.
    Splits the page at the horizontal midpoint, sorts each half top-to-bottom,
    then concatenates left column before right column.
    """
    if not blocks:
        return blocks
    page_width = max(b[2] for b in blocks) if blocks else 1
    mid   = page_width / 2
    left  = sorted([b for b in blocks if b[0] < mid],  key=lambda b: b[1])
    right = sorted([b for b in blocks if b[0] >= mid], key=lambda b: b[1])
    return left + right


def _split_sentences(text: str, target: int, overlap: int) -> list[str]:
    """
    Split text into chunks of ~target chars, breaking only at sentence boundaries.
    Maintains an overlap window so context doesn't vanish at chunk edges.
    """
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for sent in sentences:
        if current_len + len(sent) > target and current:
            chunks.append(" ".join(current))
            # Carry overlap: keep last sentences up to ~overlap chars
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


def _ocr_page(page: fitz.Page) -> str:
    """Rasterize the page and OCR it. Returns extracted text or empty string."""
    if not OCR_AVAILABLE:
        return ""
    try:
        pix  = page.get_pixmap(dpi=200)
        img  = Image.open(io.BytesIO(pix.tobytes("png")))
        text = pytesseract.image_to_string(img, config="--psm 6")
        return text.strip()
    except Exception:
        return ""


def _extract_tables(page: fitz.Page, page_label: int, total: int) -> list[dict]:
    """
    Extract tabular data using PyMuPDF's built-in table finder (requires >= 1.23).
    Each table becomes one chunk formatted as header=value rows — same style as
    the Excel parser so Claude sees consistent formatting across file types.
    """
    chunks: list[dict] = []
    try:
        tabs = page.find_tables()
    except AttributeError:
        return chunks  # PyMuPDF < 1.23 — skip silently

    for tab_idx, tab in enumerate(tabs):
        try:
            rows = tab.extract()
        except Exception:
            continue
        if not rows or len(rows) < 2:
            continue

        headers = [str(c).strip() if c else f"Col{j+1}" for j, c in enumerate(rows[0])]
        lines   = [f"[Page {page_label}, Table {tab_idx + 1}]",
                   "Columns: " + " | ".join(headers), ""]

        for row in rows[1:]:
            parts = [f"{h}={str(cell).strip()}"
                     for h, cell in zip(headers, row)
                     if cell and str(cell).strip()]
            if parts:
                lines.append(" | ".join(parts))

        content = "\n".join(lines)
        if len(content) < MIN_TEXT_LEN:
            continue

        chunks.append({
            "content":        content,
            "source_type":    "pdf_table",
            "page_number":    page_label,
            "sheet_name":     None,
            "row_start":      None,
            "row_end":        None,
            "bbox_json":      None,
            "metadata_jsonb": {
                "total_pages": total,
                "table_index": tab_idx,
                "row_count":   len(rows) - 1,
                "col_count":   len(headers),
            },
        })

    return chunks


# ── Main parser ────────────────────────────────────────────────────────────────

def parse_pdf(path: str) -> list[dict]:
    chunks: list[dict] = []
    doc   = fitz.open(path)
    total = len(doc)

    # ── Pass 1: identify repeated header/footer strings (appear on 3+ pages) ──
    hf_counts: dict[str, int] = {}
    for pg in doc:
        for block in pg.get_text("blocks"):
            x0, y0, x1, y1, text, _, btype = block
            if btype != 0:
                continue
            text = text.strip()
            if text and _is_header_footer(text, pg.rect.height, y0, y1):
                hf_counts[text] = hf_counts.get(text, 0) + 1
    repeated_hf = {t for t, c in hf_counts.items() if c >= 3}

    # ── Pass 2: extract content page by page ──────────────────────────────────
    for page_num in range(total):
        page        = doc[page_num]
        page_label  = page_num + 1

        # Tables first — they get their own chunks
        table_chunks = _extract_tables(page, page_label, total)
        chunks.extend(table_chunks)

        # Record table bboxes so we don't re-extract their text as prose
        table_rects: list[fitz.Rect] = []
        try:
            for tab in page.find_tables():
                table_rects.append(tab.bbox)
        except AttributeError:
            pass

        # Collect text blocks, excluding headers/footers and table areas
        raw_blocks  = page.get_text("blocks")
        text_blocks = []
        for block in raw_blocks:
            x0, y0, x1, y1, text, _, btype = block
            if btype != 0:
                continue
            text = text.strip()
            if not text or len(text) < MIN_TEXT_LEN:
                continue
            if text in repeated_hf:
                continue
            if any(fitz.Rect(x0, y0, x1, y1).intersects(tr) for tr in table_rects):
                continue
            text_blocks.append(block)

        text_blocks = _sort_reading_order(text_blocks)
        combined    = "\n\n".join(b[4].strip() for b in text_blocks)
        page_bboxes = [{"x0": round(b[0], 1), "y0": round(b[1], 1),
                        "x1": round(b[2], 1), "y1": round(b[3], 1)}
                       for b in text_blocks]

        # OCR fallback for scanned / image-only pages
        if len(combined) < MIN_PAGE_CHARS:
            combined = _ocr_page(page)
            if not combined:
                continue
            source_type = "pdf_ocr"
            page_bboxes = []
        else:
            source_type = "pdf_text"

        # Emit chunk(s)
        if len(combined) <= CHUNK_TARGET:
            chunks.append({
                "content":        f"[Page {page_label}]\n{combined}",
                "source_type":    source_type,
                "page_number":    page_label,
                "sheet_name":     None,
                "row_start":      None,
                "row_end":        None,
                "bbox_json":      page_bboxes,
                "metadata_jsonb": {"total_pages": total},
            })
        else:
            parts = _split_sentences(combined, CHUNK_TARGET, CHUNK_OVERLAP)
            for idx, part in enumerate(parts):
                chunks.append({
                    "content":        f"[Page {page_label}, part {idx + 1}/{len(parts)}]\n{part}",
                    "source_type":    source_type,
                    "page_number":    page_label,
                    "sheet_name":     None,
                    "row_start":      None,
                    "row_end":        None,
                    "bbox_json":      page_bboxes,
                    "metadata_jsonb": {
                        "total_pages": total,
                        "part":        idx + 1,
                        "total_parts": len(parts),
                    },
                })

    doc.close()
    return chunks
