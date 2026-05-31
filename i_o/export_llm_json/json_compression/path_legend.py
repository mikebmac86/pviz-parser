# i_o/json_compression/path_legend.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from .util import schema_fields, find_field_indices


def is_repo_relative_posix_path(p: str) -> bool:
    # Strict-ish guardrails so the legend stays "repo-internal"
    # (node ids should already be canonical repo-relative POSIX paths)
    if not p:
        return False
    if p.startswith("/"):
        return False
    if "\\" in p:
        return False
    if "\x00" in p:
        return False
    # Avoid path traversal artifacts
    parts = p.split("/")
    if any(part == ".." for part in parts):
        return False
    return True


def collect_internal_paths_from_nodes_rows(rows: Dict[str, Any]) -> List[str]:
    # Internal paths are defined as the keys of the standard 'nodes' mapping.
    internal = [k for k in rows.keys() if isinstance(k, str) and is_repo_relative_posix_path(k)]
    return sorted(set(internal))


def split_prefix(p: str) -> Tuple[str, str]:
    if "/" not in p:
        return ("", p)
    pre, suf = p.rsplit("/", 1)
    return (pre + "/", suf)


def build_path_legend(internal_paths_sorted: List[str]) -> Tuple[Dict[str, Any], Dict[str, int]]:
    prefixes: List[str] = []
    prefix_to_id: Dict[str, int] = {}
    path_entries: List[Tuple[int, str]] = []
    path_to_id: Dict[str, int] = {}

    def prefix_id(pre: str) -> int:
        if pre in prefix_to_id:
            return prefix_to_id[pre]
        prefix_to_id[pre] = len(prefixes)
        prefixes.append(pre)
        return prefix_to_id[pre]

    for full in internal_paths_sorted:
        pre, suf = split_prefix(full)
        pid = prefix_id(pre)
        path_to_id[full] = len(path_entries)
        path_entries.append((pid, suf))

    legend = {
        "prefixes": prefixes,
        "paths": [[pid, suf] for (pid, suf) in path_entries],
    }
    return legend, path_to_id


def rewrite_nodes_rows_with_path_ids(nodes: Dict[str, Any], path_to_id: Dict[str, int]) -> None:
    """
    Rewrite node rows to use path IDs instead of path strings.
    
    Handles:
    1. Row dict keys (node IDs) -> row_ids array
    2. Path string fields (file, node_id) -> path IDs (non-negative integers)
    3. Path array fields (imports) -> arrays of path IDs
    """
    rows = nodes.get("rows")
    if not isinstance(rows, dict) or not rows:
        return

    fields = schema_fields(nodes)
    if not fields:
        return

    # Fields that contain single path strings
    path_value_indices = find_field_indices(fields, {"file", "node_id"})
    
    # Fields that contain arrays of path strings (NEW)
    path_array_indices = find_field_indices(fields, {"imports"})

    row_ids: List[int] = []
    new_rows: List[List[Any]] = []

    for k in sorted(rows.keys()):
        row = rows[k]
        if not isinstance(row, list):
            row = [row]

        key_id = path_to_id.get(k)
        if key_id is None:
            continue

        # Convert single path string fields to path IDs
        for idx in path_value_indices:
            if idx < len(row) and isinstance(row[idx], str) and row[idx]:
                s = row[idx]
                pid = path_to_id.get(s)
                if pid is not None:
                    row[idx] = pid

        # Convert path array fields to arrays of path IDs (NEW)
        for idx in path_array_indices:
            if idx < len(row) and isinstance(row[idx], list):
                path_array = row[idx]
                id_array = []
                for path_str in path_array:
                    if isinstance(path_str, str):
                        pid = path_to_id.get(path_str)
                        if pid is not None:
                            id_array.append(pid)
                        else:
                            # Path not in legend, keep as string
                            id_array.append(path_str)
                    else:
                        # Not a string, keep as-is
                        id_array.append(path_str)
                row[idx] = id_array

        row_ids.append(key_id)
        new_rows.append(row)

    nodes["row_ids"] = row_ids
    nodes["rows"] = new_rows


def encode_paths_globally(obj: Any, *, path_to_id: Dict[str, int], skip_keys: Set[str]) -> Any:
    if isinstance(obj, str):
        pid = path_to_id.get(obj)
        if pid is not None:
            return -(pid + 1)
        return obj

    if isinstance(obj, list):
        return [encode_paths_globally(v, path_to_id=path_to_id, skip_keys=skip_keys) for v in obj]

    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for kk, vv in obj.items():
            if kk in skip_keys:
                out[kk] = vv
            else:
                out[kk] = encode_paths_globally(vv, path_to_id=path_to_id, skip_keys=skip_keys)
        return out

    return obj


def apply_path_legend_global_inplace(compressed: Dict[str, Any]) -> None:
    """
    Build and apply a global path legend for *internal file paths*.

    IMPORTANT SCOPE NOTE:
      - The legend is built ONLY from node ids (repo-internal files).
      - The global replacement pass will encode ANY string value that equals a node id.
    """
    nodes = compressed.get("nodes")
    if not isinstance(nodes, dict):
        return

    rows = nodes.get("rows")
    if not isinstance(rows, dict) or not rows:
        return

    internal_paths_sorted = collect_internal_paths_from_nodes_rows(rows)
    if not internal_paths_sorted:
        return

    path_legend, path_to_id = build_path_legend(internal_paths_sorted)
    compressed["path_legend"] = path_legend

    rewrite_nodes_rows_with_path_ids(nodes, path_to_id)

    skip_keys = {"path_legend"}
    for top_k in list(compressed.keys()):
        if top_k in skip_keys:
            continue
        compressed[top_k] = encode_paths_globally(compressed[top_k], path_to_id=path_to_id, skip_keys=skip_keys)


def decode_path_id(path_legend: Dict[str, Any], path_id: int) -> Optional[str]:
    prefixes = path_legend.get("prefixes")
    paths = path_legend.get("paths")
    if not isinstance(prefixes, list) or not isinstance(paths, list):
        return None
    if path_id < 0 or path_id >= len(paths):
        return None
    entry = paths[path_id]
    if not (isinstance(entry, list) and len(entry) == 2):
        return None
    pre_id, suf = entry
    if not isinstance(pre_id, int) or not isinstance(suf, str):
        return None
    if pre_id < 0 or pre_id >= len(prefixes):
        return None
    pre = prefixes[pre_id]
    if not isinstance(pre, str):
        return None
    return pre + suf


def decode_path_legend_global_inplace(decoded: Dict[str, Any], path_legend: Dict[str, Any]) -> None:
    def decode_value(obj: Any) -> Any:
        if isinstance(obj, int) and obj < 0:
            pid = (-obj) - 1
            s = decode_path_id(path_legend, pid)
            return s if s is not None else obj
        if isinstance(obj, list):
            return [decode_value(v) for v in obj]
        if isinstance(obj, dict):
            out: Dict[str, Any] = {}
            for kk, vv in obj.items():
                if kk == "path_legend":
                    out[kk] = vv
                else:
                    out[kk] = decode_value(vv)
            return out
        return obj

    for top_k in list(decoded.keys()):
        if top_k == "path_legend":
            continue
        decoded[top_k] = decode_value(decoded[top_k])