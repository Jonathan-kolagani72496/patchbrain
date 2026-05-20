"""
WhatsApp export parser for PatchBrain.
Handles the .txt format from WhatsApp's built-in Export Chat feature.

Format examples:
  [6/14/25, 8:23:45 AM] Jose Rodriguez: 3 loads Reeves 14H, all signed
  6/14/25, 8:23:45 AM - Jose Rodriguez: pump changed on T-04
  [2025-06-14 08:23:45] Rodriguez: ran 4 trucks today Cimarron
"""

import re
from datetime import datetime
from typing import Optional


# All known WhatsApp timestamp formats
TIMESTAMP_PATTERNS = [
    r'\[(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)\]',
    r'(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)\s*-',
    r'\[(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}(?::\d{2})?)\]',
]

MESSAGE_RE = re.compile(
    r'(?:'
    r'\[(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)\]\s+'
    r'|(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)\s+-\s+'
    r'|(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}(?::\d{2})?)\s+-\s+'
    r')'
    r'([^:]+?):\s+(.*)',
    re.DOTALL
)

# Keywords that suggest operational rig data
RIG_KEYWORDS = [
    'load', 'loads', 'ticket', 'bbl', 'barrel', 'truck', 'run',
    'pump', 'signed', 'approved', 'job', 'lease', 'rig', 'well',
    'safety', 'h2s', 'equipment', 'changed', 'replaced', 'repair',
    'service', 'maintenance', 'hours', 'down', 'shutdown', 'complete',
]


def _parse_timestamp(date_str: str, time_str: str) -> Optional[str]:
    """Try multiple date formats and return ISO string."""
    combined = f"{date_str} {time_str}".strip()
    formats = [
        "%m/%d/%y %I:%M:%S %p", "%m/%d/%y %I:%M %p",
        "%m/%d/%y %H:%M:%S",    "%m/%d/%y %H:%M",
        "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %H:%M:%S",    "%m/%d/%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",    "%Y-%m-%d %H:%M",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(combined, fmt).isoformat()
        except ValueError:
            continue
    return combined  # fallback: return raw string


def parse_whatsapp(path: str) -> list[dict]:
    """
    Parses a WhatsApp .txt export into chunks.
    Each chunk = one message, with sender + timestamp + content.
    Groups consecutive messages from the same sender if they're short.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()

    # Split into lines and reconstruct messages
    # (messages can span multiple lines)
    lines       = raw.split("\n")
    messages    = []
    current_msg = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        match = MESSAGE_RE.match(line)
        if match:
            # Save previous message
            if current_msg:
                messages.append(current_msg)

            # Parse groups: (date1, time1) or (date2, time2) or (date3, time3)
            g = match.groups()
            if g[0]:    # Format 1: [date, time]
                ts = _parse_timestamp(g[0], g[1])
            elif g[2]:  # Format 2: date, time -
                ts = _parse_timestamp(g[2], g[3])
            else:       # Format 3: date time -
                ts = _parse_timestamp(g[4], g[5])

            sender  = (g[6] or "").strip()
            content = (g[7] or "").strip()

            # Skip system messages
            if sender.lower() in ("", "messages and calls are end-to-end encrypted"):
                current_msg = None
                continue

            current_msg = {
                "timestamp": ts,
                "sender":    sender,
                "content":   content,
                "is_rig_data": _is_rig_data(content),
            }
        else:
            # Continuation of previous message
            if current_msg:
                current_msg["content"] += "\n" + line

    if current_msg:
        messages.append(current_msg)

    return _messages_to_chunks(messages)


def _is_rig_data(text: str) -> bool:
    """Returns True if message likely contains operational rig data."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in RIG_KEYWORDS)


def _messages_to_chunks(messages: list[dict]) -> list[dict]:
    """
    Converts messages to PatchBrain chunks.
    Groups nearby messages (within ~1 hour) from the same context.
    Each chunk is ~5-10 messages to give Claude enough context.
    """
    if not messages:
        return []

    chunks       = []
    GROUP_SIZE   = 8   # messages per chunk
    OVERLAP      = 2   # overlap between chunks for context continuity

    i = 0
    while i < len(messages):
        group  = messages[i : i + GROUP_SIZE]
        lines  = []
        senders = set()

        for msg in group:
            ts      = msg.get("timestamp", "")
            sender  = msg.get("sender", "")
            content = msg.get("content", "")
            senders.add(sender)
            lines.append(f"[{ts}] {sender}: {content}")

        content_text = "\n".join(lines)

        # Tag if this group contains rig data
        has_rig_data = any(m.get("is_rig_data") for m in group)

        chunks.append({
            "content":     content_text,
            "source_type": "whatsapp_message",
            "page_number": None,
            "sheet_name":  None,
            "row_start":   None,
            "row_end":     None,
            "bbox_json":   None,
            "metadata_jsonb": {
                "message_count":    len(group),
                "senders":          list(senders),
                "sender":           group[0].get("sender", "") if group else "",
                "timestamp_start":  group[0].get("timestamp", "") if group else "",
                "timestamp_end":    group[-1].get("timestamp", "") if group else "",
                "has_rig_data":     has_rig_data,
                "group_index":      i // (GROUP_SIZE - OVERLAP),
            }
        })

        i += GROUP_SIZE - OVERLAP

    return chunks
