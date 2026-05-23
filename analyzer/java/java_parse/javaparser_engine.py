#saas_analyzer/analyzer/java/parse_java/javaparser_engine.py
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import JavaImport, JavaParsedFile
from .regex_engine import _count_loc_code  # reuse LOC counter for now


class JavaParserUnavailable(RuntimeError):
    pass


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v.strip() if isinstance(v, str) and v.strip() else default


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if not isinstance(v, str) or not v.strip():
        return default
    try:
        return int(v.strip())
    except Exception:
        return default


def _jar_path() -> str:
    jar = os.environ.get("PVIZ_JAVAPARSER_JAR")
    if jar and jar.strip():
        return jar.strip()
    # Reasonable default (adjust to your repo/image)
    return "/opt/pviz/pviz-javaparser-cli.jar"


def _java_bin() -> str:
    return _env("PVIZ_JAVA_BIN", "java")


def _run_javaparser_cli(file_path: Path) -> Dict[str, Any]:
    jar = _jar_path()
    java = _java_bin()
    timeout_s = _env_int("PVIZ_JAVAPARSER_TIMEOUT_S", 30)

    if not Path(jar).exists():
        raise JavaParserUnavailable(f"javaparser jar not found at {jar}")

    cmd = [java, "-jar", jar, "--file", str(file_path)]
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except FileNotFoundError as e:
        raise JavaParserUnavailable(f"java binary not found: {java}") from e
    except subprocess.TimeoutExpired as e:
        raise JavaParserUnavailable(f"javaparser cli timed out after {timeout_s}s") from e

    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()

    # Non-zero rc with no stdout is almost always a hard failure
    if not out and p.returncode != 0:
        raise JavaParserUnavailable(
            f"javaparser cli failed (rc={p.returncode}): {err[:300]}"
        )

    # Sometimes tools log to stderr even on success; treat "no stdout" as failure regardless
    if not out:
        raise JavaParserUnavailable(
            f"javaparser cli produced no output (rc={p.returncode}): {err[:300]}"
        )

    try:
        data = json.loads(out)
    except Exception as e:
        raise JavaParserUnavailable(
            f"javaparser cli returned non-json output (rc={p.returncode}): {out[:300]} | stderr={err[:300]}"
        ) from e

    # If the CLI returns {"ok": false, "error": "..."} still treat as parse-level failure
    # (but we return data for caller to decide how to surface)
    return data


def _to_str_list(v: Any) -> List[str]:
    if not isinstance(v, list):
        return []
    out: List[str] = []
    for x in v:
        if x is None:
            continue
        s = str(x).strip()
        if s:
            out.append(s)
    return out


def _dedupe_sorted(v: List[str]) -> List[str]:
    # deterministic like current behavior
    return sorted(set(s for s in v if s))


def _compute_classes_fq(package: Optional[str], classes: List[str]) -> Optional[List[str]]:
    if not package or not classes:
        return None
    pkg = package.strip()
    if not pkg:
        return None
    # "Outer.Inner" becomes "pkg.Outer.Inner" (valid FQ reference in PViz terms)
    return _dedupe_sorted([f"{pkg}.{c}" for c in classes if c])


def _parse_imports(data: Dict[str, Any]) -> tuple[Optional[List[JavaImport]], Optional[List[str]], Optional[List[str]], Optional[List[str]]]:
    """
    Support multiple JSON shapes:
      - "imports": ["a.b.C", "static x.y.Z.*", "a.b.*"]
      - "imports": [{"target":"a.b.C","is_static":false,"is_wildcard":false}, ...]
      - "imports": [{"name":"a.b.C","static":false,"wildcard":false}, ...]
    """
    raw_imports = data.get("imports")
    imports_raw: List[str] = []

    parsed: List[JavaImport] = []

    if isinstance(raw_imports, list):
        for item in raw_imports:
            if isinstance(item, str):
                s = item.strip()
                if not s:
                    continue
                imports_raw.append(s)

                # Very tolerant parsing for string form:
                # allow "static foo.bar.Baz.*" or "foo.bar.*" or "foo.bar.Baz"
                is_static = False
                s2 = s
                if s2.startswith("static "):
                    is_static = True
                    s2 = s2[len("static ") :].strip()

                is_wildcard = s2.endswith(".*")
                target = s2[:-2] if is_wildcard else s2

                if target:
                    parsed.append(JavaImport(target=target, is_wildcard=is_wildcard, is_static=is_static))

            elif isinstance(item, dict):
                # prefer canonical keys; allow alternates
                target = item.get("target") or item.get("name") or item.get("import")
                if isinstance(target, str):
                    target_s = target.strip()
                else:
                    target_s = ""

                is_static = item.get("is_static")
                if not isinstance(is_static, bool):
                    is_static = item.get("static")
                is_static_b = bool(is_static)

                is_wildcard = item.get("is_wildcard")
                if not isinstance(is_wildcard, bool):
                    is_wildcard = item.get("wildcard")
                is_wild_b = bool(is_wildcard)

                if target_s:
                    imports_raw.append(target_s)
                    # If the tool includes ".*" in target, normalize it
                    if target_s.endswith(".*"):
                        is_wild_b = True
                        target_s = target_s[:-2]
                    parsed.append(JavaImport(target=target_s, is_wildcard=is_wild_b, is_static=is_static_b))

    if not parsed and not imports_raw:
        return None, None, None, None

    imports_wildcard = _dedupe_sorted([i.target for i in parsed if i.is_wildcard])
    imports_static = _dedupe_sorted([i.target for i in parsed if i.is_static])
    imports = sorted(parsed, key=lambda x: (x.target, x.is_static, x.is_wildcard))

    return imports, _dedupe_sorted(imports_raw), imports_static or None, imports_wildcard or None


