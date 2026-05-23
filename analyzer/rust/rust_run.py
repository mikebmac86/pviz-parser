from __future__ import annotations

"""
Rust artifact builders - following Java analyzer pattern.

Orchestrates:
  1. Build folder index with parallel parsing (PARSE ONCE)
     - Returns (idx, parse_cache)
  2. Build nodefacts using parse_cache (NO RE-PARSING)
  3. Optionally build edges
  4. Write artifacts to set dir
  5. Return fragment {nodes, edges, meta}

Performance:
  Same optimization as Java - parse once, reuse cache for nodefacts
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# Rust FolderIndex builders / types (to be created)
from analyzer.rust.rust_folder_index import (
    build_folder_index,
    save_folder_index,
)

# Rust artifact builders (to be created)
from analyzer.rust.rust_build_artifacts import (
    build_nodefacts_from_folder_index,
    build_edges_from_folder_index,
)

# Canonical store types
from analyzer_store.types import FolderIndex

# Diagnostics logging
try:
    from diagnostics.logging import log_event as _log_event  # type: ignore
except Exception:  # pragma: no cover
    _log_event = None  # type: ignore


def log_event(name: str, **k) -> None:
    if _log_event is not None:
        try:
            _log_event(name, **k)
            return
        except Exception:
            pass
    parts = " ".join(f"{kk}={k[kk]!r}" for kk in sorted(k.keys()))
    print(f"[RUST] {name} {parts}".rstrip())


def _write_json(path: Path, obj: Any) -> None:
    """Lightweight JSON writer (avoids external deps)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")


def _diag_print(name: str, **k: Any) -> None:
    """
    DIAGNOSTIC ONLY prints.

    Intentionally does NOT go through log_event/structured logging so you can
    grep these easily and remove later.
    """
    parts = " ".join(f"{kk}={k[kk]!r}" for kk in sorted(k.keys()))
    print(f"[RUST_DIAG] {name} {parts}".rstrip())


def _diag_cache_stats(parse_cache: Any) -> Dict[str, Any]:
    """
    Best-effort shape inspection for parse_cache (dict-like assumed).
    """
    out: Dict[str, Any] = {
        "cache_type": type(parse_cache).__name__,
        "size": 0,
        "keys_sample": [],
        "value_type_sample": None,
        "ok": False,
    }
    try:
        if isinstance(parse_cache, dict):
            out["size"] = len(parse_cache)
            keys = list(parse_cache.keys())
            out["keys_sample"] = [str(k) for k in keys[:5]]
            if keys:
                v0 = parse_cache[keys[0]]
                out["value_type_sample"] = type(v0).__name__
            out["ok"] = True
    except Exception:
        pass
    return out


