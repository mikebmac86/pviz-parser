from __future__ import annotations

"""
Rust NodeFacts symbol extraction helper.

UPDATED to accept either:
  - Path: Will parse the file (original behavior)
  - RustParsedFile-like object: Will use cached data (optimized path)

This eliminates code duplication and provides a clean interface for both
cached and uncached workflows.

Goal:
  - Provide a *stable, Rust-semantic* symbol view for NodeFacts population.
  - Be tolerant: never raise; return empty symbols with parse_status="error" on failure.
  - Be future-proof: works with cached data or fresh parsing.

What "good" means here:
  - Capture Rust-meaningful structure WITHOUT doing heavyweight graph analytics:
      * module_path (crate::foo::bar)
      * declared types (structs, enums, traits)
      * public surface exports (pub items) when available
      * members (functions, fields) when available
      * derives/attributes when available
      * loc_code when available
  - Keep the return type deterministic (stable sorting, dedupe).
  - Keep schema parity fields that NodeFacts expects (including crosstalk placeholder).
"""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, List, Mapping, Optional, Tuple, Union

from adapters.canonical import to_posix
from analyzer.rust.rust_canonical import derive_module_from_path


# ---------------------------------------------------------------------------
# Public output model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeFactsSymbols:
    # Rust-specific semantic anchors
    module_path: Optional[str]               # crate::foo::bar
    declared_types: Tuple[str, ...]          # struct/enum/trait names
    declared_types_fq: Tuple[str, ...]       # fq names when known (crate::Type)
    public_exports: Tuple[str, ...]          # best-effort public API surface

    # NodeFacts compatibility fields (historical names)
    classes: Tuple[str, ...]                 # alias for declared_types (kept for callers)
    functions: Tuple[str, ...]               # function names
    globals: Tuple[str, ...]                 # field names (best-effort)
    exports: Tuple[str, ...]                 # alias for public_exports (kept for callers)
    rust_use_statements: Tuple[str, ...]

    # Optional richer structure if parser provides it
    annotations: Tuple[str, ...]             # derive macros + attributes
    loc_code: Optional[int]

    # Schema parity placeholder (Rust doesn't emit this today)
    crosstalk_candidates_py_v1: Tuple[Mapping[str, Any], ...]

    # "ok" | "warn" | "error"
    parse_status: str


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _safe_str(x: Any) -> Optional[str]:
    try:
        if x is None:
            return None
        s = str(x).strip()
        return s or None
    except Exception:
        return None


def _norm_parse_status(s: Any) -> str:
    ss = str(s or "").strip().lower()
    if not ss:
        return "ok"
    if ss in {"ok", "success"}:
        return "ok"
    if "warn" in ss or ss in {"partial", "degraded"}:
        return "warn"
    if "error" in ss or "fail" in ss or "exception" in ss or ss in {"err"}:
        return "error"
    # unknown -> warn (safer than pretending ok)
    return "warn"


def _names(seq: Any) -> List[str]:
    """
    Extract names from a sequence of:
      - strings
      - objects with .name
      - dicts with "name"
    """
    out: List[str] = []
    for s in (seq or []):
        try:
            if isinstance(s, str):
                nm = s
            elif isinstance(s, Mapping):
                nm = s.get("name")
            else:
                nm = getattr(s, "name", None)
            nm2 = _safe_str(nm)
            if nm2:
                out.append(nm2)
        except Exception:
            continue
    return out


def _stable_unique(vals: Iterable[str]) -> Tuple[str, ...]:
    try:
        return tuple(sorted({v for v in vals if v}))
    except Exception:
        seen = set()
        out: List[str] = []
        for v in vals:
            if not v or v in seen:
                continue
            seen.add(v)
            out.append(v)
        return tuple(out)


def _int_or_none(x: Any) -> Optional[int]:
    try:
        if x is None:
            return None
        return int(x)
    except Exception:
        return None


def _cfg_get(cfg: Any, key: str, default: Any = None) -> Any:
    """getattr/getitem tolerant accessor"""
    try:
        if isinstance(cfg, Mapping):
            return cfg.get(key, default)
    except Exception:
        pass
    try:
        return getattr(cfg, key, default)
    except Exception:
        return default


def _is_like_rust_parsed_file(x: Any) -> bool:
    """
    Structural check for RustParsedFile-like objects.
    Avoid isinstance() because cache records may come from different module contexts.
    """
    try:
        if x is None:
            return False
        if hasattr(x, "file_path") and hasattr(x, "parse_status") and hasattr(x, "ok"):
            return True
        # weaker: still allow ParsedFileView-like objects with module_path/functions/structs
        if hasattr(x, "module_path") and hasattr(x, "functions") and hasattr(x, "structs"):
            return True
    except Exception:
        pass
    return False


