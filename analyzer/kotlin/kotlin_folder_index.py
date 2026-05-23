from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple
import os
import re
import time

from analyzer_store.types import FOLDER_INDEX_SCHEMA, FileEntry, FolderIndex
from analyzer_store.io_utils import iso_utc, blake2s_short, atomic_write_json, load_json_bytes
from adapters.canonical import repo_rel, to_posix

from analyzer.kotlin.parse_kotlin import KotlinParsedFile, parse_kotlin_path
from analyzer.kotlin.kotlin_canonical import (
    fq_decl,
    normalize_kotlin_import_spec,
    resolve_kotlin_import,
)

try:
    from diagnostics.logging import log_event
except Exception:
    def log_event(*_a, **_k):
        return


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def _diag_enabled(cfg: object) -> bool:
    try:
        if bool(getattr(cfg, "kotlin_diag", False)):
            return True
    except Exception:
        pass

    v = os.getenv("PVIZ_KOTLIN_DIAG", "").strip().lower()
    return v in {"1", "true", "yes", "on"}


def _diag_print(cfg: object, name: str, **k) -> None:
 #   if not _diag_enabled(cfg):
  #      return

    parts = " ".join(f"{kk}={k[kk]!r}" for kk in sorted(k.keys()))
    print(f"[KOTLIN_DIAG] {name} {parts}".rstrip())


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

_RE_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")

_KOTLIN_IGNORE_SYMBOLS: Set[str] = {
    "Any", "Nothing", "Unit", "String", "Char", "Boolean",
    "Byte", "Short", "Int", "Long", "Float", "Double",
    "Array", "List", "Set", "Map", "Collection", "Iterable", "Sequence",
    "MutableList", "MutableSet", "MutableMap", "MutableCollection",
    "Pair", "Triple", "Result", "Exception", "Throwable",
    "println", "print", "readLine", "TODO", "lazy", "apply", "let", "run",
    "also", "with", "use", "when", "if", "else", "for", "while", "do",
    "return", "break", "continue", "throw", "try", "catch", "finally",
    "class", "interface", "object", "enum", "data", "sealed", "open",
    "abstract", "override", "private", "public", "protected", "internal",
    "fun", "val", "var", "constructor", "companion", "package", "import",
    "true", "false", "null", "this", "super", "is", "in", "as", "by",
    "where", "typealias",
    "java", "javax", "kotlin", "kotlinx", "org", "com", "net", "io",
}


def _iter_kotlin_files(
    root: Path,
    *,
    include_tests: bool = False,
    include_kts: bool = True,
) -> List[Path]:
    root = Path(root).resolve()
    suffixes = {".kt"} | ({".kts"} if include_kts else set())
    out: List[Path] = []

    try:
        for p in root.rglob("*"):
            try:
                if not p.is_file() or p.suffix.lower() not in suffixes:
                    continue

                sp = to_posix(str(p)).lower()

                if not include_tests and (
                    "/src/test/" in sp
                    or "/test/" in sp
                    or "/tests/" in sp
                    or p.name.lower().endswith("test.kt")
                ):
                    continue

                if any(
                    seg in sp
                    for seg in (
                        "/.git/", "/build/", "/.gradle/", "/target/", "/out/",
                        "/.idea/", "/.cache/", "/.pviz_store/", "/generated/",
                        "/build/generated/",
                    )
                ):
                    continue

                out.append(p.resolve())
            except Exception:
                continue
    except Exception:
        pass

    return sorted(set(out), key=lambda x: to_posix(str(x)))

def _count_kotlin_metrics(text: str) -> Tuple[int, int, int, int, Optional[float]]:
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
        out_chars: List[str] = []
        saw_comment = False

        while i < len(raw):
            if in_block:
                saw_comment = True
                j = raw.find("*/", i)
                if j == -1:
                    i = len(raw)
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

def _parse_hash_to_int(hash_value: object) -> Optional[int]:
    s = str(hash_value or "").strip()
    if not s:
        return None

    try:
        return int(s)
    except Exception:
        pass

    try:
        ss = s.lower()
        if ss.startswith("0x"):
            ss = ss[2:]
        return int(ss, 16)
    except Exception:
        return None


