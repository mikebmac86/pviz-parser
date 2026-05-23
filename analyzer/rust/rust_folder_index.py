from __future__ import annotations

"""
FolderIndex — deterministic snapshot of a workspace's Rust files.

Following Java analyzer pattern:
  - Parallel parsing with ProcessPoolExecutor
  - Caches RustParsedFile objects for reuse in build_artifacts.py
  - Uses rustparser_cli for accurate parsing
  - Eliminates double-parsing bottleneck

Schema: FOLDER_INDEX_SCHEMA (analyzer_store.types)

Design mirrors analyzer/java:
  - Deterministic: stable sorting, deduped lists
  - Error tolerant: parse_status captures failures; facts may be partial
  - use_statements: from rustparser_cli
  - use_internal: recomputed after normalization, based on internal module index
  - IDs are *file ids* (repo-relative POSIX path) for FileEntry.id and FolderIndex.files keys
  - FileEntry.file remains repo-relative POSIX file path
  - Atomic writes through analyzer_store.io_utils.atomic_write_json

v1.1 CLI path fallback
----------------------
If cfg.rustparser_cli_path is missing/empty, build_folder_index() now checks:

  - PVIZ_RUSTPARSER_CLI
  - PVIZ_RUSTPARSER_CLI_PATH
  - RUSTPARSER_CLI

This keeps the bucket/config layer unchanged while allowing the parser binary to
be injected from the same shell that runs pviz_cli.py.
"""

from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple
from concurrent.futures import ProcessPoolExecutor, as_completed
import time
import os

from analyzer_store.types import FOLDER_INDEX_SCHEMA, FileEntry, FolderIndex
from analyzer_store.io_utils import iso_utc, blake2s_short, atomic_write_json, load_json_bytes

# Canonical helpers (project)
from adapters.canonical import repo_rel, to_posix

# Single source of truth for Rust canonicalization/resolution
from analyzer.rust.rust_canonical import (
    normalize_rust_use_path,
    resolve_rust_use,
    derive_module_from_path,
)

# Rust parsing
from analyzer.rust.rust_parse import parse_rust_file
from analyzer.rust.rust_parse import RustParsedFile

# Diagnostics logging
try:
    from diagnostics.logging import log_event
except Exception:  # pragma: no cover
    def log_event(*_a, **_k):
        return


# ---------------------------------------------------------------------------
# DIAGNOSTIC ONLY prints
# ---------------------------------------------------------------------------

