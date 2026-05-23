# analyzer/index.py
from __future__ import annotations
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from analyzer.analyzer_types import (
    SemanticEntry,
    Edge,
    ImportRef,
    NameMap,
    FileNode,
    ParsedModule,
    SymbolSummary,
)
from analyzer.config import AnalyzerCfg, load_config
from analyzer.parse import parse_file
from analyzer.module_resolve import (
    module_names_for_path, drop_init_suffix, resolve_import_candidates
)
from analyzer.module_map import ModuleMap
from adapters.canonical import normalize_root as norm_root_SSOT
from i_o.workspace_io import get_active_workspace

# ---------- helpers ----------

def _safe_path(node_or_path: Any) -> Path:
    """
    Accept FileNode-like (has .path), mapping with 'path', or a path-like.
    Always returns a Path (best-effort, no exceptions).
    """
    try:
        p = getattr(node_or_path, "path", None)
    except Exception:
        p = None
    if p is None and isinstance(node_or_path, dict):
        p = node_or_path.get("path")
    if p is None:
        p = node_or_path
    try:
        return Path(p)
    except Exception:
        return get_active_workspace().scan_root

def _normalize_root(root: Path | str) -> Path:
    """
    Make sure the provided root is a directory-like 'scan root' (read-only):
      - expanduser, resolve (non-strict)
      - if points to a file, use its parent
    Delegates to adapters.canonical.normalize_root (SSOT).
    """
    p = norm_root_SSOT(root)
    if p is not None:
        return p
    # Conservative fallback (shouldn't normally run)
    r = Path(root).expanduser()
    try:
        r = r.resolve(strict=False)
    except Exception:
        pass

def _defs_from_parsed(parsed: ParsedModule) -> Dict[str, List[str]]:
    """
    Convert SymbolSummary lists into the simple dict shape expected by SemanticEntry.defs.
    """
    def _names(items: List[SymbolSummary]) -> List[str]:
        return [s.name for s in (items or [])]
    return {
        "classes": _names(parsed.classes),
        "functions": _names(parsed.functions),
        "globals": _names(parsed.globals),
    }

def _build_alias_map(refs: List[ImportRef]) -> Dict[str, str]:
    """
    Build a per-file alias map: local_name -> fully_qualified_target.
    - 'from X import Y as A' -> A: 'X.Y' (or Y: 'X.Y' if no alias)
    - 'import pkg.sub as ps'  -> ps: 'pkg.sub' (or 'pkg' -> 'pkg' if no alias)
    - star imports are skipped
    """
    aliases: Dict[str, str] = {}
    for ref in refs or []:
        if ref.is_from:
            base = ref.module_token or ""
            for name, asname in ref.names:
                if name == "*":
                    continue
                target = f"{base}.{name}" if base else name
                aliases[asname or name] = target
        else:
            for name, asname in ref.names:
                if name == "*":
                    continue
                bound = asname or name.split(".")[0]
                aliases[bound] = name
    return aliases


# ---------- public API ----------

