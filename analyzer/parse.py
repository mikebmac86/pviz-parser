from __future__ import annotations
import ast
from pathlib import Path
from typing import List, Optional, Tuple

from analyzer.config import AnalyzerCfg
from analyzer.analyzer_types import ImportRef, SymbolSummary, ParsedModule
from analyzer.imports_lex import extract_lexical_imports
from analyzer.ast_common import (
    ModuleSymbolCollector,
    collect_typing_aliases_and_tc_names,
    collect_top_level_imports_and_tc_blocks,
    docstring_spans,
)

# v1.7 metadata support: optional enhanced collector for detailed symbol extraction
try:
    from analyzer.ast_common import EnhancedModuleSymbolCollector
    ENHANCED_COLLECTOR_AVAILABLE = True
except ImportError:
    ENHANCED_COLLECTOR_AVAILABLE = False

# --- optional injection: python crosstalk candidates (safe, non-fatal) ---
try:
    from analyzer.extract_crosstalk import extract_crosstalk_candidates_py_v1  # type: ignore
except Exception:  # pragma: no cover
    extract_crosstalk_candidates_py_v1 = None  # type: ignore


# ----------------- small helpers -----------------

def _pack(names: List[str], kind: str) -> List[SymbolSummary]:
    return [SymbolSummary(name=n, kind=kind) for n in (names or [])]


# ----------------- LOC determination -----------------
import io
import tokenize


def _count_loc_for_source(text: str) -> dict:
    """
    Deterministic LOC breakdown for Python source.
    Returns: { total, blank, comment, docstring, code }
    """
    lines = text.splitlines()
    total = len(lines)
    blank_lines = {i + 1 for i, ln in enumerate(lines) if not ln.strip()}

    # AST docstring spans
    doc_lines = set()
    try:
        tree = ast.parse(text)
        for (s, e) in docstring_spans(tree):
            for ln in range(s, e + 1):
                doc_lines.add(ln)
    except Exception:
        # On syntax error, treat as no docstrings; continue with tokenize-only
        pass

    # Tokenize to identify comment-only vs code lines
    comment_only = set()
    tokens_by_line = {}

    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            ttype = tok.type
            lno = tok.start[0]
            if ttype in (
                tokenize.ENCODING,
                tokenize.ENDMARKER,
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
            ):
                continue
            tokens_by_line.setdefault(lno, set()).add(ttype)
    except Exception:
        tokens_by_line = {}

    for lno, types in tokens_by_line.items():
        non_comment = {t for t in types if t != tokenize.COMMENT}
        if types and not non_comment:
            comment_only.add(lno)

    def _is_code_line(lno: int) -> bool:
        if lno in blank_lines:
            return False
        types = tokens_by_line.get(lno, set())
        if not types:
            return False
        # If it's a docstring line and we only see STRING/COMMENT, keep it as docstring
        non_trivial = {t for t in types if t not in (tokenize.COMMENT, tokenize.STRING)}
        if lno in doc_lines and not non_trivial:
            return False
        # Any non-comment tokens => code
        return len({t for t in types if t != tokenize.COMMENT}) > 0

    docstring = sum(1 for ln in doc_lines if (ln not in blank_lines and not _is_code_line(ln)))
    comment = sum(1 for ln in comment_only if (ln not in blank_lines and ln not in doc_lines))
    blank = len(blank_lines)
    code = total - blank - docstring - comment

    return {
        "total": total,
        "blank": blank,
        "comment": comment,
        "docstring": docstring,
        "code": code,
    }


# ------------------------------------------------------
# ----------------- public API -----------------

