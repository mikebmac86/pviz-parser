# saas_analyzer/analyzer/java/folder_index.py
from __future__ import annotations

"""
FolderIndex — deterministic snapshot of a workspace's Java files.

ENHANCED:
  - Parallel parsing with ProcessPoolExecutor
  - Caches JavaParsedFile objects for reuse in build_artifacts.py
  - Uses parser-provided imports instead of regex
  - Adds import classification:
      internal
      internal_unresolved
      stdlib
      third_party
  - Adds aggregate LOC/SLOC/import diagnostics to meta
  - Keeps canonical file-id keys aligned with Python/Go/Kotlin/Rust
"""

from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple
from concurrent.futures import ProcessPoolExecutor, as_completed
from collections import Counter
import time
import os

from analyzer_store.types import FOLDER_INDEX_SCHEMA, FileEntry, FolderIndex
from analyzer_store.io_utils import iso_utc, blake2s_short, atomic_write_json, load_json_bytes
from adapters.canonical import repo_rel, to_posix

from analyzer.java.java_canonical import (
    normalize_java_import_spec,
    resolve_java_import,
    derive_package_from_path,
    build_fq_typename,
)

from analyzer.java.java_parse import parse_java_file
from analyzer.java.java_parse import JavaParsedFile

try:
    from diagnostics.logging import log_event
except Exception:  # pragma: no cover
    def log_event(*_a, **_k):
        return


# ---------------------------------------------------------------------------
# Diagnostic-only prints
# ---------------------------------------------------------------------------

def _diag_print(name: str, **k) -> None:
    parts = " ".join(f"{kk}={k[kk]!r}" for kk in sorted(k.keys()))
    print(f"[JAVA_DIAG] {name} {parts}".rstrip())


def _diag_sample_map(m: Mapping, *, max_items: int = 5) -> Dict[str, object]:
    out: Dict[str, object] = {
        "type": type(m).__name__,
        "size": None,
        "keys_sample": [],
        "value_type_sample": None,
        "ok": False,
    }
    try:
        out["size"] = len(m)  # type: ignore[arg-type]
        keys = list(m.keys())  # type: ignore[call-arg]
        out["keys_sample"] = [str(k) for k in keys[:max_items]]
        if keys:
            out["value_type_sample"] = type(m[keys[0]]).__name__  # type: ignore[index]
        out["ok"] = True
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STDLIB_PREFIXES = (
    "java.",
    "javax.",
    "jdk.",
    "sun.",
    "com.sun.",
)

def _tuple_strs(value: object) -> Tuple[str, ...]:
    """
    Normalize a JSON-loaded sequence into tuple[str, ...].
    Tolerates older artifacts with missing/null/scalar fields.
    """
    if value is None:
        return tuple()

    if isinstance(value, str):
        s = value.strip()
        return (s,) if s else tuple()

    if isinstance(value, (list, tuple, set, frozenset)):
        out: List[str] = []
        for item in value:
            if item is None:
                continue
            s = str(item).strip()
            if s:
                out.append(s)
        return tuple(out)

    s = str(value).strip()
    return (s,) if s else tuple()


def _mapping_or_empty(value: object) -> Mapping[str, object]:
    if isinstance(value, dict):
        return value
    return {}

def _iter_java_files(root: Path, *, include_tests: bool = True) -> List[Path]:
    root = root.resolve()
    out: List[Path] = []
    try:
        for p in root.rglob("*.java"):
            if not p.is_file():
                continue
            if not include_tests:
                s = to_posix(str(p)).lower()
                if "/test/" in s or p.name.lower().endswith("test.java"):
                    continue
            out.append(p.resolve())
    except Exception:
        pass
    return sorted(set(out), key=lambda p: to_posix(str(p)))


def _count_java_metrics(text: str) -> Tuple[int, int, int, int, Optional[float]]:
    """
    Compute normalized Java metrics.

    Returns:
        loc, sloc, comment_lines, blank_lines, comment_pct

    loc:
        Physical line count, including blanks.

    sloc:
        Lines containing code after stripping comments.

    comment_lines:
        Comment-only physical lines.

    blank_lines:
        Blank / whitespace-only physical lines.

    comment_pct:
        comment_lines / loc
    """
    loc = 0
    sloc = 0
    comment_lines = 0
    blank_lines = 0
    in_block = False

    for raw in text.splitlines():
        loc += 1

        if not raw.strip():
            blank_lines += 1
            continue

        i = 0
        n = len(raw)
        out_chars: List[str] = []
        saw_comment = False

        while i < n:
            if in_block:
                saw_comment = True
                j = raw.find("*/", i)
                if j == -1:
                    i = n
                    break
                in_block = False
                i = j + 2
                continue

            if raw.startswith("/*", i):
                saw_comment = True
                in_block = True
                i += 2
                continue

            if raw.startswith("//", i):
                saw_comment = True
                break

            out_chars.append(raw[i])
            i += 1

        has_code = bool("".join(out_chars).strip())

        if has_code:
            sloc += 1
        elif saw_comment:
            comment_lines += 1

    comment_pct = (comment_lines / loc) if loc else None
    return loc, sloc, comment_lines, blank_lines, comment_pct