def _empty_symbols(*, parse_status: str = "error") -> NodeFactsSymbols:
    return NodeFactsSymbols(
        module_path=None,
        declared_types=(),
        declared_types_fq=(),
        public_exports=(),
        classes=(),
        functions=(),
        globals=(),
        exports=(),
        rust_use_statements=(),
        annotations=(),
        loc_code=None,
        crosstalk_candidates_py_v1=(),
        parse_status=_norm_parse_status(parse_status),
    )


# ---------------------------------------------------------------------------
# Internal: Convert parsed data to symbols
# ---------------------------------------------------------------------------

def _extract_symbols_from_parsed(
    parsed: Any,
    path: Union[Path, str],
    cfg: Any,
    warnings: Optional[List[str]] = None,
) -> NodeFactsSymbols:
    """
    Extract symbols from a parsed object (RustParsedFile-like or ParsedFileView-like).

    Common logic used by both code paths:
      - Fresh parsing (from parse_rust_any_file)
      - Cached data (from RustParsedFile cache)
    """
    # ----------------------------
    # Module path (with path-based fallback)
    # ----------------------------
    module_path = _safe_str(getattr(parsed, "module_path", None)) if parsed is not None else None

    if not module_path:
        # Best-effort: derive from repo_root + repo-relative posix path, if available
        try:
            repo_root = _cfg_get(cfg, "repo_root", None)
            p = Path(path) if not isinstance(path, Path) else path
            if repo_root:
                rr = Path(repo_root).resolve()
                rel = to_posix(str(p.resolve().relative_to(rr)))
                module_path = _safe_str(derive_module_from_path(rel, rr))  # type: ignore[arg-type]
        except Exception:
            module_path = None

    # ----------------------------
    # Declared types (structs, enums, traits)
    # ----------------------------
    declared_simple: List[str] = []
    try:
        declared_simple.extend(_names(getattr(parsed, "structs", None)))
        declared_simple.extend(_names(getattr(parsed, "enums", None)))
        declared_simple.extend(_names(getattr(parsed, "traits", None)))
    except Exception:
        pass

    # Fallback: pre-extracted name lists
    try:
        if not declared_simple:
            declared_simple.extend(_names(getattr(parsed, "struct_names", None)))
            declared_simple.extend(_names(getattr(parsed, "enum_names", None)))
            declared_simple.extend(_names(getattr(parsed, "trait_names", None)))
    except Exception:
        pass

    # Final fallback: "classes" compatibility (ParsedFileView may have this)
    try:
        if not declared_simple:
            declared_simple.extend(_names(getattr(parsed, "classes", None)))
    except Exception:
        pass

    declared_types = _stable_unique(declared_simple)

    # FQ names when we have a module_path
    declared_fq: List[str] = []
    if module_path:
        for nm in declared_types:
            declared_fq.append(f"{module_path}::{nm}")
    declared_types_fq = _stable_unique(declared_fq) if declared_fq else declared_types

    # ----------------------------
    # Members (functions, fields)
    # ----------------------------
    functions: List[str] = []
    globals_: List[str] = []
    annotations: List[str] = []

    for attr in ("functions", "function_names"):
        try:
            functions.extend(_names(getattr(parsed, attr, None)))
        except Exception:
            pass

    # Fields from structs (best-effort)
    try:
        structs = getattr(parsed, "structs", None) or []
        for struct in structs:
            fields = getattr(struct, "fields", None)
            if fields:
                for f in fields:
                    ff = _safe_str(f)
                    if ff:
                        globals_.append(ff)
    except Exception:
        pass

    # Fallback: globals field
    try:
        globals_.extend(_names(getattr(parsed, "globals", None)))
    except Exception:
        pass

    # Derives + Attributes
    try:
        for struct in (getattr(parsed, "structs", None) or []):
            for d in (getattr(struct, "derives", None) or []):
                dd = _safe_str(d)
                if dd:
                    annotations.append(f"derive:{dd}")
    except Exception:
        pass

    try:
        for enum in (getattr(parsed, "enums", None) or []):
            for d in (getattr(enum, "derives", None) or []):
                dd = _safe_str(d)
                if dd:
                    annotations.append(f"derive:{dd}")
    except Exception:
        pass

    # Prefer attributes_raw if present, else attributes (to align with your RustParsedFile model)
    try:
        raw_attrs = getattr(parsed, "attributes_raw", None)
        if raw_attrs:
            for a in raw_attrs:
                aa = _safe_str(a)
                if aa:
                    annotations.append(aa)
    except Exception:
        pass

    try:
        attrs = getattr(parsed, "attrs", None)
        if attrs:
            for a in attrs:
                aa = _safe_str(a)
                if aa:
                    annotations.append(aa)
    except Exception:
        pass

    # Back-compat: some variants store token strings on "attributes"
    try:
        attrs2 = getattr(parsed, "attributes", None)
        if attrs2:
            for a in attrs2:
                aa = _safe_str(a)
                if aa:
                    annotations.append(aa)
    except Exception:
        pass

    # ----------------------------
    # Public exports (best-effort)
    # ----------------------------
    exports_list: List[str] = []
    for attr in ("public_exports", "all_exports", "exports"):
        try:
            exports_list.extend(_names(getattr(parsed, attr, None)))
        except Exception:
            pass

    # Optional fallback behavior: only if caller requests it
    allow_export_fallback = bool(_cfg_get(cfg, "rust_exports_fallback_to_declared", False))
    if not exports_list and allow_export_fallback:
        exports_list.extend(list(declared_types_fq) or list(declared_types))

    public_exports = _stable_unique(exports_list)

    # ----------------------------
    # LOC code (optional)
    # ----------------------------
    loc_code = _int_or_none(getattr(parsed, "loc_code", None))

    # ----------------------------
    # Crosstalk placeholder (schema parity)
    # ----------------------------
    try:
        raw = getattr(parsed, "crosstalk_candidates_py_v1", None) or ()
        crosstalk_candidates_py_v1 = tuple(raw) if not isinstance(raw, tuple) else raw  # type: ignore[assignment]
    except Exception:
        crosstalk_candidates_py_v1 = ()

    rust_use_specs: List[str] = []

    try:
        for u in getattr(parsed, "use_statements", None) or []:
            pth = getattr(u, "path", None)
            if isinstance(pth, str) and pth.strip():
                rust_use_specs.append(pth.strip())
    except Exception:
        pass

    rust_use_statements = _stable_unique(rust_use_specs)

    # ----------------------------
    # Parse status
    # ----------------------------
    parse_status = _norm_parse_status(getattr(parsed, "parse_status", "ok"))

    # If parser indicates problems, degrade ok -> warn
    try:
        problems = getattr(parsed, "problems", None)
        if problems and parse_status == "ok":
            parse_status = "warn"
    except Exception:
        pass

    # If dispatcher returned warnings, degrade ok -> warn
    if parse_status == "ok" and warnings:
        parse_status = "warn"

    # NodeFacts compatibility: keep legacy fields sane
    classes = declared_types  # keep classes as simple names; FQ names live in declared_types_fq
    functions_t = _stable_unique(functions)
    globals_t = _stable_unique(globals_)
    annotations_t = _stable_unique(annotations)

    return NodeFactsSymbols(
        module_path=module_path,
        declared_types=declared_types,
        declared_types_fq=declared_types_fq,
        public_exports=public_exports,
        classes=classes,
        functions=functions_t,
        globals=globals_t,
        exports=public_exports,  # alias
        rust_use_statements=rust_use_statements,
        annotations=annotations_t,
        loc_code=loc_code,
        crosstalk_candidates_py_v1=crosstalk_candidates_py_v1,
        parse_status=parse_status,
    )


