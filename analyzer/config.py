from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Any, Union
from dataclasses import fields as _dc_fields
import os
import math

# --- TOML import robustness ---------------------------------------------------
try:
    import tomllib as _toml  # Py 3.11+
except Exception:  # pragma: no cover
    try:
        import tomli as _toml  # optional dependency on older Pythons
    except Exception:
        _toml = None  # loader will return defaults if None

# ----------------------------
# Analyzer configuration model
# ----------------------------

@dataclass
class AnalyzerCfg:
    """
    Config for discovery, parsing, and graph building. All fields have safe defaults.
    """
    # Workspace discovery
    include: List[str] = field(default_factory=lambda: ["**/*.py"])
    exclude: List[str] = field(default_factory=lambda: [
        "**/.venv/**", "**/.tox/**", "**/.mypy_cache/**", "**/__pycache__/**",
        "**/.git/**", "**/site-packages/**", "**/build/**", "**/dist/**"
    ])
    follow_symlinks: bool = False

    # Source layout / module mapping
    src_layout_dirs: List[str] = field(default_factory=lambda: ["src"])
    allow_namespace_pkgs: bool = True
    honor_dunder_init: bool = True

    # Parsing & indexing
    max_file_bytes: int = 20_000_000
    parse_docstrings: bool = False
    capture_globals: bool = True
    capture_defs: bool = True

    # v1.7 metadata support:
    # Enables richer Python symbol metadata extraction such as type hints,
    # decorators, signatures, methods, attributes, and line numbers.
    #
    # This flag is only about metadata enrichment. It does NOT control SCC
    # computation, runtime-vs-conceptual cycle semantics, or any NodeFacts
    # schema versioning beyond whether detailed metadata fields are populated.
    detailed_symbols: bool = True

    # Import modeling (dual index)
    ast_imports: bool = True
    lexical_imports: bool = True
    include_type_checking_imports: bool = True
    collapse_relative_imports: bool = True

    # Advanced import/facade handling
    scan_function_scope: bool = True
    infer_facade_reexports: bool = True
    dynamic_package_keep: str = "pattern"           # "off" | "pattern" | "all"
    dynamic_keep_patterns: List[str] = field(default_factory=lambda: ["export_*.py"])
    treat_conditional_edges_as_keep: bool = True
    keep_package_on_unresolved_exports: bool = True
    detect_dynamic_imports: bool = True
    delegate_comment_tokens: List[str] = field(default_factory=lambda: [
        "pviz:delegate-import", "pviz:delegate-call"
    ])

    # Edge building
    prefer_ast: bool = True
    longest_prefix_match: bool = True
    external_module_prefixes: List[str] = field(default_factory=list)

    # Pruning & presentation (graph-level)
    prune_zero_degree: bool = False
    prune_tests: bool = False
    test_dir_markers: List[str] = field(default_factory=lambda: ["tests", "test"])

    # Caching
    cache_dir: Optional[str] = None
    use_mtime_cache: bool = True

    # Experimental
    track_alias_maps: bool = True
    capture_edge_evidence: bool = True

    # Layout hints (consumed by ui/scene; kept here for centralization)
    layout_defaults: Dict[str, Any] = field(default_factory=lambda: {
        "lane_mode": "right-anchored",
        "satellites": "imports",
    })

    # ─────────────────────────────────────────────────────────────────────
    # Analyzer coordination flags (used by zones + artifacts)
    # ─────────────────────────────────────────────────────────────────────
    mode: str = "classic"                     # "classic" | "zones"
    emit_edges: bool = True
    edge_policy: str = "strict"               # "strict" | "soft"
    zoom: float = 1.0
    layout_policy: Optional[str] = None


# ----------------------------
# Loader
# ----------------------------

