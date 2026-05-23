#saas_analyzer/analyzer/java/parse_java/models.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass(frozen=True)
class JavaImport:
    """
    Normalized representation of a Java import statement.

    Examples:
      import foo.bar.Baz;        -> target="foo.bar.Baz", is_wildcard=False, is_static=False
      import foo.bar.*;          -> target="foo.bar",     is_wildcard=True,  is_static=False
      import static foo.Baz.*;   -> target="foo.Baz",     is_wildcard=True,  is_static=True
    """
    target: str
    is_wildcard: bool
    is_static: bool


@dataclass(frozen=True)
class JavaParsedFile:
    path: Path
    parse_status: str                 # "ok" | "error"
    error_snippet: Optional[str]

    package: Optional[str]

    classes: List[str]                # top-level (or nested) types (simple names or "Outer.Inner")
    functions: List[str]              # methods + constructors
    globals: List[str]                # fields/constants + enum constants + record components
    all_exports: List[str]            # public surface (best-effort)

    loc_code: Optional[int]           # nonblank, non-comment LOC

    # ---------------------------------------------------------------------
    # Additive fields (safe to ignore by existing call sites)
    # ---------------------------------------------------------------------

    # Imports as parsed by the engine (JavaParser preferred; regex fallback can populate too)
    imports: Optional[List[JavaImport]] = None

    # Debug/diagnostics: raw import strings or whatever the engine saw
    imports_raw: Optional[List[str]] = None

    # Convenience splits (helpful for FolderIndex and future edge policies)
    imports_static: Optional[List[str]] = None          # base targets for static imports
    imports_wildcard: Optional[List[str]] = None        # base targets for wildcard imports

    # Optional fully-qualified class names (package + classes)
    classes_fq: Optional[List[str]] = None

    # Annotation inventory (file-level + per-declaration best-effort)
    annotations: Optional[Dict[str, int]] = None
    decl_annotations: Optional[Dict[str, List[str]]] = None
