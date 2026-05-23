#saas_analyzer/analyzer/java/nodefacts_symbols.py
from __future__ import annotations

"""
Java NodeFacts symbol extraction helper.

UPDATED to accept either:
  - Path: Will parse the file (original behavior)
  - JavaParsedFile: Will use cached data (optimized path)

This eliminates code duplication and provides a clean interface for both
cached and uncached workflows.

Goal:
  - Provide a *stable, Java-semantic* symbol view for NodeFacts population.
  - Be tolerant: never raise; return empty symbols with parse_status="error" on failure.
  - Be future-proof: works with cached data or fresh parsing.

Performance:
  - With Path: ~200ms (subprocess call)
  - With JavaParsedFile: ~2ms (in-memory transformation)
  - 100x faster when using cache!

What "good" means here:
  - Capture Java-meaningful structure WITHOUT doing heavyweight graph analytics:
      * package (with fallback to path-based derivation)
      * declared types (simple + FQNs)
      * public surface exports (best-effort)
      * members (methods/ctors/fields) when available
      * annotations when available
      * loc_code when available
  - Keep the return type deterministic (stable sorting, dedupe).
  - Keep schema parity fields that NodeFacts expects (including crosstalk placeholder).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional, Tuple, Union

from analyzer.java.java_canonical import derive_package_from_path, build_fq_typename

# Import JavaParsedFile for type checking
from analyzer.java.java_parse import JavaParsedFile


# ---------------------------------------------------------------------------
# Public output model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeFactsSymbols:
    # Java-specific semantic anchors
    package: Optional[str]
    declared_types: Tuple[str, ...]          # simple names
    declared_types_fq: Tuple[str, ...]       # fq names when known (pkg.Type)
    public_exports: Tuple[str, ...]          # best-effort public API surface

    # NodeFacts compatibility fields (historical names)
    classes: Tuple[str, ...]                 # alias for declared_types_fq/simple (kept for callers)
    functions: Tuple[str, ...]               # methods + ctors (best-effort)
    globals: Tuple[str, ...]                 # fields/constants/enum constants/record components (best-effort)
    exports: Tuple[str, ...]                 # alias for public_exports (kept for callers)

    # Optional richer structure if parser provides it
    annotations: Tuple[str, ...]             # annotation simple or fq names
    loc_code: Optional[int]

    # Import visibility (populated by folder_index after resolution)
    imports_all_raw: Tuple[str, ...]         # all import specs before resolution
    imports_external: Tuple[str, ...]        # specs that resolved to no internal file

    # Schema parity placeholder (Java doesn't emit this today)
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
        # fallback: preserve encounter order
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


# ---------------------------------------------------------------------------
# Internal: Convert parsed data to symbols
# ---------------------------------------------------------------------------

def _extract_symbols_from_parsed(
    parsed: Any,
    path: Union[Path, str],
    warnings: Optional[List[str]] = None,
) -> NodeFactsSymbols:
    """
    Internal helper: Extract symbols from a parsed object (JavaParsedFile or ParsedFileView).
    
    This is the common logic used by both code paths:
    - Fresh parsing (from parse_java_any_file)
    - Cached data (from JavaParsedFile)
    """
    # ----------------------------
    # Package (with path-based fallback)
    # ----------------------------
    pkg: Optional[str] = None
    try:
        # Common attribute names across parser variants
        pkg = _safe_str(getattr(parsed, "package", None)) or _safe_str(getattr(parsed, "pkg", None))
    except Exception:
        pkg = None

    # Fallback: derive from file path if parser didn't extract package
    if not pkg:
        try:
            pkg = derive_package_from_path(str(path))
        except Exception:
            pkg = None

    # ----------------------------
    # Declared types (simple + fq)
    # ----------------------------
    declared_simple: List[str] = []
    declared_fq: List[str] = []

    try:
        declared_simple.extend(_names(getattr(parsed, "declared_types", None)))
    except Exception:
        pass

    try:
        # Older dispatcher style
        if not declared_simple:
            declared_simple.extend(_names(getattr(parsed, "classes", None)))
    except Exception:
        pass

    try:
        declared_fq.extend(_names(getattr(parsed, "declared_types_fq", None)))
    except Exception:
        pass
    
    # Also check classes_fq (JavaParsedFile field)
    try:
        if not declared_fq:
            declared_fq.extend(_names(getattr(parsed, "classes_fq", None)))
    except Exception:
        pass

    # If we have simple names but not fq, derive fq using package
    if declared_simple and not declared_fq and pkg:
        for nm in declared_simple:
            # Handle simple names and inner class notation (OuterClass.InnerClass)
            if '.' in nm and nm[0].isupper():
                # Likely OuterClass.InnerClass format
                parts = nm.split('.', 1)
                fq = build_fq_typename(pkg, parts[0], parts[1])
            else:
                # Simple class name
                fq = build_fq_typename(pkg, nm)
            declared_fq.append(fq)

    declared_types = _stable_unique(declared_simple)
    declared_types_fq = _stable_unique(declared_fq)

    # ----------------------------
    # Members (methods/ctors, fields/constants)
    # ----------------------------
    functions: List[str] = []
    globals_: List[str] = []
    annotations: List[str] = []

    # Methods/constructors
    for attr in ("methods", "functions"):
        try:
            functions.extend(_names(getattr(parsed, attr, None)))
        except Exception:
            pass
    for attr in ("constructors", "ctors"):
        try:
            # Prefix ctor names to avoid colliding with normal methods
            for nm in _names(getattr(parsed, attr, None)):
                functions.append(f"{nm}::<init>")
        except Exception:
            pass

    # Fields/constants/etc.
    for attr in ("fields", "globals"):
        try:
            globals_.extend(_names(getattr(parsed, attr, None)))
        except Exception:
            pass
    for attr in ("enum_constants", "record_components"):
        try:
            # Mark these distinctly so downstream can differentiate
            tag = "::<enum>" if attr == "enum_constants" else "::<record>"
            for nm in _names(getattr(parsed, attr, None)):
                globals_.append(f"{nm}{tag}")
        except Exception:
            pass

    # Annotations
    for attr in ("annotations", "annos"):
        try:
            # Handle both list and dict formats
            annos_data = getattr(parsed, attr, None)
            if annos_data:
                if isinstance(annos_data, dict):
                    # Dict format: keys are annotation names
                    annotations.extend([str(k) for k in annos_data.keys() if str(k).strip()])
                else:
                    # List format
                    annotations.extend(_names(annos_data))
        except Exception:
            pass

    # ----------------------------
    # Public exports (best-effort)
    # ----------------------------
    exports_list: List[str] = []

    # Explicit list from dispatcher / parser
    for attr in ("public_exports", "all_exports", "exports"):
        try:
            exports_list.extend(_names(getattr(parsed, attr, None)))
        except Exception:
            pass

    # Public types hint
    if not exports_list:
        try:
            exports_list.extend(_names(getattr(parsed, "public_types", None)))
        except Exception:
            pass

    # Fallback to declared types
    if not exports_list:
        exports_list.extend(list(declared_types_fq) or list(declared_types))

    public_exports = _stable_unique(exports_list)

    # ----------------------------
    # LOC code (optional)
    # ----------------------------
    loc_code = None
    try:
        loc_code = _int_or_none(getattr(parsed, "loc_code", None))
    except Exception:
        loc_code = None

    # ----------------------------
    # Crosstalk placeholder (schema parity)
    # ----------------------------
    crosstalk_candidates_py_v1: Tuple[Mapping[str, Any], ...]
    try:
        raw = getattr(parsed, "crosstalk_candidates_py_v1", None) or ()
        if isinstance(raw, tuple):
            crosstalk_candidates_py_v1 = raw  # type: ignore[assignment]
        else:
            crosstalk_candidates_py_v1 = tuple(raw)  # type: ignore[arg-type]
    except Exception:
        crosstalk_candidates_py_v1 = ()

    # ----------------------------
    # Parse status
    # ----------------------------
    # Check for explicit parse_status first (JavaParsedFile)
    parse_status = "ok"
    try:
        status = getattr(parsed, "parse_status", None)
        if status:
            parse_status = str(status)
    except Exception:
        pass
    
    # If no explicit status, check for warnings
    if parse_status == "ok" and warnings:
        parse_status = "warn"

    # For NodeFacts compatibility: keep legacy fields populated sensibly
    classes = declared_types_fq or declared_types
    functions_t = _stable_unique(functions)
    globals_t = _stable_unique(globals_)
    annotations_t = _stable_unique(annotations)

    return NodeFactsSymbols(
        package=pkg,
        declared_types=declared_types,
        declared_types_fq=declared_types_fq,
        public_exports=public_exports,
        classes=classes,
        functions=functions_t,
        globals=globals_t,
        exports=public_exports,  # alias
        annotations=annotations_t,
        loc_code=loc_code,
        imports_all_raw=_stable_unique(getattr(parsed, 'imports_all_raw', None) or ()),
        imports_external=_stable_unique(getattr(parsed, 'imports_external', None) or ()),
        crosstalk_candidates_py_v1=crosstalk_candidates_py_v1,
        parse_status=parse_status,
    )


# ---------------------------------------------------------------------------
# Main entry (accepts Path OR JavaParsedFile)
# ---------------------------------------------------------------------------

def parse_symbols_for_nodefacts(
    path_or_parsed: Union[Path, JavaParsedFile], 
    cfg: Any
) -> NodeFactsSymbols:
    """
    Parse Java file symbols for NodeFacts population.
    
    Can accept either:
      - Path: Will parse the file (subprocess call ~200ms)
      - JavaParsedFile: Will use cached data (in-memory ~2ms)
    
    This eliminates the need for duplicate code in build_artifacts.py!

    Returns a normalized symbol bundle.

    Tolerance contract:
      - Never raises.
      - On failure, returns empty symbols and parse_status="error".

    "Good" behavior:
      - Uses parser-provided package / types / exports when present.
      - Falls back to path-based package derivation if parser doesn't extract package.
      - Falls back sensibly if the parser provides only partial structure.
      - Produces deterministic, stable tuples.
    
    Performance:
      - With Path: ~200ms (parses file)
      - With JavaParsedFile: ~2ms (uses cache)
    """
    # Check if we received a JavaParsedFile (cached data)
    if isinstance(path_or_parsed, JavaParsedFile):
        # FAST PATH: Use cached parsed data
        pf = path_or_parsed
        path = pf.path
        
        # Already have parsed data, just extract symbols
        return _extract_symbols_from_parsed(pf, path, warnings=None)
    
    # SLOW PATH: Parse the file
    path = Path(path_or_parsed)
    
    try:
        # Lazy import to avoid circular dependency
        from analyzer.java.java_parse_dispatch import parse_java_any_file
        
        parsed, warns = parse_java_any_file(path, cfg)
    except Exception:
        return NodeFactsSymbols(
            package=None,
            declared_types=(),
            declared_types_fq=(),
            public_exports=(),
            classes=(),
            functions=(),
            globals=(),
            exports=(),
            annotations=(),
            loc_code=None,
            imports_all_raw=(),
            imports_external=(),
            crosstalk_candidates_py_v1=(),
            parse_status="error",
        )

    if not parsed:
        return NodeFactsSymbols(
            package=None,
            declared_types=(),
            declared_types_fq=(),
            public_exports=(),
            classes=(),
            functions=(),
            globals=(),
            exports=(),
            annotations=(),
            loc_code=None,
            imports_all_raw=(),
            imports_external=(),
            crosstalk_candidates_py_v1=(),
            parse_status="error",
        )

    # Extract symbols from parsed data
    return _extract_symbols_from_parsed(parsed, path, warnings=warns)