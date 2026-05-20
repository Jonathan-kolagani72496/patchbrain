"""
PDF parser using PyMuPDF.
Returns a list of chunk dicts with content + citation metadata.
"""

import fitz  # PyMuPDF


def parse_pdf(path: str) -> list[dict]:
    """
    Parses a PDF into text chunks.
    Each chunk corresponds to roughly one page's worth of text blocks.
    Stores page number and bounding box data for citations.
    """
    chunks = []
    doc = fitz.open(path)

    for page_num in range(len(doc)):
        page = doc[page_num]

        # Get text blocks with positions
        blocks = page.get_text("blocks")  # [(x0, y0, x1, y1, text, block_no, block_type)]

        page_text_parts = []
        page_bboxes = []

        for block in blocks:
            x0, y0, x1, y1, text, block_no, block_type = block
            if block_type != 0:  # 0 = text, 1 = image
                continue
            text = text.strip()
            if not text or len(text) < 10:  # skip headers/noise
                continue
            page_text_parts.append(text)
            page_bboxes.append({
                "x0": round(x0, 1), "y0": round(y0, 1),
                "x1": round(x1, 1), "y1": round(y1, 1),
            })

        if not page_text_parts:
            continue

        combined = "\n\n".join(page_text_parts)

        # If page is short, keep it as one chunk
        # If page is long (>1200 chars), split at natural breaks
        if len(combined) <= 1200:
            chunks.append({
                "content": f"[Page {page_num + 1}]\n{combined}",
                "source_type": "pdf_text",
                "page_number": page_num + 1,
                "bbox_json": page_bboxes,
                "sheet_name": None,
                "row_start": None,
                "row_end": None,
                "metadata_jsonb": {"total_pages": len(doc)},
            })
        else:
            # Split long pages into ~800-char chunks with overlap
            chunk_size = 800
            overlap = 80
            i = 0
            part = 0
            while i < len(combined):
                text_slice = combined[i : i + chunk_size]
                chunks.append({
                    "content": f"[Page {page_num + 1}, part {part + 1}]\n{text_slice}",
                    "source_type": "pdf_text",
                    "page_number": page_num + 1,
                    "bbox_json": page_bboxes,
                    "sheet_name": None,
                    "row_start": None,
                    "row_end": None,
                    "metadata_jsonb": {
                        "total_pages": len(doc),
                        "char_start": i,
                        "char_end": i + len(text_slice),
                    },
                })
                i += chunk_size - overlap
                part += 1

    doc.close()
    return chunks