# analyzer/imports_lex.py
from __future__ import annotations
from pathlib import Path
from typing import List, Tuple, Optional
import ast
import re

from analyzer.analyzer_types import ImportRef
from analyzer.config import AnalyzerCfg
from analyzer.ast_common import (
    ScopeAndTCVisitor,
    collect_typing_aliases_and_tc_names,
    node_source_segment,
    match_dynamic_import_literal,
    has_dunder_getattr,  # re-exported below
)

__all__ = ["extract_lexical_imports", "lex_import_lines", "has_dunder_getattr"]

_RE_IMPORT = re.compile(r"^\s*import\s+.+$")
_RE_FROM   = re.compile(r"^\s*from\s+[\w\.]+\s+import\s+.+$")

def lex_import_lines(text: str) -> List[str]:
    out: List[str] = []
    if not text:
        return out
    for ln in text.splitlines():
        if _RE_IMPORT.match(ln) or _RE_FROM.match(ln):
            out.append(ln.rstrip())
    return out


# ---------- Public API ----------
 
def extract_lexical_imports(
    path: Path,
    source: str,
    cfg: Optional[AnalyzerCfg] = None,
    *,
    tree: Optional[ast.AST] = None,
) -> List[ImportRef]:
    """
    Return ImportRef records with:
      - raw text
      - module token / names / level / is_from
      - TYPE_CHECKING guard (under_type_checking)
      - conditional + scope ("module" | "function" | "class")
      - tags (e.g., ["delegate"]) for function-scoped or directive-marked imports
      - synthetic dynamic imports (tags include ["dynamic","delegate"])
    """
    tree = tree or ast.parse(source, filename=str(path))

    include_tc = True
    if cfg is not None and hasattr(cfg, "include_type_checking_imports"):
        include_tc = bool(getattr(cfg, "include_type_checking_imports"))

    detect_dynamic = True
    if cfg is not None and hasattr(cfg, "detect_dynamic_imports"):
        detect_dynamic = bool(getattr(cfg, "detect_dynamic_imports"))

    delegate_tokens: List[str] = ["pviz:delegate-import", "pviz:delegate-call"]
    if cfg is not None and hasattr(cfg, "delegate_comment_tokens"):
        tokens = getattr(cfg, "delegate_comment_tokens")
        if isinstance(tokens, list) and tokens:
            delegate_tokens = tokens

    typing_aliases, tc_names = collect_typing_aliases_and_tc_names(tree)
    results: List[ImportRef] = []

    def compute_tags(raw: str, scope: str, base: Optional[List[str]] = None) -> List[str]:
        tags: List[str] = []
        if scope != "module":
            tags.append("delegate")
        if any(tok in raw for tok in delegate_tokens):
            if "delegate" not in tags:
                tags.append("delegate")
        if base:
            tags.extend(x for x in base if x not in tags)
        return tags

    # -------- First pass: collect static imports --------

    class ImportCollector(ScopeAndTCVisitor):
        def __init__(self) -> None:
            super().__init__(typing_aliases, tc_names)

        def on_import(self, n: ast.Import, *, under_tc: bool) -> None:
            if not include_tc and under_tc:
                return
            raw = node_source_segment(source, n)
            names: List[Tuple[str, Optional[str]]] = [(a.name, a.asname) for a in n.names]
            scope = self.current_scope()
            results.append(
                ImportRef(
                    raw=raw,
                    module_token=None,
                    names=names,
                    is_from=False,
                    level=0,
                    under_type_checking=under_tc,
                    lineno=n.lineno,
                    end_lineno=getattr(n, "end_lineno", n.lineno),
                    conditional=(scope != "module") or under_tc,
                    scope=scope,
                    tags=compute_tags(raw, scope),
                )
            )

        def on_from(self, n: ast.ImportFrom, *, under_tc: bool) -> None:
            if not include_tc and under_tc:
                return
            raw = node_source_segment(source, n)
            names: List[Tuple[str, Optional[str]]] = [(a.name, a.asname) for a in n.names]
            scope = self.current_scope()
            results.append(
                ImportRef(
                    raw=raw,
                    module_token=n.module,  # may be None for 'from . import X'
                    names=names,
                    is_from=True,
                    level=n.level or 0,
                    under_type_checking=under_tc,
                    lineno=n.lineno,
                    end_lineno=getattr(n, "end_lineno", n.lineno),
                    conditional=(scope != "module") or under_tc,
                    scope=scope,
                    tags=compute_tags(raw, scope),
                )
            )

    ImportCollector().visit(tree)

    # -------- Second pass: detect dynamic imports (optional) --------

    if detect_dynamic:
        class DynamicCollector(ScopeAndTCVisitor):
            def __init__(self) -> None:
                super().__init__(typing_aliases, tc_names)

            def on_call(self, call: ast.Call, *, under_tc: bool) -> None:
                mod_literal = match_dynamic_import_literal(call)
                if not mod_literal:
                    return

                raw = node_source_segment(source, call)
                scope = self.current_scope()
                tags = compute_tags(raw, scope, base=["dynamic", "delegate"])

                results.append(
                    ImportRef(
                        raw=raw,
                        module_token=mod_literal,
                        names=[],                    # dynamic access; symbol unknown here
                        is_from=False,
                        level=0,                     # treat as absolute
                        under_type_checking=under_tc,
                        lineno=getattr(call, "lineno", 1),
                        end_lineno=getattr(call, "end_lineno", getattr(call, "lineno", 1)),
                        conditional=(scope != "module") or under_tc,
                        scope=scope,
                        tags=tags,
                    )
                )

        DynamicCollector().visit(tree)

    return results
