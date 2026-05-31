# i_o/json_compression/edge_codecs.py
"""
Edge compression codec.

Edges have a consistent structure but may have optional fields:
  {"src": <path>, "dst": <path>, "kind": <enum>, "evidence": <object>}

This codec applies:
1. Field abbreviation (src->s, dst->d, kind->k, evidence->e)
2. Enum encoding for "kind" field
3. Schema-based array encoding

Path compression (negative IDs) is handled by the global path legend pass.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .util import encode_enum, decode_enum


# Known edge kinds - extend as needed
EDGE_KINDS = [
    "import",
    "call",
    "reference",
    "inherit",
    "uses",
    "exports",
]


def compress_edges(edges: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compress edge list into schema-encoded format.
    
    Preserves all fields including optional ones like 'evidence'.
    
    Input: [{"src": "a.py", "dst": "b.py", "kind": "import", "evidence": {...}}, ...]
    Output: {
        "schema": ["s", "d", "k", "e"],
        "legend": {"s": "src", "d": "dst", "k": "kind", "e": "evidence"},
        "kinds": ["import", "call", ...],
        "rows": [["a.py", "b.py", 0, {...}], ...]
    }
    """
    if not edges:
        return {"schema": [], "legend": {}, "kinds": [], "rows": []}
    
    # Collect all edge kinds in this dataset
    kinds_seen = set()
    for edge in edges:
        kind = edge.get("kind")
        if kind:
            kinds_seen.add(kind)
    
    # Use known kinds + any new ones found
    edge_kinds = []
    for known in EDGE_KINDS:
        if known in kinds_seen:
            edge_kinds.append(known)
    
    # Add any unknown kinds at the end
    for kind in sorted(kinds_seen):
        if kind not in edge_kinds:
            edge_kinds.append(kind)
    
    # Determine schema dynamically based on fields present
    all_fields = set()
    for edge in edges:
        if isinstance(edge, dict):
            all_fields.update(edge.keys())
    
    # Core fields in preferred order
    schema = []
    legend = {}
    
    if "src" in all_fields:
        schema.append("s")
        legend["s"] = "src"
    if "dst" in all_fields:
        schema.append("d")
        legend["d"] = "dst"
    if "kind" in all_fields:
        schema.append("k")
        legend["k"] = "kind"
    if "evidence" in all_fields:
        schema.append("e")
        legend["e"] = "evidence"
    
    # Add any other fields
    for field in sorted(all_fields):
        if field not in ["src", "dst", "kind", "evidence"]:
            abbr = field[:1]  # Simple abbreviation
            schema.append(abbr)
            legend[abbr] = field
    
    # Compress to rows
    rows = []
    for edge in edges:
        row = []
        for abbr in schema:
            field = legend[abbr]
            value = edge.get(field)
            
            # Encode kind enum
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


def decode_edges(compressed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Decode schema-encoded edges back to list of dicts.
    
    Handles both compressed format and pass-through (for backward compatibility).
    """
    # If it's already a list, pass through (backward compat)
    if isinstance(compressed, list):
        return compressed
    
    schema = compressed.get("schema", [])
    legend = compressed.get("legend", {})
    kinds = compressed.get("kinds", [])
    rows = compressed.get("rows", [])
    
    # Map schema positions to field names
    field_names = [legend.get(abbr, abbr) for abbr in schema]
    
    edges = []
    for row in rows:
        edge: Dict[str, Any] = {}
        for i, field_name in enumerate(field_names):
            if i < len(row):
                value = row[i]
                
                # Decode kind enum
                if field_name == "kind":
                    value = decode_enum(value, kinds)
                
                # Only include non-None values
                if value is not None:
                    edge[field_name] = value
        
        edges.append(edge)
    
    return edges