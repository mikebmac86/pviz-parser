from __future__ import annotations

import sys
from collections import Counter
from typing import Any, Dict, List, Mapping, Optional, Set

from .utils import dedupe_preserve_order, normalize_posix


# ---- stdlib detection: prefer runtime truth, fall back to static set ----

_STDLIB_FALLBACK: Set[str] = {
    "typing", "types", "typing_extensions",
    "__future__", "__main__", "builtins",
    "collections", "dataclasses", "enum", "array", "queue",
    "heapq", "bisect", "weakref",
    "functools", "itertools", "operator",
    "pathlib", "os", "sys", "io", "glob", "shutil", "tempfile",
    "fileinput", "stat", "filecmp", "fnmatch",
    "re", "string", "textwrap", "unicodedata", "stringprep",
    "difflib", "readline", "rlcompleter",
    "struct", "codecs", "pickle", "json", "csv", "configparser",
    "netrc", "xdrlib", "plistlib",
    "datetime", "time", "calendar", "zoneinfo",
    "math", "cmath", "random", "statistics", "decimal", "fractions", "numbers",
    "asyncio", "threading", "multiprocessing", "concurrent",
    "subprocess", "_thread", "dummy_threading", "contextvars",
    "socket", "ssl", "http", "urllib", "email", "smtplib",
    "poplib", "imaplib", "nntplib", "ftplib", "telnetlib",
    "socketserver", "xmlrpc",
    "cgi", "cgitb", "wsgiref", "base64", "binascii", "quopri",
    "uu", "html", "xml",
    "logging", "warnings", "traceback", "pdb", "unittest",
    "doctest", "inspect", "dis", "timeit", "profile", "pstats",
    "trace", "faulthandler",
    "argparse", "getopt", "copy", "pprint", "reprlib",
    "contextlib", "abc", "atexit", "gc", "importlib",
    "pkgutil", "modulefinder", "runpy", "site", "sysconfig",
    "platform", "signal", "errno", "ctypes", "mmap", "resource",
    "nis", "syslog", "grp", "pwd", "spwd", "crypt", "termios",
    "tty", "pty", "fcntl", "pipes", "posix", "select",
    "gzip", "bz2", "lzma", "zipfile", "tarfile", "zlib",
    "hashlib", "hmac", "secrets",
    "sqlite3", "dbm",
    "audioop", "wave", "chunk", "colorsys", "imghdr", "sndhdr",
    "tkinter", "turtle",
    "gettext", "locale",
    "ast", "symtable", "token", "keyword", "tokenize", "tabnanny",
    "py_compile", "compileall", "code", "codeop",
    "msilib", "msvcrt", "winreg", "winsound",
    "posixpath", "curses",
    "imp", "formatter", "optparse", "asynchat", "asyncore",
    "smtpd", "distutils",
}

_STDLIB_RUNTIME: Optional[Set[str]] = None
try:
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        _STDLIB_RUNTIME = set(names)
except Exception:
    _STDLIB_RUNTIME = None


def _is_stdlib_import(import_path: str) -> bool:
    root_module = import_path.split(".", 1)[0]
    if not root_module:
        return False
    if _STDLIB_RUNTIME is not None:
        return root_module in _STDLIB_RUNTIME
    return root_module in _STDLIB_FALLBACK


def _detect_repo_packages(files_raw: Mapping[str, Any]) -> Set[str]:
    packages: Set[str] = set()

    for info in files_raw.values():
        if not isinstance(info, Mapping):
            continue
        file_path = info.get("file", "")
        if isinstance(file_path, str) and "/" in file_path:
            top_dir = normalize_posix(file_path).split("/", 1)[0]
            if top_dir:
                packages.add(top_dir)

    for module_id in files_raw.keys():
        if isinstance(module_id, str) and module_id:
            packages.add(module_id.split(".", 1)[0])

    return packages


