from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from analyzer.kotlin.kotlin_folder_index import build_folder_index, save_folder_index
from analyzer.kotlin.kotlin_build_artifacts import (
    build_nodefacts_from_folder_index,
    build_edges_from_folder_index,
)
from analyzer_store.types import FolderIndex

try:
    from diagnostics.logging import log_event as _log_event
except Exception:
    _log_event = None


def log_event(name: str, **k) -> None:
    if _log_event is not None:
        try:
            _log_event(name, **k)
            return
        except Exception:
            pass

    parts = " ".join(f"{kk}={k[kk]!r}" for kk in sorted(k.keys()))
    print(f"[KOTLIN] {name} {parts}".rstrip())


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")


def analyze_files_kotlin(
    *,
    repo_root: Path,
    artifact_dir: Path,
    cfg: Any,
    files: Sequence[Path],
    folder_index_name: str = "folder_index_kotlin.json",
    build_edges: bool = True,
    write_nodefacts: bool = True,
) -> Dict[str, object]:
    """
    Kotlin bucket analyzer.

    Intentionally keeps diagnostics lightweight here. Detailed performance
    diagnosis for Kotlin belongs in kotlin_folder_index._parse_kotlin_files(),
    where parser strategy can be observed directly.
    """
    t0 = time.perf_counter()

    repo_root = Path(repo_root).resolve()
    artifact_dir = Path(artifact_dir).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    nodefacts_name = getattr(cfg, "nodefacts_name", "nodefacts.json")
    edges_name = getattr(cfg, "edges_name", "edges.json")
    folder_index_name_eff = getattr(cfg, "folder_index_name", folder_index_name)

    nodefacts_path = artifact_dir / nodefacts_name
    edges_path = artifact_dir / edges_name
    folder_index_path = artifact_dir / folder_index_name_eff

    log_event(
        "KOTLIN:analyze_files_begin",
        repo_root=str(repo_root),
        artifact_dir=str(artifact_dir),
        files_in=len(files),
        build_edges=bool(build_edges),
        write_nodefacts=bool(write_nodefacts),
    )

    # -----------------------------------------------------------------------
    # STEP 1: Build folder index and parse cache
    # -----------------------------------------------------------------------
    try:
        t_idx = time.perf_counter()

        idx, parse_cache = build_folder_index(
            root=repo_root,
            cfg=cfg,
            files=list(files),
        )

        save_folder_index(idx, folder_index_path)

        try:
            cfg.kotlin_parse_cache = parse_cache
        except Exception:
            pass

        log_event(
            "KOTLIN:folder_index_built",
            ms=int((time.perf_counter() - t_idx) * 1000),
            files=len(getattr(idx, "files", {}) or {}),
            cache_size=len(parse_cache) if isinstance(parse_cache, dict) else 0,
        )

    except Exception as e:
        log_event(
            "KOTLIN:folder_index_failed",
            err=f"{type(e).__name__}:{str(e)[:200]}",
            files_in=len(files),
            repo_root=str(repo_root),
        )

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
                "analyzer": "kotlin",
                "files_in": len(files),
                "error": "folder_index_build_failed",
            },
        }

    meta = idx.meta if isinstance(getattr(idx, "meta", None), dict) else {}

    # -----------------------------------------------------------------------
    # STEP 2: Build nodefacts
    # -----------------------------------------------------------------------
    if write_nodefacts:
        t_nf = time.perf_counter()

        nodefacts_obj = build_nodefacts_from_folder_index(
            idx=idx,
            repo_root=repo_root,
            cfg=cfg,
        )

        log_event(
            "KOTLIN:nodefacts_built",
            ms=int((time.perf_counter() - t_nf) * 1000),
            nodes=len(nodefacts_obj.get("nodes", {}) or {}),
        )
    else:
        nodefacts_obj = {
            "schema_version": "nodefacts@v1.6",
            "language": "kotlin",
            "nodes": {},
        }

    nodefacts_nodes: Dict[str, Dict[str, Any]] = nodefacts_obj.get("nodes", {}) or {}

    # -----------------------------------------------------------------------
    # STEP 3: Optional edges
    # -----------------------------------------------------------------------
    edges_obj: Dict[str, Any] = {
        "schema_version": "edges@v1",
        "language": "kotlin",
        "edges": [],
    }
    edges_payload = []

    if build_edges:
        t_edges = time.perf_counter()

        edges_obj = build_edges_from_folder_index(
            idx=idx,
            cfg=cfg,
            internal_only=True,
        )

        edges_payload = edges_obj.get("edges", []) or []

        log_event(
            "KOTLIN:edges_built",
            edges_total=len(edges_payload),
            edges_ms=int((time.perf_counter() - t_edges) * 1000),
        )

    # -----------------------------------------------------------------------
    # STEP 4: Write artifacts
    # -----------------------------------------------------------------------
    try:
        _write_json(nodefacts_path, nodefacts_obj)
    except Exception as e:
        log_event(
            "KOTLIN:nodefacts_write_failed",
            err=f"{type(e).__name__}:{str(e)[:200]}",
            path=str(nodefacts_path),
        )

    if build_edges:
        try:
            _write_json(edges_path, edges_obj)
        except Exception as e:
            log_event(
                "KOTLIN:edges_write_failed",
                err=f"{type(e).__name__}:{str(e)[:200]}",
                path=str(edges_path),
            )

    total_ms = int((time.perf_counter() - t0) * 1000)

    log_event(
        "KOTLIN:set_dir_ready",
        nodefacts=str(nodefacts_path),
        edges=str(edges_path if build_edges else ""),
        folder_index=str(folder_index_path),
        nodes=len(nodefacts_nodes),
        edges_count=len(edges_payload),
        parsed_ok=int(meta.get("parsed_count", 0) or 0),
        parsed_err=int(meta.get("parse_issues_count", 0) or 0),
        total_ms=total_ms,
    )

    edges_summary = edges_obj.get("meta", {}) if isinstance(edges_obj, dict) else {}

    return {
        "nodes": nodefacts_nodes,
        "edges": edges_payload if build_edges else [],
        "meta": {
            "analyzer": "kotlin",
            "files_in": len(files),
            "files_used": int(meta.get("eligible_count", 0) or 0),
            "parsed_ok": int(meta.get("parsed_count", 0) or 0),
            "parsed_err": int(meta.get("parse_issues_count", 0) or 0),
            "artifact_dir": str(artifact_dir),
            "folder_index": str(folder_index_path),
            "edges_kotlin": str(edges_path) if build_edges else "",
            "edges_summary": edges_summary,
            "fq_decl_index_count": int(meta.get("fq_decl_index_count", 0) or 0),
            "package_index_count": int(meta.get("package_index_count", 0) or 0),
            "explicit_internal_edge_count": int(meta.get("explicit_internal_edge_count", 0) or 0),
            "symbol_internal_edge_count": int(meta.get("symbol_internal_edge_count", 0) or 0),
            "parse_cache_hits": len(getattr(cfg, "kotlin_parse_cache", {}) or {}),
        },
    }


def _write_empty_artifacts(
    *,
    nodefacts_path: Path,
    edges_path: Optional[Path],
    folder_index_path: Path,
    repo_root: Path,
) -> None:
    from analyzer_store.types import FOLDER_INDEX_SCHEMA

    idx = FolderIndex(
        schema=FOLDER_INDEX_SCHEMA,
        meta={
            "created": "",
            "root": str(repo_root),
            "language": "kotlin",
            "eligible_count": "0",
            "parsed_count": "0",
            "internal_edge_count": "0",
            "explicit_internal_edge_count": "0",
            "symbol_internal_edge_count": "0",
            "parse_issues_count": "0",
            "fq_decl_index_count": "0",
            "package_index_count": "0",
        },
        files={},
    )

    try:
        save_folder_index(idx, folder_index_path)
    except Exception:
        pass

    try:
        _write_json(
            nodefacts_path,
            {
                "schema_version": "nodefacts@v1.6",
                "language": "kotlin",
                "nodes": {},
            },
        )
    except Exception:
        pass

    if edges_path:
        try:
            _write_json(
                edges_path,
                {
                    "schema_version": "edges@v1",
                    "language": "kotlin",
                    "edges": [],
                },
            )
        except Exception:
            pass
