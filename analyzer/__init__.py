# analyzer/__init__.py
"""
Analyzer public API router.

External code should import from here (not submodules) to avoid binding to
internal layout.

Example:
    from analyzer import (
        AnalyzerCfg, load_config, load_config_search,
        ModuleMap, module_names_for_path, expand_targets,
        extract_lexical_imports, parse_file,
        build_indices_with_diagnostics, build_indices,
        build_edges_inputs, build_edges_lookups,
        FileNode, ImportRef, SemanticEntry, NameMap, Edge, Graph,
        AnalysisResult,
    )
"""

__all__ = [
    # config
    "AnalyzerCfg", "load_config", "load_config_search",

    # fs (workspace + discovery)
    "SUPPORTED_EXTS", "EXCLUDE_DIRS", "is_excluded",
    "ensure_dir_root", "infer_project_root", "normalize", "is_supported",
    "normalize_files", "iter_source_files", "iter_project_files",
    "detect_src_roots", "contiguous_pkg_chain",
    "Workspace", "merge_includes", "normalize_workspace",
    "add_files", "add_folder", "clear_workspace", "common_root",

    # module ↔ path mapping
    "ModuleMap",

    # import resolution semantics
    "absolutize_module", "expand_targets",
    "module_names_for_path", "drop_init_suffix",
    "is_external_module", "producer_id_for_module",
    "resolve_dotted", "logical_module_for_path", "resolve_import_candidates",

    # lexical imports & parsing
    "extract_lexical_imports", "parse_file", "ParsedModule",

    # indices
    "build_indices_with_diagnostics", "build_indices",
    "build_edges_inputs", "build_edges_lookups",
    "build_import_edges",

    # types (models)
    "SymbolSummary", "ImportRowMeta", "FileNode",
    "ImportRef", "SemanticEntry", "NameMap",
    "EdgeEvidence", "EdgeReason", "Edge", "Graph", "AnalysisResult", "Layout",

    # parsing
    "parse_module",

    # top-level helper
    "analyze",

    # duplicate code analyzer
    "analyze_duplicate_code",
    "DuplicateCodeReport", "CloneGroup", "CloneMember",

    # dead code analyzer
    "analyze_dead_code",
    "DeadCodeReport",
    "ModuleReport",
    "SymbolUse",
]


__version__ = "0.1.0"

# --- config ---
from .config import AnalyzerCfg, load_config, load_config_search

# --- fs (workspace & filesystem helpers) ---
from .fs import (
    SUPPORTED_EXTS, EXCLUDE_DIRS, is_excluded,
    ensure_dir_root, infer_project_root, normalize, is_supported,
    normalize_files, iter_source_files, iter_project_files,
    detect_src_roots, contiguous_pkg_chain,
    Workspace, merge_includes, normalize_workspace,
    add_files, add_folder, clear_workspace, common_root,
)

from .duplicate_code import (
    analyze_duplicate_code,
    DuplicateCodeReport,
    CloneGroup,
    CloneMember,
)

# --- module mapping ---
from .module_map import ModuleMap

# --- import resolution semantics ---
from .module_resolve import (
    absolutize_module,
    expand_targets,
    module_names_for_path,
    drop_init_suffix,
    is_external_module,
    producer_id_for_module,
    resolve_dotted,
    logical_module_for_path, resolve_import_candidates
)

# --- lexical imports & parsing ---
from .imports_lex import extract_lexical_imports
from .parse import parse_file, ParsedModule, parse_module

# --- indices / edge-builder bridges ---
from .index import (
    build_indices_with_diagnostics,
    build_indices,
    build_edges_inputs,
    build_edges_lookups,
    build_import_edges,
)

from .dead_code import analyze_dead_code, DeadCodeReport, ModuleReport, SymbolUse

# --- types (data models) ---
from .analyzer_types import (
    SymbolSummary, ImportRowMeta, FileNode,
    ImportRef, SemanticEntry, NameMap,
    EdgeEvidence, EdgeReason, Edge, Graph,
    AnalysisResult, Layout
)

from pathlib import Path
from typing import Dict, Optional, List

def analyze(
    root: Path,
    files: Dict[str, FileNode],
    *,
    cfg: Optional[AnalyzerCfg] = None,
) -> AnalysisResult:
    """
    One-stop analyzer entrypoint:
      - builds semantic & lexical indices + alias maps
      - collects per-file diagnostics from parse()
      - returns a single AnalysisResult for downstream layers
    """
    # Normalize to a scan root (dir) with non-strict resolve
    root = normalize(ensure_dir_root(Path(root)))

    # Core indices and diagnostics
    sem_idx, lex_idx, name_map, diagnostics = build_indices_with_diagnostics(root, files, cfg=cfg)

    edges = []
    try:
        edges = build_import_edges(root, files, cfg=cfg)
    except Exception as e:
        print(f"[EDGES:BUILD:ERROR] {type(e).__name__}: {e}")

    file_ids: List[str] = list(files.keys())

    # --- construct, then log, then return ---
    res = AnalysisResult(
        root=str(root),
        files=file_ids,
        sem_idx=sem_idx,
        lex_idx=lex_idx,
        name_map=name_map,
        diagnostics=diagnostics,
        # edges=edges,  # uncomment if supported by AnalysisResult
    )

    return res