def _is_likely_internal(import_path: str, repo_packages: Set[str]) -> bool:
    root = import_path.split(".", 1)[0]
    return root in repo_packages


def build_folders(folder_index: Mapping[str, Any]) -> Dict[str, Any]:
    files_raw = folder_index.get("files", {}) or {}

    if isinstance(files_raw, list):
        files_dict: Dict[str, Any] = {}
        for item in files_raw:
            if isinstance(item, Mapping):
                key = item.get("file") or item.get("module_id") or item.get("id")
                if isinstance(key, str) and key:
                    files_dict[key] = item
        files_raw = files_dict

    if not isinstance(files_raw, Mapping):
        return {"files": {}}

    repo_packages = _detect_repo_packages(files_raw)
    out_files: Dict[str, Any] = {}

    for module_id, info in files_raw.items():
        if not isinstance(info, Mapping):
            continue

        file_path = info.get("file")
        if not isinstance(file_path, str) or not file_path:
            continue

        name = info.get("name") or (module_id.rsplit(".", 1)[-1] if isinstance(module_id, str) else "")

        f_entry: Dict[str, Any] = {
            "id": module_id,
            "file": normalize_posix(file_path),
            "name": name,
            "parse_status": info.get("parse_status"),
        }

        for k in (
            "loc",
            "sloc",
            "comment_lines",
            "blank_lines",
            "comment_pct",
            "size_bytes",
            "mtime",
            "hash",
        ):
            if k in info and info.get(k) is not None:
                f_entry[k] = info.get(k)

        imports_internal = dedupe_preserve_order([x for x in (info.get("imports_internal") or []) if isinstance(x, str)])
        imports_all = dedupe_preserve_order([x for x in (info.get("imports_all") or []) if isinstance(x, str)])

        internal_set = set(imports_internal)
        imports_external_raw = [imp for imp in imports_all if imp not in internal_set]

        moved_internal: List[str] = []
        imports_external: List[str] = []
        for imp in imports_external_raw:
            if _is_likely_internal(imp, repo_packages):
                moved_internal.append(imp)
            else:
                imports_external.append(imp)

        if moved_internal:
            imports_internal = dedupe_preserve_order(imports_internal + moved_internal)
            internal_set = set(imports_internal)

        imports_stdlib: List[str] = []
        imports_third_party: List[str] = []
        for imp in imports_external:
            if _is_stdlib_import(imp):
                imports_stdlib.append(imp)
            else:
                imports_third_party.append(imp)

        import_counts: Dict[str, int] = {
            "internal": len(imports_internal),
            "external": len(imports_external),
        }
        if imports_stdlib:
            import_counts["stdlib"] = len(imports_stdlib)
        if imports_third_party:
            import_counts["third_party"] = len(imports_third_party)

        f_entry["import_counts"] = import_counts

        if imports_internal:
            f_entry["imports_internal"] = imports_internal
        if imports_external:
            f_entry["imports_external"] = imports_external
        if imports_stdlib:
            f_entry["imports_stdlib"] = imports_stdlib
        if imports_third_party:
            f_entry["imports_third_party"] = imports_third_party

        style_counts = info.get("import_style_counts")
        if isinstance(style_counts, Mapping):
            cleaned = {k: v for k, v in style_counts.items() if isinstance(k, str) and isinstance(v, int)}
            if cleaned:
                f_entry["import_style_counts"] = cleaned

        if info.get("error_snippet"):
            f_entry["error_snippet"] = info.get("error_snippet")

        out_files[str(module_id)] = f_entry

    return {"files": out_files}


