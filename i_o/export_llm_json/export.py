from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, List, Set
from collections import Counter

from diagnostics.logging import log_event

from .artifacts import find_artifact, find_optional_artifact, load_json, load_json_optional
from .builders_discovery import build_discovery
from .builders_folders import build_folders, compute_import_summary, merge_folder_indexes
from .builders_node_order import build_node_order
from .builders_nodes_edges import build_edges, build_nodes
from .builders_zones import build_zones
from .summary import build_summary
from .utils import edge_counts_from_edges


def _infer_python_from_node(n: Mapping[str, Any], nid: str) -> bool:
    # Prefer explicit file field if present (python NodeFactsNode has "file")
    f = n.get("file")
    if isinstance(f, str) and f:
        suf = Path(f).suffix.lower()
        return suf in (".py", ".pyi")

    # Fallback: node id is repo-relative file id in your python path
    # (you canonicalize NodeId as a file id)
    suf = Path(nid).suffix.lower()
    return suf in (".py", ".pyi")


def _infer_go_from_node(n: Mapping[str, Any], nid: str) -> bool:
    # Prefer explicit file field if present
    f = n.get("file")
    if isinstance(f, str) and f:
        return Path(f).suffix.lower() == ".go"
    # Fallback: node id might be file-id style
    return Path(nid).suffix.lower() == ".go"


def _infer_java_from_node(n: Mapping[str, Any], nid: str) -> bool:
    # Prefer explicit file field if present
    f = n.get("file")
    if isinstance(f, str) and f:
        return Path(f).suffix.lower() == ".java"
    # Fallback: node id might be file-id style
    return Path(nid).suffix.lower() == ".java"

def _infer_kotlin_from_node(n: Mapping[str, Any], nid: str) -> bool:
    f = n.get("file")
    if isinstance(f, str) and f:
        return Path(f).suffix.lower() in (".kt", ".kts")
    return Path(nid).suffix.lower() in (".kt", ".kts")

def _infer_rust_from_node(n: Mapping[str, Any], nid: str) -> bool:
    """Infer if node is Rust from file extension."""
    # Prefer explicit file field if present
    f = n.get("file")
    if isinstance(f, str) and f:
        return Path(f).suffix.lower() == ".rs"
    # Fallback: node id might be file-id style
    return Path(nid).suffix.lower() == ".rs"


def _norm_lang(s: str) -> str:
    """
    Normalize common language aliases to stable canonical names
    (keeps your bundle meta consistent across analyzers).
    """
    v = (s or "").strip().lower()
    if not v:
        return v
    aliases = {
        "py": "python",
        "python3": "python",
        "ts": "typescript",
        "tsx": "typescript",
        "js": "javascript",
        "jsx": "javascript",
        "go": "go",
        "golang": "go",
        "java": "java",
        "rust": "rust",
        "rs": "rust",
        "kotlin": "kotlin",
        "kt": "kotlin",
        "kts": "kotlin",
    }
    return aliases.get(v, v)


@dataclass
class LLMExportConfig:
    artifact_dir: Path
    output_path: Path
    repo_root: Optional[Path] = None
    repo_name: Optional[str] = None
    mode: str = "zones"
    output_format: str = "standard"  # "standard" | "compressed"
    include_folder_index_payload: bool = False