def _diag_enabled(cfg: object) -> bool:
    """
    Diagnostic print gate:
      - cfg.rust_diag == True, OR
      - env PVIZ_RUST_DIAG in {"1","true","yes","on"}
    """
    try:
        if bool(getattr(cfg, "rust_diag", False)):
            return True
    except Exception:
        pass
    v = os.getenv("PVIZ_RUST_DIAG", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _diag_print(cfg: object, name: str, **k) -> None:
    """
    DIAGNOSTIC ONLY prints.

    Intentionally does NOT go through log_event/structured logging so you can
    grep these easily and remove later.

    NOTE:
      Currently left ungated to match the active debugging version you provided.
      Restore the _diag_enabled(cfg) guard once Rust parser path wiring is stable.
    """
    # if not _diag_enabled(cfg):
    #     return
    parts = " ".join(f"{kk}={k[kk]!r}" for kk in sorted(k.keys()))
    print(f"[RUST_DIAG] {name} {parts}".rstrip())


def _diag_sample_map(m: Mapping, *, max_items: int = 5) -> Dict[str, object]:
    """Best-effort map sampling for diagnostics."""
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
            v0 = m[keys[0]]  # type: ignore[index]
            out["value_type_sample"] = type(v0).__name__
        out["ok"] = True
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iter_rust_files(root: Path, *, include_tests: bool = True) -> List[Path]:
    """Deterministic, de-duped list of .rs files under root."""
    root = root.resolve()
    out: List[Path] = []
    try:
        for p in root.rglob("*.rs"):
            if not p.is_file():
                continue
            if not include_tests:
                s = to_posix(str(p))
                if "/test/" in s.lower() or "/tests/" in s.lower():
                    continue
            out.append(p.resolve())
    except Exception:
        pass
    return sorted(set(out), key=lambda p: to_posix(str(p)))


def _count_rust_metrics(text: str) -> Tuple[int, int, int, int, Optional[float]]:
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


def _chunk_files(files: List[Path], max_workers: int) -> List[List[Path]]:
    """
    Chunking by count (simple + deterministic).
    NOTE: if you see imbalanced worker times, consider size-weighted chunking.
    """
    if not files or max_workers <= 1:
        return [files]

    # small chunks give better load balancing for "spawn CLI per file"
    # while staying deterministic.
    target_chunks = max_workers * 4
    chunk_size = max(1, len(files) // target_chunks)
    chunks: List[List[Path]] = []
    for i in range(0, len(files), chunk_size):
        chunk = files[i:i + chunk_size]
        if chunk:
            chunks.append(chunk)
    return chunks


def _parse_hash_to_int(hash_str: str) -> Optional[int]:
    """
    blake2s_short() may return decimal or hex depending on implementation.
    We accept both.
    """
    s = (hash_str or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except Exception:
        pass

    # hex-like
    try:
        ss = s.lower()
        if ss.startswith("0x"):
            ss = ss[2:]
        return int(ss, 16)
    except Exception:
        return None


def _resolve_rustparser_cli_path(cfg: object) -> Optional[str]:
    """
    Resolve rustparser_cli path from cfg first, then environment.

    Order:
      1. cfg.rustparser_cli_path
      2. PVIZ_RUSTPARSER_CLI
      3. PVIZ_RUSTPARSER_CLI_PATH
      4. RUSTPARSER_CLI

    Returns:
      String path if the value points to an existing file, otherwise None.

    Side effect:
      If an env var provides a valid file, cfg.rustparser_cli_path is updated
      best-effort so downstream diagnostics/builders see the same value.
    """
    raw = getattr(cfg, "rustparser_cli_path", None)

    if raw:
        try:
            p = Path(str(raw)).expanduser()
            if p.exists() and p.is_file():
                return str(p)
        except Exception:
            pass

    cli_env = (
        os.getenv("PVIZ_RUSTPARSER_CLI")
        or os.getenv("PVIZ_RUSTPARSER_CLI_PATH")
        or os.getenv("RUSTPARSER_CLI")
        or ""
    ).strip()

    if not cli_env:
        return None

    try:
        p = Path(cli_env).expanduser()
        if p.exists() and p.is_file():
            resolved = str(p)
            try:
                setattr(cfg, "rustparser_cli_path", resolved)
            except Exception:
                pass
            return resolved
    except Exception:
        return None

    return None


def _normalize_use_with_context(use_path: str, module_path: Optional[str]) -> str:
    """
    Best-effort rewrite for relative Rust imports:
      - self::x   => <module_path>::x
      - super::x  => <parent(module_path)>::x
      - crate::x  => crate::x (kept)
      - ::x       => x (absolute from crate root/external; keep without leading ::)

    This is a *string rewrite only*; it does not require a compiler.
    """
    s = (use_path or "").strip()
    if not s:
        return ""

    # strip leading absolute qualifier
    if s.startswith("::"):
        s = s.lstrip(":")
        return s

    if not module_path:
        return s

    mp = module_path.strip()
    if not mp:
        return s

    # normalize repeated :: from heuristic oddities
    while ":::" in s:
        s = s.replace(":::", "::")

    if s.startswith("self::"):
        rest = s[len("self::"):]
        if rest:
            return f"{mp}::{rest}"
        return mp

    if s.startswith("super::"):
        rest = s[len("super::"):]
        # compute parent module
        parts = mp.split("::")
        if len(parts) > 1:
            parent = "::".join(parts[:-1])
        else:
            parent = mp
        if rest:
            return f"{parent}::{rest}"
        return parent

    # crate::, std::, etc: leave as-is
    return s


def _parse_files_batch(
    repo_root_str: str,
    paths: List[Path],
    max_bytes: Optional[int],
    cli_path: Optional[str],
) -> Dict[str, Tuple[RustParsedFile, int, int, int, int, Optional[float], int, str, str]]:
    """
    Worker function: Parse a batch of Rust files.

    Returns dict of:
      file_id (repo-relative posix) ->
      (parsed_file, loc, sloc, comment_lines, blank_lines, comment_pct,
       size_bytes, mtime_iso, hash_str)
    """
    repo_root = Path(repo_root_str)
    results: Dict[str, Tuple[RustParsedFile, int, int, int, int, Optional[float], int, str, str]] = {}

    for path in paths:
        try:
            rel_file = repo_rel(path, repo_root)
            file_id = to_posix(rel_file)

            # Read file for metadata
            data = path.read_bytes()
            short_hash = blake2s_short(data)
            size_bytes = len(data)

            try:
                mtime_iso = iso_utc(path.stat().st_mtime)
            except Exception:
                mtime_iso = ""

            # Check size eligibility
            if isinstance(max_bytes, int) and max_bytes > 0 and size_bytes > max_bytes:
                pf = RustParsedFile(
                    file_path=path,
                    ok=False,
                    parse_status="file_too_large",
                    error=f"file_too_large:{size_bytes}",
                )
                results[file_id] = (pf, 0, 0, 0, 0, None, size_bytes, mtime_iso, str(short_hash))
                continue

            # Decode text (for loc/sloc only)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("latin-1", errors="replace")

            loc, sloc, comment_lines, blank_lines, comment_pct = _count_rust_metrics(text)

            pf = parse_rust_file(path, cli_path=Path(cli_path) if cli_path else None)

            results[file_id] = (
                pf,
                loc,
                sloc,
                comment_lines,
                blank_lines,
                comment_pct,
                size_bytes,
                mtime_iso,
                str(short_hash),
            )

        except Exception as e:
            try:
                rel_file = repo_rel(path, repo_root)
                file_id = to_posix(rel_file)
            except Exception:
                file_id = to_posix(str(path))

            pf = RustParsedFile(
                file_path=path,
                ok=False,
                parse_status="error",
                error=f"{type(e).__name__}:{str(e)[:100]}",
            )
            results[file_id] = (pf, 0, 0, 0, 0, None, 0, "", "")

    return results


def _build_module_indexes(
    *,
    file_ids: List[str],
    parsed_by_id: Mapping[str, RustParsedFile],
    repo_root: Path,
) -> Tuple[Dict[str, str], Dict[str, Set[str]]]:
    """
    Build:
      - module_to_file: module path -> file_id (repo-relative posix path)
      - crate_to_files: crate name -> {file_id,...}

    Source of truth:
      - module paths come from parse_rust_file() (rustparser_cli).
      - if module_path missing, fall back to derive_module_from_path(file_id, repo_root).
    """
    module_to_file: Dict[str, str] = {}
    crate_to_files: Dict[str, Set[str]] = {}

    for fid in file_ids:
        pf = parsed_by_id.get(fid)

        mod_path: Optional[str] = None
        if pf is not None and isinstance(pf.module_path, str) and pf.module_path.strip():
            mod_path = pf.module_path.strip()
        if not mod_path:
            mod_path = derive_module_from_path(fid, repo_root)

        crate_name: Optional[str] = None
        if mod_path:
            if mod_path.startswith("crate::"):
                crate_name = "crate"
            elif "::" in mod_path:
                crate_name = mod_path.split("::", 1)[0]
            else:
                crate_name = mod_path

        if crate_name:
            crate_to_files.setdefault(crate_name, set()).add(fid)

        if mod_path and mod_path not in module_to_file:
            module_to_file[mod_path] = fid

    return module_to_file, crate_to_files


def _build_mod_decl_internal_edges(
    *,
    file_ids: List[str],
    parsed_by_id: Mapping[str, RustParsedFile],
    module_to_file: Mapping[str, str],
    repo_root: Path,
) -> Dict[str, Tuple[str, ...]]:
    """
    Turn `mod foo;` declarations into additional internal edges.

    Without a compiler, best-effort:
      - Use current file's module_path (or derive fallback)
      - If declaration is inline (mod foo { ... }), do nothing
      - For "mod foo;" => edge to module "<current_module>::foo" if present in module_to_file
    """
    out: Dict[str, Tuple[str, ...]] = {}

    for fid in file_ids:
        pf = parsed_by_id.get(fid)
        if pf is None:
            out[fid] = ()
            continue

        mod_path = None
        if isinstance(getattr(pf, "module_path", None), str) and pf.module_path.strip():
            mod_path = pf.module_path.strip()
        if not mod_path:
            mod_path = derive_module_from_path(fid, repo_root)

        decls = getattr(pf, "mod_declarations", None) or []
        edges: Set[str] = set()

        for d in decls:
            try:
                # skip inline mods: they live inside this file
                if bool(getattr(d, "is_inline", False)):
                    continue
                name = getattr(d, "name", None)
                if not isinstance(name, str) or not name.strip():
                    continue
                if not mod_path:
                    continue
                target_mod = f"{mod_path}::{name.strip()}"
                target_fid = module_to_file.get(target_mod)
                if target_fid:
                    edges.add(to_posix(target_fid))
            except Exception:
                continue

        out[fid] = tuple(sorted(edges))

    return out


# ---------------------------------------------------------------------------
# Public API (following Java pattern)
# ---------------------------------------------------------------------------

def build_folder_index(
    root: Path,
    cfg,
    files: Optional[Sequence[Path]] = None,
    *,
    parsed_cache: Optional[Dict[str, RustParsedFile]] = None,
) -> Tuple[FolderIndex, Dict[str, RustParsedFile]]:
    """
    Build FolderIndex for Rust with parallel parsing and caching.

    Args:
        root: Repository root path
        cfg: Configuration object
        files: Optional pre-filtered file list for bucket mode
        parsed_cache: Optional pre-parsed files (for avoiding re-parsing)

    Returns:
        (FolderIndex, parsed_cache_dict)

    The returned parsed_cache can be passed to build_nodefacts_from_folder_index()
    to avoid re-parsing files.

    Config options:
        cfg.max_file_bytes: Size limit for files
        cfg.max_workers: Parallelization (default: cpu_count)
        cfg.rustparser_cli_path: Path to rustparser_cli binary
        cfg.include_tests: include tests/ directories (default True)
        cfg.rust_diag: enable _diag_print (default False)
        cfg.rust_include_mod_decl_edges_internal: include `mod foo;` edges as internal (default True)
    """
    t_start = time.perf_counter()
    log_event("RUST_FOLDER_INDEX:build_enter", root=str(root))

    root = Path(root).resolve()

    # publish repo_root for downstream cache lookups (abs keys in build_artifacts edges path)
    try:
        setattr(cfg, "repo_root", str(root))
    except Exception:
        pass

    _diag_print(
        cfg,
        "build_enter",
        root=str(root),
        cfg_type=type(cfg).__name__,
        has_parsed_cache=bool(parsed_cache),
        parsed_cache_len=(len(parsed_cache) if isinstance(parsed_cache, dict) else None),
        include_tests=bool(getattr(cfg, "include_tests", True)),
    )

    # Determine file list based on mode
    if files is None:
        rust_files = _iter_rust_files(root, include_tests=getattr(cfg, "include_tests", True))
        log_event("RUST_FOLDER_INDEX:iter_files_end", root=str(root), count=len(rust_files), mode="full_repo")
        _diag_print(cfg, "iter_files_end", mode="full_repo", count=len(rust_files))
        if rust_files:
            _diag_print(cfg, "iter_files_sample", first=str(rust_files[0]), last=str(rust_files[-1]))
    else:
        rust_files_filtered: List[Path] = []
        skipped_outside = 0
        skipped_non_rust = 0

        for p in files:
            try:
                ap = p.resolve()
            except Exception:
                continue
            if not ap.exists() or not ap.is_file():
                continue
            if ap.suffix.lower() != ".rs":
                skipped_non_rust += 1
                continue
            try:
                ap.relative_to(root)
                rust_files_filtered.append(ap)
            except Exception:
                skipped_outside += 1

        rust_files = sorted(set(rust_files_filtered), key=lambda p: to_posix(str(p)))
        log_event(
            "RUST_FOLDER_INDEX:iter_files_end",
            root=str(root),
            count=len(rust_files),
            mode="bucket",
            skipped_outside=skipped_outside,
            skipped_non_rust=skipped_non_rust,
        )
        _diag_print(
            cfg,
            "iter_files_end",
            mode="bucket",
            count=len(rust_files),
            skipped_outside=skipped_outside,
            skipped_non_rust=skipped_non_rust,
        )
        if rust_files:
            _diag_print(cfg, "iter_files_sample", first=str(rust_files[0]), last=str(rust_files[-1]))

    mb = getattr(cfg, "max_file_bytes", None)
    if not isinstance(mb, int) or mb <= 0:
        mb = getattr(cfg, "max_bytes_per_file", None)
    max_bytes = mb if isinstance(mb, int) and mb > 0 else None

    cli_path = _resolve_rustparser_cli_path(cfg)

    _diag_print(
        cfg,
        "config",
        max_bytes=max_bytes,
        cli_path=str(cli_path) if cli_path else None,
        cli_exists=(Path(str(cli_path)).exists() if cli_path else False),
        env_pviz_rustparser_cli=os.getenv("PVIZ_RUSTPARSER_CLI", ""),
        env_pviz_rustparser_cli_path=os.getenv("PVIZ_RUSTPARSER_CLI_PATH", ""),
        env_rustparser_cli=os.getenv("RUSTPARSER_CLI", ""),
        cfg_max_workers=getattr(cfg, "max_workers", None),
    )

    # ---------------------------------------------------------------------------
    # PARALLEL PARSING (or use provided cache)
    # ---------------------------------------------------------------------------
    parsed_by_id: Dict[str, RustParsedFile] = {}
    file_metadata: Dict[str, Tuple[int, int, int, int, Optional[float], int, str, str]] = {}

    if parsed_cache:
        log_event("RUST_FOLDER_INDEX:using_cache", files=len(parsed_cache))
        _diag_print(cfg, "using_cache", **_diag_sample_map(parsed_cache))

        for path in rust_files:
            rel_file = repo_rel(path, root)
            file_id = to_posix(rel_file)

            # accept multiple key shapes (new + legacy) without breaking determinism
            cached: Optional[RustParsedFile] = None
            try:
                if file_id in parsed_cache:
                    cached = parsed_cache[file_id]
                elif rel_file in parsed_cache:
                    cached = parsed_cache[rel_file]
                else:
                    abs_key = str(path.resolve())
                    if abs_key in parsed_cache:
                        cached = parsed_cache[abs_key]
            except Exception:
                cached = None

            if cached is not None:
                parsed_by_id[file_id] = cached
                # Still need metadata
                try:
                    data = path.read_bytes()
                    size_bytes = len(data)
                    mtime_iso = iso_utc(path.stat().st_mtime)
                    short_hash = blake2s_short(data)
                    text = data.decode("utf-8", errors="replace")
                    loc, sloc, comment_lines, blank_lines, comment_pct = _count_rust_metrics(text)
                    file_metadata[file_id] = (
                        loc,
                        sloc,
                        comment_lines,
                        blank_lines,
                        comment_pct,
                        size_bytes,
                        mtime_iso,
                        str(short_hash),
                    )
                except Exception:
                    file_metadata[file_id] = (0, 0, 0, 0, None, 0, "", "")

        _diag_print(cfg, "using_cache_done", parsed=len(parsed_by_id), metadata=len(file_metadata))

    else:
        cpu_count = os.cpu_count() or 1
        cfg_mw = getattr(cfg, "max_workers", None)

        if isinstance(cfg_mw, int) and cfg_mw > 0:
            max_workers = cfg_mw
        else:
            max_workers = cpu_count

        max_workers = min(max(1, max_workers), max(1, len(rust_files)))

        _diag_print(cfg, "parse_plan", cpu_count=cpu_count, max_workers=max_workers, files=len(rust_files))

        if max_workers <= 1 or len(rust_files) < 10:
            log_event("RUST_FOLDER_INDEX:parse_serial", files=len(rust_files))
            t_parse = time.perf_counter()

            batch_results = _parse_files_batch(
                str(root),
                rust_files,
                max_bytes,
                str(cli_path) if cli_path else None,
            )
            for file_id, (
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
                parsed_by_id[file_id] = pf
                file_metadata[file_id] = (
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
                "RUST_FOLDER_INDEX:parse_serial_done",
                files=len(rust_files),
                ms=int((time.perf_counter() - t_parse) * 1000),
            )

            ok = sum(1 for pf in parsed_by_id.values() if getattr(pf, "ok", False))
            err = len(parsed_by_id) - ok
            _diag_print(
                cfg,
                "parse_serial_done",
                ms=int((time.perf_counter() - t_parse) * 1000),
                parsed=len(parsed_by_id),
                ok=ok,
                err=err,
            )

        else:
            log_event("RUST_FOLDER_INDEX:parse_parallel", files=len(rust_files), workers=max_workers)
            t_parse = time.perf_counter()

            chunks = _chunk_files(rust_files, max_workers)
            _diag_print(cfg, "parse_parallel_begin", chunks=len(chunks), workers=max_workers)

            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        _parse_files_batch,
                        str(root),
                        chunk,
                        max_bytes,
                        str(cli_path) if cli_path else None,
                    ): idx
                    for idx, chunk in enumerate(chunks)
                }

                completed = 0
                for future in as_completed(futures):
                    batch_idx = futures[future]
                    try:
                        batch_results = future.result()
                        for file_id, (
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
                            parsed_by_id[file_id] = pf
                            file_metadata[file_id] = (
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
                            "RUST_FOLDER_INDEX:batch_done",
                            batch=batch_idx,
                            batch_size=len(chunks[batch_idx]),
                            completed=completed,
                            total=len(rust_files),
                        )
                        _diag_print(
                            cfg,
                            "batch_done",
                            batch=batch_idx,
                            batch_size=len(chunks[batch_idx]),
                            completed=completed,
                            total=len(rust_files),
                        )
                    except Exception as e:
                        log_event(
                            "RUST_FOLDER_INDEX:batch_error",
                            batch=batch_idx,
                            error=f"{type(e).__name__}:{str(e)[:100]}",
                        )
                        _diag_print(cfg, "batch_error", batch=batch_idx, exc_type=type(e).__name__, exc=str(e)[:300])

            log_event(
                "RUST_FOLDER_INDEX:parse_parallel_done",
                files=len(rust_files),
                parsed=len(parsed_by_id),
                ms=int((time.perf_counter() - t_parse) * 1000),
            )

            ok = sum(1 for pf in parsed_by_id.values() if getattr(pf, "ok", False))
            err = len(parsed_by_id) - ok
            _diag_print(
                cfg,
                "parse_parallel_done",
                ms=int((time.perf_counter() - t_parse) * 1000),
                parsed=len(parsed_by_id),
                ok=ok,
                err=err,
                metadata=len(file_metadata),
            )

    # ---------------------------------------------------------------------------
    # BUILD FILE ENTRIES
    # ---------------------------------------------------------------------------
    files_map: Dict[str, FileEntry] = {}
    parse_issues: List[str] = []
    file_ids: List[str] = []

    if len(parsed_by_id) < len(rust_files):
        _diag_print(cfg, "parsed_mismatch", rust_files=len(rust_files), parsed=len(parsed_by_id))

    for path in rust_files:
        rel_file = repo_rel(path, root)
        file_id = to_posix(rel_file)
        file_ids.append(file_id)

        pf = parsed_by_id.get(file_id)
        if not pf:
            pf = RustParsedFile(
                file_path=path,
                ok=False,
                parse_status="missing_from_parsed_cache",
                error="missing_from_parsed_cache",
            )
            parsed_by_id[file_id] = pf

        metadata = file_metadata.get(file_id, (0, 0, 0, 0, None, 0, "", ""))
        loc, sloc, comment_lines, blank_lines, comment_pct, size_bytes, mtime_iso, hash_str = metadata

        # preserve original behavior, but ensure failures can't silently be "ok"
        parse_status = getattr(pf, "parse_status", None) or "ok"
        if (parse_status == "ok") and (not bool(getattr(pf, "ok", False))) and (getattr(pf, "error", None) or ""):
            parse_status = "error"

        err_snip = pf.error

        if not getattr(pf, "ok", False):
            parse_issues.append(f"{file_id}: {err_snip or 'parse_error'}")

        # Extract + normalize use statements
        # - best-effort relative rewriting based on pf.module_path
        use_specs: List[str] = []
        if getattr(pf, "use_statements", None):
            seen: Set[str] = set()
            cur_mod = getattr(pf, "module_path", None)
            for use_stmt in pf.use_statements:
                raw_path = getattr(use_stmt, "path", "") or ""
                raw_path = _normalize_use_with_context(raw_path, cur_mod)

                spec = normalize_rust_use_path(
                    raw_path,
                    is_glob=bool(getattr(use_stmt, "is_glob", False)),
                    is_pub=bool(getattr(use_stmt, "is_pub", False)),
                )
                if spec and spec not in seen:
                    seen.add(spec)
                    use_specs.append(spec)

        # Determinism: sort specs
        imports_all = tuple(sorted(use_specs))

        eligible = True
        if isinstance(max_bytes, int) and max_bytes > 0 and size_bytes > max_bytes:
            eligible = False

        name = Path(file_id).name
        hash_int = _parse_hash_to_int(hash_str)

        entry = FileEntry(
            id=file_id,
            file=file_id,  # keep posix-normalized
            name=name,
            parse_status=parse_status,
            imports_all=imports_all,
            imports_internal=tuple(),  # recomputed later
            imports_runtime=tuple(),
            imports_runtime_internal=tuple(),
            loc=loc if loc else None,
            sloc=sloc if sloc else None,
            comment_lines=comment_lines if comment_lines else None,
            blank_lines=blank_lines if blank_lines else None,
            comment_pct=comment_pct,
            size_bytes=size_bytes if size_bytes else None,
            mtime=mtime_iso if mtime_iso else None,
            hash=hash_int,
            import_style_counts={},
            error_snippet=err_snip,
            eligible=eligible,
        )
        # IMPORTANT: FolderIndex.files keys are file_id (posix repo-rel)
        files_map[file_id] = entry

    with_uses = sum(1 for fe in files_map.values() if fe.imports_all)
    _diag_print(cfg, "file_entries_built", entries=len(files_map), with_uses=with_uses, parse_issues=len(parse_issues))

    # ---------------------------------------------------------------------------
    # BUILD MODULE INDEXES
    # ---------------------------------------------------------------------------
    file_ids_sorted = sorted(set(file_ids), key=lambda s: to_posix(s))
    module_to_file, crate_to_files = _build_module_indexes(
        file_ids=file_ids_sorted,
        parsed_by_id=parsed_by_id,
        repo_root=root,
    )

    # Publish maps for downstream builders (edges external resolution; module-id-space helpers)
    try:
        setattr(cfg, "rust_module_to_file", dict(module_to_file))
        setattr(cfg, "rust_crate_to_files", {k: tuple(sorted(v)) for k, v in crate_to_files.items()})
    except Exception:
        pass

    _diag_print(
        cfg,
        "module_indexes_built",
        module_index=len(module_to_file),
        crate_index=len(crate_to_files),
    )
    if module_to_file:
        items = list(module_to_file.items())[:5]
        _diag_print(cfg, "module_index_sample", sample=[f"{k} -> {v}" for (k, v) in items])
    if crate_to_files:
        ck = next(iter(crate_to_files.keys()))
        _diag_print(cfg, "crate_index_sample", crate=str(ck), file_count=len(crate_to_files.get(ck) or set()))

    # Additional internal edges from `mod foo;` (configurable, default True)
    mod_decl_edges_by_file: Dict[str, Tuple[str, ...]] = {}
    if bool(getattr(cfg, "rust_include_mod_decl_edges_internal", True)):
        mod_decl_edges_by_file = _build_mod_decl_internal_edges(
            file_ids=file_ids_sorted,
            parsed_by_id=parsed_by_id,
            module_to_file=module_to_file,
            repo_root=root,
        )
    else:
        mod_decl_edges_by_file = {fid: () for fid in file_ids_sorted}

    # ---------------------------------------------------------------------------
    # RESOLVE USE_INTERNAL
    # ---------------------------------------------------------------------------
    files2: Dict[str, FileEntry] = {}
    internal_edge_count = 0

    # Resolution diagnostics
    resolved_internal_specs_total = 0
    resolved_internal_files_total = 0
    rr_reason_counts: Dict[str, int] = {}

    for fid, fe in files_map.items():
        mapped: List[str] = []

        for spec in (fe.imports_all or ()):
            resolved_internal_specs_total += 1
            rr_list = resolve_rust_use(
                spec,
                module_to_file=module_to_file,
                crate_to_files=crate_to_files,
            )
            for rr in rr_list:
                reason = getattr(rr, "reason", "") or ""
                if reason:
                    rr_reason_counts[reason] = rr_reason_counts.get(reason, 0) + 1

                if rr.kind == "internal" and rr.resolved:
                    mapped.append(to_posix(rr.resolved))

        # Add module-declaration internal edges
        extra_mod_edges = mod_decl_edges_by_file.get(fid, ())
        if extra_mod_edges:
            mapped.extend(list(extra_mod_edges))

        internal_files = tuple(sorted(set(mapped)))
        resolved_internal_files_total += len(internal_files)
        internal_edge_count += len(internal_files)

        if internal_files == fe.imports_internal:
            files2[fid] = fe
        else:
            files2[fid] = FileEntry(
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
                import_style_counts=fe.import_style_counts,
                error_snippet=fe.error_snippet,
                eligible=fe.eligible,
            )

    files_map = files2

    with_internal = sum(1 for fe in files_map.values() if fe.imports_internal)
    _diag_print(
        cfg,
        "use_internal_resolved",
        internal_edge_count=int(internal_edge_count),
        files_with_internal=with_internal,
        specs_considered=resolved_internal_specs_total,
        internal_files_total=resolved_internal_files_total,
        rr_reason_counts=dict(sorted(rr_reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))) if rr_reason_counts else {},
    )

    # ---------------------------------------------------------------------------
    # BUILD FOLDER INDEX
    # ---------------------------------------------------------------------------
    eligible_count = sum(1 for v in files_map.values() if bool(getattr(v, "eligible", True)))
    parsed_count = sum(1 for fid in files_map.keys() if bool(getattr(parsed_by_id.get(fid), "ok", False)))
    total_loc = sum(int(getattr(fe, "loc", 0) or 0) for fe in files_map.values())
    total_sloc = sum(int(getattr(fe, "sloc", 0) or 0) for fe in files_map.values())
    total_comment_lines = sum(int(getattr(fe, "comment_lines", 0) or 0) for fe in files_map.values())
    total_blank_lines = sum(int(getattr(fe, "blank_lines", 0) or 0) for fe in files_map.values())
    comment_pct_total = (total_comment_lines / total_loc) if total_loc else None

    meta: Dict[str, str] = {
        "created": str(iso_utc()),
        "root": str(to_posix(root.resolve())),
        "language": "rust",
        "eligible_count": str(int(eligible_count)),
        "parsed_count": str(int(parsed_count)),
        "internal_edge_count": str(int(internal_edge_count)),
        "total_loc": str(int(total_loc)),
        "total_sloc": str(int(total_sloc)),
        "total_comment_lines": str(int(total_comment_lines)),
        "total_blank_lines": str(int(total_blank_lines)),
        "comment_pct": str(comment_pct_total if comment_pct_total is not None else ""),
        "parse_issues_count": str(len(parse_issues)),
        "module_index_count": str(len(module_to_file)),
        "crate_index_count": str(len(crate_to_files)),
    }

    # add a compact summary of resolution failure reasons (if any)
    if rr_reason_counts:
        # keep it small / stable: top 5
        top = sorted(rr_reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        meta["resolve_reason_top"] = ",".join(f"{k}:{v}" for k, v in top)

    idx = FolderIndex(schema=FOLDER_INDEX_SCHEMA, meta=meta, files=files_map)

    # Publish parse cache + maps for downstream builders
    try:
        setattr(cfg, "rust_parse_cache", dict(parsed_by_id))
    except Exception:
        pass

    total_time = time.perf_counter() - t_start
    log_event(
        "RUST_FOLDER_INDEX:build_return",
        root=str(root),
        eligible=eligible_count,
        parsed=parsed_count,
        internal_edges=internal_edge_count,
        parse_issues=len(parse_issues),
        total_loc=total_loc,
        total_sloc=total_sloc,
        total_comment_lines=total_comment_lines,
        total_blank_lines=total_blank_lines,
        comment_pct=comment_pct_total,
        module_index=len(module_to_file),
        crate_index=len(crate_to_files),
        total_ms=int(total_time * 1000),
    )

    _diag_print(
        cfg,
        "build_return",
        total_ms=int(total_time * 1000),
        eligible=int(eligible_count),
        parsed=int(parsed_count),
        parse_issues=len(parse_issues),
        total_loc=total_loc,
        total_sloc=total_sloc,
        total_comment_lines=total_comment_lines,
        total_blank_lines=total_blank_lines,
        comment_pct=comment_pct_total,
        internal_edge_count=int(internal_edge_count),
        module_index=len(module_to_file),
        crate_index=len(crate_to_files),
        parsed_cache_returned=len(parsed_by_id),
    )
    if parse_issues:
        _diag_print(cfg, "parse_issues_sample", sample=parse_issues[:3])

    # Return parsed cache keyed by file_id (posix), consistent with FolderIndex.files keys
    return idx, parsed_by_id


def save_folder_index(idx: FolderIndex, path: Path) -> None:
    """
    Serialize FolderIndex using the same on-disk convention as Java/Go/Python:
      - record uses 'folder_id' (renamed from 'id')
    """
    files_payload: Dict[str, dict] = {}
    for k, v in idx.files.items():
        rec = asdict(v)
        if "id" in rec:
            rec["folder_id"] = rec.pop("id")
        files_payload[k] = rec

    payload = {"schema": idx.schema, "meta": dict(idx.meta), "files": files_payload}
    atomic_write_json(payload, Path(path))


def load_folder_index(path: Path) -> FolderIndex:
    """
    Load FolderIndex.

    Accepts either:
      - 'folder_id' (preferred), or
      - legacy 'id' (fallback)
    """
    data = load_json_bytes(Path(path))
    if not isinstance(data, dict) or not data:
        raise ValueError(f"Invalid or empty FolderIndex JSON at {path}")

    schema = data.get("schema")
    if schema != FOLDER_INDEX_SCHEMA:
        raise ValueError(f"Unexpected schema: {schema}")

    files: Dict[str, FileEntry] = {}
    files_raw: Mapping[str, Mapping[str, object]] = data.get("files", {})  # type: ignore[assignment]

    for key, rec in files_raw.items():
        fid = rec.get("folder_id") or rec.get("id")
        if not fid:
            raise KeyError(f"Missing 'folder_id' (or legacy 'id') in record for {key}")

        file_id = to_posix(str(fid))
        file_path = rec.get("file", file_id) or file_id

        imports_all_raw = rec.get("imports_all", []) or ()
        if isinstance(imports_all_raw, (list, tuple)):
            imports_all = tuple(imports_all_raw)
        else:
            imports_all = tuple()

        imports_internal_raw = rec.get("imports_internal", []) or ()
        if isinstance(imports_internal_raw, (list, tuple)):
            imports_internal = tuple(imports_internal_raw)
        else:
            imports_internal = tuple()

        files[file_id] = FileEntry(
            id=file_id,
            file=to_posix(str(file_path)),
            name=str(rec.get("name", "")),
            parse_status=str(rec.get("parse_status", "ok")),
            imports_all=imports_all,
            imports_internal=imports_internal,
            imports_runtime=tuple(rec.get("imports_runtime") or imports_internal),
            imports_runtime_internal=tuple(
                rec.get("imports_runtime_internal")
                or rec.get("imports_runtime")
                or imports_internal
            ),
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
            eligible=rec.get("eligible", True),
        )

    return FolderIndex(schema=data["schema"], meta=data.get("meta", {}), files=files)