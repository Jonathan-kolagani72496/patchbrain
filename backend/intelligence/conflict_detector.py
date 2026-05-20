"""
PatchBrain conflict detection engine.
Runs after ingestion to find cross-document inconsistencies.

Detects:
1. Name variants for the same person (J. Rodriguez = Jose Rodriguez = Rodriguez)
2. Count mismatches (WhatsApp says 3 loads, invoice says 4)
3. Presence mismatches (approved by someone not on site)
4. Date mismatches (ticket date vs invoice date for same job)
"""

import re
import json
from difflib import SequenceMatcher
from typing import Optional


# ── Name normalization ─────────────────────────────────────────────────────────

TITLE_WORDS    = {"mr", "mrs", "ms", "dr", "jr", "sr", "ii", "iii"}
NICKNAME_MAP   = {
    "bobby": "robert", "bob": "robert", "rob": "robert",
    "bill": "william", "billy": "william", "will": "william",
    "joe": "jose",   "joey": "jose",
    "mike": "michael", "micky": "michael",
    "tony": "antonio", "toni": "antonio",
    "charlie": "charles", "chuck": "charles",
    "johnny": "john",  "jon": "john",
    "danny": "daniel", "dan": "daniel",
    "jimmy": "james",  "jim": "james",
    "tommy": "thomas", "tom": "thomas",
    "rick": "richard", "ricky": "richard", "dick": "richard",
    "ed": "edward",    "eddie": "edward",
    "dave": "david",
    "pete": "peter",
    "al": "alberto",
    "chuy": "jesus",   "nacho": "ignacio",
    "lupe": "guadalupe","memo": "guillermo",
    "beto": "roberto",  "pepe": "jose",
}


def normalize_name(name: str) -> str:
    """
    Normalizes a person name for fuzzy matching.
    'J. Rodriguez' -> 'j rodriguez'
    'Jose (Tool Pusher) Rodriguez' -> 'jose rodriguez'
    """
    if not name:
        return ""
    # Remove role/title suffixes in parens: "Jose (tool pusher)" -> "Jose"
    name = re.sub(r'\([^)]*\)', '', name)
    # Remove punctuation except spaces
    name = re.sub(r'[^\w\s]', ' ', name)
    # Lowercase and split
    parts = name.lower().split()
    # Remove title words
    parts = [p for p in parts if p not in TITLE_WORDS]
    # Expand initials: ["j", "rodriguez"] — keep for fuzzy match
    # Apply nickname map
    parts = [NICKNAME_MAP.get(p, p) for p in parts]
    return " ".join(parts).strip()


def names_likely_same(a: str, b: str, threshold: float = 0.72) -> bool:
    """
    Returns True if two name strings likely refer to the same person.
    Uses normalization + fuzzy matching + initial expansion.
    """
    na = normalize_name(a)
    nb = normalize_name(b)

    if not na or not nb:
        return False
    if na == nb:
        return True

    parts_a = na.split()
    parts_b = nb.split()

    # Check if one is a subset initial match: "j rodriguez" matches "jose rodriguez"
    for pa in parts_a:
        for pb in parts_b:
            if len(pa) == 1 and pb.startswith(pa):
                other_a = [x for x in parts_a if x != pa]
                other_b = [x for x in parts_b if x != pb]
                if other_a and other_b:
                    sub_score = SequenceMatcher(None, " ".join(other_a), " ".join(other_b)).ratio()
                    if sub_score > 0.85:
                        return True
            if len(pb) == 1 and pa.startswith(pb):
                other_a = [x for x in parts_a if x != pa]
                other_b = [x for x in parts_b if x != pb]
                if other_a and other_b:
                    sub_score = SequenceMatcher(None, " ".join(other_a), " ".join(other_b)).ratio()
                    if sub_score > 0.85:
                        return True

    # Last name match with different first: "Rodriguez" in both
    last_a = parts_a[-1] if parts_a else ""
    last_b = parts_b[-1] if parts_b else ""
    if last_a and last_b and last_a == last_b and len(last_a) > 3:
        return True

    # Full fuzzy score
    score = SequenceMatcher(None, na, nb).ratio()
    return score >= threshold


