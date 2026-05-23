#saas_analyzer/analyzer/java/parse_java/regex_engine.py
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence, Set, Dict
import re

from .models import JavaParsedFile, JavaImport


# ---------------------------------------------------------------------------
# Helpers: comments + normalization
# ---------------------------------------------------------------------------

def _strip_comments_keep_lines(text: str) -> str:
    """
    Remove // line comments and /* */ block comments while preserving line breaks.
    Conservative heuristic (not a full lexer).
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


def _count_loc_code(text: str) -> int:
    stripped = _strip_comments_keep_lines(text)
    return sum(1 for ln in stripped.splitlines() if ln.strip())


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


def _has_public(mods: str) -> bool:
    return "public" in (mods or "")


# ---------------------------------------------------------------------------
# Regexes (annotation-aware, still conservative)
# ---------------------------------------------------------------------------

_RE_PACKAGE = re.compile(
    r"^\s*package\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*;",
    re.MULTILINE,
)

_ANNOT_STACK = r"(?:\s*@[\w.]+(?:\s*\([^)]*\))?\s*)*"

_RE_TOPLEVEL_TYPE = re.compile(
    rf"^\s*{_ANNOT_STACK}"
    r"(?P<mods>(?:public|protected|private|abstract|final|static|sealed|non-sealed|\s)*)"
    r"(?P<kind>class|interface|enum|record|@interface)\s+"
    r"(?P<name>[A-Za-z_]\w*)\b",
    re.MULTILINE,
)

_RE_RECORD_HEADER = re.compile(
    rf"^\s*{_ANNOT_STACK}"
    r"(?P<mods>(?:public|protected|private|abstract|final|static|sealed|non-sealed|\s)*)"
    r"record\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<components>[^)]*)\)",
    re.MULTILINE,
)

_RE_METHOD = re.compile(
    rf"^\s*{_ANNOT_STACK}"
    r"(?P<mods>(?:public|protected|private|static|final|abstract|synchronized|native|strictfp|\s)*)"
    r"(?:<[^>]+>\s*)?"
    r"(?P<rtype>[A-Za-z_]\w*(?:\s*<[^>]+>)?(?:\[\])?)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*\(",
    re.MULTILINE,
)

def _re_constructor_for(class_name: str) -> re.Pattern:
    return re.compile(
        rf"^\s*{_ANNOT_STACK}"
        r"(?P<mods>(?:public|protected|private|\s)*)"
        rf"{re.escape(class_name)}\s*\(",
        re.MULTILINE,
    )

_METHOD_EXCLUDE = {
    "if", "for", "while", "switch", "catch", "return", "new", "throw",
    "do", "else", "try", "finally", "case", "assert", "super", "this",
}

_RE_FIELD = re.compile(
    rf"^\s*{_ANNOT_STACK}"
    r"(?P<mods>(?:public|protected|private|static|final|transient|volatile|\s)*)"
    r"(?P<type>[A-Za-z_]\w*(?:\s*<[^;>]+>)?(?:\[\])?)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*(?:=[^;]*)?;",
    re.MULTILINE,
)

_RE_ENUM_BLOCK = re.compile(
    rf"^\s*{_ANNOT_STACK}"
    r"(?P<mods>(?:public|protected|private|abstract|final|static|\s)*)"
    r"enum\s+(?P<name>[A-Za-z_]\w*)\s*\{{(?P<body>[\s\S]*?)\}}",
    re.MULTILINE,
)

_RE_ENUM_CONST = re.compile(
    r"^\s*([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*(?:,|;|\s*$)",
    re.MULTILINE,
)

_RE_IMPORT = re.compile(
    r"^\s*import\s+(?P<static>static\s+)?(?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)(?P<wild>\.\*)?\s*;",
    re.MULTILINE,
)


def _parse_record_components(components: str) -> List[str]:
    out: List[str] = []
    for part in (components or "").split(","):
        seg = part.strip()
        if not seg:
            continue
        seg = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", seg).strip()
        m = re.search(r"([A-Za-z_]\w*)\s*$", seg)
        if m:
            out.append(m.group(1))
    return out


def _parse_enum_constants(enum_body: str) -> List[str]:
    body = enum_body or ""
    semi = body.find(";")
    if semi != -1:
        body = body[:semi]
    body = re.sub(r"\{[^{}]*\}", "", body)

    out: List[str] = []
    for m in _RE_ENUM_CONST.finditer(body):
        name = m.group(1)
        if name:
            out.append(name)
    return out


# ---------------------------------------------------------------------------
# Compatibility API (text-based)
# ---------------------------------------------------------------------------

def extract_package(text: str) -> Optional[str]:
    if not isinstance(text, str) or not text:
        return None
    text_nc = _strip_comments_keep_lines(text)
    m = _RE_PACKAGE.search(text_nc)
    return m.group(1) if m else None


def extract_declared_types(text: str, package: Optional[str] = None) -> List[str]:
    if not isinstance(text, str) or not text:
        return []
    text_nc = _strip_comments_keep_lines(text)

    names: List[str] = []

    for rm in _RE_RECORD_HEADER.finditer(text_nc):
        name = rm.group("name")
        if name:
            names.append(name)

    for tm in _RE_TOPLEVEL_TYPE.finditer(text_nc):
        name = tm.group("name")
        if name:
            names.append(name)

    return _dedupe_preserve(names)


def extract_imports(text: str) -> List[JavaImport]:
    if not isinstance(text, str) or not text:
        return []
    text_nc = _strip_comments_keep_lines(text)

    out: List[JavaImport] = []
    for m in _RE_IMPORT.finditer(text_nc):
        raw = (m.group("name") or "").strip()
        if not raw:
            continue
        is_static = bool(m.group("static"))
        is_wild = bool(m.group("wild"))
        out.append(JavaImport(target=raw, is_wildcard=is_wild, is_static=is_static))

    return out


# ---------------------------------------------------------------------------
# File parse (regex engine)
# ---------------------------------------------------------------------------

def parse_java_file(path: Path) -> JavaParsedFile:
    pkg: Optional[str] = None
    classes: List[str] = []
    functions: List[str] = []
    globals_: List[str] = []
    exports: List[str] = []
    imports: List[JavaImport] = []
    imports_raw: List[str] = []
    loc_code: Optional[int] = None

    status = "ok"
    err: Optional[str] = None

    try:
        data = Path(path).read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1", errors="replace")

        loc_code = _count_loc_code(text)
        text_nc = _strip_comments_keep_lines(text)

        # Extract imports — shares text_nc with the rest of the parse
        seen_import_keys: Set[str] = set()
        for _im in _RE_IMPORT.finditer(text_nc):
            raw = (_im.group("name") or "").strip()
            if not raw:
                continue
            is_static = bool(_im.group("static"))
            is_wild = bool(_im.group("wild"))
            key = f"{raw}|{is_static}|{is_wild}"
            if key not in seen_import_keys:
                seen_import_keys.add(key)
                imports.append(JavaImport(target=raw, is_wildcard=is_wild, is_static=is_static))
                imports_raw.append(raw)

        m = _RE_PACKAGE.search(text_nc)
        if m:
            pkg = m.group(1)

        public_types: List[str] = []
        record_components_by_type: Dict[str, List[str]] = {}

        for rm in _RE_RECORD_HEADER.finditer(text_nc):
            name = rm.group("name")
            mods = rm.group("mods") or ""
            comps = rm.group("components") or ""
            if name:
                classes.append(name)
                if _has_public(mods):
                    public_types.append(name)
                record_components_by_type[name] = _parse_record_components(comps)

        for tm in _RE_TOPLEVEL_TYPE.finditer(text_nc):
            name = tm.group("name")
            mods = tm.group("mods") or ""
            if not name:
                continue
            classes.append(name)
            if _has_public(mods):
                public_types.append(name)

        public_methods: List[str] = []
        for mm in _RE_METHOD.finditer(text_nc):
            name = mm.group("name")
            if not name or name in _METHOD_EXCLUDE:
                continue
            mods = mm.group("mods") or ""
            functions.append(name)
            if _has_public(mods):
                public_methods.append(name)

        public_ctors: List[str] = []
        for tname in _dedupe_preserve(classes):
            cre = _re_constructor_for(tname)
            for cm in cre.finditer(text_nc):
                mods = cm.group("mods") or ""
                functions.append(tname)
                if _has_public(mods):
                    public_ctors.append(tname)

        public_fields: List[str] = []
        for fm in _RE_FIELD.finditer(text_nc):
            name = fm.group("name")
            if not name:
                continue
            mods = fm.group("mods") or ""
            globals_.append(name)
            if _has_public(mods):
                public_fields.append(name)

        public_record_components: List[str] = []
        for tname, comps in record_components_by_type.items():
            globals_.extend(comps)
            if tname in public_types:
                public_record_components.extend(comps)

        public_enum_consts: List[str] = []
        for em in _RE_ENUM_BLOCK.finditer(text_nc):
            mods = em.group("mods") or ""
            body = em.group("body") or ""
            consts = _parse_enum_constants(body)
            globals_.extend(consts)
            if _has_public(mods):
                public_enum_consts.extend(consts)

        exports = (
            public_types
            + public_methods
            + public_ctors
            + public_fields
            + public_record_components
            + public_enum_consts
        )
        if not exports:
            exports = list(classes)

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

    return JavaParsedFile(
        path=Path(path),
        parse_status=status,
        error_snippet=err[:200] if err else None,
        package=pkg,
        classes=classes,
        functions=functions,
        globals=globals_,
        all_exports=exports,
        loc_code=loc_code,
        imports=imports if imports else None,
        imports_raw=imports_raw if imports_raw else None,
    )