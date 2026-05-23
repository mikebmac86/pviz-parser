# analyzer/ts/extract_imports.py
from __future__ import annotations

from typing import List, Optional, Tuple

from .model import RawImport

# Integration: keep crosstalk extraction in its own module, but make it easy
# for callers to pull both results from one place.
try:
    from .extract_crosstalk import extract_crosstalk_candidates_ts_v1  # type: ignore
except Exception:  # pragma: no cover
    extract_crosstalk_candidates_ts_v1 = None  # type: ignore


def _node_text(source: bytes, node) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _string_literal_value(source: bytes, node) -> Optional[str]:
    """
    Extract a plain quoted string literal value.

    Accepted:
      "foo"
      'foo'

    Rejected:
      template strings
      identifiers
      arbitrary expression text
    """
    txt = _node_text(source, node).strip()
    if len(txt) >= 2 and ((txt[0] == txt[-1] == '"') or (txt[0] == txt[-1] == "'")):
        return txt[1:-1]
    return None


def _loc(node) -> Optional[Tuple[int, int]]:
    try:
        return (node.start_point[0], node.start_point[1])
    except Exception:
        return None


def _first_string_descendant(node):
    """
    Find the first plain string descendant.

    This is intentionally used only for call-expression arguments, where the
    search is scoped to require(...) / import(...) arguments.

    Do not use this as a fallback for import/export declarations; those should
    rely on the grammar's source field to avoid capturing arbitrary string
    literals from large statement nodes.
    """
    stack = list(getattr(node, "children", []) or [])
    while stack:
        cur = stack.pop()
        if cur.type == "string":
            return cur
        stack.extend(getattr(cur, "children", []) or [])
    return None


def _module_spec_from_source_field(source: bytes, n) -> Optional[str]:
    """
    Extract module specifier from the canonical tree-sitter 'source' field.

    This is intentionally strict. If the grammar does not expose a source field,
    return None rather than scanning arbitrary string descendants.
    """
    src = n.child_by_field_name("source")
    if src is None:
        return None

    if src.type == "string":
        return _string_literal_value(source, src)

    # Some grammar variants may wrap the source string. Search only inside the
    # source field, not the whole import/export statement.
    s2 = _first_string_descendant(src)
    if s2 is not None:
        return _string_literal_value(source, s2)

    return None


def _call_arg_string_spec(source: bytes, args) -> Optional[str]:
    """
    Extract the first plain string argument from require("x") or import("x").

    This stays scoped to the argument node, so it does not produce the broad
    overcapture that happened when scanning entire import/export statements.
    """
    s = _first_string_descendant(args)
    if s is None:
        return None
    return _string_literal_value(source, s)


def extract_imports(*, tree, source: bytes) -> List[RawImport]:
    """
    Import extraction ONLY.

    Captures:
      - ESM imports: import ... from "x"; import "x"
      - Re-exports: export ... from "x"; export * from "x"
      - CommonJS: require("x")
      - Dynamic import: import("x")

    Does NOT capture:
      - arbitrary string literals
      - fetch URLs
      - DOM event names
      - CSS classes
      - error messages
      - crosstalk evidence strings
    """
    out: List[RawImport] = []
    root = tree.root_node

    def walk(n):
        # ESM import declarations.
        #
        # Strictly use the grammar source field. Do not fallback to arbitrary
        # descendant strings, because some statement nodes can contain unrelated
        # string literals.
        if n.type in ("import_statement", "import_declaration"):
            spec = _module_spec_from_source_field(source, n)
            if spec:
                out.append(RawImport(spec=spec, kind="import", symbols=[], loc=_loc(n)))

        # Re-export declarations.
        #
        # Only export statements with an explicit source field are imports.
        # Plain `export const x = "..."` must not become a reexport import.
        elif n.type in ("export_statement", "export_clause", "export_declaration"):
            spec = _module_spec_from_source_field(source, n)
            if spec:
                out.append(RawImport(spec=spec, kind="reexport", symbols=[], loc=_loc(n)))

        # CommonJS: require("x") or dynamic import("x").
        #
        # Here descendant-string scanning is allowed, but only inside the
        # arguments node.
        elif n.type == "call_expression":
            callee = n.child_by_field_name("function")
            args = n.child_by_field_name("arguments")

            if callee is not None and args is not None:
                callee_txt = _node_text(source, callee).strip()

                if callee_txt in ("require", "import"):
                    spec = _call_arg_string_spec(source, args)
                    if spec:
                        kind = "require" if callee_txt == "require" else "dynamic_import"
                        out.append(RawImport(spec=spec, kind=kind, symbols=[], loc=_loc(n)))

        for ch in getattr(n, "children", []) or []:
            walk(ch)

    walk(root)
    return out


def extract_imports_and_crosstalk(*, tree, source: bytes):
    """
    Convenience integration helper:
      - Always returns imports from extract_imports()
      - Returns crosstalk candidates if extract_crosstalk_candidates_ts_v1 is available
        otherwise returns an empty list.

    Return:
      (imports: List[RawImport], candidates: List[CandidateV1])
    """
    imports = extract_imports(tree=tree, source=source)

    if extract_crosstalk_candidates_ts_v1 is None:
        return imports, []

    try:
        candidates = extract_crosstalk_candidates_ts_v1(tree=tree, source=source)
    except Exception:
        # Never allow crosstalk extraction to break import extraction.
        candidates = []

    return imports, candidates