def _dedupe_preserve(seq: Sequence[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for s in seq:
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _chunk_files(files: List[Path], max_workers: int) -> List[List[Path]]:
    if not files or max_workers <= 1:
        return [files]

    chunk_size = max(1, len(files) // max_workers)
    chunks: List[List[Path]] = []

    for i in range(0, len(files), chunk_size):
        chunk = files[i:i + chunk_size]
        if chunk:
            chunks.append(chunk)

    return chunks


def _import_root(spec: str) -> str:
    spec = (spec or "").strip()
    if not spec:
        return ""
    if spec.startswith("static "):
        spec = spec[len("static "):].strip()
    if spec.endswith(".*"):
        spec = spec[:-2]
    return spec.split(".", 1)[0].strip()


def _is_stdlib_import(spec: str) -> bool:
    s = (spec or "").strip()
    if s.startswith("static "):
        s = s[len("static "):].strip()
    return s.startswith(_STDLIB_PREFIXES)


def _candidate_packages_for_import(spec: str) -> List[str]:
    s = (spec or "").strip()
    if not s:
        return []

    if s.startswith("static "):
        s = s[len("static "):].strip()

    if s.endswith(".*"):
        s = s[:-2]

    parts = [p for p in s.split(".") if p]
    return [".".join(parts[:n]) for n in range(len(parts), 0, -1)]


def _classify_unresolved_import(
    spec: str,
    *,
    package_to_files: Mapping[str, Set[str]],
    internal_roots: Set[str],
) -> str:
    """
    Classify an unresolved import without pretending it created an edge.

    Returns:
      stdlib
      internal_unresolved
      third_party
    """
    spec = (spec or "").strip()
    if not spec:
        return "third_party"

    if _is_stdlib_import(spec):
        return "stdlib"

    root = _import_root(spec)
    if root and root in internal_roots:
        return "internal_unresolved"

    for cand in _candidate_packages_for_import(spec):
        if cand in package_to_files:
            return "internal_unresolved"

    return "third_party"


def _metadata_from_file(path: Path) -> Tuple[int, int, int, int, Optional[float], int, str, str]:
    try:
        data = path.read_bytes()
        short_hash = blake2s_short(data)
        size_bytes = len(data)

        try:
            mtime_iso = iso_utc(path.stat().st_mtime)
        except Exception:
            mtime_iso = ""

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1", errors="replace")

        loc, sloc, comment_lines, blank_lines, comment_pct = _count_java_metrics(text)

        return loc, sloc, comment_lines, blank_lines, comment_pct, size_bytes, str(mtime_iso), str(short_hash)
    except Exception:
        return 0, 0, 0, 0, None, 0, "", ""


def _parse_files_batch(
    repo_root_str: str,
    paths: List[Path],
    max_bytes: Optional[int],
) -> Dict[str, Tuple[JavaParsedFile, int, int, int, int, Optional[float], int, str, str]]:
    repo_root = Path(repo_root_str)
    results: Dict[str, Tuple[JavaParsedFile, int, int, int, int, Optional[float], int, str, str]] = {}

    for path in paths:
        try:
            rel_file = to_posix(repo_rel(path, repo_root))
            data = path.read_bytes()
            short_hash = blake2s_short(data)
            size_bytes = len(data)

            try:
                mtime_iso = iso_utc(path.stat().st_mtime)
            except Exception:
                mtime_iso = ""

            if isinstance(max_bytes, int) and max_bytes > 0 and size_bytes > max_bytes:
                pf = JavaParsedFile(
                    path=path,
                    parse_status="error",
                    error_snippet=f"file_too_large:{size_bytes}",
                    package=None,
                    classes=[],
                    functions=[],
                    globals=[],
                    all_exports=[],
                    loc_code=None,
                )
                results[rel_file] = (pf, 0, 0, 0, 0, None, size_bytes, str(mtime_iso), str(short_hash))
                continue

            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("latin-1", errors="replace")

            loc, sloc, comment_lines, blank_lines, comment_pct = _count_java_metrics(text)
            pf = parse_java_file(path)

            results[rel_file] = (
                pf,
                loc,
                sloc,
                comment_lines,
                blank_lines,
                comment_pct,
                size_bytes,
                str(mtime_iso),
                str(short_hash),
            )

        except Exception as e:
            try:
                rel_file = to_posix(repo_rel(path, repo_root))
            except Exception:
                rel_file = to_posix(str(path))

            pf = JavaParsedFile(
                path=path,
                parse_status="error",
                error_snippet=f"{type(e).__name__}:{str(e)[:100]}",
                package=None,
                classes=[],
                functions=[],
                globals=[],
                all_exports=[],
                loc_code=None,
            )
            results[rel_file] = (pf, 0, 0, 0, 0, None, 0, "", "")

    return results


def _build_type_indexes(
    *,
    rel_files: List[str],
    parsed_by_rel: Mapping[str, JavaParsedFile],
) -> Tuple[Dict[str, str], Dict[str, Set[str]], Dict[str, Set[str]]]:
    type_to_file: Dict[str, str] = {}
    package_to_files: Dict[str, Set[str]] = {}
    all_type_locations: Dict[str, Set[str]] = {}

    for rel in rel_files:
        file_id = to_posix(rel)
        pf = parsed_by_rel.get(file_id) or parsed_by_rel.get(rel)

        pkg: Optional[str] = None
        if pf is not None and isinstance(pf.package, str) and pf.package.strip():
            pkg = pf.package.strip()
        if not pkg:
            pkg = derive_package_from_path(file_id)

        if pkg:
            package_to_files.setdefault(pkg, set()).add(file_id)

        decl: List[str] = []
        if pf is not None and isinstance(pf.classes, list):
            decl = [str(x) for x in pf.classes if x]

        for simple_or_nested in decl:
            fq = ""

            if pkg and simple_or_nested.startswith(pkg + "."):
                fq = simple_or_nested
            elif pkg and "." in simple_or_nested:
                outer, inner = simple_or_nested.split(".", 1)
                fq = build_fq_typename(pkg, outer, inner)
            elif pkg:
                fq = build_fq_typename(pkg, simple_or_nested)
            else:
                fq = simple_or_nested

            if not fq:
                continue

            all_type_locations.setdefault(fq, set()).add(file_id)
            if fq not in type_to_file:
                type_to_file[fq] = file_id

    type_collisions = {
        fq: files
        for fq, files in all_type_locations.items()
        if len(files) > 1
    }

    return type_to_file, package_to_files, type_collisions


def _safe_int_hash(hash_str: str) -> Optional[int]:
    if not hash_str:
        return None
    try:
        return int(hash_str)
    except Exception:
        pass
    try:
        hs = hash_str.lower()
        if hs.startswith("0x"):
            hs = hs[2:]
        return int(hs, 16)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_folder_index(
    root: Path,
    cfg,
    files: Optional[Sequence[Path]] = None,
    *,
    parsed_cache: Optional[Dict[str, JavaParsedFile]] = None,
) -> Tuple[FolderIndex, Dict[str, JavaParsedFile]]:
    t_start = time.perf_counter()
    log_event("JAVA_FOLDER_INDEX:build_enter", root=str(root))

    root = Path(root).resolve()

    try:
        _diag_print(
            "build_enter",
            root=str(root),
            cfg_type=type(cfg).__name__,
            has_parsed_cache=bool(parsed_cache),
            parsed_cache_len=(len(parsed_cache) if isinstance(parsed_cache, dict) else None),
            include_tests=bool(getattr(cfg, "include_tests", True)),
        )
    except Exception:
        pass

    if files is None:
        java_files = _iter_java_files(root, include_tests=getattr(cfg, "include_tests", True))
        log_event("JAVA_FOLDER_INDEX:iter_files_end", root=str(root), count=len(java_files), mode="full_repo")
        try:
            _diag_print("iter_files_end", mode="full_repo", count=len(java_files))
            if java_files:
                _diag_print("iter_files_sample", first=str(java_files[0]), last=str(java_files[-1]))
        except Exception:
            pass
    else:
        java_files_filtered: List[Path] = []
        skipped_outside = 0
        skipped_non_java = 0

        for p in files:
            try:
                ap = Path(p).resolve()
            except Exception:
                continue

            if not ap.exists() or not ap.is_file():
                continue

            if ap.suffix.lower() != ".java":
                skipped_non_java += 1
                continue

            try:
                ap.relative_to(root)
                java_files_filtered.append(ap)
            except Exception:
                skipped_outside += 1

        java_files = sorted(set(java_files_filtered), key=lambda p: to_posix(str(p)))
        log_event(
            "JAVA_FOLDER_INDEX:iter_files_end",
            root=str(root),
            count=len(java_files),
            mode="bucket",
            skipped_outside=skipped_outside,
            skipped_non_java=skipped_non_java,
        )
        try:
            _diag_print(
                "iter_files_end",
                mode="bucket",
                count=len(java_files),
                skipped_outside=skipped_outside,
                skipped_non_java=skipped_non_java,
            )
            if java_files:
                _diag_print("iter_files_sample", first=str(java_files[0]), last=str(java_files[-1]))
        except Exception:
            pass

    max_bytes = getattr(cfg, "max_file_bytes", getattr(cfg, "max_bytes_per_file", None))
    use_jar_imports = getattr(cfg, "java_use_jar_imports", True)

    try:
        _diag_print(
            "config",
            max_bytes=max_bytes,
            use_jar_imports=bool(use_jar_imports),
            cfg_max_workers=getattr(cfg, "max_workers", None),
        )
    except Exception:
        pass

    parsed_by_rel: Dict[str, JavaParsedFile] = {}
    file_metadata: Dict[str, Tuple[int, int, int, int, Optional[float], int, str, str]] = {}

    if parsed_cache:
        log_event("JAVA_FOLDER_INDEX:using_cache", files=len(parsed_cache))
        try:
            _diag_print("using_cache", **_diag_sample_map(parsed_cache))
        except Exception:
            pass

        for path in java_files:
            rel_file = to_posix(repo_rel(path, root))
            if rel_file in parsed_cache:
                parsed_by_rel[rel_file] = parsed_cache[rel_file]
                file_metadata[rel_file] = _metadata_from_file(path)

        try:
            _diag_print("using_cache_done", parsed=len(parsed_by_rel), metadata=len(file_metadata))
        except Exception:
            pass
    else:
        cpu_count = os.cpu_count() or 1
        max_workers = getattr(cfg, "max_workers", cpu_count)
        max_workers = min(max(1, max_workers), len(java_files) or 1)

        try:
            _diag_print("parse_plan", cpu_count=cpu_count, max_workers=max_workers, files=len(java_files))
        except Exception:
            pass

        if max_workers <= 1 or len(java_files) < 10:
            log_event("JAVA_FOLDER_INDEX:parse_serial", files=len(java_files))
            t_parse = time.perf_counter()

            batch_results = _parse_files_batch(str(root), java_files, max_bytes)
            for rel_file, (
                pf,
                loc,
                sloc,
                comment_lines,
                blank_lines,
                comment_pct,
                size,
                mtime,
                hash_str,
            ) in batch_results.items():
                rel_norm = to_posix(rel_file)
                parsed_by_rel[rel_norm] = pf
                file_metadata[rel_norm] = (
                    loc,
                    sloc,
                    comment_lines,
                    blank_lines,
                    comment_pct,
                    size,
                    mtime,
                    hash_str,
                )

            log_event(
                "JAVA_FOLDER_INDEX:parse_serial_done",
                files=len(java_files),
                ms=int((time.perf_counter() - t_parse) * 1000),
            )

            try:
                ok = sum(1 for pf in parsed_by_rel.values() if getattr(pf, "parse_status", None) == "ok")
                err = len(parsed_by_rel) - ok
                _diag_print(
                    "parse_serial_done",
                    ms=int((time.perf_counter() - t_parse) * 1000),
                    parsed=len(parsed_by_rel),
                    ok=ok,
                    err=err,
                )
            except Exception:
                pass
        else:
            log_event("JAVA_FOLDER_INDEX:parse_parallel", files=len(java_files), workers=max_workers)
            t_parse = time.perf_counter()
            chunks = _chunk_files(java_files, max_workers)

            try:
                _diag_print("parse_parallel_begin", chunks=len(chunks), workers=max_workers)
            except Exception:
                pass

            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(_parse_files_batch, str(root), chunk, max_bytes): idx
                    for idx, chunk in enumerate(chunks)
                }

                completed = 0
                for future in as_completed(futures):
                    batch_idx = futures[future]
                    try:
                        batch_results = future.result()
                        for rel_file, (
                            pf,
                            loc,
                            sloc,
                            comment_lines,
                            blank_lines,
                            comment_pct,
                            size,
                            mtime,
                            hash_str,
                        ) in batch_results.items():
                            rel_norm = to_posix(rel_file)
                            parsed_by_rel[rel_norm] = pf
                            file_metadata[rel_norm] = (
                                loc,
                                sloc,
                                comment_lines,
                                blank_lines,
                                comment_pct,
                                size,
                                mtime,
                                hash_str,
                            )

                        completed += len(batch_results)
                        log_event(
                            "JAVA_FOLDER_INDEX:batch_done",
                            batch=batch_idx,
                            batch_size=len(chunks[batch_idx]),
                            completed=completed,
                            total=len(java_files),
                        )
                        try:
                            _diag_print(
                                "batch_done",
                                batch=batch_idx,
                                batch_size=len(chunks[batch_idx]),
                                completed=completed,
                                total=len(java_files),
                            )
                        except Exception:
                            pass
                    except Exception as e:
                        log_event(
                            "JAVA_FOLDER_INDEX:batch_error",
                            batch=batch_idx,
                            error=f"{type(e).__name__}:{str(e)[:100]}",
                        )
                        try:
                            _diag_print("batch_error", batch=batch_idx, exc_type=type(e).__name__, exc=str(e)[:300])
                        except Exception:
                            pass

            log_event(
                "JAVA_FOLDER_INDEX:parse_parallel_done",
                files=len(java_files),
                parsed=len(parsed_by_rel),
                ms=int((time.perf_counter() - t_parse) * 1000),
            )

            try:
                ok = sum(1 for pf in parsed_by_rel.values() if getattr(pf, "parse_status", None) == "ok")
                err = len(parsed_by_rel) - ok
                _diag_print(
                    "parse_parallel_done",
                    ms=int((time.perf_counter() - t_parse) * 1000),
                    parsed=len(parsed_by_rel),
                    ok=ok,
                    err=err,
                    metadata=len(file_metadata),
                )
            except Exception:
                pass

    files_map: Dict[str, FileEntry] = {}
    parse_issues: List[str] = []
    rel_files: List[str] = []

    try:
        if len(parsed_by_rel) != len(java_files):
            _diag_print("parsed_mismatch", java_files=len(java_files), parsed=len(parsed_by_rel))
    except Exception:
        pass

    for path in java_files:
        rel_file = to_posix(repo_rel(path, root))
        rel_files.append(rel_file)

        (
            loc,
            sloc,
            comment_lines,
            blank_lines,
            comment_pct,
            size_bytes,
            mtime_iso,
            hash_str,
        ) = file_metadata.get(rel_file, (0, 0, 0, 0, None, 0, "", ""))

        pf = parsed_by_rel.get(rel_file)
        if not pf:
            pf = JavaParsedFile(
                path=path,
                parse_status="error",
                error_snippet="missing_from_parsed_cache",
                package=None,
                classes=[],
                functions=[],
                globals=[],
                all_exports=[],
                loc_code=None,
            )
            parsed_by_rel[rel_file] = pf

        parse_status = pf.parse_status or "ok"
        err_snip = pf.error_snippet

        if parse_status != "ok":
            parse_issues.append(f"{rel_file}: {err_snip or 'parse_error'}")

        imports_all_list: List[str] = []

        if use_jar_imports and getattr(pf, "imports", None):
            seen: Set[str] = set()
            for imp in pf.imports:
                spec = normalize_java_import_spec(
                    imp.target,
                    is_wildcard=imp.is_wildcard,
                    is_static=imp.is_static,
                )
                if spec and spec not in seen:
                    seen.add(spec)
                    imports_all_list.append(spec)
        elif getattr(pf, "imports_raw", None):
            imports_all_list = _dedupe_preserve([str(x) for x in pf.imports_raw if x])

        eligible = True
        if isinstance(max_bytes, int) and max_bytes > 0 and size_bytes > max_bytes:
            eligible = False

        file_id = rel_file
        hash_int = _safe_int_hash(hash_str)

        files_map[file_id] = FileEntry(
            id=file_id,
            file=rel_file,
            name=Path(rel_file).name,
            parse_status=parse_status,
            imports_all=tuple(sorted(set(imports_all_list))),
            imports_internal=tuple(),
            imports_runtime=tuple(),
            imports_runtime_internal=tuple(),
            loc=int(loc) if loc else None,
            sloc=int(sloc) if sloc else None,
            comment_lines=int(comment_lines) if comment_lines else None,
            blank_lines=int(blank_lines) if blank_lines else None,
            comment_pct=float(comment_pct) if comment_pct is not None else None,
            size_bytes=int(size_bytes) if size_bytes else None,
            mtime=mtime_iso if mtime_iso else None,
            hash=hash_int,
            import_style_counts={},
            symbol_internal=tuple(),
            imports_external=tuple(),  # computed after resolution
            language_facts={
                "java": {
                    "package": getattr(pf, "package", None),
                    "imports": list(tuple(sorted(set(imports_all_list)))),
                    "classes": list(getattr(pf, "classes", []) or []),
                    "functions": list(getattr(pf, "functions", []) or []),
                    "globals": list(getattr(pf, "globals", []) or []),
                    "exports": list(getattr(pf, "all_exports", []) or []),
                }
            },
            error_snippet=err_snip,
            eligible=eligible,
        )

    try:
        with_imports = sum(1 for fe in files_map.values() if fe.imports_all and len(fe.imports_all) > 0)
        _diag_print("file_entries_built", entries=len(files_map), with_imports=with_imports, parse_issues=len(parse_issues))
    except Exception:
        pass

    rel_files_sorted = sorted(set(rel_files), key=lambda s: to_posix(s))
    type_to_file, package_to_files, type_collisions = _build_type_indexes(
        rel_files=rel_files_sorted,
        parsed_by_rel=parsed_by_rel,
    )

    internal_roots: Set[str] = set()
    for pkg in package_to_files.keys():
        root_name = pkg.split(".", 1)[0].strip()
        if root_name:
            internal_roots.add(root_name)

    try:
        _diag_print(
            "type_indexes_built",
            type_index=len(type_to_file),
            package_index=len(package_to_files),
            type_collisions=len(type_collisions),
        )
        if type_to_file:
            items = list(type_to_file.items())[:5]
            _diag_print("type_index_sample", sample=[f"{k} -> {v}" for (k, v) in items])
        if package_to_files:
            pk = next(iter(package_to_files.keys()))
            _diag_print("package_index_sample", package=str(pk), file_count=len(package_to_files.get(pk) or set()))
    except Exception:
        pass

    files2: Dict[str, FileEntry] = {}

    internal_edge_count = 0
    resolved_internal_files_total = 0
    resolved_specs_considered_total = 0

    stdlib_total = 0
    third_party_total = 0
    internal_unresolved_total = 0

    unresolved_root_counter: Counter[str] = Counter()
    stdlib_root_counter: Counter[str] = Counter()
    third_party_root_counter: Counter[str] = Counter()
    internal_unresolved_root_counter: Counter[str] = Counter()

    for k, fe in files_map.items():
        mapped: List[str] = []

        stdlib_specs: List[str] = []
        third_party_specs: List[str] = []
        internal_unresolved_specs: List[str] = []

        for spec in (fe.imports_all or ()):
            resolved_specs_considered_total += 1

            rr_list = resolve_java_import(
                spec,
                type_to_file=type_to_file,
                package_to_files=package_to_files,
            )

            internal_targets = [
                to_posix(rr.resolved)
                for rr in rr_list
                if rr.kind == "internal" and rr.resolved
            ]

            if internal_targets:
                mapped.extend(internal_targets)
                continue

            classification = _classify_unresolved_import(
                spec,
                package_to_files=package_to_files,
                internal_roots=internal_roots,
            )

            root_name = _import_root(spec) or "<unknown>"
            unresolved_root_counter[root_name] += 1

            if classification == "stdlib":
                stdlib_specs.append(spec)
                stdlib_root_counter[root_name] += 1
            elif classification == "internal_unresolved":
                internal_unresolved_specs.append(spec)
                internal_unresolved_root_counter[root_name] += 1
            else:
                third_party_specs.append(spec)
                third_party_root_counter[root_name] += 1

        internal_files = tuple(sorted(set(x for x in mapped if x and x != fe.id)))
        resolved_internal_files_total += len(internal_files)
        internal_edge_count += len(internal_files)

        stdlib_specs_t = tuple(sorted(set(stdlib_specs)))
        third_party_specs_t = tuple(sorted(set(third_party_specs)))
        internal_unresolved_specs_t = tuple(sorted(set(internal_unresolved_specs)))

        stdlib_total += len(stdlib_specs_t)
        third_party_total += len(third_party_specs_t)
        internal_unresolved_total += len(internal_unresolved_specs_t)

        # v1.9: unresolved specs belong here, not in imports_all.
        # imports_all should remain the raw Java import surface from the parser.
        imports_external = tuple(
            sorted(set(stdlib_specs_t + third_party_specs_t + internal_unresolved_specs_t))
        )

        style_counts = {
            "internal": len(internal_files),
            "external": len(imports_external),
            "stdlib": len(stdlib_specs_t),
            "third_party": len(third_party_specs_t),
            "internal_unresolved": len(internal_unresolved_specs_t),
            "explicit_internal": len(internal_files),
            "symbol_internal": 0,
        }

        # Optional richer Java details under the language-neutral extension field.
        language_facts = dict(getattr(fe, "language_facts", {}) or {})
        java_facts = dict(language_facts.get("java", {}) or {})
        java_facts.update({
            "imports_stdlib": list(stdlib_specs_t),
            "imports_third_party": list(third_party_specs_t),
            "imports_internal_unresolved": list(internal_unresolved_specs_t),
        })
        language_facts["java"] = java_facts

        files2[k] = FileEntry(
            id=fe.id,
            file=fe.file,
            name=fe.name,
            parse_status=fe.parse_status,
            imports_all=fe.imports_all,
            imports_internal=internal_files,
            imports_runtime=internal_files,
            imports_runtime_internal=internal_files,
            loc=fe.loc,
            sloc=fe.sloc,
            comment_lines=getattr(fe, "comment_lines", None),
            blank_lines=getattr(fe, "blank_lines", None),
            comment_pct=getattr(fe, "comment_pct", None),
            size_bytes=fe.size_bytes,
            mtime=fe.mtime,
            hash=fe.hash,
            import_style_counts=style_counts,
            symbol_internal=fe.symbol_internal,
            imports_external=imports_external,
            language_facts=language_facts,
            error_snippet=fe.error_snippet,
            eligible=fe.eligible,
        )

    files_map = files2

    external_import_count = sum(
        len(getattr(fe, "imports_external", ()) or ())
        for fe in files_map.values()
    )

    try:
        with_internal = sum(1 for fe in files_map.values() if fe.imports_internal and len(fe.imports_internal) > 0)
        _diag_print(
            "imports_internal_resolved",
            internal_edge_count=int(internal_edge_count),
            files_with_internal=with_internal,
            specs_considered=resolved_specs_considered_total,
            internal_files_total=resolved_internal_files_total,
            stdlib=stdlib_total,
            third_party=third_party_total,
            internal_unresolved=internal_unresolved_total,
        )
    except Exception:
        pass

    parsed_count = sum(1 for v in files_map.values() if v.parse_status == "ok")
    eligible_count = sum(1 for v in files_map.values() if bool(getattr(v, "eligible", True)))

    total_loc = sum(int(v.loc or 0) for v in files_map.values())
    total_sloc = sum(int(v.sloc or 0) for v in files_map.values())
    total_comment_lines = sum(int(getattr(v, "comment_lines", 0) or 0) for v in files_map.values())
    total_blank_lines = sum(int(getattr(v, "blank_lines", 0) or 0) for v in files_map.values())
    comment_pct_total = (total_comment_lines / total_loc) if total_loc else None
    total_size_bytes = sum(int(v.size_bytes or 0) for v in files_map.values())

    def _top(counter: Counter[str], n: int) -> str:
        return ",".join(f"{k}:{v}" for k, v in counter.most_common(n))

    meta: Dict[str, str] = {
        "created": str(iso_utc()),
        "root": str(to_posix(root.resolve())),
        "language": "java",
        "eligible_count": str(int(eligible_count)),
        "parsed_count": str(int(parsed_count)),
        "internal_edge_count": str(int(internal_edge_count)),
        "parse_issues_count": str(len(parse_issues)),
        "type_index_count": str(len(type_to_file)),
        "package_index_count": str(len(package_to_files)),
        "type_collision_count": str(len(type_collisions)),
        "total_loc": str(int(total_loc)),
        "total_sloc": str(int(total_sloc)),
        "total_comment_lines": str(int(total_comment_lines)),
        "total_blank_lines": str(int(total_blank_lines)),
        "comment_pct": str(comment_pct_total if comment_pct_total is not None else ""),
        "total_size_bytes": str(int(total_size_bytes)),
        "imports_total_specs_considered": str(int(resolved_specs_considered_total)),
        "imports_internal_count": str(int(internal_edge_count)),
        "external_import_count": str(int(external_import_count)),
        "imports_stdlib_count": str(int(stdlib_total)),
        "imports_third_party_count": str(int(third_party_total)),
        "imports_internal_unresolved_count": str(int(internal_unresolved_total)),
    }

    top_unresolved_roots = _top(unresolved_root_counter, 12)
    top_stdlib_roots = _top(stdlib_root_counter, 8)
    top_third_party_roots = _top(third_party_root_counter, 8)
    top_internal_unresolved_roots = _top(internal_unresolved_root_counter, 8)

    if top_unresolved_roots:
        meta["top_unresolved_roots"] = top_unresolved_roots
    if top_stdlib_roots:
        meta["top_stdlib_roots"] = top_stdlib_roots
    if top_third_party_roots:
        meta["top_third_party_roots"] = top_third_party_roots
    if top_internal_unresolved_roots:
        meta["top_internal_unresolved_roots"] = top_internal_unresolved_roots

    if type_collisions:
        meta["type_collision_sample"] = ",".join(
            f"{fq}:{len(files)}" for fq, files in list(type_collisions.items())[:8]
        )

    idx = FolderIndex(schema=FOLDER_INDEX_SCHEMA, meta=meta, files=files_map)

    total_time = time.perf_counter() - t_start
    log_event(
        "JAVA_FOLDER_INDEX:build_return",
        root=str(root),
        eligible=eligible_count,
        parsed=parsed_count,
        internal_edges=internal_edge_count,
        parse_issues=len(parse_issues),
        type_index=len(type_to_file),
        package_index=len(package_to_files),
        type_collisions=len(type_collisions),
        total_loc=total_loc,
        total_sloc=total_sloc,
        total_comment_lines=total_comment_lines,
        total_blank_lines=total_blank_lines,
        comment_pct=comment_pct_total,
        stdlib=stdlib_total,
        third_party=third_party_total,
        internal_unresolved=internal_unresolved_total,
        total_ms=int(total_time * 1000),
    )

    try:
        _diag_print(
            "build_return",
            total_ms=int(total_time * 1000),
            eligible=eligible_count,
            parsed=parsed_count,
            parse_issues=len(parse_issues),
            internal_edge_count=int(internal_edge_count),
            type_index=len(type_to_file),
            package_index=len(package_to_files),
            type_collisions=len(type_collisions),
            total_loc=total_loc,
            total_sloc=total_sloc,
            total_comment_lines=total_comment_lines,
            total_blank_lines=total_blank_lines,
            comment_pct=comment_pct_total,
            stdlib=stdlib_total,
            third_party=third_party_total,
            internal_unresolved=internal_unresolved_total,
            parsed_cache_returned=len(parsed_by_rel),
        )
        if parse_issues:
            _diag_print("parse_issues_sample", sample=parse_issues[:3])
    except Exception:
        pass

    return idx, parsed_by_rel


def save_folder_index(idx: FolderIndex, path: Path) -> None:
    files_payload: Dict[str, dict] = {}

    for k, v in idx.files.items():
        rec = asdict(v)
        if "id" in rec:
            rec["folder_id"] = rec.pop("id")
        files_payload[k] = rec

    payload = {"schema": idx.schema, "meta": dict(idx.meta), "files": files_payload}
    atomic_write_json(payload, Path(path))


def load_folder_index(path: Path) -> FolderIndex:
    data = load_json_bytes(Path(path))
    if not isinstance(data, dict) or not data:
        raise ValueError(f"Invalid or empty FolderIndex JSON at {path}")

    schema = data.get("schema")
    if schema != FOLDER_INDEX_SCHEMA:
        raise ValueError(f"Unexpected schema: {schema}")

    files: Dict[str, FileEntry] = {}
    files_raw = data.get("files", {}) or {}

    if not isinstance(files_raw, Mapping):
        return FolderIndex(schema=data["schema"], meta=data.get("meta", {}), files=files)

    for key, rec in files_raw.items():
        if not isinstance(rec, Mapping):
            continue

        fid = rec.get("folder_id") or rec.get("id")
        if not fid:
            raise KeyError(f"Missing 'folder_id' (or legacy 'id') in record for {key}")

        file_id = to_posix(str(fid))
        file_path = to_posix(str(rec.get("file", file_id) or file_id))

        imports_all = _tuple_strs(rec.get("imports_all", ()))
        imports_internal = _tuple_strs(rec.get("imports_internal", ()))
        imports_runtime = _tuple_strs(
            rec.get("imports_runtime", imports_internal)
        )
        imports_runtime_internal = _tuple_strs(
            rec.get(
                "imports_runtime_internal",
                rec.get("imports_runtime", imports_internal),
            )
        )
        symbol_internal = _tuple_strs(rec.get("symbol_internal", ()))
        imports_external = _tuple_strs(rec.get("imports_external", ()))
        language_facts = _mapping_or_empty(rec.get("language_facts", {}))

        files[file_id] = FileEntry(
            id=file_id,
            file=file_path,
            name=str(rec.get("name", "")),
            parse_status=str(rec.get("parse_status", "ok")),
            imports_all=imports_all,
            imports_internal=imports_internal,
            imports_runtime=imports_runtime,
            imports_runtime_internal=imports_runtime_internal,
            loc=rec.get("loc"),
            sloc=rec.get("sloc"),
            comment_lines=rec.get("comment_lines"),
            blank_lines=rec.get("blank_lines"),
            comment_pct=rec.get("comment_pct"),
            size_bytes=rec.get("size_bytes"),
            mtime=rec.get("mtime"),
            hash=rec.get("hash"),
            import_style_counts=_mapping_or_empty(
                rec.get("import_style_counts", {})
            ),
            symbol_internal=symbol_internal,
            imports_external=imports_external,
            language_facts=language_facts,
            error_snippet=rec.get("error_snippet"),
            eligible=bool(rec.get("eligible", True)),
        )

    return FolderIndex(schema=data["schema"], meta=data.get("meta", {}), files=files)