# ---------------------------------------------------------------------------
# Main entry (accepts Path OR RustParsedFile-like)
# ---------------------------------------------------------------------------

def parse_symbols_for_nodefacts(
    path_or_parsed: Any,
    cfg: Any,
) -> NodeFactsSymbols:
    """
    Parse Rust file symbols for NodeFacts population.

    Accepts either:
      - Path: will parse the file (subprocess call)
      - RustParsedFile-like record: uses cached data (fast path)

    Tolerance contract:
      - Never raises.
      - On failure, returns empty symbols and parse_status="error".
    """
    # Normalize cfg to be getattr-friendly (cfg_payload dicts happen in workers)
    cfg_obj: Any = cfg
    try:
        if isinstance(cfg, Mapping):
            cfg_obj = SimpleNamespace(**dict(cfg))
    except Exception:
        cfg_obj = cfg

    # FAST PATH: cached parsed data
    if _is_like_rust_parsed_file(path_or_parsed):
        try:
            pf = path_or_parsed
            p = getattr(pf, "file_path", None) or getattr(pf, "path", None)
            if p is None:
                return _empty_symbols(parse_status="error")
            return _extract_symbols_from_parsed(pf, Path(p), cfg_obj, warnings=None)
        except Exception:
            return _empty_symbols(parse_status="error")

    # SLOW PATH: parse a file path
    try:
        path = Path(path_or_parsed)
    except Exception:
        return _empty_symbols(parse_status="error")

    try:
        from analyzer.rust.rust_parse_dispatch import parse_rust_any_file

        parsed, warns = parse_rust_any_file(path, cfg_obj)
    except Exception:
        return _empty_symbols(parse_status="error")

    if not parsed:
        return _empty_symbols(parse_status="error")

    return _extract_symbols_from_parsed(parsed, path, cfg_obj, warnings=warns)
