#saas_analyzer/analyzer/java/build_artifacts.py
from __future__ import annotations

"""
Java artifact builders (FolderIndex -> NodeFacts + Edges).

UPDATED to:
  - Use parse cache when available (cfg.java_parse_cache) WITHOUT re-parsing.
  - Unify symbol parsing call-shape across cache/non-cache paths (cfg_payload).
  - Fix package-mode correctness by keying package map in **file-id space** (fe.id)
    and publishing the file->package map for downstream edge building.
  - Avoid O(n log n) re-sorts inside the per-file loop by aggregating symbol fields
    as sets and finalizing once at the end.
  - Optional enrichment plumbing for edges (extends/implements/annotations/module)
    if cached parse records contain those fields (no schema changes).

Schema constraints:
  - NodeFacts: nodefacts@v1.6 compatible (nodes dict, meta)
    - scc_id / scc_size are computed from per-language internal dependency graph
  - Edges: edges@v1 compatible (edges list of {src,dst,kind,confidence})
"""

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from concurrent.futures import ProcessPoolExecutor, as_completed
import time
import os

from analyzer_store.types import FileEntry, FolderIndex, NODEFACTS_SCHEMA
from adapters.canonical import to_posix

from analyzer.java.java_canonical import (
    normalize_java_import_spec,
    resolve_java_import,
)

from analyzer.java.java_nodefacts_symbols import NodeFactsSymbols, parse_symbols_for_nodefacts

# Diagnostics logging
try:
    from diagnostics.logging import log_event  # type: ignore
except Exception:  # pragma: no cover
    def log_event(*_a, **_k):
        return


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _stable_unique_strs(vals: Iterable[str]) -> Tuple[str, ...]:
    s: Set[str] = set()
    for v in vals or ():
        if not v:
            continue
        vv = str(v).strip()
        if vv:
            s.add(vv)
    return tuple(sorted(s))

def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _safe_nonnegative_int(value: Any, default: int = 0) -> int:
    return max(_safe_int(value, default), 0)


def _comment_pct(comment_lines: int, loc: int) -> Optional[float]:
    return (comment_lines / loc) if loc else None

def _upgrade_parse_status(cur: str, new: str) -> str:
    """
    Promote parse status across multiple files:

      ok > warn > error

    If any file is error => error
    else if any file is warn => warn
    else ok
    """
    cur = (cur or "").strip() or "ok"
    new = (new or "").strip() or "ok"
    rank = {"ok": 0, "warn": 1, "error": 2}
    return new if rank.get(new, 0) > rank.get(cur, 0) else cur

def _compute_scc_from_deps(
    node_ids: Iterable[str],
    deps_by_node: Dict[str, Set[str]],
) -> Tuple[Dict[str, str], Dict[str, int]]:
    """
    Compute strongly connected components from a node -> deps adjacency map.

    Returns:
      - scc_id_by_node: node_id -> component id
      - scc_size_by_id: component id -> component size
    """
    index = 0
    current_scc = 0
    stack: List[str] = []
    on_stack: Set[str] = set()
    indices: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    scc_id_by_node: Dict[str, str] = {}
    scc_size_by_id: Dict[str, int] = {}

    def strongconnect(v: str) -> None:
        nonlocal index, current_scc

        indices[v] = index
        lowlink[v] = index
        index += 1

        stack.append(v)
        on_stack.add(v)

        for w in deps_by_node.get(v, set()):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])

        if lowlink[v] == indices[v]:
            sid = str(current_scc)
            size = 0

            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc_id_by_node[w] = sid
                size += 1

                if w == v:
                    break

            scc_size_by_id[sid] = size
            current_scc += 1

    all_nodes: Set[str] = set(str(n) for n in node_ids if str(n).strip())
    for src, deps in deps_by_node.items():
        if src:
            all_nodes.add(src)
        for dst in deps or ():
            if dst:
                all_nodes.add(dst)

    for nid in sorted(all_nodes):
        if nid not in indices:
            strongconnect(nid)

    return scc_id_by_node, scc_size_by_id

def _java_name_from_id(node_id: str) -> str:
    """
    Name field for NodeFacts.
    - For file ids: basename
    - For package ids: last segment
    """
    s = str(node_id or "").strip()
    if not s:
        return ""
    if "/" in s:
        return s.rsplit("/", 1)[-1]
    if "." in s:
        return s.rsplit(".", 1)[-1]
    return s


def _stub_node(node_id: str) -> Dict[str, Any]:
    """
    Minimal nodefacts node entry for nodes referenced by edges but not present
    in the nodefacts aggregation map (defensive).
    """
    nid = str(node_id or "").strip()
    return {
        "id": nid,
        "name": _java_name_from_id(nid) or nid,
        "language": "java",
        "lang": "java",
        "imports": (),
        "exports": (),
        "classes": (),
        "functions": (),
        "globals": (),
        "gen_seq": 0,
        "importers_count": 0,
        "dependencies_count": 0,
        "scc_id": "",
        "scc_size": 1,
        "file": "",
        "hash": None,
        "loc": None,
        "sloc": None,
        "comment_lines": None,
        "blank_lines": None,
        "comment_pct": None,
        "file_count": 0,
        "size_bytes": None,
        "mtime": None,
        "du": None,
        "dd": None,
        "parse_status": "warn",
        "crosstalk_candidates_java_v1": (),
        # java enrichment fields (optional)
        "package": None,
        "declared_types": (),
        "declared_types_fq": (),
        "public_exports": (),
        "annotations": (),
        "imports_all_raw": (),
        "imports_external": (),
        "language_facts": {},
    }