def _extract_tokens_from_text(text: str) -> Set[str]:
    if not text:
        return set()

    tokens = set(_RE_IDENT.findall(text))
    return {
        t for t in tokens
        if t
        and t not in _KOTLIN_IGNORE_SYMBOLS
        and not t.startswith("_")
        and len(t) > 1
    }


def _read_metadata(path: Path) -> Tuple[int, int, int, str, Optional[int], str]:
    try:
        data = Path(path).read_bytes()
        size = len(data)
        mtime = iso_utc(path.stat().st_mtime)
        h = blake2s_short(data)

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1", errors="replace")

        loc, sloc, comment_lines, blank_lines, comment_pct = _count_kotlin_metrics(text)

        return loc, sloc, comment_lines, blank_lines, comment_pct, size, str(mtime), _parse_hash_to_int(h), text

    except Exception:
        return 0, 0, 0, 0, None, 0, "", None, ""


def _decl_names(pf: KotlinParsedFile) -> List[str]:
    out: List[str] = []

    for seq in (
        getattr(pf, "classes", None) or (),
        getattr(pf, "interfaces", None) or (),
        getattr(pf, "objects", None) or (),
        getattr(pf, "enums", None) or (),
        getattr(pf, "type_aliases", None) or (),
    ):
        for d in seq:
            nm = str(getattr(d, "name", "") or "").strip()
            if nm:
                out.append(nm)

    return sorted(set(out))


def _function_names(pf: KotlinParsedFile) -> List[str]:
    out: List[str] = []

    for d in (getattr(pf, "functions", None) or ()):
        nm = str(getattr(d, "name", "") or "").strip()
        if nm:
            out.append(nm)

    return sorted(set(out))


def _property_names(pf: KotlinParsedFile) -> List[str]:
    out: List[str] = []

    for d in (getattr(pf, "properties", None) or ()):
        nm = str(getattr(d, "name", "") or "").strip()
        if nm:
            out.append(nm)

    return sorted(set(out))


def _all_exported_symbol_names(pf: KotlinParsedFile) -> List[str]:
    out: List[str] = []
    out.extend(_decl_names(pf))
    out.extend(_function_names(pf))
    out.extend(_property_names(pf))
    return sorted(set(x for x in out if x and x not in _KOTLIN_IGNORE_SYMBOLS))


def _build_indexes(
    parsed_by_rel: Mapping[str, KotlinParsedFile],
) -> Tuple[
    Dict[str, str],
    Dict[str, Set[str]],
    Dict[str, str],
    Dict[str, Set[str]],
    Dict[Tuple[str, str], Set[str]],
    Dict[str, Set[str]],
    Dict[str, Set[str]],
]:
    fq_decl_to_file: Dict[str, str] = {}
    package_to_files: Dict[str, Set[str]] = {}
    file_id_to_package: Dict[str, str] = {}
    simple_symbol_to_files: Dict[str, Set[str]] = {}
    package_symbol_to_files: Dict[Tuple[str, str], Set[str]] = {}
    top_level_function_to_files: Dict[str, Set[str]] = {}
    property_to_files: Dict[str, Set[str]] = {}

    for rel, pf in parsed_by_rel.items():
        file_id = to_posix(str(rel))
        pkg = (getattr(pf, "package_name", None) or "").strip()

        if pkg:
            file_id_to_package[file_id] = pkg
            package_to_files.setdefault(pkg, set()).add(file_id)

        for nm in _all_exported_symbol_names(pf):
            simple_symbol_to_files.setdefault(nm, set()).add(file_id)
            if pkg:
                package_symbol_to_files.setdefault((pkg, nm), set()).add(file_id)
                fq = fq_decl(pkg, nm)
                if fq and fq not in fq_decl_to_file:
                    fq_decl_to_file[fq] = file_id

        for nm in _function_names(pf):
            top_level_function_to_files.setdefault(nm, set()).add(file_id)

        for nm in _property_names(pf):
            property_to_files.setdefault(nm, set()).add(file_id)

    return (
        fq_decl_to_file,
        package_to_files,
        file_id_to_package,
        simple_symbol_to_files,
        package_symbol_to_files,
        top_level_function_to_files,
        property_to_files,
    )