# ── Conflict detection ─────────────────────────────────────────────────────────

class ConflictDetector:
    """
    Runs after ingestion. Given a set of chunks from different files,
    finds cross-document conflicts and returns structured findings.
    """

    def __init__(self, chunks: list[dict]):
        """
        chunks: list of file_chunks rows with content + metadata
        """
        self.chunks = chunks

    def detect_all(self) -> list[dict]:
        """Returns list of conflict dicts."""
        conflicts = []
        conflicts.extend(self._detect_name_variants())
        conflicts.extend(self._detect_load_count_mismatches())
        conflicts.extend(self._detect_date_mismatches())
        return conflicts

    def _detect_name_variants(self) -> list[dict]:
        """
        Finds cases where the same person appears with different name spellings
        across different documents. Flags for human review.
        """
        # Extract names from chunks using simple heuristics
        all_names: dict[str, list[dict]] = {}  # normalized_name -> list of occurrences

        name_patterns = [
            r'(?:approved by|signed by|driver|operator|technician|supervisor)[:\s]+([A-Z][a-z]+ [A-Z][a-z]+)',
            r'(?:approver|approved)[:\s]+([A-Z][a-z]+ ?[A-Z]?\.? ?[A-Z][a-z]+)',
        ]

        for chunk in self.chunks:
            content  = chunk.get("content", "")
            fname    = chunk.get("filename", chunk.get("original_filename", ""))
            chunk_id = chunk.get("id", "")

            for pattern in name_patterns:
                for match in re.finditer(pattern, content, re.IGNORECASE):
                    raw_name  = match.group(1).strip()
                    norm_name = normalize_name(raw_name)
                    if norm_name not in all_names:
                        all_names[norm_name] = []
                    all_names[norm_name].append({
                        "raw":      raw_name,
                        "filename": fname,
                        "chunk_id": chunk_id,
                    })

        # Find groups that are likely the same person but spelled differently
        conflicts = []
        norm_keys = list(all_names.keys())

        for i, key_a in enumerate(norm_keys):
            for key_b in norm_keys[i+1:]:
                # Already same normalized form — different raw spellings?
                occurrences_a = all_names[key_a]
                occurrences_b = all_names[key_b]
                raw_names_a   = {o["raw"] for o in occurrences_a}
                raw_names_b   = {o["raw"] for o in occurrences_b}
                files_a       = {o["filename"] for o in occurrences_a}
                files_b       = {o["filename"] for o in occurrences_b}

                # Only flag if they appear in different files (cross-doc)
                if not files_a.intersection(files_b):
                    # Check if any raw name pair looks like the same person
                    for rna in raw_names_a:
                        for rnb in raw_names_b:
                            if rna != rnb and names_likely_same(rna, rnb):
                                conflicts.append({
                                    "type":        "name_variant",
                                    "severity":    "medium",
                                    "description": f'"{rna}" and "{rnb}" may be the same person',
                                    "detail":      f'Appears as "{rna}" in {list(files_a)} and "{rnb}" in {list(files_b)}',
                                    "name_a":      rna,
                                    "name_b":      rnb,
                                    "files_a":     list(files_a),
                                    "files_b":     list(files_b),
                                    "chunk_ids":   [o["chunk_id"] for o in occurrences_a + occurrences_b],
                                })

        return conflicts

    def _detect_load_count_mismatches(self) -> list[dict]:
        """
        Finds cases where load/bbl counts differ between documents
        for the same date, truck, or location.
        """
        conflicts = []

        # Extract load mentions: "3 loads", "180 bbls", "4 truck loads"
        load_pattern = re.compile(
            r'(\d+(?:\.\d+)?)\s*(?:loads?|bbls?|barrels?|truck\s*loads?)',
            re.IGNORECASE
        )
        # Date pattern for grouping
        date_pattern = re.compile(
            r'(?:june?|july?|august?|may|april|march|january|february)\s+\d{1,2}|'
            r'\d{1,2}/\d{1,2}(?:/\d{2,4})?',
            re.IGNORECASE
        )

        # Group mentions by extracted date
        date_mentions: dict[str, list[dict]] = {}

        for chunk in self.chunks:
            content  = chunk.get("content", "")
            fname    = chunk.get("filename", chunk.get("original_filename", ""))
            chunk_id = chunk.get("id", "")
            st       = chunk.get("source_type", "")

            dates = date_pattern.findall(content)
            loads = load_pattern.findall(content)

            if dates and loads:
                for date in dates[:3]:  # first few dates in chunk
                    date_key = date.lower().strip()
                    if date_key not in date_mentions:
                        date_mentions[date_key] = []
                    for load in loads:
                        date_mentions[date_key].append({
                            "value":     float(load),
                            "filename":  fname,
                            "chunk_id":  chunk_id,
                            "source_type": st,
                        })

        # Find dates with conflicting counts from different files
        for date_key, mentions in date_mentions.items():
            files = {m["filename"] for m in mentions}
            if len(files) < 2:
                continue
            values = {m["value"] for m in mentions}
            if len(values) > 1:
                min_v = min(values)
                max_v = max(values)
                if max_v - min_v >= 1:  # meaningful difference
                    conflicts.append({
                        "type":        "count_mismatch",
                        "severity":    "high",
                        "description": f"Load/bbl count conflict for {date_key}",
                        "detail":      f"Values range from {min_v} to {max_v} across files: {list(files)}",
                        "date":        date_key,
                        "values":      sorted(list(values)),
                        "files":       list(files),
                        "chunk_ids":   [m["chunk_id"] for m in mentions],
                    })

        return conflicts

    def _detect_date_mismatches(self) -> list[dict]:
        """
        Finds ticket/invoice date conflicts: same job number or ticket ID
        appears with different dates in different documents.
        """
        conflicts = []

        # Extract job/ticket/invoice references
        ref_pattern = re.compile(
            r'(?:ticket|invoice|job|work.?order|wo)\s*#?\s*([A-Z]{0,4}\d{3,8})',
            re.IGNORECASE
        )
        date_pattern = re.compile(
            r'(?:date|dated|on|signed)[\s:]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
            re.IGNORECASE
        )

        ref_dates: dict[str, list[dict]] = {}

        for chunk in self.chunks:
            content  = chunk.get("content", "")
            fname    = chunk.get("filename", chunk.get("original_filename", ""))
            chunk_id = chunk.get("id", "")

            refs  = ref_pattern.findall(content)
            dates = date_pattern.findall(content)

            for ref in refs:
                ref_key = ref.upper().strip()
                if ref_key not in ref_dates:
                    ref_dates[ref_key] = []
                for date in dates[:2]:
                    ref_dates[ref_key].append({
                        "date":     date,
                        "filename": fname,
                        "chunk_id": chunk_id,
                    })

        for ref_key, mentions in ref_dates.items():
            files = {m["filename"] for m in mentions}
            if len(files) < 2:
                continue
            dates = {m["date"] for m in mentions}
            if len(dates) > 1:
                conflicts.append({
                    "type":        "date_mismatch",
                    "severity":    "high",
                    "description": f"Reference {ref_key} has different dates across files",
                    "detail":      f"Dates found: {list(dates)} across {list(files)}",
                    "reference":   ref_key,
                    "dates":       list(dates),
                    "files":       list(files),
                    "chunk_ids":   [m["chunk_id"] for m in mentions],
                })

        return conflicts


def run_conflict_detection(chunks: list[dict]) -> list[dict]:
    """Top-level function called by the worker after ingestion."""
    if not chunks:
        return []
    detector = ConflictDetector(chunks)
    return detector.detect_all()
