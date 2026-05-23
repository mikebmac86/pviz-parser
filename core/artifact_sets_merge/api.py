from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .scc import inject_scc_fields_from_edges
from .config import LANGUAGE_SPECS
from .crosstalk import generate_crosstalk_edges
from .discovery import discover_language_inputs, resolve_optional_dir
from .edges import dedupe_edges, merge_edges_by_language
from .folder_index import merge_folder_indexes
from .io import load_discovered_inputs
from .meta import compute_typescript_metadata_stats, first_mapping
from .metrics import compute_code_metrics_summary_from_folder_index
from .nodes import merge_nodes_by_language
from .normalize import as_edges_list, as_nodes_map
from .publish import (
    build_counts_report,
    build_edges_payload,
    build_inputs_report,
    build_nodefacts_payload,
    publish_artifacts,
)


def publish_canonical_from_sets(
    *,
    artifacts_root: Path,
    python_set_dir: Path,
    ts_set_dir: Path,
    go_set_dir: Optional[Path] = None,
    java_set_dir: Optional[Path] = None,
    kotlin_set_dir: Optional[Path] = None,
    rust_set_dir: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    merge_folder_indexes_fn: Optional[Any] = None,
    persist_folder_index: bool = False,
    persist_reachable: bool = True,
) -> Dict[str, Any]:
    artifacts_root = artifacts_root.resolve()
    out_dir = (out_dir or artifacts_root).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    set_dirs: Dict[str, Optional[Path]] = {
        "python": python_set_dir.resolve(),
        "ts": ts_set_dir.resolve(),
        "go": resolve_optional_dir(go_set_dir),
        "java": resolve_optional_dir(java_set_dir),
        "kotlin": resolve_optional_dir(kotlin_set_dir),
        "rust": resolve_optional_dir(rust_set_dir),
    }

    discovered = discover_language_inputs(
        artifacts_root=artifacts_root,
        set_dirs=set_dirs,
        specs=LANGUAGE_SPECS,
    )
    loaded, load_errors = load_discovered_inputs(discovered)

    nodefacts_by_lang: Dict[str, Any] = {
        spec.lang: loaded.get(spec.lang, {}).get("nodefacts")
        for spec in LANGUAGE_SPECS
    }
    edges_payloads_by_lang: Dict[str, Any] = {
        spec.lang: loaded.get(spec.lang, {}).get("edges")
        for spec in LANGUAGE_SPECS
    }
    folder_indexes_by_lang: Dict[str, Any] = {
        spec.lang: loaded.get(spec.lang, {}).get("folder_index")
        for spec in LANGUAGE_SPECS
    }
    reachable_by_lang: Dict[str, Any] = {
        spec.lang: loaded.get(spec.lang, {}).get("reachable")
        for spec in LANGUAGE_SPECS
    }

    nodes_by_lang: Dict[str, Dict[str, Dict[str, Any]]] = {
        spec.lang: as_nodes_map(nodefacts_by_lang.get(spec.lang))
        for spec in LANGUAGE_SPECS
    }
    edges_by_lang: Dict[str, list[dict[str, Any]]] = {
        spec.lang: as_edges_list(edges_payloads_by_lang.get(spec.lang))
        for spec in LANGUAGE_SPECS
    }

    merged_nodes = merge_nodes_by_language(nodes_by_lang, LANGUAGE_SPECS)
    merged_edges = merge_edges_by_language(edges_by_lang, LANGUAGE_SPECS)

    crosstalk_edges, crosstalk_stats = generate_crosstalk_edges(nodes_by_lang=nodes_by_lang)
    merged_edges = dedupe_edges([*merged_edges, *crosstalk_edges])

    canonical_scc_meta = inject_scc_fields_from_edges(
        merged_nodes,
        merged_edges,
        include_runtime=True,
    )

    merged_fi = merge_folder_indexes(
        folder_indexes_by_lang=folder_indexes_by_lang,
        merge_folder_indexes_fn=merge_folder_indexes_fn,
        specs=LANGUAGE_SPECS,
    )

    metrics_summary = None
    if isinstance(merged_fi, dict):
        try:
            ms = compute_code_metrics_summary_from_folder_index(merged_fi)
            metrics_summary = ms if isinstance(ms, dict) else None
        except Exception:
            metrics_summary = None

    ts_meta_stats = compute_typescript_metadata_stats(merged_nodes)
    merged_reach = first_mapping(reachable_by_lang.get(spec.lang) for spec in LANGUAGE_SPECS)

    sources_meta = {
        spec.lang: bool(nodefacts_by_lang.get(spec.lang) is not None)
        for spec in LANGUAGE_SPECS
    }
    sources_meta["ts"] = sources_meta.pop("ts", False)

    nf_payload = build_nodefacts_payload(
        merged_nodes=merged_nodes,
        nodefacts_by_lang=nodefacts_by_lang,
        sources_meta=sources_meta,
        metrics_summary=metrics_summary,
        ts_meta_stats=ts_meta_stats,
        canonical_scc_meta=canonical_scc_meta,
    )
    edges_payload = build_edges_payload(
        merged_edges=merged_edges,
        crosstalk_edges=crosstalk_edges,
    )

    published = publish_artifacts(
        out_dir=out_dir,
        nf_payload=nf_payload,
        edges_payload=edges_payload,
        merged_fi=merged_fi,
        merged_reach=merged_reach,
        persist_folder_index=persist_folder_index,
        persist_reachable=persist_reachable,
    )

    inputs = build_inputs_report(discovered=discovered, specs=LANGUAGE_SPECS)
    counts = build_counts_report(
        nodes_by_lang=nodes_by_lang,
        edges_by_lang=edges_by_lang,
        merged_nodes=merged_nodes,
        merged_edges=merged_edges,
        crosstalk_edges=crosstalk_edges,
        merged_fi=merged_fi,
        merged_reach=merged_reach,
        metrics_summary=metrics_summary,
        specs=LANGUAGE_SPECS,
    )

    return {
        "out_dir": str(out_dir),
        "python_set_dir": str(set_dirs["python"]),
        "ts_set_dir": str(set_dirs["ts"]),
        "go_set_dir": str(set_dirs["go"]) if set_dirs["go"] else None,
        "java_set_dir": str(set_dirs["java"]) if set_dirs["java"] else None,
        "kotlin_set_dir": str(set_dirs["kotlin"]) if set_dirs["kotlin"] else None,
        "rust_set_dir": str(set_dirs["rust"]) if set_dirs["rust"] else None,
        "options": {
            "persist_folder_index": bool(persist_folder_index),
            "persist_reachable": bool(persist_reachable),
        },
        "inputs": inputs,
        "load_errors": load_errors,
        "counts": counts,
        "metrics_summary": metrics_summary,
        "crosstalk_stats": crosstalk_stats,
        "typescript_metadata": ts_meta_stats,
        "published": published,
    }