def _cache_get(
    parse_cache: Mapping[str, KotlinParsedFile],
    *,
    rel: str,
    path: Path,
    root: Path,
) -> Optional[KotlinParsedFile]:
    rel_posix = to_posix(str(rel))
    candidates = [rel, rel_posix]

    try:
        candidates.append(str(path.resolve(strict=False)))
        candidates.append(to_posix(str(path.resolve(strict=False))))
    except Exception:
        pass

    try:
        p2 = (root / rel_posix).resolve(strict=False)
        candidates.append(str(p2))
        candidates.append(to_posix(str(p2)))
    except Exception:
        pass

    for key in candidates:
        if key and key in parse_cache:
            return parse_cache[key]

    return None


def _parse_kotlin_files(
    *,
    root: Path,
    kotlin_files: Sequence[Path],
    cfg: object,
    parsed_cache: Optional[Mapping[str, KotlinParsedFile]],
    explicit_files_mode: bool,
) -> Dict[str, KotlinParsedFile]:
    parsed_by_rel: Dict[str, KotlinParsedFile] = {}

    if isinstance(parsed_cache, Mapping) and parsed_cache:
        _diag_print(cfg, "using_cache", **_diag_sample_map(parsed_cache))

        for p in kotlin_files:
            rel = repo_rel(p, root)
            rel_posix = to_posix(rel)
            cached = _cache_get(parsed_cache, rel=rel_posix, path=p, root=root)
            if cached is not None:
                parsed_by_rel[rel_posix] = cached

        _diag_print(
            cfg,
            "using_cache_done",
            requested=len(kotlin_files),
            parsed=len(parsed_by_rel),
        )

    missing_files = [
        p
        for p in kotlin_files
        if to_posix(repo_rel(p, root)) not in parsed_by_rel
    ]

    if not missing_files:
        _diag_print(
            cfg,
            "parse_strategy",
            use_directory_batch=False,
            explicit_files_mode=bool(explicit_files_mode),
            kotlin_files=len(kotlin_files),
            missing_files=0,
            parsed_from_cache=len(parsed_by_rel),
            reason="all_files_from_cache",
        )
        return parsed_by_rel

    if parsed_cache:
        _diag_print(
            cfg,
            "cache_partial_miss",
            missing=len(missing_files),
            requested=len(kotlin_files),
        )

    use_directory_batch = (
        bool(getattr(cfg, "kotlin_use_directory_batch", True))
        and len(missing_files) >= 5   # threshold avoids overhead for tiny sets
    )

    _diag_print(
        cfg,
        "parse_strategy",
        use_directory_batch=bool(use_directory_batch),
        explicit_files_mode=bool(explicit_files_mode),
        kotlin_files=len(kotlin_files),
        missing_files=len(missing_files),
        parsed_from_cache=len(parsed_by_rel),
        kotlin_use_directory_batch=bool(getattr(cfg, "kotlin_use_directory_batch", True)),
        reason=(
            "directory_batch"
            if use_directory_batch
            else (
                "disabled_by_explicit_files_mode"
                if explicit_files_mode
                else (
                    "partial_cache_miss"
                    if len(missing_files) != len(kotlin_files)
                    else "directory_batch_disabled"
                )
            )
        ),
    )

    if use_directory_batch:
        try:
            t_batch = time.perf_counter()

            _diag_print(
                cfg,
                "directory_batch_begin",
                root=str(root),
                files_requested=len(kotlin_files),
                missing_files=len(missing_files),
            )

            recs = parse_kotlin_path(root, cfg=cfg)
            wanted = {to_posix(repo_rel(p, root)) for p in kotlin_files}

            total_records = 0
            skipped_missing_path = 0
            skipped_not_wanted = 0

            for pf in recs:
                total_records += 1
                try:
                    abs_path = Path(pf.file_path).resolve(strict=False)
                    if not abs_path.exists():
                        skipped_missing_path += 1
                        continue

                    rel = to_posix(repo_rel(abs_path, root))
                    if rel in wanted:
                        parsed_by_rel[rel] = pf
                    else:
                        skipped_not_wanted += 1
                except Exception:
                    skipped_not_wanted += 1
                    continue

            _diag_print(
                cfg,
                "directory_batch_done",
                parsed=len(parsed_by_rel),
                wanted=len(wanted),
                total_records=total_records,
                skipped_missing_path=skipped_missing_path,
                skipped_not_wanted=skipped_not_wanted,
                ms=int((time.perf_counter() - t_batch) * 1000),
            )

            if parsed_by_rel:
                return parsed_by_rel

        except Exception as e:
            _diag_print(
                cfg,
                "directory_batch_failed",
                exc_type=type(e).__name__,
                exc=str(e)[:300],
            )

    t_file = time.perf_counter()

    _diag_print(
        cfg,
        "per_file_fallback_begin",
        files=len(missing_files),
        existing_parsed=len(parsed_by_rel),
        reason="directory_batch_disabled_or_failed",
    )

    per_file_ok = 0
    per_file_error = 0
    per_file_empty = 0
    sample_errors: List[str] = []

    for p in missing_files:
        rel = to_posix(repo_rel(p, root))

        try:
            recs = parse_kotlin_path(p, cfg=cfg)

            if recs:
                chosen = None
                for pf in recs:
                    try:
                        pf_rel = to_posix(repo_rel(Path(pf.file_path).resolve(strict=False), root))
                        if pf_rel == rel:
                            chosen = pf
                            break
                    except Exception:
                        pass

                parsed_by_rel[rel] = chosen or recs[0]
                per_file_ok += 1
            else:
                parsed_by_rel[rel] = KotlinParsedFile(
                    ok=False,
                    parse_status="error",
                    file_path=p,
                    error="kotlinparser_cli returned no parse records",
                )
                per_file_empty += 1

        except Exception as e:
            err = f"kotlin_cli_error:{type(e).__name__}:{str(e)[:200]}"
            parsed_by_rel[rel] = KotlinParsedFile(
                ok=False,
                parse_status="error",
                file_path=p,
                error=err,
            )
            per_file_error += 1

            if len(sample_errors) < 5:
                sample_errors.append(f"{rel}:{err}")

    _diag_print(
        cfg,
        "per_file_fallback_done",
        parsed=len(parsed_by_rel),
        attempted=len(missing_files),
        ok=per_file_ok,
        empty=per_file_empty,
        errors=per_file_error,
        sample_errors=sample_errors,
        ms=int((time.perf_counter() - t_file) * 1000),
    )

    return parsed_by_rel