def _merge_dict(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(dst)
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def _normalize(cfg: AnalyzerCfg) -> AnalyzerCfg:
    # Ensure globs are normalized (basic pass)
    cfg.include = list(cfg.include or [])
    cfg.exclude = list(cfg.exclude or [])
    cfg.src_layout_dirs = [d.strip("/\\") for d in (cfg.src_layout_dirs or [])]
    cfg.external_module_prefixes = list(cfg.external_module_prefixes or [])
    cfg.test_dir_markers = list(cfg.test_dir_markers or [])
    cfg.delegate_comment_tokens = list(cfg.delegate_comment_tokens or [])

    # Bounds
    if cfg.max_file_bytes <= 0:
        cfg.max_file_bytes = 2_000_000

    # Normalize new flags defensively
    try:
        cfg.mode = str(cfg.mode or "classic").strip().lower()
    except Exception:
        cfg.mode = "classic"
    if cfg.mode not in ("classic", "zones"):
        cfg.mode = "classic"

    try:
        cfg.edge_policy = str(cfg.edge_policy or "strict").strip().lower()
    except Exception:
        cfg.edge_policy = "strict"
    if cfg.edge_policy not in ("strict", "soft"):
        cfg.edge_policy = "strict"

    try:
        z = float(cfg.zoom)
        if not (z > 0.0 and math.isfinite(z)):
            z = 1.0
        cfg.zoom = min(max(z, 0.1), 8.0)
    except Exception:
        cfg.zoom = 1.0

    if cfg.layout_policy is not None:
        try:
            s = str(cfg.layout_policy).strip()
            cfg.layout_policy = s if s else None
        except Exception:
            cfg.layout_policy = None

    cfg.emit_edges = bool(cfg.emit_edges)

    # v1.7 metadata flag only; unrelated to SCC semantics or schema versioning.
    cfg.detailed_symbols = bool(cfg.detailed_symbols)

    return cfg


def _from_dict(d: Dict[str, Any], base: Optional[AnalyzerCfg] = None) -> AnalyzerCfg:
    base = base or AnalyzerCfg()
    as_dict = base.__dict__.copy()
    merged = _merge_dict(as_dict, d or {})

    allowed = {f.name for f in _dc_fields(AnalyzerCfg)}
    filtered = {k: v for k, v in merged.items() if k in allowed}

    cfg = AnalyzerCfg(**filtered)
    return _normalize(cfg)


def _read_toml_file(p: Path) -> Optional[Dict[str, Any]]:
    if not _toml or not p.exists():
        return None
    try:
        try:
            return _toml.loads(p.read_text(encoding="utf-8"))
        except AttributeError:
            with p.open("rb") as f:
                return _toml.load(f)  # type: ignore[attr-defined]
    except Exception:
        return None


def _extract_analyzer_table(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    if "analyzer" in data and isinstance(data["analyzer"], dict):
        return data["analyzer"]
    tool = data.get("tool")
    if isinstance(tool, dict):
        pv = tool.get("program_visualizer") or tool.get("pviz")
        if isinstance(pv, dict):
            ana = pv.get("analyzer")
            if isinstance(ana, dict):
                return ana
    return data


# --- Existing API (unchanged behavior) ---------------------------------------

def load_config(root: Path | str, filename: str = "pviz.toml") -> AnalyzerCfg:
    """
    Best-effort loader. Searches <root>/filename; if missing or TOML unavailable,
    returns defaults. Unknown keys are tolerated (ignored).
    """
    root = Path(root)
    cfg = AnalyzerCfg()
    p = root / filename
    data = _read_toml_file(p)
    if not data:
        return _normalize(cfg)

    tbl = _extract_analyzer_table(data)
    if not isinstance(tbl, dict):
        return _normalize(cfg)

    return _from_dict(tbl, base=cfg)


# --- New helper (additive): cache-first, env override, pyproject fallback -----

def load_config_search(
    scan_root: Union[Path, str],
    *,
    cache_dir: Optional[Union[Path, str]] = None,
    filename: str = "pviz.toml",
    env_var: str = "PVIZ_CONFIG",
) -> AnalyzerCfg:
    """
    Extended, read-only loader that NEVER writes to the target repo:
      1) If ENV[env_var] points to a file, load that and return.
      2) If cache_dir is provided, try <cache_dir>/<filename>.
      3) Then try <scan_root>/<filename>.
      4) Then try <scan_root>/pyproject.toml under:
         [tool.program_visualizer.analyzer] or [tool.pviz.analyzer]
      5) Else return defaults.

    This function is additive; it does not change the behavior of load_config().
    """
    cfg = AnalyzerCfg()

    # 1) env override (absolute or relative path ok)
    env_path = os.environ.get(env_var)
    if env_path:
        data = _read_toml_file(Path(env_path))
        tbl = _extract_analyzer_table(data or {})
        if isinstance(tbl, dict):
            return _from_dict(tbl, base=cfg)

    # 2) cache-first (if provided)
    if cache_dir:
        cache_p = Path(cache_dir) / filename
        data = _read_toml_file(cache_p)
        if data:
            tbl = _extract_analyzer_table(data)
            if isinstance(tbl, dict):
                return _from_dict(tbl, base=cfg)

    # 3) scan_root/<filename>
    scan_root = Path(scan_root)
    data = _read_toml_file(scan_root / filename)
    if data:
        tbl = _extract_analyzer_table(data)
        if isinstance(tbl, dict):
            return _from_dict(tbl, base=cfg)

    # 4) pyproject.toml
    data = _read_toml_file(scan_root / "pyproject.toml")
    if data:
        tbl = _extract_analyzer_table(data)
        if isinstance(tbl, dict):
            return _from_dict(tbl, base=cfg)

    # 5) defaults
    return _normalize(cfg)