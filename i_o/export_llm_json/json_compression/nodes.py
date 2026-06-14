# i_o/json_compression/nodes.py
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Set

from .types import ENUMS, PREFERRED_NODE_FIELDS
from .util import decode_enum, encode_enum, make_schema, schema_fields
from .path_legend import decode_path_id
from . import node_codecs as nc


CROSSTALK_FIELDS = (
    "crosstalk_candidates_py_v1",
    "crosstalk_candidates_ts_v1",
    "crosstalk_candidates_go_v1",
    "crosstalk_candidates_java_v1",
    "crosstalk_candidates_kotlin_v1",
    "crosstalk_candidates_rust_v1",
)


def _build_node_encoders(
    *,
    type_to_idx: Dict[str, int],
) -> Dict[str, Callable[[Any], Any]]:
    encoders: Dict[str, Callable[[Any], Any]] = {
        "parse_status": lambda v: encode_enum(v, ENUMS["parse_status"]),
        "globals_detailed": lambda v: nc.compress_globals_detailed(v, type_to_idx),  # type: ignore[arg-type]
        "functions_detailed": lambda v: nc.compress_functions_detailed(v, ENUMS, type_to_idx),  # type: ignore[arg-type]
        "classes_detailed": lambda v: nc.compress_classes_detailed(v, ENUMS, type_to_idx),  # type: ignore[arg-type]
    }

    for field in CROSSTALK_FIELDS:
        encoders[field] = lambda v: nc.compress_crosstalk(v)  # type: ignore[arg-type]

    return encoders


def _build_node_decoders(
    *,
    enums: Dict[str, List[str]],
    type_strings: List[str],
) -> Dict[str, Callable[[Any], Any]]:
    parse_status_list = enums.get("parse_status", ENUMS["parse_status"])

    decoders: Dict[str, Callable[[Any], Any]] = {
        "parse_status": lambda v: decode_enum(v, parse_status_list) if isinstance(v, int) else v,
        "globals_detailed": lambda v: nc.decode_globals_detailed(v, type_strings) if isinstance(v, dict) else v,
        "functions_detailed": lambda v: nc.decode_functions_detailed(v, enums, type_strings) if isinstance(v, list) else v,
        "classes_detailed": lambda v: nc.decode_classes_detailed(v, enums, type_strings) if isinstance(v, list) else v,
    }

    for field in CROSSTALK_FIELDS:
        decoders[field] = lambda v: nc.decode_crosstalk(v) if isinstance(v, dict) else v

    return decoders


def compress_nodes(
    nodes: Dict[str, Any],
    *,
    drop_node_fields: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    if not nodes:
        return {}

    drop_node_fields = set(drop_node_fields or set())

    all_fields: Set[str] = set()

    for node in nodes.values():
        if isinstance(node, dict):
            all_fields.update(str(k) for k in node.keys() if isinstance(k, str) and k)

    all_fields.difference_update(drop_node_fields)

    ordered_fields: List[str] = []

    for field in PREFERRED_NODE_FIELDS:
        if field in all_fields:
            ordered_fields.append(field)

    remaining = sorted(all_fields.difference(set(ordered_fields)))
    ordered_fields.extend(remaining)

    schema, legend = make_schema(ordered_fields)

    type_strings, type_to_idx = nc.build_type_table(nodes)
    encoders = _build_node_encoders(type_to_idx=type_to_idx)

    rows: Dict[str, List[Any]] = {}

    for key, node in nodes.items():
        if not isinstance(node, dict):
            rows[key] = [node]
            continue

        row: List[Any] = []

        for field in ordered_fields:
            value = node.get(field)

            encoder = encoders.get(field)
            if encoder is not None:
                value = encoder(value)

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


def decode_nodes(
    nodes_compressed: Dict[str, Any],
    path_legend: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not isinstance(nodes_compressed, dict):
        return {}

    fields = schema_fields(nodes_compressed)

    enums_raw = nodes_compressed.get("enums", {})
    enums = enums_raw if isinstance(enums_raw, dict) else {}

    rows = nodes_compressed.get("rows", {})

    type_strings_raw = nodes_compressed.get("type_strings", [])
    type_strings = type_strings_raw if isinstance(type_strings_raw, list) else []

    decoders = _build_node_decoders(
        enums=enums,  # type: ignore[arg-type]
        type_strings=type_strings,
    )

    decoded_nodes: Dict[str, Any] = {}

    # These are only for non-negative direct path IDs produced by the row_id path
    # encoding mode. Global negative sentinel refs are decoded later by
    # decode_path_legend_global_inplace().
    path_value_fields = {
        "file",
        "node_id",
    }

    path_array_fields = {
        "imports",
    }

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

                decoder = decoders.get(field_name)
                if decoder is not None:
                    value = decoder(value)

                # Decode direct non-negative path IDs for single path fields.
                if field_name in path_value_fields and isinstance(value, int):
                    decoded_path = decode_path_id(path_legend, value)
                    if decoded_path is not None:
                        value = decoded_path

                # Decode direct non-negative path IDs for path-array fields.
                if field_name in path_array_fields and isinstance(value, list):
                    decoded_array: List[Any] = []

                    for item in value:
                        if isinstance(item, int) and item >= 0:
                            decoded_path = decode_path_id(path_legend, item)
                            decoded_array.append(decoded_path if decoded_path is not None else item)
                        else:
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

            decoder = decoders.get(field_name)
            if decoder is not None:
                value = decoder(value)

            if value is not None:
                node[field_name] = value

        decoded_nodes[key] = node

    return decoded_nodes