def _java_nf_cfg_payload(cfg: Any) -> Dict[str, Any]:
    """
    Build a small, picklable cfg payload for worker processes.

    Important: do NOT pass your full cfg object into ProcessPoolExecutor
    on Windows; it may not be picklable and can cause hangs.

    Also used for cache-path symbol extraction to keep behavior consistent.
    """
    return {
        "max_file_bytes": getattr(cfg, "max_file_bytes", None),
        "max_bytes_per_file": getattr(cfg, "max_bytes_per_file", None),
        "include_tests": getattr(cfg, "include_tests", None),
        # optional java knobs that symbol parser may consult
        "java_symbol_detail": getattr(cfg, "java_symbol_detail", None),
    }


def _chunk(seq: List[str], n: int) -> List[List[str]]:
    chunks: List[List[str]] = []
    base = len(seq) // n
    extra = len(seq) % n
    start = 0
    for i in range(n):
        size = base + (1 if i < extra else 0)
        if size <= 0:
            continue
        end = start + size
        chunks.append(seq[start:end])
        start = end
    return chunks


def _chunk_by_bytes(
    rel_files: List[str],
    *,
    repo_root: Path,
    max_workers: int,
    target_batch_bytes: int = 256_000,  # ~250 KB
) -> List[List[str]]:
    batches: List[List[str]] = [[]]
    cur_bytes = 0

    for rf in rel_files:
        try:
            size = (repo_root / rf).stat().st_size
        except Exception:
            size = 8_000  # conservative default

        if cur_bytes + size > target_batch_bytes and batches[-1]:
            batches.append([])
            cur_bytes = 0

        batches[-1].append(rf)
        cur_bytes += size

    # Ensure we don't exceed worker parallelism too badly
    if len(batches) < max_workers:
        return _chunk(rel_files, max_workers)

    return batches


def _java_nf_build_symbols_batch(
    repo_root_str: str,
    rel_files: List[str],
    cfg_payload: Dict[str, Any],
) -> Tuple[Dict[str, Optional[NodeFactsSymbols]], Dict[str, str], Dict[str, str]]:
    """
    Batch worker: parse symbols for rel_files and return:
      - syms_by_relfile
      - status_by_relfile
      - file_id_to_package (BEST-EFFORT; keyed by rel_file POSIX)
    """
    repo_root = Path(repo_root_str)

    syms_by_rel: Dict[str, Optional[NodeFactsSymbols]] = {}
    status_by_rel: Dict[str, str] = {}
    pkg_by_relposix: Dict[str, str] = {}

    for rel in rel_files:
        rel_file = str(rel or "").strip()
        if not rel_file:
            continue

        try:
            abs_path = (repo_root / rel_file).resolve(strict=False)
            syms = parse_symbols_for_nodefacts(abs_path, cfg_payload)
            st = getattr(syms, "parse_status", "ok") if syms is not None else "error"
        except Exception:
            syms, st = None, "error"

        syms_by_rel[rel_file] = syms
        status_by_rel[rel_file] = str(st or "ok")

        if syms is not None:
            try:
                pkg = str(getattr(syms, "package", "") or "").strip()
                relp = to_posix(rel_file)
                if pkg and relp:
                    pkg_by_relposix[relp] = pkg
            except Exception:
                pass

    return syms_by_rel, status_by_rel, pkg_by_relposix


