from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

from .io import safe_int


def extract_scc_meta(nodefacts_by_lang: Mapping[str, Any]) -> Dict[str, Any]:
    by_language: Dict[str, Dict[str, Any]] = {}
    legacy: Dict[str, Any] = {}

    for lang, nf in nodefacts_by_lang.items():
        if not isinstance(nf, dict):
            continue
        meta = nf.get("meta")
        if not isinstance(meta, dict):
            continue

        lang_meta = {
            key: meta[key]
            for key in ("scc_conceptual", "scc_runtime", "tc_inflation")
            if key in meta
        }
        if lang_meta:
            by_language[lang] = lang_meta
            if lang == "python":
                legacy.update(lang_meta)

    if by_language:
        legacy["scc_by_language"] = by_language

    return legacy


def compute_typescript_metadata_stats(merged_nodes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    stats = {
        "files_with_types": 0,
        "files_with_interfaces": 0,
        "files_with_enums": 0,
        "total_types": 0,
        "total_interfaces": 0,
        "total_enums": 0,
        "classes_with_decorators": 0,
        "files_with_export_kinds": 0,
    }

    for node in merged_nodes.values():
        if not isinstance(node, dict):
            continue

        facts = node.get("facts") if isinstance(node.get("facts"), dict) else {}
        node_types = node.get("types") or facts.get("types", [])
        node_interfaces = node.get("interfaces") or facts.get("interfaces", [])
        node_enums = node.get("enums") or facts.get("enums", [])

        if not isinstance(node_types, list):
            node_types = []
        if not isinstance(node_interfaces, list):
            node_interfaces = []
        if not isinstance(node_enums, list):
            node_enums = []

        if node_types:
            stats["files_with_types"] += 1
            stats["total_types"] += len(node_types)
        if node_interfaces:
            stats["files_with_interfaces"] += 1
            stats["total_interfaces"] += len(node_interfaces)
        if node_enums:
            stats["files_with_enums"] += 1
            stats["total_enums"] += len(node_enums)

        has_decorators = safe_int(node.get("decorators_count"), 0) > 0
        if not has_decorators:
            node_classes = node.get("classes") or facts.get("classes", [])
            if isinstance(node_classes, list):
                has_decorators = any(
                    isinstance(cls, dict) and safe_int(cls.get("decorators_count"), 0) > 0
                    for cls in node_classes
                )

        if has_decorators:
            stats["classes_with_decorators"] += 1

        export_kinds = node.get("export_kinds") or facts.get("export_kinds", {})
        if isinstance(export_kinds, dict) and export_kinds:
            stats["files_with_export_kinds"] += 1

    return stats


def determine_merged_schema_version(nodefacts_by_lang: Mapping[str, Any]) -> str:
    versions = []

    for nf in nodefacts_by_lang.values():
        if isinstance(nf, dict):
            schema = nf.get("schema_version") or nf.get("schema")
            if schema:
                versions.append(str(schema))

    if any("nodefacts@v1.8" in v or "v1.8" in v for v in versions):
        return "nodefacts@v1.8"
    if any("nodefacts@v1.7" in v or "v1.7" in v for v in versions):
        return "nodefacts@v1.7"
    return "nodefacts@v1.6"


def first_mapping(values: Iterable[Any]) -> Optional[Mapping[str, Any]]:
    for value in values:
        if isinstance(value, Mapping):
            return value
    return None