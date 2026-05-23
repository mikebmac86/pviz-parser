#analyzer/discovery_manifest.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import os
import time
import re
from collections import Counter
try:
    from diagnostics.logging import log_event
except Exception:  # pragma: no cover
    def log_event(*_a: Any, **_k: Any) -> None:
        return

try:
    import orjson  # type: ignore
except Exception:  # pragma: no cover
    orjson = None  # type: ignore
import json


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

DISCOVERY_MANIFEST_SCHEMA = "discovery_manifest@v1"


@dataclass(frozen=True)
class ManifestSummary:
    total_files: int



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_posix(p: Path) -> str:
    try:
        return p.as_posix()
    except Exception:
        return str(p).replace("\\", "/")


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if orjson is not None:
        data = orjson.dumps(obj, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
        path.write_bytes(data + (b"\n" if not data.endswith(b"\n") else b""))
        return
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Optional[dict]:
    try:
        if not path.is_file():
            return None
        raw = path.read_bytes()
        if orjson is not None:
            return orjson.loads(raw)
        return json.loads(raw.decode("utf-8", errors="ignore"))
    except Exception:
        return None


def _default_skip_dirs() -> Set[str]:
    # Conservative + common vendor/build dirs across ecosystems
    return {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "node_modules",
        "out",
        ".next",
        ".turbo",
        ".parcel-cache",
        "target",  # rust/java
        ".pviz_store",  # never recurse into our sandbox if scan_root accidentally includes it
    }


def _classify_lang(ext: str) -> str:
    e = (ext or "").lower()
    # Keep this intentionally small and stable; can expand later.
    if e in (".py", ".pyi"):
        return "python"
    if e in (".ts", ".tsx"):
        return "ts"
    if e in (".js", ".jsx", ".mjs", ".cjs"):
        return "js"
    if e in (".json",):
        return "json"
    if e in (".yaml", ".yml"):
        return "yaml"
    if e in (".md",):
        return "markdown"
    if e in (".html", ".htm"):
        return "html"
    if e in (".css", ".scss", ".sass", ".less"):
        return "css"
    if e in (".go",):
        return "go"
    if e in (".rs",):
        return "rust"
    if e in (".java",):
        return "java"
    if e in (".kt", ".kts"):
        return "kotlin"
    if e in (".cs",):
        return "csharp"
    if e in (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh"):
        return "cpp"
    if e in (".rb",):
        return "ruby"
    if e in (".php",):
        return "php"
    if e in (".swift",):
        return "swift"
    if e in (".sql",):
        return "sql"
    if e in (".toml",):
        return "toml"
    return "other"

# ---------------------------------------------------------------------------
# Seed discovery (Python) - "start big"
# ---------------------------------------------------------------------------

_RE_MAIN_GUARD = re.compile(r'if\s+__name__\s*==\s*[\'"]__main__[\'"]\s*:', re.M)
_RE_CALL_MAIN = re.compile(r'^\s*main\(\)\s*$', re.M)

# common command patterns (we resolve module:attr later using src_roots)
_RE_UVICORN = re.compile(r'\buvicorn\s+([A-Za-z0-9_\.]+):([A-Za-z0-9_]+)', re.I)
_RE_GUNICORN = re.compile(r'\bgunicorn\s+([A-Za-z0-9_\.]+):([A-Za-z0-9_]+)', re.I)
_RE_HYPERCORN = re.compile(r'\bhypercorn\s+([A-Za-z0-9_\.]+):([A-Za-z0-9_]+)', re.I)
_RE_DAPHNE = re.compile(r'\bdaphne\s+([A-Za-z0-9_\.]+):([A-Za-z0-9_]+)', re.I)
_RE_PYTHON_M = re.compile(r'\bpython(?:3)?\s+-m\s+([A-Za-z0-9_\.]+)', re.I)
_RE_PYTHON_FILE = re.compile(r'\bpython(?:3)?\s+([A-Za-z0-9_/\.\-]+\.py)\b', re.I)
_RE_CELERY_A = re.compile(r'\bcelery\s+-A\s+([A-Za-z0-9_\.]+)\b', re.I)
_RE_ALEMBIC = re.compile(r'\balembic\s+(upgrade|revision|downgrade)\b', re.I)

# crude but effective for code heuristics
_RE_FASTAPI = re.compile(r'\bFastAPI\s*\(', re.M)
_RE_FLASK = re.compile(r'\bFlask\s*\(', re.M)
_RE_DJANGO_MANAGE = re.compile(r'\bexecute_from_command_line\s*\(', re.M)
_RE_DJANGO_WSGI = re.compile(r'\bget_wsgi_application\s*\(', re.M)
_RE_DJANGO_ASGI = re.compile(r'\bget_asgi_application\s*\(', re.M)
_RE_CELERY_CTOR = re.compile(r'\bCelery\s*\(', re.M)
_RE_RQ = re.compile(r'\brq\s+worker\b', re.I)
_RE_DRAMATIQ = re.compile(r'\bdramatiq\b', re.I)
_RE_ARQ = re.compile(r'\barq\b', re.I)
_RE_ALEMBIC_INI = re.compile(r'^\s*\[alembic\]\s*$', re.M)

_RE_DYN_IMPORT = re.compile(
    r'\b(importlib\.import_module|__import__|pkgutil\.iter_modules|'
    r'importlib\.metadata\.entry_points|pkg_resources\.iter_entry_points)\b'
)

def _safe_read_text(p: Path, *, max_bytes: int = 512_000) -> str:
    try:
        if not p.is_file():
            return ""
        data = p.read_bytes()
        if len(data) > max_bytes:
            data = data[:max_bytes]
        return data.decode("utf-8", errors="ignore")
    except Exception:
        return ""

def _is_python_file(path_posix: str) -> bool:
    lp = path_posix.lower()
    return lp.endswith(".py") or lp.endswith(".pyi")

def _posix_dirname(path_posix: str) -> str:
    # expects posix
    if "/" not in path_posix:
        return ""
    return path_posix.rsplit("/", 1)[0]

def _top_segment(path_posix: str) -> str:
    if not path_posix:
        return ""
    return path_posix.split("/", 1)[0]

def _detect_python_src_roots(sr: Path, files_rows: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """
    Big heuristic: choose likely python import roots.
    Returns (src_roots, evidence_lines)
    """
    evidence: List[str] = []
    py_paths = [r["path"] for r in files_rows if r.get("lang") == "python"]
    if not py_paths:
        return [], ["no_python_files"]

    # Count python files per top-level directory
    top_counts = Counter(_top_segment(p) for p in py_paths if "/" in p)
    root_candidates: List[Tuple[str, int]] = []
    for top, n in top_counts.items():
        if top and n >= 10:
            root_candidates.append((top, n))
    root_candidates.sort(key=lambda x: -x[1])

    # Strong signals: src/ layouts and backend/ server/ app/
    preferred = ["src", "backend", "server", "app", "services", "python"]
    src_roots: List[str] = []
    for name in preferred:
        if any(top == name for top, _n in root_candidates):
            src_roots.append(name)
            evidence.append(f"src_root:heuristic_preferred:{name}")

    # If nothing preferred, pick largest python-containing top-level dirs
    if not src_roots:
        for top, _n in root_candidates[:3]:
            src_roots.append(top)
            evidence.append(f"src_root:heuristic_topcount:{top}")

    # Add repo-root as last-resort if we have many top-level-less python files
    root_level = sum(1 for p in py_paths if "/" not in p)
    if root_level >= 10:
        src_roots.append("")  # empty means repo root
        evidence.append("src_root:repo_root_due_to_many_root_level_py")

    # Config-based bumps (pyproject hints)
    pyproject = sr / "pyproject.toml"
    if pyproject.is_file():
        txt = _safe_read_text(pyproject)
        # very light parsing: just record presence of script tables and "src" mention
        if "[project.scripts]" in txt or "[tool.poetry.scripts]" in txt:
            evidence.append("src_root:pyproject_has_scripts")
        if re.search(r'package[-_ ]dir\s*=', txt, re.I) or "src/" in txt:
            # often indicates src layout
            if "src" not in src_roots and (sr / "src").is_dir():
                src_roots.insert(0, "src")
                evidence.append("src_root:pyproject_mentions_src")

    # de-dupe while preserving order
    seen: set[str] = set()
    out: List[str] = []
    for r in src_roots:
        if r not in seen:
            seen.add(r)
            out.append(r)

    return out, evidence

def _resolve_module_to_file(module: str, *, src_roots: List[str], files_set: set[str]) -> Optional[str]:
    """
    Resolve 'pkg.mod' -> '<src_root>/pkg/mod.py' or '<src_root>/pkg/mod/__init__.py'
    Returns repo-relative posix path if found.
    """
    rel = module.replace(".", "/")
    candidates: List[str] = []
    for root in src_roots or [""]:
        prefix = f"{root}/" if root else ""
        candidates.append(f"{prefix}{rel}.py")
        candidates.append(f"{prefix}{rel}/__init__.py")
    for cand in candidates:
        if cand in files_set:
            return cand
    return None

def _seed(kind: str, file_id: str, *, symbol: Optional[str] = None, confidence: float = 0.5,
          priority: int = 50, evidence: Optional[List[str]] = None, source: str = "") -> Dict[str, Any]:
    sid = f"{file_id}:{symbol}" if symbol else file_id
    return {
        "id": sid,
        "file": file_id,
        "symbol": symbol,
        "kind": kind,
        "confidence": float(confidence),
        "priority": int(priority),
        "evidence": list(evidence or []),
        "source": source or kind,
    }

def _rank_and_dedupe_seeds(seeds: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Deduplicate by id, keep highest-confidence, and merge evidence.
    best: Dict[str, Dict[str, Any]] = {}
    for s in seeds:
        sid = str(s.get("id") or "")
        if not sid:
            continue
        if sid not in best:
            best[sid] = s
            continue
        cur = best[sid]
        if float(s.get("confidence", 0.0)) > float(cur.get("confidence", 0.0)):
            # keep stronger but merge evidence
            ev = set(cur.get("evidence") or [])
            ev.update(s.get("evidence") or [])
            s["evidence"] = sorted(ev)
            best[sid] = s
        else:
            ev = set(cur.get("evidence") or [])
            ev.update(s.get("evidence") or [])
            cur["evidence"] = sorted(ev)

    out = list(best.values())
    out.sort(key=lambda s: (int(s.get("priority", 50)), -float(s.get("confidence", 0.0)), str(s.get("id") or "")))
    return out

def _discover_python_seeds(sr: Path, files_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Big discovery pass that emits python src_roots + seed candidates.
    Uses only files already discovered by os.walk.
    """
    files_set = {r["path"] for r in files_rows if isinstance(r.get("path"), str)}
    src_roots, src_evidence = _detect_python_src_roots(sr, files_rows)

    seeds: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    # Identify config-ish files we should scan for commands and entrypoints
    config_scan_globs = (
        "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
        "compose.yml", "compose.yaml", "Procfile", "Makefile",
        "pyproject.toml", "setup.cfg", "setup.py",
        "README.md", "README.rst", "README.txt",
    )

    # Add workflows + scripts + systemd
    config_paths: List[str] = []
    for p in files_set:
        base = p.rsplit("/", 1)[-1]
        if base in config_scan_globs:
            config_paths.append(p)
        if p.startswith(".github/workflows/") and (p.endswith(".yml") or p.endswith(".yaml")):
            config_paths.append(p)
        if p.endswith(".service"):
            config_paths.append(p)
        if p.startswith("scripts/") and (p.endswith(".sh") or p.endswith(".bash")):
            config_paths.append(p)
        if p.startswith("bin/") and not p.endswith("/"):
            # often shell scripts without extension
            config_paths.append(p)

    config_paths = sorted(set(config_paths))

    # --- Detector: __main__.py files (package runner) ---
    for p in files_set:
        if p.endswith("/__main__.py") or p == "__main__.py":
            seeds.append(_seed(
                "package_main", p, confidence=0.97, priority=5,
                evidence=["file_exists:__main__.py"], source="python_package_main"
            ))

    # --- Detector: main guard ---
    py_files = [r["path"] for r in files_rows if r.get("lang") == "python" and isinstance(r.get("path"), str)]
    for rel_posix in py_files:
        txt = _safe_read_text(sr / rel_posix)
        if not txt:
            continue
        if _RE_MAIN_GUARD.search(txt):
            ev = ["has_if_dunder_main"]
            conf = 0.85
            pr = 10
            seeds.append(_seed("script_main_guard", rel_posix, confidence=conf, priority=pr, evidence=ev, source="python_main_guard"))
            if _RE_CALL_MAIN.search(txt):
                seeds.append(_seed("script_main_guard_call_main", rel_posix, symbol="main", confidence=0.92, priority=9,
                                   evidence=ev + ["calls_main()"], source="python_main_guard"))

        # Dynamic import warning (not a seed)
        if _RE_DYN_IMPORT.search(txt):
            warnings.append({
                "kind": "dynamic_imports_present",
                "file": rel_posix,
                "evidence": ["dynamic_import_callsite_detected"],
            })

    # --- Detector: pyproject/setup scripts (console_scripts) ---
    # pyproject PEP621: [project.scripts], Poetry: [tool.poetry.scripts]
    pyproject = sr / "pyproject.toml"
    if pyproject.is_file():
        txt = _safe_read_text(pyproject)
        # very lightweight table extraction: scan lines inside the section until next [..]
        def _extract_table(section: str) -> Dict[str, str]:
            out: Dict[str, str] = {}
            in_sec = False
            for line in txt.splitlines():
                line_stripped = line.strip()
                if not line_stripped or line_stripped.startswith("#"):
                    continue
                if line_stripped.startswith("[") and line_stripped.endswith("]"):
                    in_sec = (line_stripped == section)
                    continue
                if not in_sec:
                    continue
                # key = "module:func"
                m = re.match(r'([A-Za-z0-9_\-]+)\s*=\s*["\']([^"\']+)["\']', line_stripped)
                if m:
                    out[m.group(1)] = m.group(2)
            return out

        for section, prio in (("[project.scripts]", 1), ("[tool.poetry.scripts]", 2)):
            tbl = _extract_table(section)
            for name, target in tbl.items():
                if ":" not in target:
                    continue
                mod, func = target.split(":", 1)
                mod = mod.strip()
                func = func.strip()
                f = _resolve_module_to_file(mod, src_roots=src_roots, files_set=files_set)
                if f:
                    seeds.append(_seed(
                        "console_script", f, symbol=func,
                        confidence=0.99, priority=3,
                        evidence=[f"pyproject:{section}:{name}={target}", f"resolved:{mod}->{f}"],
                        source="pyproject_scripts"
                    ))
                else:
                    warnings.append({
                        "kind": "console_script_unresolved",
                        "file": "pyproject.toml",
                        "evidence": [f"{name}={target}", f"src_roots={src_roots}"],
                    })

    # setup.cfg console_scripts (very light)
    setup_cfg = sr / "setup.cfg"
    if setup_cfg.is_file():
        txt = _safe_read_text(setup_cfg)
        # look for console_scripts section
        if re.search(r'^\s*\[options\.entry_points\]\s*$', txt, re.M) and "console_scripts" in txt:
            # naive parse: find lines like "name = mod:func"
            for line in txt.splitlines():
                m = re.match(r'\s*([A-Za-z0-9_\-]+)\s*=\s*([A-Za-z0-9_\.]+):([A-Za-z0-9_]+)\s*$', line)
                if not m:
                    continue
                name, mod, func = m.group(1), m.group(2), m.group(3)
                f = _resolve_module_to_file(mod, src_roots=src_roots, files_set=files_set)
                if f:
                    seeds.append(_seed(
                        "console_script", f, symbol=func,
                        confidence=0.98, priority=4,
                        evidence=[f"setup.cfg:console_scripts:{name}={mod}:{func}", f"resolved:{mod}->{f}"],
                        source="setup_cfg_scripts"
                    ))

    # --- Detector: commands in config files (ASGI/WSGI, python -m, celery, alembic) ---
    for rel in config_paths:
        txt = _safe_read_text(sr / rel)
        if not txt:
            continue

        # uvicorn/hypercorn/daphne
        for rx, kind, conf, pr in (
            (_RE_UVICORN, "asgi_cmd", 0.98, 1),
            (_RE_HYPERCORN, "asgi_cmd", 0.96, 2),
            (_RE_DAPHNE, "asgi_cmd", 0.95, 2),
            (_RE_GUNICORN, "wsgi_cmd", 0.94, 3),
        ):
            for m in rx.finditer(txt):
                mod, attr = m.group(1), m.group(2)
                f = _resolve_module_to_file(mod, src_roots=src_roots, files_set=files_set)
                if f:
                    seeds.append(_seed(
                        kind, f, symbol=attr, confidence=conf, priority=pr,
                        evidence=[f"cmd_in:{rel}", f"{m.group(0).strip()}", f"resolved:{mod}->{f}"],
                        source="config_cmd_scan"
                    ))
                else:
                    warnings.append({"kind": "cmd_module_unresolved", "file": rel, "evidence": [m.group(0).strip(), f"src_roots={src_roots}"]})

        # python -m module
        for m in _RE_PYTHON_M.finditer(txt):
            mod = m.group(1)
            f = _resolve_module_to_file(mod, src_roots=src_roots, files_set=files_set)
            if f:
                seeds.append(_seed(
                    "python_dash_m", f, confidence=0.92, priority=6,
                    evidence=[f"cmd_in:{rel}", m.group(0).strip(), f"resolved:{mod}->{f}"],
                    source="config_cmd_scan"
                ))

        # python file.py
        for m in _RE_PYTHON_FILE.finditer(txt):
            relpath = m.group(1).replace("\\", "/")
            # only accept if it exists in discovered files
            if relpath in files_set:
                seeds.append(_seed(
                    "python_file_exec", relpath, confidence=0.88, priority=7,
                    evidence=[f"cmd_in:{rel}", m.group(0).strip()],
                    source="config_cmd_scan"
                ))

        # celery -A module
        for m in _RE_CELERY_A.finditer(txt):
            mod = m.group(1)
            f = _resolve_module_to_file(mod, src_roots=src_roots, files_set=files_set)
            if f:
                seeds.append(_seed(
                    "celery_app_cmd", f, confidence=0.94, priority=5,
                    evidence=[f"cmd_in:{rel}", m.group(0).strip(), f"resolved:{mod}->{f}"],
                    source="config_cmd_scan"
                ))

        # alembic commands
        if _RE_ALEMBIC.search(txt):
            # common: migrations/env.py
            for cand in ("migrations/env.py", "alembic/env.py"):
                if cand in files_set:
                    seeds.append(_seed("alembic_env", cand, confidence=0.95, priority=8,
                                       evidence=[f"cmd_in:{rel}", "found_alembic_command"], source="config_cmd_scan"))
                    break

    # --- Detector: framework object heuristics ---
    for rel_posix in py_files:
        txt = _safe_read_text(sr / rel_posix)
        if not txt:
            continue

        # FastAPI / Flask app objects
        if _RE_FASTAPI.search(txt):
            seeds.append(_seed("asgi_app_heuristic", rel_posix, symbol="app", confidence=0.82, priority=12,
                               evidence=["found:FastAPI("], source="python_framework_heuristics"))
        if _RE_FLASK.search(txt):
            seeds.append(_seed("wsgi_app_heuristic", rel_posix, symbol="app", confidence=0.80, priority=12,
                               evidence=["found:Flask("], source="python_framework_heuristics"))

        # Django files
        if rel_posix.endswith("manage.py") and _RE_DJANGO_MANAGE.search(txt):
            seeds.append(_seed("django_manage", rel_posix, confidence=0.92, priority=6,
                               evidence=["manage.py:execute_from_command_line"], source="python_framework_heuristics"))
        if rel_posix.endswith("wsgi.py") and _RE_DJANGO_WSGI.search(txt):
            seeds.append(_seed("django_wsgi", rel_posix, symbol="application", confidence=0.90, priority=6,
                               evidence=["wsgi:get_wsgi_application"], source="python_framework_heuristics"))
        if rel_posix.endswith("asgi.py") and _RE_DJANGO_ASGI.search(txt):
            seeds.append(_seed("django_asgi", rel_posix, symbol="application", confidence=0.90, priority=6,
                               evidence=["asgi:get_asgi_application"], source="python_framework_heuristics"))

        # Celery constructor
        if _RE_CELERY_CTOR.search(txt):
            seeds.append(_seed("celery_app_heuristic", rel_posix, confidence=0.78, priority=15,
                               evidence=["found:Celery("], source="python_worker_heuristics"))

        # Alembic ini presence (config file)
        if rel_posix.endswith("alembic.ini"):
            seeds.append(_seed("alembic_ini", rel_posix, confidence=0.90, priority=9,
                               evidence=["file_exists:alembic.ini"], source="python_migrations"))

        # tests
        lp = rel_posix.lower()
        if lp.startswith("tests/") and (lp.endswith(".py") and ("/test_" in lp or lp.endswith("_test.py") or lp.split("/")[-1].startswith("test_"))):
            seeds.append(_seed("pytest_test_file", rel_posix, confidence=0.70, priority=30,
                               evidence=["tests_convention"], source="python_tests"))
        if "unittest.main" in txt:
            seeds.append(_seed("unittest_main", rel_posix, confidence=0.80, priority=25,
                               evidence=["unittest.main()"], source="python_tests"))

    # Dedupe + rank
    seeds = _rank_and_dedupe_seeds(seeds)

    return {
        "src_roots": src_roots,
        "src_root_evidence": src_evidence,
        "seeds": seeds,
        "warnings": warnings,
    }

_RE_GO_MAIN_PKG = re.compile(r'^\s*package\s+main\s*$', re.M)
_RE_GO_MAIN_FUNC = re.compile(r'^\s*func\s+main\s*\(\s*\)\s*\{', re.M)

def _discover_go_seeds(sr: Path, files_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    go_files = [r["path"] for r in files_rows if r.get("lang") == "go" and isinstance(r.get("path"), str)]
    seeds: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    for rel_posix in go_files:
        if not rel_posix.endswith(".go"):
            continue
        txt = _safe_read_text(sr / rel_posix)
        if not txt:
            continue

        if _RE_GO_MAIN_PKG.search(txt) and _RE_GO_MAIN_FUNC.search(txt):
            # strong executable signal
            seeds.append(_seed(
                "go_main", rel_posix, symbol="main",
                confidence=0.98, priority=4,
                evidence=["package main", "func main()"], source="go_main_detect"
            ))

    seeds = _rank_and_dedupe_seeds(seeds)

    return {"seeds": seeds, "warnings": warnings}

# --- Java discovery ---------------------------------------------------------

_RE_JAVA_MAIN_METHOD = re.compile(
    r'\bpublic\s+static\s+void\s+main\s*\(\s*String\s*(?:\[\s*\]|\.\.\.)\s+\w+\s*\)',
    re.M
)

# Optional: light "this is a class-like file" bump (not required, but helps avoid weird matches)
_RE_JAVA_TYPE_DECL = re.compile(r'\b(class|interface|enum|record)\s+[A-Za-z_]\w*\b', re.M)


def _discover_java_seeds(sr: Path, files_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    java_files = [r["path"] for r in files_rows if r.get("lang") == "java" and isinstance(r.get("path"), str)]
    seeds: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    for rel_posix in java_files:
        if not rel_posix.endswith(".java"):
            continue
        txt = _safe_read_text(sr / rel_posix)
        if not txt:
            continue

        # Strong executable signal: public static void main(String[] args) / String... args
        if _RE_JAVA_MAIN_METHOD.search(txt):
            ev = ["public static void main(String[]|String...)", "java_entrypoint"]
            conf = 0.97
            pr = 6

            # If it also looks like it declares a type, bump confidence a hair
            if _RE_JAVA_TYPE_DECL.search(txt):
                conf = 0.98
                ev.append("declares_type")

            seeds.append(_seed(
                "java_main", rel_posix, symbol="main",
                confidence=conf, priority=pr,
                evidence=ev, source="java_main_detect"
            ))

    seeds = _rank_and_dedupe_seeds(seeds)
    return {"seeds": seeds, "warnings": warnings}

# --- Kotlin discovery -------------------------------------------------------

_RE_KOTLIN_MAIN = re.compile(r'\bfun\s+main\s*\(', re.M)

def _discover_kotlin_seeds(sr: Path, files_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Discover Kotlin entrypoints.

    Detects:
    - fun main() entrypoints
    - .kts scripts
    """
    kotlin_files = [
        r["path"] for r in files_rows
        if r.get("lang") == "kotlin" and isinstance(r.get("path"), str)
    ]

    seeds: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    for rel_posix in kotlin_files:
        if not (rel_posix.endswith(".kt") or rel_posix.endswith(".kts")):
            continue

        txt = _safe_read_text(sr / rel_posix)
        if not txt:
            continue

        # Standard Kotlin main()
        if _RE_KOTLIN_MAIN.search(txt):
            seeds.append(_seed(
                "kotlin_main",
                rel_posix,
                symbol="main",
                confidence=0.96,
                priority=6,
                evidence=["fun main()"],
                source="kotlin_main_detect"
            ))

        # Script files (.kts) — treat as implicit entrypoints
        if rel_posix.endswith(".kts"):
            seeds.append(_seed(
                "kotlin_script",
                rel_posix,
                confidence=0.90,
                priority=10,
                evidence=[".kts script"],
                source="kotlin_script_detect"
            ))

    seeds = _rank_and_dedupe_seeds(seeds)

    return {
        "seeds": seeds,
        "warnings": warnings,
    }

# --- Rust discovery ---------------------------------------------------------

_RE_RUST_FN_MAIN = re.compile(r'^\s*fn\s+main\s*\(\s*\)', re.M)
_RE_RUST_ASYNC_FN_MAIN = re.compile(r'^\s*async\s+fn\s+main\s*\(\s*\)', re.M)
_RE_RUST_TOKIO_MAIN = re.compile(r'#\s*\[\s*tokio\s*::\s*main\s*\]', re.M)
_RE_RUST_ACTIX_MAIN = re.compile(r'#\s*\[\s*actix_web\s*::\s*main\s*\]', re.M)


def _discover_rust_seeds(sr: Path, files_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Discover Rust main entrypoints.
    
    Detects:
    - fn main() - standard main function
    - async fn main() - async main (with tokio/actix attributes)
    - #[tokio::main] / #[actix_web::main] - async runtime attributes
    """
    rust_files = [r["path"] for r in files_rows if r.get("lang") == "rust" and isinstance(r.get("path"), str)]
    seeds: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    for rel_posix in rust_files:
        if not rel_posix.endswith(".rs"):
            continue
        txt = _safe_read_text(sr / rel_posix)
        if not txt:
            continue

        ev = []
        conf = 0.0
        pr = 10

        # Check for fn main() or async fn main()
        has_fn_main = _RE_RUST_FN_MAIN.search(txt)
        has_async_main = _RE_RUST_ASYNC_FN_MAIN.search(txt)
        has_tokio_attr = _RE_RUST_TOKIO_MAIN.search(txt)
        has_actix_attr = _RE_RUST_ACTIX_MAIN.search(txt)

        if has_fn_main or has_async_main:
            ev.append("fn main()")
            conf = 0.96
            pr = 5

            # Boost confidence for async main with runtime attribute
            if has_async_main and (has_tokio_attr or has_actix_attr):
                conf = 0.98
                pr = 4
                if has_tokio_attr:
                    ev.append("#[tokio::main]")
                if has_actix_attr:
                    ev.append("#[actix_web::main]")
            elif has_async_main:
                ev.append("async fn main()")
                conf = 0.97

            # Additional confidence boost for main.rs or src/main.rs
            if rel_posix.endswith("/main.rs") or rel_posix == "main.rs" or rel_posix == "src/main.rs":
                conf = min(0.99, conf + 0.02)
                ev.append("file:main.rs")

            # Boost for bin/ directory (cargo bin targets)
            if rel_posix.startswith("src/bin/"):
                conf = min(0.99, conf + 0.01)
                ev.append("location:src/bin/")

            seeds.append(_seed(
                "rust_main", rel_posix, symbol="main",
                confidence=conf, priority=pr,
                evidence=ev, source="rust_main_detect"
            ))

    seeds = _rank_and_dedupe_seeds(seeds)
    return {"seeds": seeds, "warnings": warnings}


def build_discovery_manifest(*, scan_root: Path) -> Tuple[dict, ManifestSummary]:
    """
    Walk scan_root once and emit a language-agnostic manifest.

    Output is repo-relative (POSIX) paths.
    """
    sr = scan_root.resolve()
    skip_dirs = _default_skip_dirs()

    files: List[Dict[str, Any]] = []
    by_lang: Dict[str, int] = {}

    # Walk (topdown) so we can prune directories cheaply
    for dirpath, dirnames, filenames in os.walk(sr):
        # prune skip dirs
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]

        dpath = Path(dirpath)
        for fn in filenames:
            p = dpath / fn
            # skip weird things
            try:
                if not p.is_file():
                    continue
            except Exception:
                continue

            try:
                rel = p.resolve().relative_to(sr)
            except Exception:
                # if it escapes, skip
                try:
                    if not _is_within(p, sr):
                        continue
                except Exception:
                    continue
                rel = p

            rel_posix = _to_posix(rel)
            ext = p.suffix.lower()
            lang = _classify_lang(ext)

            base = p.name.lower()

            # filename-based language hints (no extension)
            if base == "go.mod":
                lang = "go"
                ext = ".mod"
            elif base == "go.sum":
                lang = "go"
                ext = ".sum"

            by_lang[lang] = by_lang.get(lang, 0) + 1
            files.append(
                {
                    "path": rel_posix,
                    "ext": ext,
                    "lang": lang,
                }
            )

    files.sort(key=lambda r: str(r.get("path") or ""))

    manifest = {
        "schema_version": DISCOVERY_MANIFEST_SCHEMA,
        "meta": {
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "scan_root": _to_posix(sr),
            "total_files": len(files),
        },
        "files": files,
    }

    # Big Python seed set discovery (additive)
    try:
        py = _discover_python_seeds(sr, files)
        manifest["python"] = py
    except Exception as e:
        manifest["python"] = {
            "src_roots": [],
            "seeds": [],
            "warnings": [{"kind": "python_seed_discovery_failed", "evidence": [repr(e)]}],
        }

    try:
        manifest["go"] = _discover_go_seeds(sr, files)
    except Exception as e:
        manifest["go"] = {
            "seeds": [],
            "warnings": [{"kind": "go_seed_discovery_failed", "evidence": [repr(e)]}],
        }

    try:
        manifest["java"] = _discover_java_seeds(sr, files)
    except Exception as e:
        manifest["java"] = {
            "seeds": [],
            "warnings": [{"kind": "java_seed_discovery_failed", "evidence": [repr(e)]}],
        }

    try:
        manifest["rust"] = _discover_rust_seeds(sr, files)
    except Exception as e:
        manifest["rust"] = {
            "seeds": [],
            "warnings": [{"kind": "rust_seed_discovery_failed", "evidence": [repr(e)]}],
        }

    try:
        manifest["kotlin"] = _discover_kotlin_seeds(sr, files)
    except Exception as e:
        manifest["kotlin"] = {
            "seeds": [],
            "warnings": [{"kind": "kotlin_seed_discovery_failed", "evidence": [repr(e)]}],
        }

    return manifest, ManifestSummary(total_files=len(files))

def ensure_discovery_manifest(
    *,
    scan_root: Path,
    artifacts_dir: Path,
    force: bool = False,
) -> Tuple[Path, Optional[ManifestSummary]]:
    """
    Ensure discovery_manifest@v1 exists under artifacts_dir.

    Returns: (path, summary_or_none)
    """
    artifacts_dir = artifacts_dir.resolve()
    out_path = artifacts_dir / "discovery_manifest@v1.json"

    if not force:
        existing = _read_json(out_path)
        if isinstance(existing, dict) and existing.get("schema_version") == DISCOVERY_MANIFEST_SCHEMA:
            meta = existing.get("meta") if isinstance(existing.get("meta"), dict) else {}
            total = meta.get("total_files")
            if isinstance(total, int):
                return out_path, ManifestSummary(total_files=total)

            return out_path, None

    t0 = time.perf_counter()
    manifest, summary = build_discovery_manifest(scan_root=scan_root)
    _write_json(out_path, manifest)
    dt_ms = (time.perf_counter() - t0) * 1000.0

    log_event(
        "DISCOVERY_MANIFEST:written",
        path=str(out_path),
        total_files=summary.total_files,
        elapsed_ms=dt_ms,
    )

    return out_path, summary
