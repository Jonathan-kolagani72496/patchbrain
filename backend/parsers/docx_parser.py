"""
DOCX parser using python-docx.
Splits on headings for section-aware chunks.
"""

from docx import Document


MIN_CHUNK_CHARS = 200
MAX_CHUNK_CHARS = 900


def parse_docx(path: str) -> list[dict]:
    doc = Document(path)
    chunks = []

    current_heading = None
    current_text_parts = []

    def flush(heading, parts):
        if not parts:
            return
        content = "\n\n".join(parts).strip()
        if len(content) < MIN_CHUNK_CHARS:
            return
        prefix = f"[Section: {heading}]\n" if heading else ""
        chunks.append({
            "content": prefix + content,
            "source_type": "docx_section",
            "page_number": None,
            "sheet_name": None,
            "row_start": None,
            "row_end": None,
            "bbox_json": None,
            "metadata_jsonb": {"heading": heading},
        })

    for para in doc.paragraphs:
        style_name = para.style.name if para.style else ""
        text = para.text.strip()

        if not text:
            continue

        if "Heading" in style_name:
            # Flush previous section
            flush(current_heading, current_text_parts)
            current_heading = text
            current_text_parts = []
        else:
            current_text_parts.append(text)
            # If current chunk is getting large, flush it
            combined = "\n".join(current_text_parts)
            if len(combined) > MAX_CHUNK_CHARS:
                flush(current_heading, current_text_parts)
                current_text_parts = []

    flush(current_heading, current_text_parts)
    return chunks