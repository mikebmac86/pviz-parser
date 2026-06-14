# i_o/json_compression/edge_codecs.py
"""
Edge compression codec.

Edges have a consistent structure but may include optional metadata:

  {
      "src": <path>,
      "dst": <path>,
      "kind": <enum>,
      "confidence": <float>,
      "weight": <float>,
      "label": <str>,
      "spec": <str>,
      "reason": <str>,
      "evidence": <object>,
  }

This codec applies:
1. Field abbreviation with collision-safe abbreviations.
2. Enum encoding for the "kind" field.
3. Schema-based array encoding.

Path compression is handled by the global path legend pass.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .util import decode_enum, encode_enum


# Known edge kinds - extend as needed.
EDGE_KINDS = [
    "import",
    "call",
    "reference",
    "inherit",
    "uses",
    "exports",
]


# Stable abbreviations for common edge fields.
#
# Important: these must be unique. Do not use "s" for both src and spec.
EDGE_FIELD_ABBRS: Dict[str, str] = {
    "src": "s",
    "dst": "d",
    "kind": "k",
    "confidence": "c",
    "weight": "w",
    "label": "l",
    "reason": "r",
    "spec": "p",
    "subkind": "sk",
    "synthetic": "sy",
    "evidence": "e",
    "reasons": "rs",
    "crosstalk": "x",
}


EDGE_FIELD_ORDER = [
    "src",
    "dst",
    "kind",
    "confidence",
    "weight",
    "label",
    "spec",
    "reason",
    "subkind",
    "synthetic",
    "evidence",
    "reasons",
    "crosstalk",
]


def _abbr_for_field(field: str, used: set[str]) -> str:
    """
    Return a stable, collision-free abbreviation for an edge field.

    Known fields use EDGE_FIELD_ABBRS. Unknown fields get a deterministic
    generated abbreviation, but never collide with existing schema entries.
    """
    preferred = EDGE_FIELD_ABBRS.get(field)
    if preferred and preferred not in used:
        return preferred

    # Prefer progressively longer prefixes for unknown fields.
    for n in range(1, min(len(field), 8) + 1):
        candidate = field[:n]
        if candidate and candidate not in used:
            return candidate

    # Last-resort deterministic fallback.
    i = 1
    while True:
        candidate = f"x{i}"
        if candidate not in used:
            return candidate
        i += 1


def _ordered_fields(all_fields: set[str]) -> List[str]:
    """
    Put common edge fields first, then unknown fields alphabetically.
    """
    ordered: List[str] = []

    for field in EDGE_FIELD_ORDER:
        if field in all_fields:
            ordered.append(field)

    for field in sorted(all_fields):
        if field not in ordered:
            ordered.append(field)

    return ordered


def compress_edges(edges: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compress edge list into schema-encoded format.

    Preserves all fields, including optional metadata like label/spec/reason.

    Input:
      [
        {
          "src": "a.py",
          "dst": "b.py",
          "kind": "import",
          "label": "pkg.mod",
          "spec": "pkg.mod",
          "reason": "python_import_internal",
          "confidence": 0.8,
          "weight": 0.8
        }
      ]

    Output:
      {
        "schema": ["s", "d", "k", "c", "w", "l", "p", "r"],
        "legend": {
          "s": "src",
          "d": "dst",
          "k": "kind",
          "c": "confidence",
          "w": "weight",
          "l": "label",
          "p": "spec",
          "r": "reason"
        },
        "kinds": ["import"],
        "rows": [["a.py", "b.py", 0, 0.8, 0.8, "pkg.mod", "pkg.mod", "python_import_internal"]]
      }
    """
    if not edges:
        return {
            "schema": [],
            "legend": {},
            "kinds": [],
            "rows": [],
        }

    # Collect all edge kinds in this dataset.
    kinds_seen = set()
    for edge in edges:
        if not isinstance(edge, dict):
            continue

        kind = edge.get("kind")
        if isinstance(kind, str) and kind:
            kinds_seen.add(kind)

    # Use known kinds first, then any new ones found.
    edge_kinds: List[str] = []

    for known in EDGE_KINDS:
        if known in kinds_seen:
            edge_kinds.append(known)

    for kind in sorted(kinds_seen):
        if kind not in edge_kinds:
            edge_kinds.append(kind)

    # Determine schema dynamically based on fields present.
    all_fields: set[str] = set()

    for edge in edges:
        if isinstance(edge, dict):
            all_fields.update(str(k) for k in edge.keys() if isinstance(k, str) and k)

    ordered_fields = _ordered_fields(all_fields)

    schema: List[str] = []
    legend: Dict[str, str] = {}
    used_abbrs: set[str] = set()

    for field in ordered_fields:
        abbr = _abbr_for_field(field, used_abbrs)
        used_abbrs.add(abbr)
        schema.append(abbr)
        legend[abbr] = field

    # Compress to rows.
    rows: List[List[Any]] = []

    for edge in edges:
        if not isinstance(edge, dict):
            continue

        row: List[Any] = []

        for abbr in schema:
            field = legend[abbr]
            value = edge.get(field)

            # Encode kind enum.
            if field == "kind" and value is not None:
                value = encode_enum(value, edge_kinds)

            row.append(value)

        rows.append(row)

    return {
        "schema": schema,
        "legend": legend,
        "kinds": edge_kinds,
        "rows": rows,
    }


def decode_edges(compressed: Any) -> List[Dict[str, Any]]:
    """
    Decode schema-encoded edges back to list of dicts.

    Handles both compressed format and pass-through list format.
    """
    # Already decoded / legacy format.
    if isinstance(compressed, list):
        return compressed

    if not isinstance(compressed, dict):
        return []

    schema = compressed.get("schema", [])
    legend = compressed.get("legend", {})
    kinds = compressed.get("kinds", [])
    rows = compressed.get("rows", [])

    if not isinstance(schema, list):
        return []
    if not isinstance(legend, dict):
        return []
    if not isinstance(kinds, list):
        kinds = []
    if not isinstance(rows, list):
        return []

    # Map schema positions to field names.
    field_names = [
        legend.get(abbr, abbr)
        for abbr in schema
    ]

    edges: List[Dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, list):
            continue

        edge: Dict[str, Any] = {}

        for i, field_name in enumerate(field_names):
            if i >= len(row):
                continue

            value = row[i]

            # Decode kind enum.
            if field_name == "kind":
                value = decode_enum(value, kinds)

            # Only include non-None values.
            if value is not None:
                edge[str(field_name)] = value

        edges.append(edge)

    return edges