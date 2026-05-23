from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence

from .config import LANGUAGE_SPECS, LanguageSpec
from .io import safe_int


SCC_FIELDS = {
    "scc_id",
    "scc_size",
    "scc_id_runtime",
    "scc_size_runtime",
}


def merge_arrays(base: List[Any], overlay: List[Any]) -> List[Any]:
    if not isinstance(base, list):
        base = []
    if not isinstance(overlay, list):
        overlay = []

    if all(isinstance(x, str) for x in base + overlay):
        seen = set()
        result = []
        for item in base + overlay:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    result = list(base)
    existing_names = {
        item.get("name")
        for item in base
        if isinstance(item, dict) and "name" in item
    }

    for item in overlay:
        if isinstance(item, dict):
            name = item.get("name")
            if name and name not in existing_names:
                result.append(item)
                existing_names.add(name)
            elif not name and item not in result:
                result.append(item)
        elif item not in result:
            result.append(item)

    return result


def merge_plain_dicts_deep(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if key in SCC_FIELDS:
            continue
        if key not in result:
            result[key] = value
        elif isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_plain_dicts_deep(result[key], value)
        elif isinstance(result[key], list) and isinstance(value, list):
            result[key] = merge_arrays(result[key], value)
        else:
            result[key] = value
    return result


def merge_facts(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(base, dict):
        base = {}
    if not isinstance(overlay, dict):
        overlay = {}

    result = dict(base)

    for array_field in ["types", "interfaces", "enums", "classes"]:
        if array_field in overlay:
            result[array_field] = merge_arrays(
                result.get(array_field, []),
                overlay.get(array_field, []),
            )

    if "export_kinds" in overlay:
        if isinstance(result.get("export_kinds"), dict) and isinstance(overlay["export_kinds"], dict):
            result["export_kinds"] = {**result["export_kinds"], **overlay["export_kinds"]}
        else:
            result["export_kinds"] = overlay["export_kinds"]

    handled = {"types", "interfaces", "enums", "classes", "export_kinds"}

    for key, value in overlay.items():
        if key in handled or key in SCC_FIELDS:
            continue
        if key not in result:
            result[key] = value
        elif isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_plain_dicts_deep(result[key], value)
        elif isinstance(result[key], list) and isinstance(value, list):
            result[key] = merge_arrays(result[key], value)
        else:
            result[key] = value

    return result


def _strip_scc_fields(node: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in dict(node).items()
        if key not in SCC_FIELDS
    }


def deep_merge_node(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    # SCC fields are intentionally removed here.
    # SCC is a global graph property and must be recomputed after all nodes
    # and edges are merged, not inherited from per-language partial graphs.
    result = _strip_scc_fields(base)
    overlay = _strip_scc_fields(overlay)

    array_fields = ["types", "interfaces", "enums", "classes", "functions", "globals"]
    for field in array_fields:
        if field in overlay:
            result[field] = merge_arrays(result.get(field, []), overlay[field])

    if "export_kinds" in overlay:
        if isinstance(result.get("export_kinds"), dict) and isinstance(overlay["export_kinds"], dict):
            result["export_kinds"] = {**result["export_kinds"], **overlay["export_kinds"]}
        else:
            result["export_kinds"] = overlay["export_kinds"]

    if "facts" in overlay:
        if isinstance(result.get("facts"), dict) and isinstance(overlay["facts"], dict):
            result["facts"] = merge_facts(result["facts"], overlay["facts"])
        else:
            result["facts"] = overlay["facts"]

    count_fields = {
        "decorators_count",
        "methods_count",
        "props_count",
        "implements_count",
        "imports_count",
        "exports_count",
        "functions_count",
        "classes_count",
        "globals_count",
    }

    for count_field in count_fields:
        if count_field in overlay:
            result[count_field] = max(
                safe_int(result.get(count_field), 0),
                safe_int(overlay.get(count_field), 0),
            )

    for list_field in ["imports", "exports"]:
        if list_field in overlay:
            base_list = result.get(list_field, [])
            overlay_list = overlay[list_field]
            if isinstance(base_list, list) and isinstance(overlay_list, list):
                result[list_field] = list(dict.fromkeys(base_list + overlay_list))
            else:
                result[list_field] = overlay_list

    skip_fields = (
        set(array_fields)
        | {"export_kinds", "facts", "imports", "exports"}
        | count_fields
        | SCC_FIELDS
    )

    for key, value in overlay.items():
        if key in skip_fields:
            continue
        if key not in result:
            result[key] = value
        elif key in ("classes_detailed", "functions_detailed", "globals_detailed"):
            if value and isinstance(value, (list, tuple)):
                result[key] = value
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_plain_dicts_deep(result[key], value)
        elif isinstance(value, list) and isinstance(result.get(key), list):
            result[key] = merge_arrays(result[key], value)
        else:
            result[key] = value

    return result


def merge_nodes_by_language(
    nodes_by_lang: Mapping[str, Dict[str, Dict[str, Any]]],
    specs: Sequence[LanguageSpec] = LANGUAGE_SPECS,
) -> Dict[str, Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}

    for spec in specs:
        for node_id, node in nodes_by_lang.get(spec.lang, {}).items():
            if node_id not in merged:
                merged[node_id] = _strip_scc_fields(node)
            else:
                merged[node_id] = deep_merge_node(merged[node_id], node)

    return merged