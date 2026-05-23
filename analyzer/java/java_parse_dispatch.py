#saas_analyzer/analyzer/java/parse_dispatch.py
from __future__ import annotations

"""
Java parse dispatcher for symbol extraction (NodeFacts-facing).

Purpose:
  - Provide a single, stable entrypoint for Java that returns a Python-like
    ParsedFileView object with:
      .classes/.functions/.globals/.all_exports/.loc_code
    plus richer Java fields (Option B):
      .package/.declared_types/.declared_types_fq/.public_exports
      .methods/.constructors/.fields/.enum_constants/.record_components
      .imports (normalized), plus import convenience splits when available

  - Keep NodeFacts builder unchanged (it can keep reading .classes/.functions/.globals/.all_exports).

Current support:
  - .java -> analyzer.java.parse_java_file.parse_java_file (jar-backed in your current setup)

Return contract:
  (parsed_obj, warns)
    - parsed_obj may be None on hard error
    - warns is a list[str] (empty on success)
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Normalized ParsedFile view (same shape as Go dispatcher)
# ---------------------------------------------------------------------------

@dataclass
class ParsedSymbol:
    """
    Minimal symbol carrier used by NodeFacts builder.
    """
    name: str


@dataclass
class ParsedFileView:
    """
    A lightweight view of per-file parsed symbols.

    NodeFacts builder expects:
      - parsed.classes (iter of objects with .name)
      - parsed.functions (iter of objects with .name)
      - parsed.globals (iter of objects with .name)
      - parsed.all_exports (iter of str)
      - optional parsed.loc_code
      - optional parsed.crosstalk_candidates_py_v1 (iter of mappings)

    Java-rich additions (Option B):
      - package / declared_types / declared_types_fq / public_exports
      - methods / constructors / fields / enum_constants / record_components
      - imports + convenience splits (static/wildcard/raw) when available
      - annotations (if engine provides them)
    """

    # --- NodeFacts-compat surface (do not break) ---
    classes: List[ParsedSymbol] = field(default_factory=list)
    functions: List[ParsedSymbol] = field(default_factory=list)
    globals: List[ParsedSymbol] = field(default_factory=list)
    all_exports: List[str] = field(default_factory=list)
    loc_code: Optional[int] = None
    crosstalk_candidates_py_v1: Tuple[dict, ...] = ()

    # --- Rich Java surface (additive) ---

    # Normalized imports (prefer analyzer.java.parse_java.models.JavaImport objects)
    imports: List[Any] = field(default_factory=list)

    # Debug/raw import strings, and convenience splits
    imports_raw: List[str] = field(default_factory=list)
    imports_static: List[str] = field(default_factory=list)
    imports_wildcard: List[str] = field(default_factory=list)

    # Package + types
    package: Optional[str] = None
    declared_types: List[str] = field(default_factory=list)        # simple or Outer.Inner
    declared_types_fq: List[str] = field(default_factory=list)     # package.Type or package.Outer.Inner
    public_exports: List[str] = field(default_factory=list)        # best-effort public API surface

    # Member splits (when engine provides them; otherwise may be empty)
    methods: List[str] = field(default_factory=list)               # method names
    constructors: List[str] = field(default_factory=list)          # ctor names (usually class name)
    fields: List[str] = field(default_factory=list)                # field names
    enum_constants: List[str] = field(default_factory=list)
    record_components: List[str] = field(default_factory=list)

    # File-level annotation inventory (if provided)
    annotations: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_java_any_file(path: Path, cfg: Any = None) -> Tuple[Optional[ParsedFileView], List[str]]:
    """
    Parse a Java file and return (parsed, warns).

    This function should not raise for parse errors; it returns parsed=None and
    a warn/error message.

    Note:
      - cfg is accepted for signature parity with other dispatchers.
      - cfg is currently unused here, but can be used later (timeouts, engine selection, etc).
    """
    p = Path(path)
    if p.suffix.lower() != ".java":
        return None, [f"java_dispatch_unsupported_suffix:{p.suffix.lower()}"]
    return _parse_java(p)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _as_str_list(x: Any) -> List[str]:
    if not x:
        return []
    if isinstance(x, list):
        return [str(v) for v in x if v is not None and str(v).strip()]
    if isinstance(x, tuple):
        return [str(v) for v in x if v is not None and str(v).strip()]
    # allow single string
    if isinstance(x, str) and x.strip():
        return [x.strip()]
    return []


def _parse_java(path: Path) -> Tuple[Optional[ParsedFileView], List[str]]:
    """
    Parse Java and normalize to ParsedFileView.

    Expected contract from analyzer.java.parse_java.parse_java_file():
      - parse_status: "ok" | "error"
      - error_snippet: Optional[str]
      - package: Optional[str]
      - classes: list[str]
      - functions: list[str]
      - globals: list[str]
      - all_exports: list[str]
      - loc_code: Optional[int]

    Additive fields (if present on JavaParsedFile; safe to ignore otherwise):
      - imports: list[JavaImport]
      - imports_raw/imports_static/imports_wildcard
      - classes_fq
      - annotations
      - decl_annotations (ignored here for now)
      - methods/constructors/fields/enum_constants/record_components (if you later add them)
    """
    try:
        try:
            from analyzer.java.java_parse import parse_java_file  # type: ignore
        except Exception:
            from saas_analyzer.analyzer.java.java_parse import parse_java_file  # type: ignore
    except Exception as e:
        return None, [f"java_parse_error:import:{type(e).__name__}:{str(e)[:200]}"]

    try:
        jp = parse_java_file(path)

        if getattr(jp, "parse_status", "error") != "ok":
            snip = getattr(jp, "error_snippet", None) or "unknown"
            return None, [f"java_parse_error:{snip}"]

        # --- base (NodeFacts-facing) ---
        classes_s = _as_str_list(getattr(jp, "classes", None))
        funcs_s = _as_str_list(getattr(jp, "functions", None))
        globs_s = _as_str_list(getattr(jp, "globals", None))
        exports_s = _as_str_list(getattr(jp, "all_exports", None))

        parsed = ParsedFileView(
            classes=[ParsedSymbol(n) for n in classes_s],
            functions=[ParsedSymbol(n) for n in funcs_s],
            globals=[ParsedSymbol(n) for n in globs_s],
            all_exports=exports_s,
            loc_code=getattr(jp, "loc_code", None),
            crosstalk_candidates_py_v1=(),
        )

        # --- rich Java fields (additive) ---
        parsed.package = getattr(jp, "package", None)

        # Types
        parsed.declared_types = list(classes_s)
        parsed.declared_types_fq = _as_str_list(getattr(jp, "classes_fq", None))

        # Exports: prefer explicit public surface; else fall back to all_exports; else classes
        public_exports = _as_str_list(getattr(jp, "all_exports", None))
        if not public_exports:
            public_exports = list(parsed.declared_types_fq) or list(parsed.declared_types)
        parsed.public_exports = public_exports

        # Imports (normalized objects preferred)
        imps = getattr(jp, "imports", None)
        if isinstance(imps, list):
            parsed.imports = imps
        else:
            parsed.imports = []

        parsed.imports_raw = _as_str_list(getattr(jp, "imports_raw", None))
        parsed.imports_static = _as_str_list(getattr(jp, "imports_static", None))
        parsed.imports_wildcard = _as_str_list(getattr(jp, "imports_wildcard", None))

        # Members (if you later add these to JavaParsedFile, dispatcher will pick them up)
        parsed.methods = _as_str_list(getattr(jp, "methods", None))
        parsed.constructors = _as_str_list(getattr(jp, "constructors", None))
        parsed.fields = _as_str_list(getattr(jp, "fields", None))
        parsed.enum_constants = _as_str_list(getattr(jp, "enum_constants", None))
        parsed.record_components = _as_str_list(getattr(jp, "record_components", None))

        # Annotations
        ann = getattr(jp, "annotations", None)
        if isinstance(ann, dict):
            # if your parser emits {name:count}, keep keys (stable-ish)
            parsed.annotations = sorted([str(k) for k in ann.keys() if str(k).strip()])
        else:
            parsed.annotations = _as_str_list(ann)

        # Backfill: if we have member splits but NodeFacts-only fields are empty,
        # keep compatibility: NodeFacts reads parsed.functions/globals.
        if parsed.methods and not parsed.functions:
            parsed.functions = [ParsedSymbol(n) for n in parsed.methods]
        if parsed.fields and not parsed.globals:
            parsed.globals = [ParsedSymbol(n) for n in parsed.fields]

        return parsed, []

    except Exception as e:
        return None, [f"java_parse_error:{type(e).__name__}:{str(e)[:200]}"]
