# i_o/json_compression/nodes.py
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Set

from .types import ENUMS, PREFERRED_NODE_FIELDS
from .util import make_schema, schema_fields, find_field_indices, encode_enum, decode_enum
from .path_legend import decode_path_id
from . import node_codecs as nc


def compress_nodes(nodes: Dict[str, Any], *, drop_node_fields: Set[str]) -> Dict[str, Any]:
    if not nodes:
        return {}

    all_fields: Set[str] = set()
    for node in nodes.values():
        if isinstance(node, dict):
            all_fields.update(node.keys())

    all_fields.difference_update(drop_node_fields)

    ordered_fields: List[str] = []
    for f in PREFERRED_NODE_FIELDS:
        if f in all_fields:
            ordered_fields.append(f)

    remaining = sorted(all_fields.difference(set(ordered_fields)))
    ordered_fields.extend(remaining)

    schema, legend = make_schema(ordered_fields)

    type_strings, type_to_idx = nc.build_type_table(nodes)

    encoders: Dict[str, Callable[[Any], Any]] = {
        "parse_status": lambda v: encode_enum(v, ENUMS["parse_status"]),
        "globals_detailed": lambda v: nc.compress_globals_detailed(v, type_to_idx),  # type: ignore[arg-type]
        "functions_detailed": lambda v: nc.compress_functions_detailed(v, ENUMS, type_to_idx),  # type: ignore[arg-type]
        "classes_detailed": lambda v: nc.compress_classes_detailed(v, ENUMS, type_to_idx),  # type: ignore[arg-type]
        "crosstalk_candidates_py_v1": lambda v: nc.compress_crosstalk(v),  # type: ignore[arg-type]
        "crosstalk_candidates_ts_v1": lambda v: nc.compress_crosstalk(v),  # type: ignore[arg-type]
    }

    rows: Dict[str, List[Any]] = {}

    for key, node in nodes.items():
        if not isinstance(node, dict):
            rows[key] = [node]
            continue

        row: List[Any] = []
        for field in ordered_fields:
            value = node.get(field)
            enc = encoders.get(field)
            if enc is not None:
                value = enc(value)
            row.append(value)

        rows[key] = row

    result: Dict[str, Any] = {
        "schema": schema,
        "legend": legend,
        "enums": dict(ENUMS),
        "rows": rows,
    }

    if type_strings:
        result["type_strings"] = type_strings

    return result


def decode_nodes(nodes_compressed: Dict[str, Any], path_legend: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fields = schema_fields(nodes_compressed)
    enums = nodes_compressed.get("enums", {})
    rows = nodes_compressed.get("rows", {})
    type_strings = nodes_compressed.get("type_strings", [])

    decoded_nodes: Dict[str, Any] = {}

    parse_status_list = enums.get("parse_status", ENUMS["parse_status"])

    decoders: Dict[str, Callable[[Any], Any]] = {
        "parse_status": lambda v: decode_enum(v, parse_status_list) if isinstance(v, int) else v,
        "globals_detailed": lambda v: nc.decode_globals_detailed(v, type_strings) if isinstance(v, dict) else v,
        "functions_detailed": lambda v: nc.decode_functions_detailed(v, enums, type_strings) if isinstance(v, list) else v,
        "classes_detailed": lambda v: nc.decode_classes_detailed(v, enums, type_strings) if isinstance(v, list) else v,
        "crosstalk_candidates_py_v1": lambda v: nc.decode_crosstalk(v) if isinstance(v, dict) else v,
        "crosstalk_candidates_ts_v1": lambda v: nc.decode_crosstalk(v) if isinstance(v, dict) else v,
    }

    # Fields that contain single path values (non-negative path IDs)
    path_value_fields = {"file", "node_id"}
    
    # Fields that contain arrays of path values (non-negative path IDs) - NEW
    path_array_fields = {"imports"}

    row_ids = nodes_compressed.get("row_ids")
    if isinstance(rows, list) and isinstance(row_ids, list) and path_legend:
        for rid, row in zip(row_ids, rows):
            if not isinstance(row, list):
                decoded_nodes[f"<path_id:{rid}>"] = row
                continue

            key_path = decode_path_id(path_legend, rid) or f"<path_id:{rid}>"

            node: Dict[str, Any] = {}
            for i, field_name in enumerate(fields):
                value = row[i] if i < len(row) else None

                dec = decoders.get(field_name)
                if dec is not None:
                    value = dec(value)

                # Decode single path ID fields (file, node_id)
                if field_name in path_value_fields and isinstance(value, int):
                    pv = decode_path_id(path_legend, value)
                    if pv is not None:
                        value = pv

                # Decode path ID array fields (imports) - NEW
                if field_name in path_array_fields and isinstance(value, list) and path_legend:
                    decoded_array = []
                    for item in value:
                        if isinstance(item, int) and item >= 0:
                            # Non-negative integer = path ID
                            decoded_path = decode_path_id(path_legend, item)
                            if decoded_path is not None:
                                decoded_array.append(decoded_path)
                            else:
                                # Failed to decode, keep as-is
                                decoded_array.append(item)
                        else:
                            # Not a path ID (could be string or negative sentinel)
                            decoded_array.append(item)
                    value = decoded_array

                if value is not None:
                    node[field_name] = value

            decoded_nodes[key_path] = node

        return decoded_nodes

    if not isinstance(rows, dict):
        return decoded_nodes

    for key, row in rows.items():
        if not isinstance(row, list):
            decoded_nodes[key] = row
            continue

        node: Dict[str, Any] = {}
        for i, field_name in enumerate(fields):
            value = row[i] if i < len(row) else None
            dec = decoders.get(field_name)
            if dec is not None:
                value = dec(value)
            if value is not None:
                node[field_name] = value

        decoded_nodes[key] = node

    return decoded_nodes