def build_indices(
    root: Path,
    files: Dict[str, FileNode],
    *,
    cfg: Optional[AnalyzerCfg] = None,
) -> Tuple[
    Dict[str, SemanticEntry],   # sem_idx
    Dict[str, List[ImportRef]], # lex_idx
    NameMap                     # alias maps
]:
    """
    Build dual indices from the single source of truth (parse_file):
      - sem_idx: per-file SemanticEntry (defs + lexical import refs)
      - lex_idx: per-file ImportRef list (lexical)
      - NameMap: alias maps for diagnostics

    Notes:
      - Respects AnalyzerCfg guards (max_file_bytes, include_type_checking_imports).
      - Tolerant to IO/parse errors (skips file with minimal stubs).
      - Uses module_names_for_path to provide (physical, logical) names.
    """
    root = _normalize_root(root)
    cfg = cfg or load_config(root) or AnalyzerCfg()

    sem_idx: Dict[str, SemanticEntry] = {}
    lex_idx: Dict[str, List[ImportRef]] = {}
    per_file_aliases: Dict[str, Dict[str, str]] = {}

    # For global rollups
    alias_to_modules_acc: Dict[str, set] = {}
    module_to_aliases_acc: Dict[str, set] = {}

    for fid, node in files.items():
        path = _safe_path(node)

        # Single source of truth: parse_file (emits ParsedModule + warnings)
        parsed, warns = parse_file(path, cfg)

        # Robust stubs if parse fails
        if parsed is None:
            parsed = ParsedModule(
                classes=[],
                functions=[],
                globals=[],
                imports_ast=[],          # lexical refs (parse_file respects cfg)
                all_exports=[],
                warnings=warns or [],
                # fields used by edge builder (remain empty if parse failed)
                imports=[],
                type_checking_blocks=[],
                crosstalk_candidates_py_v1=[],
            )

        # Module names (canonical)
        module_physical, module_logical = module_names_for_path(root, path)

        # Defs & Imports (from parsed)
        defs = _defs_from_parsed(parsed)
        refs = list(parsed.imports_ast or [])  # lexical refs as-is

        # ---- Expose classification on the FileNode for bridge consumers ----
        # This lets ui/rows.py and adapters.* read node.classes/functions/globals directly.
        try:
            setattr(node, "parsed", parsed)
            setattr(node, "classes", parsed.classes)
            setattr(node, "functions", parsed.functions)
            setattr(node, "globals", parsed.globals)
            setattr(node, "crosstalk_candidates_py_v1", parsed.crosstalk_candidates_py_v1)
        except Exception:
            pass

        sem_idx[fid] = SemanticEntry(
            file_id=fid,
            path=str(path),
            module_physical=module_physical,
            module_logical=module_logical,
            defs=defs,
            imports_ast=refs,
        )
        lex_idx[fid] = refs

        # Per-file alias map
        aliases = _build_alias_map(refs)
        per_file_aliases[fid] = aliases

        # Global rollups
        for a, q in aliases.items():
            alias_to_modules_acc.setdefault(a, set()).add(q)
            module_to_aliases_acc.setdefault(q, set()).add(a)

    name_map = NameMap(
        per_file_aliases=per_file_aliases,
        alias_to_modules={k: sorted(list(v)) for k, v in alias_to_modules_acc.items()},
        module_to_aliases={k: sorted(list(v)) for k, v in module_to_aliases_acc.items()},
    )
    return sem_idx, lex_idx, name_map


def build_indices_with_diagnostics(
    root: Path,
    files: Dict[str, FileNode],
    *,
    cfg: Optional[AnalyzerCfg] = None,
):
    root = _normalize_root(root)
    cfg = cfg or load_config(root) or AnalyzerCfg()
    # First pass builds indices and stashes parsed on each node
    sem_idx, lex_idx, name_map = build_indices(root, files, cfg=cfg)

    diagnostics: Dict[str, List[str]] = {}
    for fid, node in files.items():
        parsed = getattr(node, "parsed", None)
        if parsed is not None and getattr(parsed, "warnings", None):
            warns = list(parsed.warnings or [])
        else:
            # Fallback only if not already parsed (rare)
            path = _safe_path(node)
            parsed, warns = parse_file(path, cfg)
            try:
                setattr(node, "parsed", parsed)
            except Exception:
                pass
        if warns:
            diagnostics[fid] = warns

    return sem_idx, lex_idx, name_map, diagnostics


# ---------- bridge for edge builder ----------

def build_edges_inputs(
    root: Path,
    files: Dict[str, FileNode],
    *,
    cfg: Optional[AnalyzerCfg] = None,
) -> Tuple[
    Dict[str, FileNode],                                # nodes: file_id -> FileNode
    Dict[str, str],                                     # mod_by_id: file_id -> dotted module
    Dict[str, Tuple[ParsedModule, List[str]]],          # parsed_cache: file_id -> (ParsedModule, warnings)
]:
    """
    Produce the exact trio expected by the edge builder:
      - nodes: your original FileNode dict (unchanged)
      - mod_by_id: file_id -> module_logical (dotted)
      - parsed_cache: file_id -> (ParsedModule, warnings) with .imports and .type_checking_blocks populated

 
    This uses parse_file() once per file and aligns names with module_names_for_path.
    """
    root = _normalize_root(root)
    cfg = cfg or load_config(root) or AnalyzerCfg()

    nodes: Dict[str, FileNode] = files  # pass-through
    mod_by_id: Dict[str, str] = {}
    parsed_cache: Dict[str, Tuple[ParsedModule, List[str]]] = {}

    for fid, node in files.items():
        path = _safe_path(node)

        parsed = getattr(node, "parsed", None)
        warns: List[str] = []
        if parsed is None:
            parsed, warns = parse_file(path, cfg)
            try:
                setattr(node, "parsed", parsed)
            except Exception:
                pass
            if parsed is not None:
                try:
                    node.classes = parsed.classes
                    node.functions = parsed.functions
                    node.globals = parsed.globals
                    node.crosstalk_candidates_py_v1 = parsed.crosstalk_candidates_py_v1
                except Exception:
                    pass

        else:
            # Preserve any warnings already captured
            warns = list(getattr(parsed, "warnings", []) or [])

        if parsed is None:
            parsed = ParsedModule(
                classes=[], functions=[], globals=[],
                imports_ast=[], all_exports=[], warnings=warns or [],
                imports=[], type_checking_blocks=[],
            )

        _, module_logical = module_names_for_path(root, path)

        mod_by_id[fid] = module_logical or fid
        parsed_cache[fid] = (parsed, warns or [])

    return nodes, mod_by_id, parsed_cache


