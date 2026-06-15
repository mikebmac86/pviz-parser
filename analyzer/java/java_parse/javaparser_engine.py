from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import JavaImport, JavaParsedFile
from .regex_engine import _count_loc_code  # reuse LOC counter for now


class JavaParserUnavailable(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
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


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if not isinstance(v, str) or not v.strip():
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _java_bin() -> str:
    return _env("PVIZ_JAVA_BIN", "java")


def _timeout_s() -> int:
    return _env_int("PVIZ_JAVAPARSER_TIMEOUT_S", 30)


def _debug_enabled() -> bool:
    return _env_bool("PVIZ_JAVAPARSER_DEBUG", False)


def _invoke_mode() -> str:
    """
    Supported:
      - auto: try java -jar first; retry classpath mode on manifest/main-class failure
      - jar: only java -jar
      - classpath: only java -cp <jar> <main-class>
    """
    v = _env("PVIZ_JAVAPARSER_INVOKE_MODE", "auto").lower()
    return v if v in {"auto", "jar", "classpath"} else "auto"


def _main_class() -> str:
    return _env("PVIZ_JAVAPARSER_MAIN_CLASS", "com.pviz.javaparsercli.Main")


def _lang() -> str:
    return _env("PVIZ_JAVAPARSER_LANG", "BLEEDING_EDGE")


def _classpath_for_symbol_solving() -> Optional[str]:
    """
    Forwarded to the Java CLI as --classpath.

    This is not the classpath used to run the CLI itself.
    """
    v = _env("PVIZ_JAVAPARSER_CLASSPATH", "")
    return v or None


def _root_for_symbol_solving() -> Optional[Path]:
    """
    Optional repo root forwarded to the Java CLI as --root.

    Keep this explicit. Do not guess from individual file paths here because the
    parser wrapper does not reliably know the scan root.
    """
    v = _env("PVIZ_JAVAPARSER_ROOT", "")
    if not v:
        return None

    p = Path(v).expanduser()
    try:
        p = p.resolve()
    except Exception:
        pass

    if not p.exists() or not p.is_dir():
        raise JavaParserUnavailable(f"PVIZ_JAVAPARSER_ROOT points to missing/non-directory path: {p}")

    return p


def _jar_path() -> Path:
    """
    SaaS/runtime contract:

      PVIZ_JAVAPARSER_JAR must point to the JavaParser CLI fat jar.

    The Dockerfile already copies the jar to a stable path, so do not search
    random fallback locations. Fail loudly if the env var is missing or wrong.
    """
    jar = _env("PVIZ_JAVAPARSER_JAR", "")
    if not jar:
        raise JavaParserUnavailable(
            "PVIZ_JAVAPARSER_JAR is not set; JavaParser CLI jar path must be configured explicitly"
        )

    p = Path(jar).expanduser()
    try:
        p = p.resolve()
    except Exception:
        pass

    if not p.exists() or not p.is_file():
        raise JavaParserUnavailable(f"PVIZ_JAVAPARSER_JAR points to missing file: {p}")

    return p


# ---------------------------------------------------------------------------
# CLI invocation
# ---------------------------------------------------------------------------

def _cli_args(file_path: Path) -> List[str]:
    args = ["--file", str(file_path)]

    root = _root_for_symbol_solving()
    if root is not None:
        args.extend(["--root", str(root)])

    cp = _classpath_for_symbol_solving()
    if cp:
        args.extend(["--classpath", cp])

    lang = _lang()
    if lang:
        args.extend(["--lang", lang])

    return args


def _cmd_jar(java: str, jar: Path, file_path: Path) -> List[str]:
    return [java, "-jar", str(jar), *_cli_args(file_path)]


def _cmd_classpath(java: str, jar: Path, file_path: Path) -> List[str]:
    return [java, "-cp", str(jar), _main_class(), *_cli_args(file_path)]


def _run_cmd(cmd: List[str], timeout_s: int) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except FileNotFoundError as e:
        java = cmd[0] if cmd else "java"
        raise JavaParserUnavailable(f"java binary not found: {java}") from e
    except subprocess.TimeoutExpired as e:
        raise JavaParserUnavailable(f"javaparser cli timed out after {timeout_s}s: {cmd!r}") from e

    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def _looks_like_jar_invocation_problem(stdout: str, stderr: str) -> bool:
    text = f"{stdout}\n{stderr}".lower()
    return any(
        needle in text
        for needle in (
            "no main manifest attribute",
            "could not find or load main class",
            "classnotfoundexception",
            "main method not found",
        )
    )


def _decode_json_output(cmd: List[str], rc: int, out: str, err: str) -> Dict[str, Any]:
    if not out and rc != 0:
        raise JavaParserUnavailable(
            f"javaparser cli failed cmd={cmd!r} rc={rc}: stderr={err[:2000]}"
        )

    if not out:
        raise JavaParserUnavailable(
            f"javaparser cli produced no output cmd={cmd!r} rc={rc}: stderr={err[:2000]}"
        )

    try:
        data = json.loads(out)
    except Exception as e:
        raise JavaParserUnavailable(
            f"javaparser cli returned non-json output cmd={cmd!r} rc={rc}: "
            f"stdout={out[:2000]} stderr={err[:2000]}"
        ) from e

    if not isinstance(data, dict):
        raise JavaParserUnavailable(
            f"javaparser cli returned non-object JSON cmd={cmd!r} rc={rc}: stdout={out[:1000]}"
        )

    return data


def _run_javaparser_cli(file_path: Path) -> Dict[str, Any]:
    jar = _jar_path()
    java = _java_bin()
    timeout_s = _timeout_s()
    mode = _invoke_mode()

    if mode == "jar":
        cmd = _cmd_jar(java, jar, file_path)
        rc, out, err = _run_cmd(cmd, timeout_s)
        return _decode_json_output(cmd, rc, out, err)

    if mode == "classpath":
        cmd = _cmd_classpath(java, jar, file_path)
        rc, out, err = _run_cmd(cmd, timeout_s)
        return _decode_json_output(cmd, rc, out, err)

    # auto mode: try executable jar first, then classpath fallback if the jar
    # lacks a manifest/main-class.
    jar_cmd = _cmd_jar(java, jar, file_path)
    rc, out, err = _run_cmd(jar_cmd, timeout_s)

    if out:
        try:
            return _decode_json_output(jar_cmd, rc, out, err)
        except JavaParserUnavailable:
            if not _looks_like_jar_invocation_problem(out, err):
                raise

    if rc != 0 and (_looks_like_jar_invocation_problem(out, err) or not out):
        cp_cmd = _cmd_classpath(java, jar, file_path)
        rc2, out2, err2 = _run_cmd(cp_cmd, timeout_s)

        try:
            return _decode_json_output(cp_cmd, rc2, out2, err2)
        except JavaParserUnavailable as e:
            raise JavaParserUnavailable(
                "javaparser cli failed in both jar and classpath modes. "
                f"jar_cmd={jar_cmd!r} jar_rc={rc} jar_stdout={out[:1000]} jar_stderr={err[:1000]} "
                f"classpath_cmd={cp_cmd!r} classpath_error={e}"
            ) from e

    return _decode_json_output(jar_cmd, rc, out, err)


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------

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
    return sorted(set(s for s in v if s))


def _compute_classes_fq(package: Optional[str], classes: List[str]) -> Optional[List[str]]:
    if not package or not classes:
        return None

    pkg = package.strip()
    if not pkg:
        return None

    return _dedupe_sorted([f"{pkg}.{c}" for c in classes if c])


def _parse_imports(
    data: Dict[str, Any],
) -> Tuple[
    Optional[List[JavaImport]],
    Optional[List[str]],
    Optional[List[str]],
    Optional[List[str]],
]:
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

                is_static = False
                s2 = s
                if s2.startswith("static "):
                    is_static = True
                    s2 = s2[len("static ") :].strip()

                is_wildcard = s2.endswith(".*")
                target = s2[:-2] if is_wildcard else s2

                if target:
                    parsed.append(
                        JavaImport(
                            target=target,
                            is_wildcard=is_wildcard,
                            is_static=is_static,
                        )
                    )

            elif isinstance(item, dict):
                target = item.get("target") or item.get("name") or item.get("import")
                target_s = target.strip() if isinstance(target, str) else ""

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

                    if target_s.endswith(".*"):
                        is_wild_b = True
                        target_s = target_s[:-2]

                    parsed.append(
                        JavaImport(
                            target=target_s,
                            is_wildcard=is_wild_b,
                            is_static=is_static_b,
                        )
                    )

    if not parsed and not imports_raw:
        return None, None, None, None

    imports_wildcard = _dedupe_sorted([i.target for i in parsed if i.is_wildcard])
    imports_static = _dedupe_sorted([i.target for i in parsed if i.is_static])
    imports = sorted(parsed, key=lambda x: (x.target, x.is_static, x.is_wildcard))

    return imports, _dedupe_sorted(imports_raw), imports_static or None, imports_wildcard or None


def _parse_annotations(
    data: Dict[str, Any],
) -> Tuple[Optional[Dict[str, int]], Optional[Dict[str, List[str]]]]:
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


def _error_snippet(prefix: str, msg: str) -> str:
    limit = 1200 if _debug_enabled() else 200
    return f"{prefix}: {msg}"[:limit]


# ---------------------------------------------------------------------------
# Public parser
# ---------------------------------------------------------------------------

def parse_java_file(path: Path) -> JavaParsedFile:
    """
    JavaParser-backed parse.

    Never raises. Returns parse_status="error" on failures, allowing engine.py
    to decide whether to fall back to regex in auto mode.
    """
    status = "ok"
    err: Optional[str] = None

    pkg: Optional[str] = None
    classes: List[str] = []
    functions: List[str] = []
    globals_: List[str] = []
    exports: List[str] = []
    loc_code: Optional[int] = None

    imports: Optional[List[JavaImport]] = None
    imports_raw: Optional[List[str]] = None
    imports_static: Optional[List[str]] = None
    imports_wildcard: Optional[List[str]] = None
    classes_fq: Optional[List[str]] = None
    annotations: Optional[Dict[str, int]] = None
    decl_annotations: Optional[Dict[str, List[str]]] = None

    try:
        file_path = Path(path)

        try:
            raw = file_path.read_text("utf-8")
        except UnicodeDecodeError:
            raw = file_path.read_text("latin-1", errors="replace")

        loc_code = _count_loc_code(raw)

        data = _run_javaparser_cli(file_path)

        ok = bool(data.get("ok", True))
        if not ok:
            status = "error"
            err = str(data.get("error") or "javaparser cli reported ok=false")[
                : 1200 if _debug_enabled() else 200
            ]

        cli_parse_status = data.get("parse_status")
        if status == "ok" and isinstance(cli_parse_status, str):
            ps = cli_parse_status.strip().lower()
            if ps in {"ok", "warn", "error", "partial"}:
                status = ps

        pkg = data.get("package") if isinstance(data.get("package"), str) else None

        classes = _to_str_list(data.get("declared_types")) or _to_str_list(data.get("types"))
        functions = _to_str_list(data.get("methods"))

        globals_.extend(_to_str_list(data.get("fields")))
        globals_.extend(_to_str_list(data.get("enum_constants")))
        globals_.extend(_to_str_list(data.get("record_components")))

        ex = data.get("exports")
        exports = _to_str_list(ex) if isinstance(ex, list) else []

        if not exports:
            exports = list(classes)

        imports, imports_raw, imports_static, imports_wildcard = _parse_imports(data)
        annotations, decl_annotations = _parse_annotations(data)

        classes = _dedupe_sorted(classes)
        functions = _dedupe_sorted(functions)
        globals_ = _dedupe_sorted(globals_)
        exports = _dedupe_sorted(exports)

        classes_fq_from_jar = _to_str_list(data.get("declared_types_fq"))
        if classes_fq_from_jar:
            classes_fq = _dedupe_sorted(classes_fq_from_jar)
        else:
            classes_fq = _compute_classes_fq(pkg, classes)

    except JavaParserUnavailable as e:
        status = "error"
        err = _error_snippet("JavaParserUnavailable", str(e))

    except Exception as e:
        status = "error"
        err = _error_snippet(type(e).__name__, str(e))

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