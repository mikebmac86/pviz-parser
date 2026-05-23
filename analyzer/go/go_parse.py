# backend/saas_analyzer/analyzer/go/parse-go.py
from __future__ import annotations

"""
Go source parser (Level 1 symbols) for PViz.

This module is meant to be called from the NodeFacts builder in the same spirit
as Python's analyzer.parse.parse_file(): it extracts per-file symbols that can be
stored into NodeFactsNode fields:
  - classes (Go types)
  - functions (top-level funcs + methods)
  - globals (package-scope vars/consts; includes init() if present)
  - exports (exported identifiers; capitalized)
  - loc_code (approx code LOC)

It is intentionally:
  - deterministic
  - error-tolerant
  - lightweight (no go/types, no go/packages)

Later we can add an optional type-checking pass for call edges, but this is enough
to populate NodeFacts v1.6 fields.

Integration note:
  NodeFacts builder currently calls analyzer.parse.parse_file(...) for Python.
  Once Go is wired, it can call parse_go_file(...) similarly for *.go inputs.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Set

import re


# ---------------------------------------------------------------------------
# Output model (intentionally simple & JSON-friendly)
# ---------------------------------------------------------------------------

@dataclass
class GoParsedFile:
    path: Path
    parse_status: str                 # "ok" | "warn" | "error"
    error_snippet: Optional[str]

    package: Optional[str]

    classes: List[str]                # Go "type" names
    functions: List[str]              # funcs + methods, stable naming
    globals: List[str]                # const/var names, plus "init" if present
    all_exports: List[str]            # exported identifiers

    loc_code: Optional[int]           # "code-ish" LOC (nonblank, non-comment)


# ---------------------------------------------------------------------------
# Helpers: comments + light token extraction
# ---------------------------------------------------------------------------

def _strip_go_comments_keep_lines(text: str) -> str:
    """
    Remove Go comments while preserving line breaks so LOC counting stays stable.
    Handles:
      - // line comments
      - /* block comments */
    Not a full lexer; but stable and sufficient for LOC heuristics.
    """
    out_lines: List[str] = []
    in_block = False

    for raw in text.splitlines():
        line = raw
        i = 0
        buf: List[str] = []
        while i < len(line):
            if in_block:
                j = line.find("*/", i)
                if j == -1:
                    i = len(line)
                    continue
                in_block = False
                i = j + 2
                continue

            if line.startswith("/*", i):
                in_block = True
                i += 2
                continue

            if line.startswith("//", i):
                break

            buf.append(line[i])
            i += 1

        out_lines.append("".join(buf))

    return "\n".join(out_lines)


def _count_loc_code_go(text: str) -> int:
    """
    Code LOC heuristic: non-empty lines after stripping comments.
    """
    stripped = _strip_go_comments_keep_lines(text)
    return sum(1 for ln in stripped.splitlines() if ln.strip())


# Regexes for lightweight, deterministic symbol extraction.
# These are intentionally conservative; Go syntax is rich but predictable enough
# for these patterns to work well on most codebases.
_RE_PACKAGE = re.compile(r"^\s*package\s+([A-Za-z_]\w*)\s*$", re.MULTILINE)

# type <Name> ...
_RE_TYPE = re.compile(r"^\s*type\s+([A-Za-z_]\w*)\b", re.MULTILINE)

# func <Name>(...
_RE_FUNC = re.compile(r"^\s*func\s+([A-Za-z_]\w*)\s*\(", re.MULTILINE)

# func (<recv>) <Name>(...
_RE_METHOD = re.compile(r"^\s*func\s*\(\s*[^)]*\)\s*([A-Za-z_]\w*)\s*\(", re.MULTILINE)

# var <Name> ...   OR var ( <Name> ... )
# const <Name> ... OR const ( <Name> ... )
_RE_VAR = re.compile(r"^\s*var\s+([A-Za-z_]\w*)\b", re.MULTILINE)
_RE_CONST = re.compile(r"^\s*const\s+([A-Za-z_]\w*)\b", re.MULTILINE)

# Rough capture of names within var( ... ) / const( ... ) blocks:
_RE_BLOCK_DECL_LINE = re.compile(r"^\s*([A-Za-z_]\w*)\b", re.MULTILINE)


def _extract_block_names(block_body: str) -> List[str]:
    """
    Extract potential leading identifiers from lines inside var()/const() blocks.
    Conservative: first token on a line if it looks like an identifier.
    Skips blanks and lines starting with ')' or keywords.
    """
    names: List[str] = []
    for raw in block_body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(")"):
            continue
        if line.startswith("//"):
            continue
        # common keywords inside blocks that aren't declarations
        if line.startswith(("type ", "var ", "const ", "func ", "import ", "package ")):
            continue
        m = _RE_BLOCK_DECL_LINE.match(line)
        if m:
            nm = m.group(1)
            # ignore blanks and underscore-only
            if nm and nm != "_":
                names.append(nm)
    return names


def _extract_var_const_blocks(text_no_comments: str) -> Tuple[List[str], List[str]]:
    """
    Extract names from:
      var ( ... )
      const ( ... )
    plus single-line var/const handled separately.
    """
    vars_: List[str] = []
    consts_: List[str] = []

    # Find var ( ... )
    # Very simple parenthesis block matcher based on scanning lines.
    # We accept that it’s heuristic; it’s deterministic and safe.
    def scan_blocks(keyword: str) -> List[str]:
        out: List[str] = []
        pat = f"{keyword} ("
        lines = text_no_comments.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith(keyword) and line[len(keyword):].lstrip().startswith("("):
                # collect until matching ')'
                body_lines: List[str] = []
                i += 1
                while i < len(lines):
                    ln = lines[i]
                    if ln.strip().startswith(")"):
                        break
                    body_lines.append(ln)
                    i += 1
                out.extend(_extract_block_names("\n".join(body_lines)))
            i += 1
        return out

    vars_.extend(scan_blocks("var"))
    consts_.extend(scan_blocks("const"))
    return vars_, consts_


def _dedupe_preserve(seq: Sequence[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for s in seq:
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _is_exported_go(name: str) -> bool:
    """
    Go export rule: first rune is uppercase A-Z.
    """
    if not name:
        return False
    c = name[0]
    return "A" <= c <= "Z"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_go_file(path: Path) -> GoParsedFile:
    """
    Parse a single Go file and return extracted symbols.

    This function never raises (unless the filesystem access itself is fatal);
    it returns parse_status="error" with an error snippet.
    """
    classes: List[str] = []
    functions: List[str] = []
    globals_: List[str] = []
    exports: List[str] = []
    pkg: Optional[str] = None
    err: Optional[str] = None
    status = "ok"
    loc_code: Optional[int] = None

    try:
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1", errors="replace")

        # LOC code heuristic
        loc_code = _count_loc_code_go(text)

        # Strip comments for regex scanning (reduces false positives)
        text_nc = _strip_go_comments_keep_lines(text)

        # package
        m = _RE_PACKAGE.search(text_nc)
        if m:
            pkg = m.group(1)

        # types
        classes = [m.group(1) for m in _RE_TYPE.finditer(text_nc)]

        # funcs and methods
        # Note: methods are also "func Name(" at top-level, but we keep both regexes
        # and de-dupe in the end.
        funcs = [m.group(1) for m in _RE_FUNC.finditer(text_nc)]
        meths = [m.group(1) for m in _RE_METHOD.finditer(text_nc)]

        # For now, we keep methods as plain names; receiver-qualified method naming
        # requires AST. We'll refine later once everything is connected.
        functions = funcs + meths

        # globals: var/const (single-line) + blocks
        vars_single = [m.group(1) for m in _RE_VAR.finditer(text_nc)]
        const_single = [m.group(1) for m in _RE_CONST.finditer(text_nc)]
        vars_block, consts_block = _extract_var_const_blocks(text_nc)

        globals_ = vars_single + vars_block + const_single + consts_block

        # init()
        if "init" in functions:
            # ensure it is treated like a global entrypoint as well
            globals_.append("init")

        # exports: union of exported decls
        for nm in classes:
            if _is_exported_go(nm):
                exports.append(nm)
        for nm in functions:
            if _is_exported_go(nm):
                exports.append(nm)
        for nm in globals_:
            if _is_exported_go(nm):
                exports.append(nm)

        # final normalize: stable + deduped
        classes = sorted(_dedupe_preserve(classes))
        functions = sorted(_dedupe_preserve(functions))
        globals_ = sorted(_dedupe_preserve(globals_))
        exports = sorted(_dedupe_preserve(exports))

    except Exception as e:
        status = "error"
        try:
            err = f"{type(e).__name__}: {e}"
        except Exception:
            err = type(e).__name__
        # leave other fields as empty/None

    return GoParsedFile(
        path=path,
        parse_status=status,
        error_snippet=err[:200] if err else None,
        package=pkg,
        classes=classes,
        functions=functions,
        globals=globals_,
        all_exports=exports,
        loc_code=loc_code,
    )