def export_llm_bundle(config: LLMExportConfig) -> Path:
    artifact_dir = config.artifact_dir

    nodefacts_path = find_artifact(artifact_dir, "nodefacts.json")
    edges_path = find_artifact(artifact_dir, "edges.json")

    zones_path = find_optional_artifact(artifact_dir, "zones_by_package@v1.json")
    folder_index_path = find_optional_artifact(
        artifact_dir,
        "folder_index.json",
        "sets/classic/folder_index.json",
    )

    ts_folder_index_path = find_optional_artifact(
        artifact_dir,
        "analyzers/ts/folder_index.json",
        "ts/folder_index.json",
    )

    go_folder_index_path = find_optional_artifact(
        artifact_dir,
        "analyzers/go/folder_index_go.json",
        "analyzers/go/folder_index.json",
        "go/folder_index_go.json",
        "go/folder_index.json",
    )

    java_folder_index_path = find_optional_artifact(
        artifact_dir,
        "analyzers/java/folder_index_java.json",
        "analyzers/java/folder_index.json",
        "java/folder_index_java.json",
        "java/folder_index.json",
    )

    kotlin_folder_index_path = find_optional_artifact(
        artifact_dir,
        "analyzers/kotlin/folder_index_kotlin.json",
        "analyzers/kotlin/folder_index.json",
        "kotlin/folder_index_kotlin.json",
        "kotlin/folder_index.json",
    )

    rust_folder_index_path = find_optional_artifact(
        artifact_dir,
        "analyzers/rust/folder_index_rust.json",
        "analyzers/rust/folder_index.json",
        "rust/folder_index_rust.json",
        "rust/folder_index.json",
    )
    
    reachable_path = find_optional_artifact(artifact_dir, "reachable.json")
    discovery_path = find_optional_artifact(
        artifact_dir,
        "discovery_manifest@v1.json",
        "discovery_manifest.json",
    )

    nodefacts_data = load_json(nodefacts_path)
    log_event(
        "EXPORT:nodefacts_loaded",
        path=str(nodefacts_path),
        is_mapping=isinstance(nodefacts_data, Mapping),
        has_nodes_key=("nodes" in nodefacts_data) if isinstance(nodefacts_data, Mapping) else False,
        nodes_count=len(nodefacts_data.get("nodes", {})) if isinstance(nodefacts_data, Mapping) else 0,
    )

    edges_data_raw = load_json(edges_path)

    zones_data = load_json_optional(zones_path) if zones_path is not None else None
    reachable_data = load_json_optional(reachable_path) if reachable_path is not None else None
    discovery_data = load_json_optional(discovery_path) if discovery_path is not None else None

    # ---- Keep all relevant edge artifact context ----
    # edges builder expects a Mapping with {"edges": [...]}; however the artifact may be
    # either a Mapping payload (seed/final_stats/etc) or a bare list in legacy cases.
    if isinstance(edges_data_raw, Mapping):
        edges_data: Mapping[str, Any] = edges_data_raw  # type: ignore[assignment]
        edges_meta: Optional[Dict[str, Any]] = {
            k: v for k, v in dict(edges_data_raw).items() if k != "edges"
        } or None
    else:
        edges_data = {"edges": edges_data_raw}  # legacy: list-form artifact
        edges_meta = None

    nodes = build_nodes(nodefacts_data)
    edges = build_edges(edges_data)

    out_by_src, in_by_dst = edge_counts_from_edges(edges)

    # Avoid mutating while iterating
    nodes2: Dict[str, Any] = {}
    for nid, n in nodes.items():
        if not isinstance(n, Mapping):
            nodes2[nid] = n
            continue
        nn = dict(n)
        nn["dependencies_count"] = int(out_by_src.get(nid, 0))
        nn["importers_count"] = int(in_by_dst.get(nid, 0))

        # Backfill language if not explicitly set
        if not (nn.get("lang") or nn.get("language")):
            if _infer_python_from_node(nn, nid):
                nn["language"] = "python"
            elif _infer_go_from_node(nn, nid):
                nn["language"] = "go"
            elif _infer_java_from_node(nn, nid):
                nn["language"] = "java"
            elif _infer_kotlin_from_node(nn, nid):
                nn["language"] = "kotlin"
            elif _infer_rust_from_node(nn, nid):
                nn["language"] = "rust"

        nodes2[nid] = nn
    nodes = nodes2

    zones = build_zones(zones_data) if zones_data is not None else []

    def _fi_count(fi: Any) -> int:
        if not isinstance(fi, Mapping):
            return 0
        files = fi.get("files")
        if isinstance(files, Mapping):
            return len(files)
        if isinstance(files, list):
            return len(files)
        return 0

    log_event(
        "EXPORT:folder_index_paths",
        python=str(folder_index_path) if folder_index_path else None,
        ts=str(ts_folder_index_path) if ts_folder_index_path else None,
        go=str(go_folder_index_path) if go_folder_index_path else None,
        java=str(java_folder_index_path) if java_folder_index_path else None,
        kotlin=str(kotlin_folder_index_path) if kotlin_folder_index_path else None,
        rust=str(rust_folder_index_path) if rust_folder_index_path else None,
    )

    # Folder index is expected to be canonical (folder_index.json), but keep
    # per-analyzer fallbacks for resilience during rollout / partial builds.
    python_folder_index = load_json_optional(folder_index_path) if folder_index_path is not None else None
    ts_folder_index = load_json_optional(ts_folder_index_path) if ts_folder_index_path is not None else None
    go_folder_index = load_json_optional(go_folder_index_path) if go_folder_index_path is not None else None
    java_folder_index = load_json_optional(java_folder_index_path) if java_folder_index_path is not None else None
    kotlin_folder_index = load_json_optional(kotlin_folder_index_path) if kotlin_folder_index_path is not None else None
    rust_folder_index = load_json_optional(rust_folder_index_path) if rust_folder_index_path is not None else None

    merged_folder_index = merge_folder_indexes(
        python_folder_index,
        ts_folder_index,
        go_folder_index,
        java_folder_index,
        kotlin_folder_index,
        rust_folder_index,
    )

    log_event(
        "EXPORT:folder_index_loaded_counts",
        python=_fi_count(python_folder_index),
        ts=_fi_count(ts_folder_index),
        go=_fi_count(go_folder_index),
        java=_fi_count(java_folder_index),
        kotlin=_fi_count(kotlin_folder_index),
        rust=_fi_count(rust_folder_index),
        merged=_fi_count(merged_folder_index),
    )

    folders = build_folders(merged_folder_index) if merged_folder_index is not None else None

    node_order = build_node_order(reachable_data) if reachable_data is not None else sorted(nodes.keys())

    import_summary = None
    if folders is not None and isinstance(folders, Mapping) and isinstance(folders.get("files"), Mapping):
        import_summary = compute_import_summary(folders["files"])  # type: ignore[arg-type]

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # --- Derive bundled language(s) from nodes (polyglot-safe) ---
    bundled_langs: Counter[str] = Counter()
    missing_explicit_lang = 0
    inferred_python = 0
    inferred_go = 0
    inferred_java = 0
    inferred_kotlin = 0
    inferred_rust = 0

    for nid, n in (nodes or {}).items():
        if not isinstance(n, Mapping):
            continue

        # Prefer explicit language fields (from TS/JS or any future analyzers)
        raw_lang = n.get("lang") or n.get("language")
        if isinstance(raw_lang, str) and raw_lang.strip():
            bundled_langs[_norm_lang(raw_lang)] += 1
            continue

        # Avoid treating generic "kind" as a language (it tends to be "file", etc.)
        # but if you intentionally store language in "kind" in some analyzer, keep it.
        raw_kind = n.get("kind")
        if isinstance(raw_kind, str) and raw_kind.strip():
            k = _norm_lang(raw_kind)
            if k in ("python", "typescript", "javascript", "go", "java", "rust"):
                bundled_langs[k] += 1
                continue

        # Fallback inference from file / nid suffixes
        if _infer_python_from_node(n, nid):
            bundled_langs["python"] += 1
            inferred_python += 1
        elif _infer_go_from_node(n, nid):
            bundled_langs["go"] += 1
            inferred_go += 1
        elif _infer_java_from_node(n, nid):
            bundled_langs["java"] += 1
            inferred_java += 1
        elif _infer_kotlin_from_node(n, nid):
            bundled_langs["kotlin"] += 1
            inferred_kotlin += 1
        elif _infer_rust_from_node(n, nid):
            bundled_langs["rust"] += 1
            inferred_rust += 1
        else:
            missing_explicit_lang += 1

    bundled_languages = sorted(bundled_langs.keys())
    derived_language = (
        "polyglot"
        if len(bundled_languages) > 1
        else (bundled_languages[0] if bundled_languages else "unknown")
    )

    log_event(
        "EXPORT:bundled_languages_derived",
        derived_language=derived_language,
        languages=bundled_languages,
        bundled_by_lang=dict(bundled_langs),
        inferred_python=inferred_python,
        inferred_go=inferred_go,
        inferred_java=inferred_java,
        inferred_kotlin=inferred_kotlin,
        inferred_rust=inferred_rust,
        missing_explicit_lang=missing_explicit_lang,
    )

    # ---- NEW: compute compact SLOC summary from merged folder index (no persistence required) ----
    def _allowed_code_exts_for_langs(langs: List[str]) -> Set[str]:
        # Keep conservative + aligned with your validators/analyzers
        ext_by_lang: Dict[str, Set[str]] = {
            "python": {".py"},
            "typescript": {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"},
            "javascript": {".js", ".jsx", ".mjs", ".cjs"},
            "go": {".go"},
            "java": {".java"},
            "kotlin": {".kt", ".kts"},
            "rust": {".rs"},
        }
        out: Set[str] = set()
        for l in langs:
            out |= ext_by_lang.get(_norm_lang(l), set())
        return out

    def _compute_code_metrics_from_folder_index(
        fi: Any,
        *,
        allowed_exts: Optional[Set[str]],
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(fi, Mapping):
            return None

        files_obj = fi.get("files")
        if not isinstance(files_obj, Mapping):
            return None

        loc_total = 0
        sloc_total = 0
        comment_total = 0
        blank_total = 0
        files_count = 0

        by_ext_sloc: Counter[str] = Counter()
        by_ext_loc: Counter[str] = Counter()
        by_ext_comments: Counter[str] = Counter()
        by_ext_blanks: Counter[str] = Counter()

        for f in files_obj.values():
            if not isinstance(f, Mapping):
                continue

            if f.get("eligible") is False:
                continue

            ps = f.get("parse_status")
            if isinstance(ps, str) and ps.strip().lower() == "skipped":
                continue

            p = f.get("file") or f.get("rel_posix") or f.get("path") or f.get("filename")
            if not isinstance(p, str) or not p:
                continue

            ext = Path(p).suffix.lower()
            if allowed_exts is not None and ext not in allowed_exts:
                continue

            try:
                loc_raw = f.get("loc")
                sloc_raw = f.get("sloc")

                if loc_raw is None or sloc_raw is None:
                    continue  # skip invalid files

                loc = int(loc_raw)
                sloc = int(sloc_raw)
                comments = int(f.get("comment_lines") or 0)
                blanks = int(f.get("blank_lines") or 0)
 
            except Exception:
                continue

            loc_total += loc
            sloc_total += sloc
            comment_total += comments
            blank_total += blanks
            files_count += 1

            by_ext_sloc[ext] += sloc
            by_ext_loc[ext] += loc
            by_ext_comments[ext] += comments
            by_ext_blanks[ext] += blanks

        return {
            "loc_code": loc_total,
            "sloc_code": sloc_total,
            "comment_lines_code": comment_total,
            "blank_lines_code": blank_total,
            "comment_pct_code": (comment_total / loc_total) if loc_total else None,
            "metrics_code_files": files_count,

            "sloc_by_ext_code": dict(by_ext_sloc),
            "loc_by_ext_code": dict(by_ext_loc),
            "comment_lines_by_ext_code": dict(by_ext_comments),
            "blank_lines_by_ext_code": dict(by_ext_blanks),
            "metrics_source": "merged_folder_index",
        }

    allowed_exts = _allowed_code_exts_for_langs(bundled_languages) if bundled_languages else None
    metrics_summary = _compute_code_metrics_from_folder_index(
        merged_folder_index,
        allowed_exts=allowed_exts,
    )

    if metrics_summary is not None:
        log_event(
            "EXPORT:metrics_summary_computed",
            loc_code=int(metrics_summary.get("loc_code", 0) or 0),
            sloc_code=int(metrics_summary.get("sloc_code", 0) or 0),
            comment_lines_code=int(metrics_summary.get("comment_lines_code", 0) or 0),
            blank_lines_code=int(metrics_summary.get("blank_lines_code", 0) or 0),
            metrics_code_files=int(metrics_summary.get("metrics_code_files", 0) or 0),
            metrics_source=str(metrics_summary.get("metrics_source", "")),
            langs=bundled_languages,
        )

    meta: Dict[str, Any] = {
        "generated_at": generated_at,
        "mode": config.mode,
        "language": derived_language,
        "languages": bundled_languages,
        "bundled_by_lang": dict(bundled_langs),
    }

    if config.repo_root is not None:
        meta["repo_root"] = str(config.repo_root)
    if config.repo_name is not None:
        meta["repo_name"] = config.repo_name

    # Preserve edge artifact metadata (seed/final_stats/policy/etc) for context generation.
    if edges_meta is not None:
        meta["edges_meta"] = edges_meta

    # stamp compact code metrics into meta (from merged folder index)
    if metrics_summary is not None:
        meta["loc_code"] = int(metrics_summary.get("loc_code", 0) or 0)
        meta["sloc_code"] = int(metrics_summary.get("sloc_code", 0) or 0)
        meta["comment_lines_code"] = int(metrics_summary.get("comment_lines_code", 0) or 0)
        meta["blank_lines_code"] = int(metrics_summary.get("blank_lines_code", 0) or 0)
        meta["comment_pct_code"] = metrics_summary.get("comment_pct_code")

        meta["metrics_code_files"] = int(metrics_summary.get("metrics_code_files", 0) or 0)

        # Optional debug / breakdown signals
        meta["sloc_by_ext_code"] = metrics_summary.get("sloc_by_ext_code") or {}
        meta["loc_by_ext_code"] = metrics_summary.get("loc_by_ext_code") or {}

        meta["metrics_source"] = metrics_summary.get("metrics_source") or "merged_folder_index"

    bundle: Dict[str, Any] = {
        "schema_version": "pviz-llm-bundle@v1.1",
        "meta": meta,
        "nodes": nodes,
        "edges": edges,
    }

    if zones:
        bundle["zones"] = zones
        # Also keep the raw zones artifact payload if present (context/debug)
        if zones_data is not None and isinstance(zones_data, Mapping):
            bundle["zones_artifact"] = zones_data

    if folders:
        bundle["folders"] = folders

        # IMPORTANT CHANGE (storage-safe default):
        # Previously you embedded the entire merged_folder_index payload into the bundle for context/debug.
        # That can be very large. Default to NOT embedding it, but allow opt-in if config provides a flag.
        include_fi_payload = bool(getattr(config, "include_folder_index_payload", False))
        if include_fi_payload and merged_folder_index is not None and isinstance(merged_folder_index, Mapping):
            bundle["folder_index"] = merged_folder_index

    if node_order:
        bundle["node_order"] = node_order

    bundle["summary"] = build_summary(
        meta=meta,
        nodes=nodes,
        edges=edges,
        zones=zones,
        folders=folders,
        node_order=node_order,
        import_summary=import_summary,
        nodefacts_meta=(
            nodefacts_data.get("meta")
            if isinstance(nodefacts_data, Mapping)
            else None
        ),
    )

    if discovery_data is not None and isinstance(discovery_data, Mapping):
        bundle["discovery"] = build_discovery(discovery_data, nodes=nodes)
        # Keep raw discovery manifest too (context/debug)
        bundle["discovery_manifest"] = discovery_data

    if import_summary is not None:
        bundle["import_summary"] = import_summary

    config.output_path.parent.mkdir(parents=True, exist_ok=True)

    import json
    
    # Apply compression if requested
    if config.output_format == "compressed":
        try:
            from .json_compression.run import apply_schema_encoding
            bundle = apply_schema_encoding(bundle)
            log_event("EXPORT:compression_applied", format="compressed")
        except Exception as e:
            log_event("EXPORT:compression_failed", error=str(e))
            # Fall back to standard format on compression error
            config.output_format = "standard"
    
    # Write with appropriate formatting
    with config.output_path.open("w", encoding="utf-8") as f:
        if config.output_format == "compressed":
            # Compressed: no indentation, minimal separators
            json.dump(bundle, f, separators=(',', ':'), sort_keys=False)
        else:
            # Standard: pretty-printed
            json.dump(bundle, f, indent=2, sort_keys=False)

    return config.output_path