def _is_like_parsed_file(x: object) -> bool:
    # We intentionally avoid isinstance checks that can fail across module reloads.
    # If it looks like a JavaParsedFile (has "file" or "path" and "ok"/"parse_status"), treat as such.
    try:
        return hasattr(x, "__dict__") and (hasattr(x, "file") or hasattr(x, "path"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# NodeFacts builder (from FolderIndex)
# ---------------------------------------------------------------------------

def build_nodefacts_from_folder_index(
    idx: FolderIndex,
    *,
    repo_root: Path,
    cfg: Any,
    module_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build NodeFacts from a Java FolderIndex.

    Config knobs:
      - cfg.java_node_id_space = "file" | "package"
      - cfg.java_nodefacts_symbols = "dispatcher" | "none"
      - cfg.java_max_workers (optional): overrides cfg.max_workers
      - cfg.java_parse_cache (NEW!): Dict[...] from folder_index phase
        - keys may be file-id, rel path, or posix rel path (we handle best-effort)
    """
    t0 = time.perf_counter()

    repo_root = Path(repo_root).resolve()
    files = getattr(idx, "files", {}) or {}

    # ---------------------------------------------------------------------
    # Config knobs (safe defaults)
    # ---------------------------------------------------------------------
    node_id_space = str(getattr(cfg, "java_node_id_space", "file") or "file").strip().lower()
    symbols_mode = str(getattr(cfg, "java_nodefacts_symbols", "dispatcher") or "dispatcher").strip().lower()

    if node_id_space == "package" and symbols_mode == "none":
        log_event("JAVA:nodefacts_package_mode_disabled", reason="symbols_mode=none")
        node_id_space = "file"

    # Aggregate file-space metrics up to node ids
    node_loc_total: Dict[str, int] = {}
    node_sloc_total: Dict[str, int] = {}
    node_comment_lines_total: Dict[str, int] = {}
    node_blank_lines_total: Dict[str, int] = {}
    node_file_count: Dict[str, int] = {}

    by_node: Dict[str, Dict[str, Any]] = {}
    node_rep_file: Dict[str, str] = {}

    parsed_ok = 0
    parsed_warn = 0
    parsed_err = 0

    # ---------------------------------------------------------------------
    # Deterministic FileEntry list
    # ---------------------------------------------------------------------
    fe_list: List[FileEntry] = [fe for fe in files.values() if isinstance(fe, FileEntry)]
    fe_list.sort(
        key=lambda fe: (
            to_posix(str(getattr(fe, "id", "") or "").strip()),
            to_posix(str(getattr(fe, "file", "") or "").strip()),
        )
    )

    # ---------------------------------------------------------------------
    # PASS A: Symbol extraction (use cache OR parse with multiprocessing)
    # ---------------------------------------------------------------------
    syms_by_relfile: Dict[str, Optional[NodeFactsSymbols]] = {}
    syms_status_by_relfile: Dict[str, str] = {}

    # IMPORTANT: file-id space package map (keyed by FileEntry.id POSIX)
    file_id_to_package: Dict[str, str] = {}

    # Check for parse cache from folder_index
    parse_cache = getattr(cfg, "java_parse_cache", None)

    cfg_payload = _java_nf_cfg_payload(cfg)

    if symbols_mode != "none":
        # Build deterministic rel_file list (from FileEntry.file)
        rel_files: List[str] = []
        for fe in fe_list:
            rel_file = str(getattr(fe, "file", "") or "").strip()
            if rel_file:
                rel_files.append(rel_file)

        if not rel_files:
            log_event("JAVA_NODEFACTS:no_paths_short_circuit", root=str(repo_root))
        elif isinstance(parse_cache, dict) and parse_cache:
            # FAST PATH: Use cached parse results (no re-parse)
            log_event(
                "JAVA_NODEFACTS:using_parse_cache",
                root=str(repo_root),
                cache_size=len(parse_cache),
                files_needed=len(rel_files),
            )

            t_transform = time.perf_counter()
            cache_hits = 0
            cache_misses = 0

            # Build a lookup strategy that tries multiple key flavors
            # - rel path as provided
            # - posix rel path
            # - file-id (fe.id)
            # - posix file-id
            # - absolute path (as str), if cache used that
            def _cache_get(*, rel_file: str, fe_id: str) -> Optional[object]:
                try:
                    if rel_file in parse_cache:
                        return parse_cache[rel_file]
                except Exception:
                    pass
                relp = to_posix(rel_file)
                try:
                    if relp in parse_cache:
                        return parse_cache[relp]
                except Exception:
                    pass
                try:
                    if fe_id in parse_cache:
                        return parse_cache[fe_id]
                except Exception:
                    pass
                fidp = to_posix(fe_id)
                try:
                    if fidp in parse_cache:
                        return parse_cache[fidp]
                except Exception:
                    pass
                try:
                    abs_key = str((repo_root / rel_file).resolve(strict=False))
                    if abs_key in parse_cache:
                        return parse_cache[abs_key]
                except Exception:
                    pass
                return None

            # We want per-file package values keyed by FileEntry.id
            for fe in fe_list:
                rel_file = str(getattr(fe, "file", "") or "").strip()
                fe_id = str(getattr(fe, "id", "") or "").strip()
                if not rel_file:
                    continue

                cached = _cache_get(rel_file=rel_file, fe_id=fe_id)
                syms: Optional[NodeFactsSymbols] = None

                try:
                    if cached is not None and _is_like_parsed_file(cached):
                        # Best effort: treat as parsed-file record and convert -> NodeFactsSymbols
                        syms = parse_symbols_for_nodefacts(cached, cfg_payload)
                        cache_hits += 1
                    else:
                        # cache miss or unusable record: fall back to parsing the file
                        abs_path = (repo_root / rel_file).resolve(strict=False)
                        syms = parse_symbols_for_nodefacts(abs_path, cfg_payload)
                        cache_misses += 1
                except Exception:
                    syms = None

                syms_by_relfile[rel_file] = syms
                st = getattr(syms, "parse_status", "ok") if syms is not None else "error"
                syms_status_by_relfile[rel_file] = str(st or "ok")

                if syms is not None:
                    try:
                        pkg = str(getattr(syms, "package", "") or "").strip()
                        fidp = to_posix(fe_id) or to_posix(rel_file)
                        if pkg and fidp:
                            file_id_to_package[fidp] = pkg
                    except Exception:
                        pass

            log_event(
                "JAVA_NODEFACTS:cache_transform_done",
                root=str(repo_root),
                cache_hits=cache_hits,
                cache_misses=cache_misses,
                ms=int((time.perf_counter() - t_transform) * 1000),
                pkg_map=len(file_id_to_package),
            )
        else:
            # SLOW PATH: Normal parallel parsing (no cache)
            cpu_count = os.cpu_count() or 1
            cfg_workers = int(getattr(cfg, "java_max_workers", 0) or 0)
            base_workers = int(getattr(cfg, "max_workers", cpu_count) or cpu_count)
            want = cfg_workers if cfg_workers > 0 else base_workers
            max_workers = min(max(1, want), len(rel_files))

            if max_workers <= 1:
                log_event("JAVA_NODEFACTS:sequential_fallback_begin", root=str(repo_root), paths=len(rel_files))
                batch_syms, batch_status, batch_pkg = _java_nf_build_symbols_batch(str(repo_root), rel_files, cfg_payload)
                syms_by_relfile.update(batch_syms)
                syms_status_by_relfile.update(batch_status)

                # Convert relposix -> file-id mapping best-effort (may be incomplete)
                # We try to attach packages to fe.id if we can match fe.file
                for fe in fe_list:
                    fidp = to_posix(str(getattr(fe, "id", "") or "").strip())
                    relp = to_posix(str(getattr(fe, "file", "") or "").strip())
                    pkg = batch_pkg.get(relp, "")
                    if fidp and pkg:
                        file_id_to_package[fidp] = pkg

                log_event(
                    "JAVA_NODEFACTS:sequential_fallback_end",
                    root=str(repo_root),
                    parsed=len(rel_files),
                    errors=sum(1 for v in batch_status.values() if v == "error"),
                    pkg_map=len(file_id_to_package),
                )
            else:
                batches = _chunk_by_bytes(rel_files, repo_root=repo_root, max_workers=max_workers)

                log_event(
                    "JAVA_NODEFACTS:multiprocessing_begin",
                    root=str(repo_root),
                    workers=max_workers,
                    batches=len(batches),
                    paths=len(rel_files),
                )

                mp_t0 = time.perf_counter()
                errors = 0

                with ProcessPoolExecutor(max_workers=max_workers) as ex:
                    futures = {
                        ex.submit(_java_nf_build_symbols_batch, str(repo_root), batch, cfg_payload): idx
                        for idx, batch in enumerate(batches)
                    }

                    # temp relposix->pkg, then map to file-id space
                    relposix_to_pkg: Dict[str, str] = {}

                    for fut in as_completed(futures):
                        idxb = futures[fut]
                        try:
                            batch_syms, batch_status, batch_pkg = fut.result()
                        except Exception:
                            batch_syms, batch_status, batch_pkg = {}, {}, {}
                            errors += 1

                        syms_by_relfile.update(batch_syms)
                        syms_status_by_relfile.update(batch_status)
                        relposix_to_pkg.update(batch_pkg)

                        batch_errs = sum(1 for v in batch_status.values() if v == "error")
                        errors += batch_errs

                        log_event(
                            "JAVA_NODEFACTS:worker_done",
                            root=str(repo_root),
                            batch_index=idxb,
                            batch_size=len(batches[idxb]),
                            parse_errors=batch_errs,
                            errors_total=errors,
                        )

                    # Map relposix package entries onto file-id space using fe.file
                    for fe in fe_list:
                        fidp = to_posix(str(getattr(fe, "id", "") or "").strip())
                        relp = to_posix(str(getattr(fe, "file", "") or "").strip())
                        pkg = relposix_to_pkg.get(relp, "")
                        if fidp and pkg:
                            file_id_to_package[fidp] = pkg

                log_event(
                    "JAVA_NODEFACTS:multiprocessing_end",
                    root=str(repo_root),
                    total=len(syms_by_relfile),
                    errors=errors,
                    ms=int((time.perf_counter() - mp_t0) * 1000),
                    pkg_map=len(file_id_to_package),
                )

    # Enforce safety: if package mode requested but we can't map packages reliably, disable it.
    if node_id_space == "package":
        want = sum(1 for fe in fe_list if to_posix(str(getattr(fe, "id", "") or "").strip()))
        have = len(file_id_to_package)
        if have < max(1, int(want * 0.95)):
            log_event(
                "JAVA:nodefacts_package_mode_disabled",
                reason="insufficient_package_map_file_id_space",
                have=have,
                want=want,
            )
            node_id_space = "file"

    # Publish package map for downstream (edges builder / pipeline).
    # - We keep it keyed by file-id space (POSIX).
    try:
        setattr(cfg, "java_file_id_to_package", dict(file_id_to_package))
    except Exception:
        pass

    def _node_id_for_file(*, fe: FileEntry) -> str:
        if node_id_space == "package":
            fid = to_posix(str(getattr(fe, "id", "") or "").strip())
            pkg = str(file_id_to_package.get(fid, "") or "").strip()
            return pkg or "(default)"
        return to_posix(str(getattr(fe, "id", "") or "").strip())

    # ---------------------------------------------------------------------
    # PASS A (continued): aggregate nodes
    # ---------------------------------------------------------------------

    # We aggregate these as sets to avoid repeated sort/dedupe inside the loop.
    _sym_keys = ("public_exports", "declared_types", "declared_types_fq", "annotations", "exports", "classes", "functions", "globals", "imports_all_raw", "imports_external")

    for fe in fe_list:
        rel_file = str(getattr(fe, "file", "") or "").strip()
        if not rel_file:
            continue

        syms: Optional[NodeFactsSymbols] = None
        if symbols_mode != "none":
            syms = syms_by_relfile.get(rel_file)

        node_id = _node_id_for_file(fe=fe)
        if not node_id:
            continue

        fe_loc = _safe_nonnegative_int(getattr(fe, "loc", None), 0)
        fe_sloc = _safe_nonnegative_int(getattr(fe, "sloc", None), 0)
        fe_comment_lines = _safe_nonnegative_int(getattr(fe, "comment_lines", None), 0)
        fe_blank_lines = _safe_nonnegative_int(getattr(fe, "blank_lines", None), 0)

        node_loc_total[node_id] = node_loc_total.get(node_id, 0) + fe_loc
        node_sloc_total[node_id] = node_sloc_total.get(node_id, 0) + fe_sloc
        node_comment_lines_total[node_id] = node_comment_lines_total.get(node_id, 0) + fe_comment_lines
        node_blank_lines_total[node_id] = node_blank_lines_total.get(node_id, 0) + fe_blank_lines
        node_file_count[node_id] = node_file_count.get(node_id, 0) + 1

        # Parse status combine
        sym_status = "ok"
        if symbols_mode != "none":
            sym_status = syms_status_by_relfile.get(
                rel_file,
                getattr(syms, "parse_status", "ok") if syms is not None else "error",
            )
        status = _upgrade_parse_status(str(getattr(fe, "parse_status", "ok") or "ok"), str(sym_status or "ok"))

        if status == "ok":
            parsed_ok += 1
        elif status == "warn":
            parsed_warn += 1
        else:
            parsed_err += 1

        # Init node record
        if node_id not in by_node:
            # representative file: in file-space this is accurate; in package-space it's a rep (best-effort)
            rep_file = rel_file
            node_rep_file[node_id] = rep_file

            by_node[node_id] = {
                "id": node_id,
                "name": _java_name_from_id(node_id) or node_id,
                "language": "java",
                "lang": "java",

                # graph fields (filled later)
                "imports": (),
                "importers_count": 0,
                "dependencies_count": 0,

                # legacy nodefacts symbol fields (kept)
                "exports": (),     # finalized later
                "classes": (),
                "functions": (),
                "globals": (),

                # java enrichment fields (best-effort)
                "package": (str(getattr(syms, "package", "") or "").strip() or None) if syms is not None else None,
                "declared_types": (),
                "declared_types_fq": (),
                "public_exports": (),
                "annotations": (),
                "imports_all_raw": (),
                "imports_external": (),
                "language_facts": {},

                # housekeeping / metrics
                "gen_seq": 0,
                "scc_id": "",
                "scc_size": 1,
                "file": rep_file,
                "hash": getattr(fe, "hash", None),
                "loc": 0,
                "sloc": 0,
                "comment_lines": 0,
                "blank_lines": 0,
                "comment_pct": None,
                "file_count": 0,
                "size_bytes": getattr(fe, "size_bytes", None),
                "mtime": getattr(fe, "mtime", None),
                "du": None,
                "dd": None,
                "parse_status": status,

                # reserved / forward-looking
                "crosstalk_candidates_java_v1": (),
            }

            # internal aggregation scratch (not part of schema output)
            for k in _sym_keys:
                by_node[node_id][f"__acc_{k}"] = set()  # type: ignore[assignment]
        else:
            by_node[node_id]["parse_status"] = _upgrade_parse_status(by_node[node_id].get("parse_status", "ok"), status)

        node = by_node[node_id]

        # If we see a more specific package string, prefer it (non-empty)
        if syms is not None:
            try:
                pkgv = str(getattr(syms, "package", "") or "").strip()
                if pkgv:
                    node["package"] = pkgv
            except Exception:
                pass

        # Merge symbols (if any) into sets
        if syms is not None:
            for k in _sym_keys:
                try:
                    vals = getattr(syms, k, ()) or ()
                except Exception:
                    vals = ()
                try:
                    acc = node.get(f"__acc_{k}")
                    if isinstance(acc, set):
                        for v in vals:
                            if not v:
                                continue
                            vv = str(v).strip()
                            if vv:
                                acc.add(vv)
                except Exception:
                    pass
        raw_acc = node.get("__acc_imports_all_raw")
        if isinstance(raw_acc, set):
            for raw in (getattr(fe, "imports_all", None) or ()):
                spec = str(raw or "").strip()
                if spec:
                    raw_acc.add(spec)

        ext_acc = node.get("__acc_imports_external")
        if isinstance(ext_acc, set):
            for raw in (getattr(fe, "imports_external", None) or ()):
                spec = str(raw or "").strip()
                if spec:
                    ext_acc.add(spec)

    # ---------------------------------------------------------------------
    # PASS B: deps/importers from imports_internal (file-id space)
    # ---------------------------------------------------------------------
    deps_by_node: Dict[str, Set[str]] = {}
    importers_count: Dict[str, int] = {}

    for fe in fe_list:
        src_file = to_posix(str(getattr(fe, "id", "") or "").strip())
        if not src_file:
            continue

        src = file_id_to_package.get(src_file, "") if node_id_space == "package" else src_file
        if not src:
            continue

        deps = deps_by_node.setdefault(src, set())
        for dst0 in (getattr(fe, "imports_internal", None) or ()):
            dst_file = to_posix(str(dst0 or "").strip())
            if not dst_file:
                continue

            dst = file_id_to_package.get(dst_file, "") if node_id_space == "package" else dst_file
            if not dst:
                continue
            if dst == src:
                continue

            deps.add(dst)
            importers_count[dst] = importers_count.get(dst, 0) + 1

    # Ensure stubs exist for any deps pointing to unseen nodes
    for src, deps in deps_by_node.items():
        if src not in by_node:
            by_node[src] = _stub_node(src)
        for dst in deps:
            if dst not in by_node:
                by_node[dst] = _stub_node(dst)

    # ---------------------------------------------------------------------
    # PASS C: SCC computation from finalized per-language dependency graph
    # ---------------------------------------------------------------------
    scc_id_by_node, scc_size_by_id = _compute_scc_from_deps(
        by_node.keys(),
        deps_by_node,
    )

    # Finalize symbols + computed fields
    for node_id, node in by_node.items():
        deps = deps_by_node.get(node_id, set())
        node["imports"] = tuple(sorted(deps))
        node["dependencies_count"] = len(deps)
        node["importers_count"] = importers_count.get(node_id, 0)
        sid = scc_id_by_node.get(node_id, node_id)
        node["scc_id"] = sid
        node["scc_size"] = scc_size_by_id.get(sid, 1)
        loc_total = int(node_loc_total.get(node_id, 0))
        comment_total = int(node_comment_lines_total.get(node_id, 0))

        node["loc"] = loc_total
        node["sloc"] = int(node_sloc_total.get(node_id, 0))
        node["comment_lines"] = comment_total
        node["blank_lines"] = int(node_blank_lines_total.get(node_id, 0))
        node["comment_pct"] = _comment_pct(comment_total, loc_total)
        node["file_count"] = int(node_file_count.get(node_id, 0))

        if node_id in node_rep_file and node.get("file") != node_rep_file[node_id]:
            node["file"] = node_rep_file[node_id]

        # finalize aggregated symbol sets -> sorted tuples
        for k in _sym_keys:
            acc = node.pop(f"__acc_{k}", None)
            if isinstance(acc, set):
                node[k] = tuple(sorted(acc))
            else:
                # keep existing (stubs already have tuples)
                node[k] = tuple(node.get(k, ()) or ())

        java_facts: Dict[str, Any] = {}

        if node.get("package"):
            java_facts["package"] = node.get("package")

        if node.get("imports_all_raw"):
            java_facts["imports"] = list(node.get("imports_all_raw") or ())

        if node.get("imports_external"):
            java_facts["imports_external"] = list(node.get("imports_external") or ())

        for old_key, fact_key in (
            ("classes", "classes"),
            ("functions", "functions"),
            ("globals", "globals"),
            ("annotations", "annotations"),
            ("declared_types", "declared_types"),
            ("declared_types_fq", "declared_types_fq"),
            ("public_exports", "public_exports"),
            ("exports", "exports"),
        ):
            if node.get(old_key):
                java_facts[fact_key] = list(node.get(old_key) or ())

        if java_facts:
            node["language_facts"] = {
                "java": java_facts,
            }

    nodes = {k: by_node[k] for k in sorted(by_node.keys())}

    loc_total_all = int(sum(node_loc_total.values()))
    sloc_total_all = int(sum(node_sloc_total.values()))
    comment_lines_total_all = int(sum(node_comment_lines_total.values()))
    blank_lines_total_all = int(sum(node_blank_lines_total.values()))

    nodes_with_raw_imports = sum(
        1 for node in nodes.values()
        if node.get("imports_all_raw")
    )
    raw_import_specs_total = sum(
        len(node.get("imports_all_raw") or ())
        for node in nodes.values()
    )
    nodes_with_external_imports = sum(
        1 for node in nodes.values()
        if node.get("imports_external")
    )
    external_import_specs_total = sum(
        len(node.get("imports_external") or ())
        for node in nodes.values()
    )
    nodes_with_language_facts = sum(
        1 for node in nodes.values()
        if node.get("language_facts")
    )

    out = {
        "schema_version": NODEFACTS_SCHEMA,
        "language": "java",
        "nodes": nodes,
        "meta": {
            "module_name": (module_name or ""),
            "nodes": len(nodes),
            "files_indexed": len(files),
            "parse_ok": parsed_ok,
            "parse_warn": parsed_warn,
            "parse_err": parsed_err,
            "loc_total": loc_total_all,
            "sloc_total": sloc_total_all,
            "comment_lines_total": comment_lines_total_all,
            "blank_lines_total": blank_lines_total_all,
            "comment_pct": _comment_pct(comment_lines_total_all, loc_total_all),
            "java_node_id_space": node_id_space,
            "java_nodefacts_symbols": symbols_mode,
            "nodes_with_imports_all_raw": int(nodes_with_raw_imports),
            "raw_import_specs_total": int(raw_import_specs_total),
            "nodes_with_imports_external": int(nodes_with_external_imports),
            "external_import_specs_total": int(external_import_specs_total),
            "nodes_with_language_facts": int(nodes_with_language_facts),
            "used_parse_cache": bool(isinstance(parse_cache, dict) and parse_cache),
            # publish mapping for downstream consumers (pipeline can reuse directly)
            "java_file_id_to_package": dict(file_id_to_package),
        },
    }

    log_event(
        "JAVA:nodefacts_built",
        nodes=len(nodes),
        nodes_with_imports_all_raw=int(nodes_with_raw_imports),
        raw_import_specs_total=int(raw_import_specs_total),
        external_import_specs_total=int(external_import_specs_total),
        nodes_with_language_facts=int(nodes_with_language_facts),
        ms=int((time.perf_counter() - t0) * 1000),
    )
    return out


# ---------------------------------------------------------------------------
# Edge builder (from FolderIndex)
# ---------------------------------------------------------------------------

def build_edges_from_folder_index(
    idx: FolderIndex,
    *,
    internal_only: bool = True,
    include_external: bool = False,
    drop_self_edges: bool = True,
    cfg: Any = None,
) -> Dict[str, Any]:
    """
    Build an edges@v1 object from a Java FolderIndex.

    Contract:
      - Default node-id space is file-id space (repo-relative POSIX path).
      - Internal edges derive directly from FileEntry.imports_internal.
      - External edges (optional) derive from imports_all + resolve_java_import().

    Package aggregation:
      - Enabled if cfg.java_node_id_space == "package" AND a safe file->package
        map is provided:
          - cfg.java_file_id_to_package (preferred), or
          - cfg.java_file_id_to_package_map (legacy), or
          - cfg.meta_java_file_id_to_package (if pipeline stashes it)

    Optional enrichment (no schema changes):
      - If cfg.java_edges_enrichment is truthy AND cfg.java_parse_cache contains
        parsed records with fields like extends/implements/annotations/module_*,
        we emit extra edges with kinds:
          - java:extends
          - java:implements
          - java:annotation
          - java:module:requires / exports / opens / uses / provides
        This is best-effort and purely additive.
    """
    node_id_space = str(getattr(cfg, "java_node_id_space", "file") or "file").strip().lower() if cfg else "file"

    edges_out: List[Dict[str, object]] = []
    seen: Set[Tuple[str, str, str]] = set()

    def _add_edge(
        src: str,
        dst: str,
        kind: str,
        confidence: float,
        *,
        label: Optional[str] = None,
        spec: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        if not src or not dst or not kind:
            return
        if drop_self_edges and src == dst:
            return

        key = (src, dst, kind)
        if key in seen:
            return

        seen.add(key)

        edge: Dict[str, object] = {
            "src": src,
            "dst": dst,
            "kind": kind,
            "confidence": float(confidence),
            "weight": float(confidence),
        }
        if label:
            edge["label"] = label
        if spec:
            edge["spec"] = spec
        if reason:
            edge["reason"] = reason

        edges_out.append(edge)

    files = getattr(idx, "files", {}) or {}
    fe_list: List[FileEntry] = [fe for fe in files.values() if isinstance(fe, FileEntry)]

    # Map in file-id space (POSIX)
    file_id_to_package: Dict[str, str] = {}
    if node_id_space == "package" and cfg is not None:
        m = getattr(cfg, "java_file_id_to_package", None)
        if not isinstance(m, dict) or not m:
            m = getattr(cfg, "java_file_id_to_package_map", None)
        if not isinstance(m, dict) or not m:
            m = getattr(cfg, "meta_java_file_id_to_package", None)
        if isinstance(m, dict):
            file_id_to_package = {to_posix(str(k)): str(v) for (k, v) in m.items() if k and v}
        if not file_id_to_package:
            # refuse to guess
            node_id_space = "file"

    def _map_node_id(file_id: str) -> str:
        fid = to_posix(str(file_id or "").strip())
        if not fid:
            return ""
        if node_id_space != "package":
            return fid
        return str(file_id_to_package.get(fid, "") or "").strip()

    # -------------------------
    # Internal edges (fast path)
    # -------------------------
    if internal_only:
        for fe in fe_list:
            src_file = to_posix(str(getattr(fe, "id", "") or "").strip())
            src = _map_node_id(src_file)
            if not src:
                continue

            for dst0 in (getattr(fe, "imports_internal", None) or ()):
                dst_file = to_posix(str(dst0 or "").strip())
                dst = _map_node_id(dst_file)
                if not dst:
                    continue
                if drop_self_edges and dst == src:
                    continue

                _add_edge(
                    src,
                    dst,
                    "java:import:internal",
                    0.8,
                    reason="java_import_internal",
                )

    # -------------------------
    # External edges (optional)
    # -------------------------
    if include_external:
        type_to_file = getattr(cfg, "java_type_to_file", None) if cfg else None
        package_to_files = getattr(cfg, "java_package_to_files", None) if cfg else None

        for fe in fe_list:
            src_file = to_posix(str(getattr(fe, "id", "") or "").strip())
            src = _map_node_id(src_file)
            if not src:
                continue

            for raw in (getattr(fe, "imports_all", None) or ()):
                spec = normalize_java_import_spec(str(raw or ""), is_wildcard=False, is_static=False)
                if not spec:
                    continue

                if type_to_file and package_to_files:
                    results = resolve_java_import(spec, type_to_file=type_to_file, package_to_files=package_to_files)
                    for rr in results:
                        if getattr(rr, "kind", "") == "internal":
                            continue
                        kind = f"java:import:{getattr(rr, 'kind', 'external')}"
                        dst = str(getattr(rr, "spec", spec) or spec)

                        _add_edge(
                            src,
                            dst,
                            kind,
                            0.4,
                            label=dst,
                            spec=spec,
                            reason="java_import_external_resolved",
                        )
                else:
                    kind = "java:import:external_raw"
                    dst = spec
                    _add_edge(
                        src,
                        dst,
                        kind,
                        0.2,
                        label=spec,
                        spec=spec,
                        reason="java_import_external_raw",
                    )

    # -------------------------
    # Optional enrichment edges
    # -------------------------
    enrich = bool(getattr(cfg, "java_edges_enrichment", False)) if cfg else False
    parse_cache = getattr(cfg, "java_parse_cache", None) if cfg else None

    if enrich and isinstance(parse_cache, dict) and parse_cache:
        # Lookup cached record using file-id & rel-path variants (same strategy as NodeFacts)
        def _cache_get(fe: FileEntry) -> Optional[object]:
            rel_file = str(getattr(fe, "file", "") or "").strip()
            fe_id = str(getattr(fe, "id", "") or "").strip()
            if rel_file and rel_file in parse_cache:
                return parse_cache[rel_file]
            relp = to_posix(rel_file)
            if relp and relp in parse_cache:
                return parse_cache[relp]
            if fe_id and fe_id in parse_cache:
                return parse_cache[fe_id]
            fidp = to_posix(fe_id)
            if fidp and fidp in parse_cache:
                return parse_cache[fidp]
            try:
                repo_root = getattr(cfg, "repo_root", None)
                if repo_root and rel_file:
                    abs_key = str((Path(repo_root) / rel_file).resolve(strict=False))
                    if abs_key in parse_cache:
                        return parse_cache[abs_key]
            except Exception:
                pass
            return None

        # Best-effort extractors from parsed cache records. We do NOT assume a concrete shape,
        # only that some fields may exist. If they don't, this produces nothing.
        for fe in fe_list:
            src_file = to_posix(str(getattr(fe, "id", "") or "").strip())
            src = _map_node_id(src_file)
            if not src:
                continue

            rec = _cache_get(fe)
            if rec is None:
                continue

            # Try common field names:
            # - extends_fq / implements_fq (list[str])
            # - annotations_fq (list[str])
            # - module_requires/exports/opens/uses/provides (list[str])
            try:
                extends_fq = getattr(rec, "extends_fq", None) or getattr(rec, "extends", None) or ()
                for t in extends_fq or ():
                    dst = str(t or "").strip()
                    if dst:
                        _add_edge(src, dst, "java:extends", 0.6)
            except Exception:
                pass

            try:
                impl_fq = getattr(rec, "implements_fq", None) or getattr(rec, "implements", None) or ()
                for t in impl_fq or ():
                    dst = str(t or "").strip()
                    if dst:
                        _add_edge(src, dst, "java:implements", 0.6)
            except Exception:
                pass

            try:
                ann_fq = getattr(rec, "annotations_fq", None) or getattr(rec, "annotations", None) or ()
                for a in ann_fq or ():
                    dst = str(a or "").strip()
                    if dst:
                        _add_edge(src, dst, "java:annotation", 0.5)
            except Exception:
                pass

            # module-info signals
            try:
                for v in (getattr(rec, "module_requires", None) or ()):
                    dst = str(v or "").strip()
                    if dst:
                        _add_edge(src, dst, "java:module:requires", 0.5)
            except Exception:
                pass

            try:
                for v in (getattr(rec, "module_exports", None) or ()):
                    dst = str(v or "").strip()
                    if dst:
                        _add_edge(src, dst, "java:module:exports", 0.5)
            except Exception:
                pass

            try:
                for v in (getattr(rec, "module_opens", None) or ()):
                    dst = str(v or "").strip()
                    if dst:
                        _add_edge(src, dst, "java:module:opens", 0.5)
            except Exception:
                pass

            try:
                for v in (getattr(rec, "module_uses", None) or ()):
                    dst = str(v or "").strip()
                    if dst:
                        _add_edge(src, dst, "java:module:uses", 0.5)
            except Exception:
                pass

            try:
                for v in (getattr(rec, "module_provides", None) or ()):
                    dst = str(v or "").strip()
                    if dst:
                        _add_edge(src, dst, "java:module:provides", 0.5)
            except Exception:
                pass

    # Deterministic edge order
    edges_out.sort(key=lambda e: (str(e.get("kind", "")), str(e.get("src", "")), str(e.get("dst", ""))))

    out = {
        "schema_version": "edges@v1",
        "language": "java",
        "edges": edges_out,
        "meta": {
            "total": len(edges_out),
            "internal": sum(1 for e in edges_out if e.get("kind") == "java:import:internal"),
            "java_node_id_space": node_id_space,
            "external_included": bool(include_external),
            "enrichment_included": bool(enrich and isinstance(parse_cache, dict) and parse_cache),
        },
    }
    return out