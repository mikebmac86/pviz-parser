from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .config import LANGUAGE_SPECS, LanguageSpec
from .io import write_json
from .meta import determine_merged_schema_version, extract_scc_meta
from .metrics import metrics_int, metrics_val


def build_nodefacts_payload(
    *,
    merged_nodes: Dict[str, Dict[str, Any]],
    nodefacts_by_lang: Mapping[str, Any],
    sources_meta: Dict[str, bool],
    metrics_summary: Optional[Mapping[str, Any]],
    ts_meta_stats: Dict[str, Any],
    canonical_scc_meta=None,
) -> Dict[str, Any]:
    canonical_scc_meta = canonical_scc_meta or {}
    
    return {
        "schema_version": determine_merged_schema_version(nodefacts_by_lang),
        "language": "mixed",
        "nodes": merged_nodes,
        "meta": {
            "nodes": len(merged_nodes),
            "sources": sources_meta,
            "loc_code": metrics_int(metrics_summary, "loc_code", 0),
            "sloc_code": metrics_int(metrics_summary, "sloc_code", 0),
            "comment_lines_code": metrics_int(metrics_summary, "comment_lines_code", 0),
            "blank_lines_code": metrics_int(metrics_summary, "blank_lines_code", 0),
            "comment_pct_code": metrics_val(metrics_summary, "comment_pct_code"),
            "metrics_code_files": metrics_int(metrics_summary, "metrics_code_files", 0),
            "metrics_missing_files": metrics_int(metrics_summary, "metrics_missing_files", 0),
            "loc_by_lang": metrics_val(metrics_summary, "loc_by_lang", {}),
            "sloc_by_lang": metrics_val(metrics_summary, "sloc_by_lang", {}),
            "scc_conceptual": canonical_scc_meta.get("scc_conceptual"),
            "scc_runtime": canonical_scc_meta.get("scc_runtime"),
            "tc_inflation": canonical_scc_meta.get("tc_inflation"),
            "comment_lines_by_lang": metrics_val(metrics_summary, "comment_lines_by_lang", {}),
            "blank_lines_by_lang": metrics_val(metrics_summary, "blank_lines_by_lang", {}),
            "files_by_lang": metrics_val(metrics_summary, "files_by_lang", {}),
            "typescript_metadata": ts_meta_stats,
            **extract_scc_meta(nodefacts_by_lang),
        },
    }


def build_edges_payload(
    *,
    merged_edges: list[dict[str, Any]],
    crosstalk_edges: list[dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "schema_version": "edges@v1",
        "language": "mixed",
        "edges": merged_edges,
        "meta": {
            "total": len(merged_edges),
            "crosstalk_edges": len(crosstalk_edges),
        },
    }


def build_inputs_report(
    *,
    discovered: Mapping[str, Mapping[str, Optional[Path]]],
    specs: Sequence[LanguageSpec] = LANGUAGE_SPECS,
) -> Dict[str, Optional[str]]:
    inputs: Dict[str, Optional[str]] = {}

    for spec in specs:
        prefix = spec.key
        lang_paths = discovered.get(spec.lang, {})
        inputs[f"{prefix}_nodefacts"] = str(lang_paths.get("nodefacts")) if lang_paths.get("nodefacts") else None
        inputs[f"{prefix}_edges"] = str(lang_paths.get("edges")) if lang_paths.get("edges") else None
        inputs[f"{prefix}_folder_index"] = str(lang_paths.get("folder_index")) if lang_paths.get("folder_index") else None
        inputs[f"{prefix}_reachable"] = str(lang_paths.get("reachable")) if lang_paths.get("reachable") else None

    return inputs


def build_counts_report(
    *,
    nodes_by_lang: Mapping[str, Dict[str, Dict[str, Any]]],
    edges_by_lang: Mapping[str, list[dict[str, Any]]],
    merged_nodes: Dict[str, Dict[str, Any]],
    merged_edges: list[dict[str, Any]],
    crosstalk_edges: list[dict[str, Any]],
    merged_fi: Any,
    merged_reach: Any,
    metrics_summary: Optional[Mapping[str, Any]],
    specs: Sequence[LanguageSpec] = LANGUAGE_SPECS,
) -> Dict[str, Any]:
    counts: Dict[str, Any] = {}

    for spec in specs:
        prefix = spec.key
        counts[f"nodes_{prefix}"] = len(nodes_by_lang.get(spec.lang, {}))
        counts[f"edges_{prefix}"] = len(edges_by_lang.get(spec.lang, []))

    counts.update(
        {
            "nodes_merged": len(merged_nodes),
            "edges_crosstalk": len(crosstalk_edges),
            "edges_merged": len(merged_edges),
            "folder_index_merged": bool(merged_fi is not None),
            "reachable_merged": bool(merged_reach is not None),
            "loc_code": metrics_int(metrics_summary, "loc_code", 0),
            "sloc_code": metrics_int(metrics_summary, "sloc_code", 0),
            "comment_lines_code": metrics_int(metrics_summary, "comment_lines_code", 0),
            "blank_lines_code": metrics_int(metrics_summary, "blank_lines_code", 0),
            "metrics_code_files": metrics_int(metrics_summary, "metrics_code_files", 0),
            "metrics_missing_files": metrics_int(metrics_summary, "metrics_missing_files", 0),
        }
    )

    return counts


def publish_artifacts(
    *,
    out_dir: Path,
    nf_payload: Dict[str, Any],
    edges_payload: Dict[str, Any],
    merged_fi: Any,
    merged_reach: Any,
    persist_folder_index: bool,
    persist_reachable: bool,
) -> Dict[str, Optional[str]]:
    out_nf_p = out_dir / "nodefacts.json"
    out_edges_p = out_dir / "edges.json"
    out_fi_p = out_dir / "folder_index.json"
    out_reach_p = out_dir / "reachable.json"

    write_json(out_nf_p, nf_payload)
    write_json(out_edges_p, edges_payload)

    published_folder_index: Optional[str] = None
    if persist_folder_index and merged_fi is not None:
        write_json(out_fi_p, merged_fi)
        published_folder_index = str(out_fi_p)

    published_reachable: Optional[str] = None
    if persist_reachable and merged_reach is not None:
        write_json(out_reach_p, merged_reach)
        published_reachable = str(out_reach_p)

    return {
        "nodefacts": str(out_nf_p),
        "edges": str(out_edges_p),
        "folder_index": published_folder_index,
        "reachable": published_reachable,
    }