def analyze_files_rust(
    *,
    repo_root: Path,
    artifact_dir: Path,
    cfg: Any,
    files: Sequence[Path],
    folder_index_name: str = "folder_index_rust.json",
    build_edges: bool = True,
    write_nodefacts: bool = True,
) -> Dict[str, object]:
    """
    Rust bucket analyzer (optimized to eliminate double-parsing).

    Orchestrates:
      1. Build folder index with parallel parsing (PARSE ONCE)
         - Returns (idx, parse_cache)
      2. Build nodefacts using parse_cache (NO RE-PARSING)
      3. Optionally build edges
      4. Write artifacts to set dir
      5. Return fragment {nodes, edges, meta}

    Writes into artifact_dir (set dir):
      - nodefacts.json
      - edges.json (if build_edges=True)
      - folder_index_rust.json

    Performance improvement:
      - Before: Files parsed twice (once for folder_index, once for nodefacts)
      - After: Files parsed once, results cached and reused
      - Speedup: ~2x from eliminating redundant parsing
      - Plus: Both phases now parallelized for additional speedup
    """
    t_all = time.perf_counter()

    repo_root = repo_root.resolve()
    artifact_dir = artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # Config-driven names
    nodefacts_name = getattr(cfg, "nodefacts_name", "nodefacts.json")
    edges_name = getattr(cfg, "edges_name", "edges.json")
    folder_index_name_eff = getattr(cfg, "folder_index_name", folder_index_name)

    nodefacts_path = artifact_dir / nodefacts_name
    edges_path = artifact_dir / edges_name
    folder_index_path = artifact_dir / folder_index_name_eff

    log_event(
        "RUST:analyze_files_begin",
        repo_root=str(repo_root),
        artifact_dir=str(artifact_dir),
        files_in=len(files),
        build_edges=bool(build_edges),
        write_nodefacts=bool(write_nodefacts),
    )

    # DIAG: basic invocation snapshot
    try:
        _diag_print(
            "invoke",
            repo_root=str(repo_root),
            artifact_dir=str(artifact_dir),
            files_in=len(files),
            nodefacts_name=str(nodefacts_name),
            edges_name=str(edges_name),
            folder_index_name=str(folder_index_name_eff),
            cfg_type=type(cfg).__name__,
        )
        if files:
            _diag_print(
                "files_sample",
                first=str(files[0]),
                last=str(files[-1]),
            )
    except Exception:
        pass

    # -----------------------------------------------------------------------
    # STEP 1: Build folder index (PARSE ONCE - returns cache!)
    # -----------------------------------------------------------------------
    try:
        t_idx = time.perf_counter()
        idx, parse_cache = build_folder_index(
            root=repo_root,
            cfg=cfg,
            files=list(files),
        )
        save_folder_index(idx, folder_index_path)

        log_event(
            "RUST:folder_index_built",
            ms=int((time.perf_counter() - t_idx) * 1000),
            files=len(idx.files),
            cache_size=len(parse_cache),
        )

        # DIAG: parse_cache shape + idx meta summary
        try:
            stats = _diag_cache_stats(parse_cache)
            _diag_print(
                "folder_index_done",
                ms=int((time.perf_counter() - t_idx) * 1000),
                idx_files=len(getattr(idx, "files", {}) or {}),
                idx_meta_keys=len(getattr(idx, "meta", {}) or {}) if isinstance(getattr(idx, "meta", None), dict) else 0,
                **stats,
            )

            # If cache values look like RustParsedFile, sample a few fields
            if isinstance(parse_cache, dict) and parse_cache:
                k0 = next(iter(parse_cache.keys()))
                v0 = parse_cache.get(k0)
                sample_fields = {}
                for attr in ("ok", "parse_status", "module_path", "use_statements", "functions", "structs"):
                    try:
                        if hasattr(v0, attr):
                            val = getattr(v0, attr)
                            # For lists, just show count
                            if isinstance(val, list):
                                sample_fields[f"val0_{attr}_count"] = len(val)
                            else:
                                sample_fields[f"val0_{attr}"] = val
                    except Exception:
                        pass
                if sample_fields:
                    _diag_print("parse_cache_value0_sample", key0=str(k0), **sample_fields)
        except Exception:
            pass

    except Exception as e:
        log_event("RUST:folder_index_failed", err=f"{type(e).__name__}: {e}"[:200])

        # DIAG: exception surface
        try:
            _diag_print("folder_index_exception", exc_type=type(e).__name__, exc=str(e)[:300])
        except Exception:
            pass

        # Write empty artifacts on failure
        _write_empty_artifacts(
            nodefacts_path=nodefacts_path,
            edges_path=edges_path if build_edges else None,
            folder_index_path=folder_index_path,
            repo_root=repo_root,
        )

        return {
            "nodes": {},
            "edges": [],
            "meta": {
                "analyzer": "rust",
                "files_in": len(files),
                "error": "folder_index_build_failed",
            },
        }

    meta = idx.meta if isinstance(getattr(idx, "meta", None), dict) else {}

    # DIAG: FolderIndex meta snapshot
    try:
        _diag_print(
            "folder_index_meta",
            eligible_count=meta.get("eligible_count"),
            parsed_count=meta.get("parsed_count"),
            parse_issues_count=meta.get("parse_issues_count"),
            internal_edge_count=meta.get("internal_edge_count"),
            module_index_count=meta.get("module_index_count"),
            crate_index_count=meta.get("crate_index_count"),
        )
    except Exception:
        pass

    # -----------------------------------------------------------------------
    # STEP 2: Build nodefacts (USE CACHE - no re-parsing!)
    # -----------------------------------------------------------------------
    nodefacts_obj: Dict[str, Any]
    if write_nodefacts:
        t_nf = time.perf_counter()

        # CRITICAL: Pass parse_cache to avoid re-parsing!
        # Update cfg to include the cache
        cfg.rust_parse_cache = parse_cache

        # DIAG: confirm cfg got cache
        try:
            _diag_print(
                "nodefacts_begin",
                cache_attached=bool(getattr(cfg, "rust_parse_cache", None) is not None),
                cache_len=(len(getattr(cfg, "rust_parse_cache", {}) or {}) if isinstance(getattr(cfg, "rust_parse_cache", None), dict) else None),
            )
        except Exception:
            pass

        nodefacts_obj = build_nodefacts_from_folder_index(
            idx=idx,
            repo_root=repo_root,
            cfg=cfg,
        )

        log_event(
            "RUST:nodefacts_built",
            ms=int((time.perf_counter() - t_nf) * 1000),
            nodes=len(nodefacts_obj.get("nodes", {})),
        )

        # DIAG: quick nodefacts summary
        try:
            nodes_map = nodefacts_obj.get("nodes", {}) or {}
            _diag_print(
                "nodefacts_done",
                ms=int((time.perf_counter() - t_nf) * 1000),
                nodes=len(nodes_map),
                schema=str(nodefacts_obj.get("schema_version", "")),
            )
            if isinstance(nodes_map, dict) and nodes_map:
                nid0 = next(iter(nodes_map.keys()))
                n0 = nodes_map.get(nid0, {}) or {}
                _diag_print(
                    "nodefacts_node0_sample",
                    node0_id=str(nid0),
                    node0_file=str(n0.get("file", "")),
                    node0_module_path=n0.get("module_path"),
                    node0_use_count=(len(n0.get("use_statements", ()) or ())),
                    node0_parse_status=str(n0.get("parse_status", "")),
                )
        except Exception:
            pass
    else:
        nodefacts_obj = {"schema_version": "nodefacts@v1.6", "language": "rust", "nodes": {}}

    nodefacts_nodes: Dict[str, Dict[str, Any]] = nodefacts_obj.get("nodes", {}) or {}

    # -----------------------------------------------------------------------
    # STEP 3: Optional edges
    # -----------------------------------------------------------------------
    edges_obj: Dict[str, Any] = {"schema_version": "edges@v1", "language": "rust", "edges": []}
    edges_payload: List[Dict[str, Any]] = []

    if build_edges:
        t_edges = time.perf_counter()
        edges_obj = build_edges_from_folder_index(idx=idx, cfg=cfg, internal_only=True)
        edges_payload = edges_obj.get("edges", []) or []
        log_event("RUST:edges_built", edges_total=len(edges_payload), edges_ms=int((time.perf_counter() - t_edges) * 1000))

        # DIAG: edges summary + sample
        try:
            _diag_print(
                "edges_done",
                ms=int((time.perf_counter() - t_edges) * 1000),
                edges_total=len(edges_payload),
                schema=str(edges_obj.get("schema_version", "")) if isinstance(edges_obj, dict) else "",
            )
            if edges_payload:
                e0 = edges_payload[0] or {}
                _diag_print(
                    "edge0_sample",
                    kind=str(e0.get("kind", "")),
                    src=str(e0.get("src", "")),
                    dst=str(e0.get("dst", "")),
                    confidence=e0.get("confidence"),
                )
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # STEP 4: Write artifacts
    # -----------------------------------------------------------------------
    try:
        _write_json(nodefacts_path, nodefacts_obj)
        # DIAG: confirm write sizes
        try:
            _diag_print(
                "write_nodefacts_ok",
                path=str(nodefacts_path),
                bytes=int(nodefacts_path.stat().st_size),
            )
        except Exception:
            pass
    except Exception as e:
        log_event("RUST:nodefacts_write_failed", err=f"{type(e).__name__}: {e}"[:200])
        try:
            _diag_print("write_nodefacts_failed", exc_type=type(e).__name__, exc=str(e)[:300])
        except Exception:
            pass

    if build_edges:
        try:
            _write_json(edges_path, edges_obj)
            try:
                _diag_print(
                    "write_edges_ok",
                    path=str(edges_path),
                    bytes=int(edges_path.stat().st_size),
                )
            except Exception:
                pass
        except Exception as e:
            log_event("RUST:edges_write_failed", err=f"{type(e).__name__}: {e}"[:200])
            try:
                _diag_print("write_edges_failed", exc_type=type(e).__name__, exc=str(e)[:300])
            except Exception:
                pass

    log_event(
        "RUST:set_dir_ready",
        nodefacts=str(nodefacts_path),
        edges=str(edges_path if build_edges else ""),
        folder_index=str(folder_index_path),
        nodes=len(nodefacts_nodes),
        edges_count=len(edges_payload),
        parsed_ok=int(meta.get("parsed_count", 0) or 0),
        parsed_err=int(meta.get("parse_issues_count", 0) or 0),
        total_ms=int((time.perf_counter() - t_all) * 1000),
    )

    # DIAG: final summary
    try:
        _diag_print(
            "done",
            total_ms=int((time.perf_counter() - t_all) * 1000),
            nodes=len(nodefacts_nodes),
            edges=(len(edges_payload) if build_edges else 0),
            cache_size=(len(parse_cache) if isinstance(parse_cache, dict) else None),
        )
    except Exception:
        pass

    # -----------------------------------------------------------------------
    # Return fragment for in-process consumers
    # -----------------------------------------------------------------------
    edges_summary = edges_obj.get("meta", {}) if isinstance(edges_obj, dict) else {}
    return {
        "nodes": nodefacts_nodes,
        "edges": edges_payload if build_edges else [],
        "meta": {
            "analyzer": "rust",
            "files_in": len(files),
            "files_used": int(meta.get("eligible_count", 0) or 0),
            "parsed_ok": int(meta.get("parsed_count", 0) or 0),
            "parsed_err": int(meta.get("parse_issues_count", 0) or 0),
            "artifact_dir": str(artifact_dir),
            "folder_index": str(folder_index_path),
            "edges_rust": str(edges_path) if build_edges else "",
            "edges_summary": edges_summary,
            "module_index_count": int(meta.get("module_index_count", 0) or 0),
            "crate_index_count": int(meta.get("crate_index_count", 0) or 0),
            "parse_cache_hits": len(parse_cache) if parse_cache else 0,
        },
    }


def _write_empty_artifacts(
    *,
    nodefacts_path: Path,
    edges_path: Optional[Path],
    folder_index_path: Path,
    repo_root: Path,
) -> None:
    """Write empty artifacts when analysis fails."""
    from analyzer.rust import rust_folder_index as fi
    from analyzer_store.types import FOLDER_INDEX_SCHEMA

    idx = FolderIndex(
        schema=FOLDER_INDEX_SCHEMA,
        meta={
            "created": str(fi.iso_utc()),
            "root": str(fi.to_posix(repo_root)),
            "language": "rust",
            "eligible_count": "0",
            "parsed_count": "0",
            "internal_edge_count": "0",
            "parse_issues_count": "0",
            "module_index_count": "0",
            "crate_index_count": "0",
        },
        files={},
    )

    try:
        save_folder_index(idx, folder_index_path)
    except Exception:
        pass

    try:
        _write_json(nodefacts_path, {"schema_version": "nodefacts@v1.6", "language": "rust", "nodes": {}})
    except Exception:
        pass

    if edges_path:
        try:
            _write_json(edges_path, {"schema_version": "edges@v1", "language": "rust", "edges": []})
        except Exception:
            pass