def compute_import_summary(out_files: Dict[str, Any]) -> Dict[str, Any]:
    total_internal = 0
    total_external = 0
    total_stdlib = 0
    total_third_party = 0

    third_party_usage: Counter = Counter()
    package_files: Dict[str, Set[str]] = {}

    for _module_id, f_entry in (out_files or {}).items():
        if not isinstance(f_entry, Mapping):
            continue

        counts = f_entry.get("import_counts", {}) if isinstance(f_entry.get("import_counts"), Mapping) else {}
        total_internal += int(counts.get("internal", 0) or 0)
        total_external += int(counts.get("external", 0) or 0)
        total_stdlib += int(counts.get("stdlib", 0) or 0)
        total_third_party += int(counts.get("third_party", 0) or 0)

        fpath = f_entry.get("file")
        fpath_s = fpath if isinstance(fpath, str) else None

        imports_third_party = (
            f_entry.get("imports_third_party", [])
            if isinstance(f_entry.get("imports_third_party"), list)
            else []
        )

        for imp in imports_third_party:
            if not isinstance(imp, str) or not imp:
                continue

            package = imp.split(".", 1)[0]
            if not package:
                continue

            third_party_usage[package] += 1

            if fpath_s:
                package_files.setdefault(package, set()).add(fpath_s)

    summary: Dict[str, Any] = {
        "totals": {
            "internal": total_internal,
            "external": total_external,
            "stdlib": total_stdlib,
            "third_party": total_third_party,
        }
    }

    if third_party_usage:
        top_packages = []

        for pkg, count in third_party_usage.most_common(20):
            files = sorted(package_files.get(pkg, set()))

            top_packages.append(
                {
                    "package": pkg,
                    "import_count": int(count),
                    "file_count": len(files),
                    "sample_files": files[:5],
                }
            )

        summary["top_third_party_packages"] = top_packages

    return summary

def merge_folder_indexes(
    python_idx: Optional[Mapping[str, Any]] = None,
    ts_idx: Optional[Mapping[str, Any]] = None,
    go_idx: Optional[Mapping[str, Any]] = None,
    java_idx: Optional[Mapping[str, Any]] = None,
    kotlin_idx: Optional[Mapping[str, Any]] = None,
    rust_idx: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Merge folder indexes from multiple language analyzers.
    
    Combines files and meta from Python, TypeScript, Go, Java, and Rust analyzers
    into a unified folder index.
    
    Args:
        python_idx: Python folder index (optional)
        ts_idx: TypeScript folder index (optional)
        go_idx: Go folder index (optional)
        java_idx: Java folder index (optional)
        rust_idx: Rust folder index (optional)
    
    Returns:
        Merged folder index dict, or None if all inputs are None
    """
    # Collect all non-None indexes
    indexes = [
        idx for idx in [
            python_idx, ts_idx, go_idx, java_idx, kotlin_idx, rust_idx  # ADD kotlin_idx
        ]
        if idx is not None
    ]
    
    if not indexes:
        return None
    
    # If only one index, return it directly
    if len(indexes) == 1:
        return dict(indexes[0]) if isinstance(indexes[0], Mapping) else None
    
    # Merge files from all indexes
    merged_files: Dict[str, Any] = {}
    
    for idx in indexes:
        if not isinstance(idx, Mapping):
            continue
        
        files = idx.get("files")
        if isinstance(files, Mapping):
            merged_files.update(files)
    
    # Merge meta from all indexes
    # Strategy: first index provides base, subsequent indexes add missing keys
    merged_meta: Dict[str, Any] = {}
    
    for idx in indexes:
        if not isinstance(idx, Mapping):
            continue
        
        meta = idx.get("meta")
        if isinstance(meta, Mapping):
            for key, val in meta.items():
                if key not in merged_meta:
                    merged_meta[key] = val
 
    # Add aggregated language list to meta
    languages = []
    for idx in indexes:
        if isinstance(idx, Mapping):
            meta = idx.get("meta")
            if isinstance(meta, Mapping):
                lang = meta.get("language") or meta.get("lang")
                if lang and lang not in languages:
                    languages.append(str(lang))
    
    if languages:
        merged_meta["languages"] = sorted(languages)
    
    return {
        "schema_version": "folder_index@v1",
        "files": merged_files,
        "meta": merged_meta,
    }