def build_edges_lookups(
    root: Path,
    files: Dict[str, FileNode],
    *,
    cfg: Optional[AnalyzerCfg] = None,
) -> Tuple[
    Dict[str, FileNode],                                # nodes
    Dict[str, str],                                     # mod_by_id: file_id -> dotted
    Dict[str, Tuple[ParsedModule, List[str]]],          # parsed_cache
    Dict[str, str],                                     # dotted_to_file: dotted -> file_id
]:
    nodes, mod_by_id, parsed_cache = build_edges_inputs(root, files, cfg=cfg)
    dotted_to_file: Dict[str, str] = {}
    for fid, dotted in mod_by_id.items():
        if dotted:
            dotted_to_file.setdefault(dotted, fid)  # prefer first; adjust if needed
    return nodes, mod_by_id, parsed_cache, dotted_to_file

# --- add at the end of index.py ---

def build_import_edges(
    root: Path,
    files: Dict[str, FileNode],
    *,
    cfg: Optional[AnalyzerCfg] = None,
) -> List[Edge]:
    """
    Produce import edges by resolving ImportRef entries via resolve_import_candidates.
    This is the missing link that turns lexical imports into actual edges.
    """
    root = _normalize_root(root)
    cfg = cfg or load_config(root) or AnalyzerCfg()

    # Reuse our existing indexing pass
    sem_idx, lex_idx, _name_map = build_indices(root, files, cfg=cfg)

    # Module map for physical resolution
    mmap = ModuleMap(root, cfg)

    edges: List[Edge] = []

    for file_id, sem in sem_idx.items():
        # Determine the consumer's logical module (drop __init__ for pkg-relative resolution)
        consumer_logical = drop_init_suffix(sem.module_logical or "")

        for iref in lex_idx.get(file_id, []):
            # Respect TYPE_CHECKING filtering
            if not getattr(cfg, "include_type_checking_imports", True) and iref.under_type_checking:
                continue

            # Resolve based on 'import ...' vs 'from ... import ...'
            if not iref.is_from:
                # plain: import pkg, import pkg.sub as ps
                for mod_name, _asname in iref.names:
                    if mod_name == "*":
                        continue
                    hard, soft = resolve_import_candidates(
                        mod_token=mod_name,
                        names=[],
                        star=False,
                        consumer_logical_module=consumer_logical,
                        m=mmap,
                        cfg=cfg,
                        level=0,  # absolute
                    )   
  
                    for pid in hard:
                        edges.append(Edge(
                            kind="import",
                            src=pid,
                            dst=file_id,
                            meta={
                                "evidence": "lex",
                                "strength": "hard",
                                "scope": iref.scope,
                                "tags": iref.tags,
                                "lineno": iref.lineno,
                            },
                        ))
                    for pid in soft:
                        edges.append(Edge(
                            kind="import",
                            src=pid,
                            dst=file_id,
                            meta={
                                "evidence": "facade/dynamic",
                                "strength": "soft",
                                "scope": iref.scope,
                                "tags": iref.tags,
                                "lineno": iref.lineno,
                            },
                        ))
            else:
                # from .foo import a, b  |  from . import a
                star = any(n == "*" for n, _ in iref.names)
                names = [n for n, _ in iref.names if n != "*"]

                hard, soft = resolve_import_candidates(
                    mod_token=iref.module_token or "",   # raw token, no prefixed dots
                    names=names,
                    star=star,
                    consumer_logical_module=consumer_logical,
                    m=mmap,
                    cfg=cfg,
                    level=iref.level or 0,               # << pass relative level here
                )
    
                for pid in hard:
                    edges.append(Edge(
                        kind="import",
                        src=pid,
                        dst=file_id,
                        meta={
                            "evidence": "lex",
                            "strength": "hard",
                            "scope": iref.scope,
                            "tags": iref.tags,
                            "lineno": iref.lineno,
                        },
                    ))
                for pid in soft:
                    edges.append(Edge(
                        kind="import",
                        src=pid,
                        dst=file_id,
                        meta={
                            "evidence": "facade/dynamic",
                            "strength": "soft",
                            "scope": iref.scope,
                            "tags": iref.tags,
                            "lineno": iref.lineno,
                        },
                    ))

    return edges
