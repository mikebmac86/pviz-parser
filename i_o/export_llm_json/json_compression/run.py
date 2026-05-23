# i_o/json_compression/run.py
"""
i_o/json_compression

Schema-based JSON compression for LLM-optimized output.

Split from the legacy monolith i_o/json_compression.py.
Public API is preserved: apply_schema_encoding(), decode_schema().
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .nodes import compress_nodes, decode_nodes
from .path_legend import apply_path_legend_global_inplace, decode_path_legend_global_inplace
from .edge_codecs import compress_edges, decode_edges
from .format_guide import get_format_guide


def apply_schema_encoding(
    data: Dict[str, Any],
    *,
    drop_top_level: Optional[List[str]] = None,
    drop_node_fields: Optional[List[str]] = None,
    include_format_guide: bool = True,
    compress_edges: bool = True,
) -> Dict[str, Any]:
    """
    Transform normalized graph into schema-encoded format.

    Lossless by default (round-trip decode should match input),
    except for anything explicitly dropped via drop_* parameters.
    
    Args:
        data: Input data dictionary
        drop_top_level: Top-level keys to exclude from output
        drop_node_fields: Node fields to exclude from compression
        include_format_guide: Include decoding instructions for LLMs (default: True, ~5KB overhead)
        compress_edges: Apply edge codec for additional compression (default: True)
    """
    drop_top_level_set = set(drop_top_level or [])
    drop_node_fields_set = set(drop_node_fields or [])

    compressed: Dict[str, Any] = {}

    # Add format guide first for LLM context
    if include_format_guide:
        compressed["_format"] = get_format_guide()

    # Preserve core identity
    for k in ("schema_version", "meta"):
        if k in data and k not in drop_top_level_set:
            compressed[k] = data.get(k)

    # Compress nodes section
    if "nodes" in data and "nodes" not in drop_top_level_set:
        compressed["nodes"] = compress_nodes(data["nodes"], drop_node_fields=drop_node_fields_set)

    # Compress edges section
    if "edges" in data and "edges" not in drop_top_level_set:
        if compress_edges and isinstance(data["edges"], list):
            compressed["edges"] = compress_edges_func(data["edges"])
        else:
            compressed["edges"] = data["edges"]

    # Pass through other sections (zones, node_order, summary, etc.)
    for k in ("zones", "node_order", "summary", "discovery", "discovery_manifest"):
        if k in data and k not in drop_top_level_set:
            compressed[k] = data[k]

    # If caller wants other keys preserved, keep them unless explicitly dropped
    for k, v in data.items():
        if k in compressed:
            continue
        if k in drop_top_level_set:
            continue
        compressed[k] = v

    # -------------------------------------------------------------------
    # Path compression layer - applies to nodes and edges
    # -------------------------------------------------------------------
    apply_path_legend_global_inplace(compressed)

    return compressed


# Rename to avoid conflict with parameter
compress_edges_func = compress_edges


def decode_schema(compressed: Dict[str, Any]) -> Dict[str, Any]:
    """
    Decode schema-encoded format back to normalized graph.
    
    Handles both new compressed format and legacy pass-through format
    for backward compatibility.
    """
    decoded: Dict[str, Any] = {}

    for k in ("schema_version", "meta"):
        if k in compressed:
            decoded[k] = compressed.get(k)

    path_legend = compressed.get("path_legend")
    
    # Decode nodes
    if "nodes" in compressed:
        decoded["nodes"] = decode_nodes(
            compressed["nodes"],
            path_legend if isinstance(path_legend, dict) else None,
        )

    # Decode edges
    if "edges" in compressed:
        decoded["edges"] = decode_edges(compressed["edges"])

    # Pass through other sections
    for k in ("zones", "node_order", "summary", "discovery", "discovery_manifest"):
        if k in compressed:
            decoded[k] = compressed[k]

    # Preserve other keys (skip internal _format guide)
    for k, v in compressed.items():
        if k in decoded:
            continue
        if k.startswith("_"):  # Skip internal metadata like _format
            continue
        decoded[k] = v

    # Decode negative sentinel path refs across the whole JSON
    if isinstance(path_legend, dict):
        decode_path_legend_global_inplace(decoded, path_legend)

    decoded.pop("path_legend", None)
    return decoded