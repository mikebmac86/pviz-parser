# backend/saas_analyzer/analyzer/go/run.py
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# Canonical FolderIndex builders / types
from analyzer.go.go_folder_index import (
    build_folder_index,
    save_folder_index,
    FolderIndex,
    FOLDER_INDEX_SCHEMA,
)

# Canonical Go artifact builders
from analyzer.go.go_build_artifacts import (
    build_nodefacts_from_folder_index,
    build_edges_from_folder_index,
)

# Single-source normalization for any local derivations
from analyzer.go.go_canonical import normalize_import_spec, resolve_go_import

# Local module (for empty-folder-index meta helpers)
from analyzer.go import go_folder_index as fi

# Go AST batch parser (goextract)
from analyzer.go.go_parse_dispatch import get_go_batch_parser

# ---------------------------------------------------------------------------
# Logging (prefers PViz diagnostics logging; falls back to print)
# ---------------------------------------------------------------------------
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
    print(f"[GO] {name} {parts}".rstrip())


def _write_json(path: Path, obj: Any) -> None:
    """Lightweight JSON writer (avoids external deps)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")


def _cfg_bool(cfg: Any, name: str, default: bool) -> bool:
    try:
        return bool(getattr(cfg, name, default))
    except Exception:
        return default


def _cfg_int(cfg: Any, name: str, default: int) -> int:
    try:
        v = getattr(cfg, name, default)
        return int(v) if v is not None else default
    except Exception:
        return default


def analyze_files_go(
    *,
    repo_root: Path,
    artifact_dir: Path,
    cfg: Any,
    files: Sequence[Path],
    folder_index_name: str = "folder_index_go.json",
    build_edges: bool = True,
    include_external_edges: bool = False,
) -> Dict[str, object]:
    """
    Go bucket analyzer (TS-analogous set-dir writer).

    Orchestrates:
      1. Build folder index (canonical builder)
      2. REQUIRED: Preload Go AST batch extractor (single subprocess for all files)
      3. Build nodefacts from folder index (canonical builder)
      4. Optionally build edges from folder index (canonical builder)
      5. Write artifacts to set dir (nodefacts + edges + folder_index)
      6. Optionally emit Go-only sidecar artifacts (details + edge reasons)
      7. Return fragment {nodes, edges, meta}
    """
    t_all = time.perf_counter()

    repo_root = repo_root.resolve()
    artifact_dir = artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # Config-driven names (TS-style), with safe defaults
    nodefacts_name = getattr(cfg, "nodefacts_name", "nodefacts.json")
    edges_name = getattr(cfg, "edges_name", "edges.json")
    folder_index_name_eff = getattr(cfg, "folder_index_name", folder_index_name)

    nodefacts_path = artifact_dir / nodefacts_name
    edges_path = artifact_dir / edges_name
    folder_index_path = artifact_dir / folder_index_name_eff
    cfg_include_external = _cfg_bool(cfg, "include_external_edges", include_external_edges)
    emit_go_details = _cfg_bool(cfg, "emit_go_details", False)
    go_details_name = getattr(cfg, "go_details_name", "go_details.json")
    go_details_path = artifact_dir / str(go_details_name)

    emit_edge_reasons = _cfg_bool(cfg, "emit_edge_reasons", False)
    go_edge_reasons_name = getattr(cfg, "go_edge_reasons_name", "go_edge_reasons.json")
    go_edge_reasons_path = artifact_dir / str(go_edge_reasons_name)
    max_reasons_per_edge = _cfg_int(cfg, "max_reasons_per_edge", 50)

    # Required AST posture:
    # - default True (so you don't have to wire a flag everywhere)
    # - can be disabled only if explicitly set False (debug escape hatch)
    require_go_ast = _cfg_bool(cfg, "require_go_ast", True)

    log_event(
        "GO:analyze_files_begin",
        repo_root=str(repo_root),
        artifact_dir=str(artifact_dir),
        files_in=len(files),
        build_edges=bool(build_edges),
        include_external_edges=bool(cfg_include_external),
        nodefacts_name=str(nodefacts_name),
        edges_name=str(edges_name),
        folder_index_name=str(folder_index_name_eff),
        emit_go_details=bool(emit_go_details),
        emit_edge_reasons=bool(emit_edge_reasons),
        require_go_ast=bool(require_go_ast),
    )

    # -----------------------------------------------------------------------
    # STEP 1: Build folder index (canonical)
    # -----------------------------------------------------------------------
    try:
        idx = build_folder_index(
            root=repo_root,
            cfg=cfg,
            files=list(files),  # bucket mode: pre-filtered file list
        )
        save_folder_index(idx, folder_index_path)
    except Exception as e:
        log_event("GO:folder_index_failed", err=f"{type(e).__name__}: {e}"[:200])

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
                "analyzer": "go",
                "files_in": len(files),
                "error": "folder_index_build_failed",
            },
        }

    # meta from folder index (stringly typed)
    meta = idx.meta if isinstance(getattr(idx, "meta", None), dict) else {}
    module_path = meta.get("module_path") or None

    # -----------------------------------------------------------------------
    # STEP 2: REQUIRED: Preload Go AST batch extractor (single subprocess)
    # -----------------------------------------------------------------------
    if require_go_ast:
        t_ast = time.perf_counter()
        try:
            # Build absolute file list from idx.files to match what NodeFacts will parse.
            abs_go_files: List[Path] = []
            for fe in (idx.files or {}).values():
                rel = str(getattr(fe, "file", "") or "").strip()
                if not rel:
                    continue
                if not rel.lower().endswith(".go"):
                    continue
                abs_go_files.append((repo_root / rel).resolve(strict=False))

            # Respect cfg knobs (same as previous step 2)
            include_docs = _cfg_bool(cfg, "goextract_include_docs", True)
            include_imports = _cfg_bool(cfg, "goextract_include_imports", True)
            include_build = _cfg_bool(cfg, "goextract_include_build", True)
            timeout_s = _cfg_int(cfg, "goextract_timeout_s", 180)
            batch_size = _cfg_int(cfg, "goextract_batch_size", 0)

            bp = get_go_batch_parser()

            # Optional explicit binary path
            try:
                gb = getattr(cfg, "goextract_bin", None)
                if gb:
                    bp.helper_bin = Path(gb)  # type: ignore[attr-defined]
            except Exception:
                pass

            # Chunk if requested
            if batch_size and batch_size > 0:
                for i in range(0, len(abs_go_files), batch_size):
                    bp.load_batch(
                        abs_go_files[i : i + batch_size],
                        include_docs=include_docs,
                        include_imports=include_imports,
                        include_build=include_build,
                        timeout_s=timeout_s,
                    )
            else:
                bp.load_batch(
                    abs_go_files,
                    include_docs=include_docs,
                    include_imports=include_imports,
                    include_build=include_build,
                    timeout_s=timeout_s,
                )

            log_event(
                "GO:ast_batch_ready",
                files=len(abs_go_files),
                docs=bool(include_docs),
                imports=bool(include_imports),
                build=bool(include_build),
                chunk_size=int(batch_size or 0),
                ast_ms=int((time.perf_counter() - t_ast) * 1000),
            )
        except Exception as e:
            # Required posture: fail the set-dir run loudly,
            # but still write empty artifacts like TS does.
            log_event("GO:ast_batch_required_failed", err=f"{type(e).__name__}: {e}"[:200])

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
                    "analyzer": "go",
                    "files_in": len(files),
                    "files_used": int(meta.get("eligible_count", 0) or 0),
                    "error": "go_ast_required_failed",
                },
            }

    # -----------------------------------------------------------------------
    # STEP 3: Build nodefacts (canonical)
    # -----------------------------------------------------------------------
    nodefacts_obj: Dict[str, Any]
    try:
        nodefacts_obj = build_nodefacts_from_folder_index(
            idx=idx,
            repo_root=repo_root,
            cfg=cfg,
            module_path=module_path,
        )
    except Exception as e:
        log_event("GO:nodefacts_failed", err=f"{type(e).__name__}: {e}"[:200])
        nodefacts_obj = {"schema_version": "nodefacts@v1.6", "language": "go", "nodes": {}}

    nodefacts_nodes: Dict[str, Dict[str, Any]] = nodefacts_obj.get("nodes", {}) or {}

    # -----------------------------------------------------------------------
    # STEP 4: Optional edges (canonical)
    # -----------------------------------------------------------------------
    edges_obj: Dict[str, Any] = {"schema_version": "edges@v1", "language": "go", "edges": []}
    edges_payload: List[Dict[str, object]] = []

    if build_edges:
        t_edges = time.perf_counter()
        try:
            edges_obj = build_edges_from_folder_index(
                idx=idx,
                module_path=module_path,
                internal_only=True,
                include_external=bool(cfg_include_external),
                drop_self_edges=True,
            )
            edges_payload = edges_obj.get("edges", []) or []
            log_event(
                "GO:edges_built",
                edges_total=len(edges_payload),
                edges_ms=int((time.perf_counter() - t_edges) * 1000),
            )
        except Exception as e:
            log_event("GO:edges_failed", err=f"{type(e).__name__}: {e}"[:200])
            edges_obj = {"schema_version": "edges@v1", "language": "go", "edges": []}
            edges_payload = []

    # -----------------------------------------------------------------------
    # STEP 5: Write primary artifacts
    # -----------------------------------------------------------------------
    try:
        _write_json(nodefacts_path, nodefacts_obj)
    except Exception as e:
        log_event("GO:nodefacts_write_failed", err=f"{type(e).__name__}: {e}"[:200])

    if build_edges:
        try:
            _write_json(edges_path, edges_obj)
        except Exception as e:
            log_event("GO:edges_write_failed", err=f"{type(e).__name__}: {e}"[:200])

    # -----------------------------------------------------------------------
    # STEP 6: Optional Go-only sidecars (trim at merge time)
    #   - go_details.json: extra symbol/import/build info not in NodeFacts schema
    #   - go_edge_reasons.json: explain internal edges via import specs / prefix collapse tried chain
    # -----------------------------------------------------------------------
    if emit_go_details or emit_edge_reasons:
        try:
            details: Dict[str, Any] = {
                "schema_version": "go_details@v1",
                "language": "go",
                "meta": {
                    "module_path": str(module_path or ""),
                    "files_indexed": int(meta.get("eligible_count", 0) or 0),
                    "parsed_ok": int(meta.get("parsed_count", 0) or 0),
                    "parse_issues": int(meta.get("parse_issues_count", 0) or 0),
                },
                "files": {},
            }

            # We *always* try to read the cache if require_go_ast or enable_go_ast is true.
            bp = None
            if require_go_ast or _cfg_bool(cfg, "enable_go_ast", False):
                try:
                    bp = get_go_batch_parser()
                except Exception:
                    bp = None

            for fe in (idx.files or {}).values():
                rel = str(getattr(fe, "file", "") or "").strip()
                if not rel:
                    continue
                abs_path = (repo_root / rel).resolve(strict=False)

                rec: Dict[str, Any] = {
                    "rel_file": rel,
                    "pkg_id": normalize_import_spec(getattr(fe, "id", "") or ""),
                    "parse_status": str(getattr(fe, "parse_status", "ok") or "ok"),

                    "loc": getattr(fe, "loc", None),
                    "sloc": getattr(fe, "sloc", None),
                    "comment_lines": getattr(fe, "comment_lines", None),
                    "blank_lines": getattr(fe, "blank_lines", None),
                    "comment_pct": getattr(fe, "comment_pct", None),

                    "imports_all": list(getattr(fe, "imports_all", None) or ()),
                    "imports_internal": list(getattr(fe, "imports_internal", None) or ()),
                    "eligible": bool(getattr(fe, "eligible", True)),
                    "size_bytes": getattr(fe, "size_bytes", None),
                    "mtime": getattr(fe, "mtime", None),
                }

                # Attach AST-derived details if available (docs/imports/build/symbols)
                if bp is not None:
                    try:
                        gp = bp.get(abs_path)  # type: ignore[union-attr]
                        if gp:
                            if _cfg_bool(cfg, "go_details_include_symbols", True):
                                rec["symbols"] = gp.symbols
                            if _cfg_bool(cfg, "goextract_include_imports", True):
                                rec["imports"] = gp.imports
                            if _cfg_bool(cfg, "go_details_include_build_flags", True):
                                rec["build"] = gp.build
                            rec["goextract_parse_status"] = getattr(gp, "parse_status", None)
                            rec["goextract_error_snippet"] = getattr(gp, "error_snippet", None)
                            rec["package"] = getattr(gp, "package", None)
                    except Exception:
                        pass

                details["files"][rel] = rec

            if emit_go_details:
                try:
                    _write_json(go_details_path, details)
                    log_event("GO:details_written", path=str(go_details_path), files=len(details.get("files", {})))
                except Exception as e:
                    log_event("GO:details_write_failed", err=f"{type(e).__name__}: {e}"[:200])

            if emit_edge_reasons and build_edges:
                try:
                    internal_ids = {
                        normalize_import_spec(getattr(fe, "id", "") or "")
                        for fe in (idx.files or {}).values()
                    }
                    internal_ids = {x for x in internal_ids if x}

                    reasons: Dict[str, Any] = {
                        "schema_version": "go_edge_reasons@v1",
                        "language": "go",
                        "meta": {"module_path": str(module_path or ""), "max_reasons_per_edge": max_reasons_per_edge},
                        "reasons": {},  # "src||dst" -> list[reason]
                    }

                    def edge_key(src: str, dst: str) -> str:
                        return f"{src}||{dst}"

                    for fe in (idx.files or {}).values():
                        src = normalize_import_spec(getattr(fe, "id", "") or "")
                        if not src:
                            continue
                        rel = str(getattr(fe, "file", "") or "").strip()

                        for raw in (getattr(fe, "imports_all", None) or ()):
                            spec = normalize_import_spec(raw or "")
                            if not spec:
                                continue

                            rr = resolve_go_import(
                                spec,
                                module_path=module_path,
                                internal_pkg_ids=internal_ids,
                                allow_prefix_collapse=True,
                            )
                            if rr.kind != "internal" or not rr.resolved:
                                continue
                            dst = normalize_import_spec(rr.resolved)
                            if not dst or dst == src:
                                continue

                            k = edge_key(src, dst)
                            lst = reasons["reasons"].setdefault(k, [])
                            if len(lst) >= max_reasons_per_edge:
                                continue
                            lst.append(
                                {
                                    "file": rel,
                                    "spec": spec,
                                    "resolved": dst,
                                    "tried": list(rr.tried or ()),
                                }
                            )

                    _write_json(go_edge_reasons_path, reasons)
                    log_event(
                        "GO:edge_reasons_written",
                        path=str(go_edge_reasons_path),
                        edges=len(reasons.get("reasons", {})),
                    )
                except Exception as e:
                    log_event("GO:edge_reasons_failed", err=f"{type(e).__name__}: {e}"[:200])

        except Exception as e:
            log_event("GO:sidecar_failed", err=f"{type(e).__name__}: {e}"[:200])

    # -----------------------------------------------------------------------
    # Final logging + return fragment
    # -----------------------------------------------------------------------
    log_event(
        "GO:set_dir_ready",
        nodefacts=str(nodefacts_path),
        edges=str(edges_path if build_edges else ""),
        folder_index=str(folder_index_path),
        nodes=len(nodefacts_nodes),
        edges_count=len(edges_payload),
        parsed_ok=int(meta.get("parsed_count", 0) or 0),
        total_ms=int((time.perf_counter() - t_all) * 1000),
    )

    edges_summary = edges_obj.get("meta", {}) if isinstance(edges_obj, dict) else {}
    return {
        "nodes": nodefacts_nodes,
        "edges": edges_payload if build_edges else [],
        "meta": {
            "analyzer": "go",
            "files_in": len(files),
            "files_used": int(meta.get("eligible_count", 0) or 0),
            "parsed_ok": int(meta.get("parsed_count", 0) or 0),
            "parsed_err": int(meta.get("parse_issues_count", 0) or 0),
            "artifact_dir": str(artifact_dir),
            "folder_index": str(folder_index_path),
            "edges_go": str(edges_path) if build_edges else "",
            "edges_summary": edges_summary,
            "module_path": str(module_path or ""),
            "go_details": str(go_details_path) if emit_go_details else "",
            "go_edge_reasons": str(go_edge_reasons_path) if (emit_edge_reasons and build_edges) else "",
        },
    }


def _write_empty_artifacts(
    *,
    nodefacts_path: Path,
    edges_path: Optional[Path],
    folder_index_path: Path,
    repo_root: Path,
) -> None:
    """
    Write empty artifacts when analysis fails.
    Ensures set dir is complete even on errors (TS-style).
    """
    idx = FolderIndex(
        schema=FOLDER_INDEX_SCHEMA,
        meta={
            "created": str(fi.iso_utc()),
            "root": str(fi.to_posix(repo_root)),
            "language": "go",
            "eligible_count": "0",
            "parsed_count": "0",
            "internal_edge_count": "0",
            "module_path": "",
            "go_mod": "",
            "parse_issues_count": "0",
            "mode": "bucket_files",
        },
        files={},
    )

    try:
        save_folder_index(idx, folder_index_path)
    except Exception:
        pass

    try:
        _write_json(nodefacts_path, {"schema_version": "nodefacts@v1.6", "language": "go", "nodes": {}})
    except Exception:
        pass

    if edges_path:
        try:
            _write_json(edges_path, {"schema_version": "edges@v1", "language": "go", "edges": []})
        except Exception:
            pass