def _parse_annotations(data: Dict[str, Any]) -> tuple[Optional[Dict[str, int]], Optional[Dict[str, List[str]]]]:
    """
    Accept either:
      - "annotations": {"RestController": 2, "Autowired": 5}
      - "annotations": ["RestController", "Autowired", ...] (we count them)
    and optionally:
      - "decl_annotations": {"MyClass": ["Service"], "MyClass#foo": ["Transactional"]}
    """
    ann = data.get("annotations")
    annotations: Optional[Dict[str, int]] = None

    if isinstance(ann, dict):
        tmp: Dict[str, int] = {}
        for k, v in ann.items():
            if not isinstance(k, str) or not k.strip():
                continue
            try:
                n = int(v)
            except Exception:
                n = 0
            if n > 0:
                tmp[k.strip()] = n
        annotations = tmp or None
    elif isinstance(ann, list):
        tmp: Dict[str, int] = {}
        for x in ann:
            if x is None:
                continue
            s = str(x).strip()
            if not s:
                continue
            tmp[s] = tmp.get(s, 0) + 1
        annotations = tmp or None

    decl = data.get("decl_annotations")
    decl_annotations: Optional[Dict[str, List[str]]] = None
    if isinstance(decl, dict):
        tmp2: Dict[str, List[str]] = {}
        for k, v in decl.items():
            if not isinstance(k, str) or not k.strip():
                continue
            vals = _to_str_list(v)
            if vals:
                tmp2[k.strip()] = _dedupe_sorted(vals)
        decl_annotations = tmp2 or None

    return annotations, decl_annotations


def parse_java_file(path: Path) -> JavaParsedFile:
    """
    JavaParser-backed parse.
    Never raises: returns parse_status="error" on failures.
    """
    status = "ok"
    err: Optional[str] = None

    pkg: Optional[str] = None
    classes: List[str] = []
    functions: List[str] = []
    globals_: List[str] = []
    exports: List[str] = []
    loc_code: Optional[int] = None

    # additive fields
    imports: Optional[List[JavaImport]] = None
    imports_raw: Optional[List[str]] = None
    imports_static: Optional[List[str]] = None
    imports_wildcard: Optional[List[str]] = None
    classes_fq: Optional[List[str]] = None
    annotations: Optional[Dict[str, int]] = None
    decl_annotations: Optional[Dict[str, List[str]]] = None

    try:
        # Keep loc_code computed in Python for now (jar can add later).
        try:
            raw = Path(path).read_text("utf-8")
        except UnicodeDecodeError:
            raw = Path(path).read_text("latin-1", errors="replace")
        loc_code = _count_loc_code(raw)

        data = _run_javaparser_cli(Path(path))

        ok = bool(data.get("ok", True))
        if not ok:
            status = "error"
            err = str(data.get("error") or "javaparser cli reported ok=false")[:200]

        pkg = data.get("package") if isinstance(data.get("package"), str) else None

        # Support both old and new JAR field names
        classes = _to_str_list(data.get("declared_types")) or _to_str_list(data.get("types"))
        functions = _to_str_list(data.get("methods"))

        # globals = fields + enum consts + record comps
        globals_.extend(_to_str_list(data.get("fields")))
        globals_.extend(_to_str_list(data.get("enum_constants")))
        globals_.extend(_to_str_list(data.get("record_components")))

        ex = data.get("exports")
        exports = _to_str_list(ex) if isinstance(ex, list) else []
        if not exports:
            # Preserve old behavior: if no exports computed, default to classes
            exports = list(classes)

        imports, imports_raw, imports_static, imports_wildcard = _parse_imports(data)
        annotations, decl_annotations = _parse_annotations(data)

        classes = _dedupe_sorted(classes)
        functions = _dedupe_sorted(functions)
        globals_ = _dedupe_sorted(globals_)
        exports = _dedupe_sorted(exports)

        # Prefer JAR-computed FQ names (more accurate), fallback to local computation
        classes_fq_from_jar = _to_str_list(data.get("declared_types_fq"))
        if classes_fq_from_jar:
            classes_fq = _dedupe_sorted(classes_fq_from_jar)
        else:
            classes_fq = _compute_classes_fq(pkg, classes)

    except JavaParserUnavailable as e:
        status = "error"
        err = f"JavaParserUnavailable: {e}"[:200]
    except Exception as e:
        status = "error"
        err = f"{type(e).__name__}: {e}"[:200]

    return JavaParsedFile(
        path=Path(path),
        parse_status=status,
        error_snippet=err,
        package=pkg,
        classes=classes,
        functions=functions,
        globals=globals_,
        all_exports=exports,
        loc_code=loc_code,
        imports=imports,
        imports_raw=imports_raw,
        imports_static=imports_static,
        imports_wildcard=imports_wildcard,
        classes_fq=classes_fq,
        annotations=annotations,
        decl_annotations=decl_annotations,
    )