def parse_file(path: Path, cfg: Optional[AnalyzerCfg] = None) -> Tuple[Optional[ParsedModule], List[str]]:
    """
    Parse a Python module into a summary (defs/exports) and import refs.

    Returns: (ParsedModule | None, warnings)
      - None on IO/Syntax errors (warnings will include reason).

    Version notes
    -------------
    v1.7:
      Supports detailed metadata extraction via cfg.detailed_symbols.

    Important:
      Parsing-level detailed symbol extraction is a metadata concern only.
      SCC computation, conceptual/runtime SCC semantics, tc_inflation, and
      NodeFacts schema version escalation are handled in later graph/artifact
      layers, not here.
    """
    warnings: List[str] = []
    cfg = cfg or AnalyzerCfg()

    # --- file load with size guard
    try:
        try:
            if cfg.max_file_bytes and path.stat().st_size > cfg.max_file_bytes:
                return None, [f"File too large (> {cfg.max_file_bytes} bytes): {path}"]
        except Exception:
            pass
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return None, [f"IOError: {e}"]

    # --- LOC stats (safe, independent of AST success)
    loc_stats = _count_loc_for_source(text)

    # --- parse to AST
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as e:
        return None, [f"SyntaxError: {e.lineno}:{getattr(e, 'offset', 0)} {e.msg}"]

    # --- defs + __all__ (module-scope only) ---
    # v1.7 metadata support: use enhanced collector if available and enabled
    use_detailed = (
        ENHANCED_COLLECTOR_AVAILABLE
        and getattr(cfg, "detailed_symbols", False)
    )

    if use_detailed:
        # Richer symbol extraction for metadata fields such as
        # classes_detailed / functions_detailed / globals_detailed.
        v = EnhancedModuleSymbolCollector(detailed=True)
    else:
        # Backward-compatible/simple collector.
        v = ModuleSymbolCollector()

    v.visit(tree)
    warnings.extend(v.warnings)

    # --- AST-backed imports for edge builder (module-scope & TYPE_CHECKING blocks)
    typing_aliases, tc_names = collect_typing_aliases_and_tc_names(tree)
    imports, type_blocks = collect_top_level_imports_and_tc_blocks(tree, typing_aliases, tc_names)

    # --- lexical imports for diagnostics (respects cfg)
    imports_ast_lex: List[ImportRef] = extract_lexical_imports(path, text, cfg, tree=tree)

    parsed = ParsedModule(
        classes=_pack(v.classes, "class"),
        functions=_pack(v.functions, "function"),
        globals=_pack(v.globals, "global"),
        imports_ast=imports_ast_lex,                 # lexical refs (for diagnostics)
        all_exports=v.all_exports,
        warnings=warnings,
        # >>> fields required by edge builder <<<
        imports=imports,                             # top-level ast.Import/ast.ImportFrom
        type_checking_blocks=type_blocks,            # lists of import stmts under TYPE_CHECKING
    )

    # Attach LOC in a backward-compatible way
    try:
        setattr(parsed, "loc", loc_stats)
        setattr(parsed, "loc_code", loc_stats.get("code"))
    except Exception:
        pass

    # v1.7 metadata fields: attach detailed metadata if extracted
    if use_detailed:
        if hasattr(v, "classes_detailed") and v.classes_detailed:
            try:
                setattr(parsed, "classes_detailed", v.classes_detailed)
            except Exception:
                pass

        if hasattr(v, "functions_detailed") and v.functions_detailed:
            try:
                setattr(parsed, "functions_detailed", v.functions_detailed)
            except Exception:
                pass

        if hasattr(v, "globals_detailed") and v.globals_detailed:
            try:
                setattr(parsed, "globals_detailed", v.globals_detailed)
            except Exception:
                pass

    # --- Inject crosstalk candidates (backward-compatible attribute; non-fatal) ---
    if extract_crosstalk_candidates_py_v1 is not None:
        try:
            # Use rel_posix if available; caller can override later
            rel_posix = path.as_posix()
            cands = extract_crosstalk_candidates_py_v1(source=text, rel_path=rel_posix)
            setattr(parsed, "crosstalk_candidates_py_v1", cands)
        except Exception:
            # Never allow crosstalk extraction to break parsing.
            try:
                setattr(parsed, "crosstalk_candidates_py_v1", [])
            except Exception:
                pass

    return parsed, warnings


def parse_module(src_text: str) -> ParsedModule:
    """
    Return a full ParsedModule from source text (no cfg; imports_ast left empty).

    Notes
    -----
    - This helper uses the default/basic collector.
    - It does not enable v1.7 detailed metadata extraction unless a cfg-aware
      path is used elsewhere.
    - Parsing here remains unrelated to SCC versioning or dual-SCC semantics.
    """
    if not src_text:
        return ParsedModule(
            classes=[], functions=[], globals=[],
            imports_ast=[], all_exports=[], warnings=[],
            imports=[], type_checking_blocks=[],
        )

    try:
        tree = ast.parse(src_text)
    except Exception:
        return ParsedModule(
            classes=[], functions=[], globals=[],
            imports_ast=[], all_exports=[], warnings=[],
            imports=[], type_checking_blocks=[],
        )

    # LOC (best effort)
    try:
        loc_stats = _count_loc_for_source(src_text or "")
    except Exception:
        loc_stats = {"code": 0}

    # Default collector only; no cfg means no v1.7 detailed metadata extraction here.
    v = ModuleSymbolCollector()
    v.visit(tree)

    typing_aliases, tc_names = collect_typing_aliases_and_tc_names(tree)
    imports, type_blocks = collect_top_level_imports_and_tc_blocks(tree, typing_aliases, tc_names)

    pm = ParsedModule(
        classes=_pack(v.classes, "class"),
        functions=_pack(v.functions, "function"),
        globals=_pack(v.globals, "global"),
        imports_ast=[],                 # not produced here
        all_exports=v.all_exports,
        warnings=v.warnings,
        imports=imports,
        type_checking_blocks=type_blocks,
    )
    try:
        setattr(pm, "loc", loc_stats)
        setattr(pm, "loc_code", loc_stats.get("code"))
    except Exception:
        pass

    # --- Inject crosstalk candidates (no rel_path available here) ---
    if extract_crosstalk_candidates_py_v1 is not None:
        try:
            cands = extract_crosstalk_candidates_py_v1(source=src_text, rel_path="")
            setattr(pm, "crosstalk_candidates_py_v1", cands)
        except Exception:
            try:
                setattr(pm, "crosstalk_candidates_py_v1", [])
            except Exception:
                pass

    return pm