def _resolve_symbol_edges(
    *,
    file_id: str,
    pf: Optional[KotlinParsedFile],
    tokens: Set[str],
    file_id_to_package: Mapping[str, str],
    package_to_files: Mapping[str, Set[str]],
    simple_symbol_to_files: Mapping[str, Set[str]],
    package_symbol_to_files: Mapping[Tuple[str, str], Set[str]],
    top_level_function_to_files: Mapping[str, Set[str]],
    property_to_files: Mapping[str, Set[str]],
    max_symbol_edges_per_file: int,
) -> Tuple[Set[str], Dict[str, int]]:
    if not tokens:
        return set(), {}

    low_signal_symbols = {
        "html", "HTML", "text", "Text", "table", "Table",
        "tag", "Tag", "date", "Date", "calendar", "Calendar",
        "time", "Time", "product", "Product", "customer", "Customer",
        "order", "Order", "city", "City",
    }

    local_decls: Set[str] = set()
    if pf is not None:
        local_decls.update(_all_exported_symbol_names(pf))

    usable_tokens = {
        t for t in tokens
        if t
        and t not in local_decls
        and t not in _KOTLIN_IGNORE_SYMBOLS
        and t not in low_signal_symbols
    }

    if not usable_tokens:
        return set(), {}

    out: Set[str] = set()
    reason_counts: Dict[str, int] = {
        "same_package_symbol": 0,
        "global_symbol": 0,
        "top_level_function": 0,
        "property": 0,
    }

    pkg = file_id_to_package.get(file_id)
    same_pkg_files = package_to_files.get(pkg, set()) if pkg else set()

    # Highest-confidence: same package, exact symbol, unambiguous.
    if pkg and same_pkg_files:
        for tok in sorted(usable_tokens):
            candidates = set(package_symbol_to_files.get((pkg, tok), ()))
            candidates.discard(file_id)

            if len(candidates) != 1:
                continue

            candidate = next(iter(candidates))
            if candidate in same_pkg_files:
                out.add(candidate)
                reason_counts["same_package_symbol"] += 1
                if len(out) >= max_symbol_edges_per_file:
                    return out, reason_counts

    # Global type/object-like symbols only. Avoid lowercase function/property names globally.
    for tok in sorted(usable_tokens):
        if not tok[:1].isupper():
            continue

        candidates = set(simple_symbol_to_files.get(tok, ()))
        candidates.discard(file_id)

        if len(candidates) == 1:
            out.update(candidates)
            reason_counts["global_symbol"] += 1
            if len(out) >= max_symbol_edges_per_file:
                return out, reason_counts

    # Only allow top-level function inference for non-lowercase names.
    # Most Kotlin top-level functions are lowercase, so global lowercase matching is too noisy.
    for tok in sorted(usable_tokens):
        if not tok[:1].isupper():
            continue

        candidates = set(top_level_function_to_files.get(tok, ()))
        candidates.discard(file_id)

        if len(candidates) == 1:
            out.update(candidates)
            reason_counts["top_level_function"] += 1
            if len(out) >= max_symbol_edges_per_file:
                return out, reason_counts

    # Same for properties: avoid global lowercase property matches.
    for tok in sorted(usable_tokens):
        if not tok[:1].isupper():
            continue

        candidates = set(property_to_files.get(tok, ()))
        candidates.discard(file_id)

        if len(candidates) == 1:
            out.update(candidates)
            reason_counts["property"] += 1
            if len(out) >= max_symbol_edges_per_file:
                return out, reason_counts

    return out, reason_counts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_folder_index(
    root: Path,
    cfg,
    files: Optional[Sequence[Path]] = None,
    *,
    parsed_cache: Optional[Dict[str, KotlinParsedFile]] = None,
) -> Tuple[FolderIndex, Dict[str, KotlinParsedFile]]:
    t0 = time.perf_counter()
    root = Path(root).resolve()

    include_tests = bool(getattr(cfg, "include_tests", False))
    include_kts = bool(getattr(cfg, "include_kts", True))

    mb = getattr(cfg, "max_file_bytes", None)
    if not isinstance(mb, int) or mb <= 0:
        mb = getattr(cfg, "max_bytes_per_file", None)
    max_bytes = mb if isinstance(mb, int) and mb > 0 else None

    max_symbol_edges_per_file = getattr(cfg, "kotlin_max_symbol_edges_per_file", None)
    if not isinstance(max_symbol_edges_per_file, int) or max_symbol_edges_per_file <= 0:
        max_symbol_edges_per_file = 25

    enable_symbol_edges = bool(getattr(cfg, "kotlin_enable_symbol_edges", True))

    try:
        setattr(cfg, "repo_root", str(root))
    except Exception:
        pass

    log_event("KOTLIN_FOLDER_INDEX:build_enter", root=str(root))

    explicit_files_mode = files is not None

    if files is None:
        kotlin_files = _iter_kotlin_files(
            root,
            include_tests=include_tests,
            include_kts=include_kts,
        )

        log_event(
            "KOTLIN_FOLDER_INDEX:iter_files_end",
            root=str(root),
            count=len(kotlin_files),
            mode="full_repo",
        )
    else:
        suffixes = {".kt"} | ({".kts"} if include_kts else set())
        tmp: List[Path] = []
        skipped_outside = 0
        skipped_non_kotlin = 0
        skipped_tests = 0

        for p in files:
            try:
                ap = Path(p).resolve()
            except Exception:
                continue

            if not ap.is_file() or ap.suffix.lower() not in suffixes:
                skipped_non_kotlin += 1
                continue

            if not include_tests:
                sp = to_posix(str(ap)).lower()
                if (
                    "/src/test/" in sp
                    or "/test/" in sp
                    or "/tests/" in sp
                    or ap.name.lower().endswith("test.kt")
                ):
                    skipped_tests += 1
                    continue

            try:
                ap.relative_to(root)
                tmp.append(ap)
            except Exception:
                skipped_outside += 1

        kotlin_files = sorted(set(tmp), key=lambda x: to_posix(str(x)))

        log_event(
            "KOTLIN_FOLDER_INDEX:iter_files_end",
            root=str(root),
            count=len(kotlin_files),
            mode="bucket",
            skipped_outside=skipped_outside,
            skipped_non_kotlin=skipped_non_kotlin,
            skipped_tests=skipped_tests,
        )

    t_parse = time.perf_counter()

    parsed_by_rel = _parse_kotlin_files(
        root=root,
        kotlin_files=kotlin_files,
        cfg=cfg,
        parsed_cache=parsed_cache,
        explicit_files_mode=explicit_files_mode,
    )

    log_event(
        "KOTLIN_FOLDER_INDEX:parse_done",
        files=len(kotlin_files),
        parsed=len(parsed_by_rel),
        ms=int((time.perf_counter() - t_parse) * 1000),
    )

    (
        fq_decl_to_file,
        package_to_files,
        file_id_to_package,
        simple_symbol_to_files,
        package_symbol_to_files,
        top_level_function_to_files,
        property_to_files,
    ) = _build_indexes(parsed_by_rel)

    try:
        cfg.kotlin_parse_cache = dict(parsed_by_rel)
        cfg.kotlin_fq_decl_to_file = dict(fq_decl_to_file)
        cfg.kotlin_package_to_files = {k: sorted(v) for k, v in package_to_files.items()}
        cfg.kotlin_file_id_to_package = dict(file_id_to_package)
        cfg.kotlin_simple_symbol_to_files = {
            k: sorted(v) for k, v in simple_symbol_to_files.items()
        }
        cfg.kotlin_package_symbol_to_files = {
            f"{pkg}.{sym}": sorted(v)
            for (pkg, sym), v in package_symbol_to_files.items()
        }
        cfg.kotlin_top_level_function_to_files = {
            k: sorted(v) for k, v in top_level_function_to_files.items()
        }
        cfg.kotlin_property_to_files = {
            k: sorted(v) for k, v in property_to_files.items()
        }
    except Exception:
        pass

    files_map: Dict[str, FileEntry] = {}
    parse_issues: List[str] = []

    internal_edge_count = 0
    explicit_internal_edge_count = 0
    symbol_internal_edge_count = 0
    symbol_reason_totals: Dict[str, int] = {}

    for path in kotlin_files:
        rel = to_posix(repo_rel(path, root))
        file_id = rel
        pf = parsed_by_rel.get(rel)

        loc, sloc, comment_lines, blank_lines, comment_pct, size, mtime, hash_int, text = _read_metadata(path)
        eligible = not (isinstance(max_bytes, int) and max_bytes > 0 and size > max_bytes)

        imports_all_raw: Tuple[str, ...] = ()
        imports_all: Tuple[str, ...] = ()
        mapped: List[str] = []
        external_specs: List[str] = []
        resolved_internal_specs: Set[str] = set()

        if not eligible:
            parse_status = "skipped"
            error = f"file_too_large:{size}"
            loc_code = loc

        elif pf is None:
            parse_status = "error"
            error = "missing_from_parse_cache"
            loc_code = loc

        else:
            parse_status = "ok" if pf.parse_status == "ok" and pf.ok else (pf.parse_status or "error")
            error = pf.error
            loc_code = pf.loc_code or loc

            imports_set: Set[str] = set()
            for imp in (pf.imports or []):
                spec = normalize_kotlin_import_spec(
                    getattr(imp, "path", "") or "",
                    is_wildcard=bool(getattr(imp, "is_wildcard", False)),
                )
                if spec:
                    imports_set.add(spec)

            imports_all_raw = tuple(sorted(imports_set))

            for spec in imports_all_raw:
                results = resolve_kotlin_import(
                    spec,
                    fq_decl_to_file=fq_decl_to_file,
                    package_to_files=package_to_files,
                )

                internal_targets = [
                    to_posix(rr.resolved)
                    for rr in results
                    if rr.kind == "internal" and rr.resolved
                ]

                if internal_targets:
                    mapped.extend(internal_targets)
                    resolved_internal_specs.add(spec)
                else:
                    external_specs.append(spec)

            imports_all = tuple(
                sorted(spec for spec in imports_all_raw if spec not in resolved_internal_specs)
            )

        if parse_status != "ok":
            parse_issues.append(f"{file_id}: {error or parse_status}")

        explicit_import_edges = set(x for x in mapped if x and x != file_id)
        explicit_internal_edge_count += len(explicit_import_edges)

        symbol_edges: Set[str] = set()
        if enable_symbol_edges and eligible and parse_status == "ok":
            tokens: Set[str] = set()

            if pf is not None:
                # PRIMARY SIGNAL (parser-derived)
                tokens.update(getattr(pf, "refs_raw", []) or [])

                # OPTIONAL: keep text fallback but lower priority
                if not tokens:
                    tokens.update(_extract_tokens_from_text(text))

            symbol_edges, reason_counts = _resolve_symbol_edges(
                file_id=file_id,
                pf=pf,
                tokens=tokens,
                file_id_to_package=file_id_to_package,
                package_to_files=package_to_files,
                simple_symbol_to_files=simple_symbol_to_files,
                package_symbol_to_files=package_symbol_to_files,
                top_level_function_to_files=top_level_function_to_files,
                property_to_files=property_to_files,
                max_symbol_edges_per_file=max_symbol_edges_per_file,
            )
            symbol_edges.discard(file_id)

            for reason, count in reason_counts.items():
                symbol_reason_totals[reason] = symbol_reason_totals.get(reason, 0) + int(count)

        symbol_internal_edge_count += len(symbol_edges - explicit_import_edges)

        imports_internal = tuple(sorted(explicit_import_edges))
        symbol_internal = tuple(sorted(symbol_edges))
        imports_external_fe = tuple(sorted(set(external_specs)))

        internal_edge_count += len(imports_internal)

        imports_runtime = imports_internal
        imports_runtime_internal = imports_internal

        if pf is not None and (
            imports_external_fe
            or imports_all_raw
            or getattr(pf, "imports_all_raw", None) is None
        ):
            try:
                from dataclasses import replace as _dc_replace

                update: Dict[str, object] = {}

                if imports_external_fe:
                    update["imports_external"] = list(imports_external_fe)

                if not getattr(pf, "imports_all_raw", None) and imports_all_raw:
                    raw = [s[:-2] if s.endswith(".*") else s for s in imports_all_raw]
                    update["imports_all_raw"] = raw

                if update:
                    pf = _dc_replace(pf, **update)
                    parsed_by_rel[rel] = pf
            except Exception:
                pass

        style_counts = {
            "internal": len(imports_internal),
            "external": len(imports_external_fe),
            "explicit_internal": len(explicit_import_edges),
            "symbol_internal": len(symbol_edges - explicit_import_edges),
        }

        fe = FileEntry(
            id=file_id,
            file=rel,
            name=Path(rel).name,
            parse_status=parse_status,
            imports_all=imports_all,
            imports_internal=imports_internal,
            imports_runtime=imports_runtime,
            imports_runtime_internal=imports_runtime_internal,
            symbol_internal=symbol_internal,
            loc=loc if loc else None,
            sloc=loc_code if loc_code else None,
            comment_lines=comment_lines if comment_lines else None,
            blank_lines=blank_lines if blank_lines else None,
            comment_pct=comment_pct,
            size_bytes=size if size else None,
            mtime=mtime if mtime else None,
            hash=hash_int,
            import_style_counts=style_counts,
            error_snippet=error,
            eligible=eligible,
        )

        files_map[file_id] = fe
    total_loc = sum(int(getattr(fe, "loc", 0) or 0) for fe in files_map.values())
    total_sloc = sum(int(getattr(fe, "sloc", 0) or 0) for fe in files_map.values())
    total_comment_lines = sum(int(getattr(fe, "comment_lines", 0) or 0) for fe in files_map.values())
    total_blank_lines = sum(int(getattr(fe, "blank_lines", 0) or 0) for fe in files_map.values())
    comment_pct_total = (total_comment_lines / total_loc) if total_loc else None
    parsed_count = sum(1 for fe in files_map.values() if fe.parse_status == "ok")
    eligible_count = sum(1 for fe in files_map.values() if bool(getattr(fe, "eligible", True)))

    meta = {
        "created": str(iso_utc()),
        "root": str(to_posix(root.resolve())),
        "language": "kotlin",
        "eligible_count": str(int(eligible_count)),
        "parsed_count": str(int(parsed_count)),
        "internal_edge_count": str(int(internal_edge_count)),
        "explicit_internal_edge_count": str(int(explicit_internal_edge_count)),
        "symbol_internal_edge_count": str(int(symbol_internal_edge_count)),
        "parse_issues_count": str(len(parse_issues)),
        "total_loc": str(int(total_loc)),
        "total_sloc": str(int(total_sloc)),
        "total_comment_lines": str(int(total_comment_lines)),
        "total_blank_lines": str(int(total_blank_lines)),
        "comment_pct": str(comment_pct_total if comment_pct_total is not None else ""),
        "fq_decl_index_count": str(len(fq_decl_to_file)),
        "package_index_count": str(len(package_to_files)),
        "file_package_index_count": str(len(file_id_to_package)),
        "simple_symbol_index_count": str(len(simple_symbol_to_files)),
        "package_symbol_index_count": str(len(package_symbol_to_files)),
        "top_level_function_index_count": str(len(top_level_function_to_files)),
        "property_index_count": str(len(property_to_files)),
    }

    if symbol_reason_totals:
        top = sorted(symbol_reason_totals.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
        meta["symbol_reason_top"] = ",".join(f"{k}:{v}" for k, v in top)

    idx = FolderIndex(schema=FOLDER_INDEX_SCHEMA, meta=meta, files=files_map)

    log_event(
        "KOTLIN_FOLDER_INDEX:build_return",
        root=str(root),
        eligible=eligible_count,
        parsed=parsed_count,
        internal_edges=internal_edge_count,
        explicit_internal_edges=explicit_internal_edge_count,
        symbol_internal_edges=symbol_internal_edge_count,
        parse_issues=len(parse_issues),
        total_loc=total_loc,
        total_sloc=total_sloc,
        total_comment_lines=total_comment_lines,
        total_blank_lines=total_blank_lines,
        comment_pct=comment_pct_total,
        total_ms=int((time.perf_counter() - t0) * 1000),
    )

    return idx, parsed_by_rel


def save_folder_index(idx: FolderIndex, path: Path) -> None:
    files_payload: Dict[str, dict] = {}

    for k, v in idx.files.items():
        rec = asdict(v)
        if "id" in rec:
            rec["folder_id"] = rec.pop("id")
        files_payload[k] = rec

    atomic_write_json(
        {"schema": idx.schema, "meta": dict(idx.meta), "files": files_payload},
        Path(path),
    )


def load_folder_index(path: Path) -> FolderIndex:
    data = load_json_bytes(Path(path))
    if not isinstance(data, dict) or not data:
        raise ValueError(f"Invalid or empty FolderIndex JSON at {path}")

    if data.get("schema") != FOLDER_INDEX_SCHEMA:
        raise ValueError(f"Unexpected schema: {data.get('schema')}")

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

        imports_all = tuple(rec.get("imports_all", []) or ())
        imports_internal = tuple(rec.get("imports_internal", []) or ())
        imports_runtime = tuple(rec.get("imports_runtime") or imports_internal)
        imports_runtime_internal = tuple(
            rec.get("imports_runtime_internal")
            or rec.get("imports_runtime")
            or imports_internal
        )
        symbol_internal = tuple(rec.get("symbol_internal", []) or ())

        files[file_id] = FileEntry(
            id=file_id,
            file=file_path,
            name=str(rec.get("name", "")),
            parse_status=str(rec.get("parse_status", "ok")),
            imports_all=imports_all,
            imports_internal=imports_internal,
            imports_runtime=imports_runtime,
            imports_runtime_internal=imports_runtime_internal,
            symbol_internal=symbol_internal,
            loc=rec.get("loc"),
            sloc=rec.get("sloc"),
            comment_lines=rec.get("comment_lines"),
            blank_lines=rec.get("blank_lines"),
            comment_pct=rec.get("comment_pct"),
            size_bytes=rec.get("size_bytes"),
            mtime=rec.get("mtime"),
            hash=rec.get("hash"),
            import_style_counts=rec.get("import_style_counts", {}) or {},
            error_snippet=rec.get("error_snippet"),
            eligible=bool(rec.get("eligible", True)),
        )

    return FolderIndex(schema=data["schema"], meta=data.get("meta", {}), files=files)
